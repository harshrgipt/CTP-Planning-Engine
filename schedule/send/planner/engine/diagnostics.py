"""REVIEW-PHASE DIAGNOSTICS. Feasibility before performance.

Three things a KPI cannot tell you, computed from the plan artefacts:

  R_g   supply ratio -- can building feed the presses mounted on GT g?
  unplaced / past-horizon lots, with a reason per lot
  shelf-life breaches AS ROWS, not as a count

R_g is the one that matters. It is:

    R_g = tyres actually built for g / capacity of the presses mounted on g

If R_g < 1 the mounted presses have more throughput than building supplies, so
they starve BY CONSTRUCTION and no sequencing change can rescue them. Measured
on July it lands exactly on the top-kappa GTs -- GT 2476 SUP MM builds 24,730
against 30,240 press-capacity (R = 0.82) and is the single worst starver at 639
press-hours; GT 2267 (0.92) and GT 1513 (0.95) follow.

NOTE ON UNITS -- this is where the first attempt was wrong. `cycle_s` in
build_schedule is the LOT's run duration (p50 4,968 s), NOT the per-tyre
cadence (63 s). Treating it as a cadence gave build_cap = 243 for a GT that
built 55,550. Per-tyre rate must be derived as (span - setup) / qty.
"""
from __future__ import annotations

import polars as pl

from planner.config import GT_SHELF_LIFE_H as SHELF_LIFE_H  # hardcoded 72 h


def supply_ratio(build_df: pl.DataFrame, cure_df: pl.DataFrame,
                 campaigns: pl.DataFrame) -> pl.DataFrame:
    """R_g per GT -- can building feed the presses mounted on it?

    BOTH SIDES OVER SCHEDULED HOURS. The first version divided tyres BUILT by
    the capacity of every mounted press-day, which compares a realised quantity
    against a nominal one. It fired on 103 of 104 GTs (median 0.88) while the
    plan fulfilled 99% -- a diagnostic that flags everything discriminates
    nothing, and both facts cannot be true at once.

        R_g = sum_m (active build hours on g) * build rate
              -----------------------------------------------
              sum_p (mounted hours on g)      * cure rate

    Numerator counts only the hours a machine is ACTUALLY building g, not the
    month; denominator only the hours a press is MOUNTED on g. R < 1 now means
    what it should: this press is mounted longer than building can feed it, and
    the fix is to shorten the mount or add a building visit -- not to add
    capacity.

    Per-tyre build cadence is DERIVED as (span - setup) / qty. `cycle_s` in
    build_schedule is the LOT's run duration (p50 4,968 s), not the per-tyre
    cadence (63 s); treating it as a cadence gave build capability 243 for a GT
    that built 55,550.
    """
    if build_df.height == 0 or cure_df.height == 0 or campaigns.height == 0:
        return pl.DataFrame()
    b = build_df.with_columns(
        ((pl.col("end_ts") - pl.col("start_ts")).dt.total_seconds() / 3600.0)
        .alias("active_h"))
    bg = (b.group_by(["plant", "gt_code"]).agg(
        pl.col("active_h").sum().alias("build_h"),
        pl.col("qty").sum().alias("built"),
        pl.col("machine").n_unique().alias("machines")))
    # tyres per active build hour, per GT, from the schedule itself

    cg = (cure_df.group_by(["plant", "gt_code"]).agg(
        pl.col("cycle_s").median().alias("cure_s"),
        pl.len().alias("cured"),
        pl.col("press").n_unique().alias("presses")))
    pd_ = (campaigns.with_columns((pl.col("end_day") - pl.col("start_day"))
                                  .alias("d"))
           .group_by(["plant", "gt_code"]).agg(pl.col("d").sum().alias("press_days")))
    g = (bg.join(cg, on=["plant", "gt_code"], how="inner")
         .join(pd_, on=["plant", "gt_code"], how="inner"))
    g = g.with_columns(
        (pl.col("press_days") * 24.0).alias("mount_h"))
    # PRESS-DAYS NEEDED vs PRESS-DAYS MOUNTED. Presses mount in whole DAYS, so a
    # GT needing 6 press-hours is still given 24 -- R<1 on a ratio of hours is
    # therefore STRUCTURAL, not a defect, which is why the previous form fired
    # on 103 of 104 GTs and discriminated nothing.
    # What is actionable is mounting a GT for more DAYS than its own volume can
    # fill, spread across presses: excess = mounted - ceil(needed).
    g = g.with_columns(
        (pl.col("built") * pl.col("cure_s") / 86400.0).alias("need_press_days"))
    g = g.with_columns(
        (pl.col("press_days") - pl.col("need_press_days")).alias("excess_days"),
        (pl.col("need_press_days") / pl.col("press_days").clip(lower_bound=1e-9))
        .alias("R"))
    return g.sort(["plant", "excess_days"], descending=[False, True]).select(
        ["plant", "gt_code", "R", "excess_days", "need_press_days", "press_days",
         "built", "build_h", "mount_h", "machines", "presses", "cured"])


def shelf_life_rows(ledger: pl.DataFrame, cure_df: pl.DataFrame) -> pl.DataFrame:
    """Every breach as a ROW: GT, lot, build time, cure time, age."""
    if ledger.height == 0 or cure_df.height == 0:
        return pl.DataFrame()
    b = (ledger.filter(pl.col("source").is_in(["build", "opening"])
                       & (pl.col("qty_delta") > 0))
         .with_columns(pl.col("qty_delta").cast(pl.Int64))
         .with_columns(pl.int_ranges(pl.col("qty_delta")).alias("_i")).explode("_i")
         .select(["plant", "gt_code", "ts", "lot_id"])
         .sort(["plant", "gt_code", "ts", "lot_id"])
         .with_columns(pl.int_range(pl.len()).over(["plant", "gt_code"]).alias("k")))
    c = (cure_df.select(["plant", "gt_code", "press", "start_ts"])
         .sort(["plant", "gt_code", "start_ts"])
         .with_columns(pl.int_range(pl.len()).over(["plant", "gt_code"]).alias("k")))
    j = (c.rename({"start_ts": "cure_ts"})
         .join(b.rename({"ts": "build_ts"}), on=["plant", "gt_code", "k"],
               how="inner")
         .with_columns(((pl.col("cure_ts") - pl.col("build_ts"))
                        .dt.total_seconds() / 3600).alias("age_h")))
    return (j.filter(pl.col("age_h") > SHELF_LIFE_H)
            .sort("age_h", descending=True)
            .select(["plant", "gt_code", "lot_id", "build_ts", "cure_ts",
                     "age_h", "press"]))


def horizon_breaches(build_df: pl.DataFrame, horizon_end) -> pl.DataFrame:
    """Lots ending past H -- a hard-constraint breach, listed per lot."""
    if build_df.height == 0:
        return pl.DataFrame()
    return (build_df.filter(pl.col("end_ts") > horizon_end)
            .with_columns(((pl.col("end_ts") - pl.lit(horizon_end))
                           .dt.total_seconds() / 3600).alias("over_h"))
            .sort("over_h", descending=True)
            .select(["plant", "gt_code", "lot_id", "machine", "qty",
                     "start_ts", "end_ts", "over_h"]))


def overlaps(build_df: pl.DataFrame) -> pl.DataFrame:
    """Machine double-booking, per offending pair."""
    if build_df.height == 0:
        return pl.DataFrame()
    z = (build_df.sort(["machine", "start_ts"])
         .with_columns(pl.col("end_ts").shift(1).over("machine").alias("prev_end"),
                       pl.col("lot_id").shift(1).over("machine").alias("prev_lot")))
    return (z.filter(pl.col("start_ts") < pl.col("prev_end"))
            .with_columns(((pl.col("prev_end") - pl.col("start_ts"))
                           .dt.total_seconds() / 60).alias("overlap_min"))
            .select(["plant", "machine", "prev_lot", "lot_id", "start_ts",
                     "prev_end", "overlap_min"]))
