"""Are we lotting like the plant? Burst production, or flow?

    python scripts/compare_lotting.py [YYYY-MM]

Compares our engine's build schedule against the plant's own July output on the
things that decide whether green tyres wait:

  1. RUN LENGTH      consecutive same-GT tyres on a machine
  2. RUNS PER GT     how many times a GT is revisited in the month
  3. REPLENISH GAP   hours between consecutive runs of the same GT  (T_g)
  4. BURSTINESS      share of a GT's month built on its single biggest day
  5. SPREAD          how many days a GT is built on, and how evenly
  6. INTRA-DAY SHAPE do we deliver a day's quantity in one block or across shifts

Little's Law says I = lambda x W. Same demand, same presses => the only way our
inventory is 2.3x the plant's is that our W is larger. These six say WHERE the
wait comes from.
"""
from __future__ import annotations

import statistics as st
import sys
from datetime import date
from pathlib import Path

import polars as pl

from planner.config import CONFIG
from planner.data.warehouse import duck, set_cutoff
from planner.runs.logger import log


def runs_from(df: pl.DataFrame, key: str, tcol: str) -> pl.DataFrame:
    """Gaps-and-islands: consecutive same-GT rows on a resource = one run."""
    d = df.sort([key, tcol])
    d = d.with_columns(pl.col("gt_code").shift(1).over(key).alias("_p"))
    d = d.with_columns((pl.col("gt_code") != pl.col("_p")).fill_null(True)
                       .cum_sum().over(key).alias("_run"))
    return (d.group_by([key, "_run", "gt_code"])
            .agg(pl.col("qty").sum().alias("size"),
                 pl.col(tcol).min().alias("t0"),
                 pl.col(tcol).max().alias("t1"))
            .sort([key, "t0"]))


def main(month: str) -> int:
    set_cutoff(None)
    y, m = (int(x) for x in month.split("-"))
    eng = Path(CONFIG.paths.root) / "output" / "engine" / month / "build_schedule.parquet"
    if not eng.exists():
        eng = Path(CONFIG.paths.root) / "output" / "engine" / "jul" / "build_schedule.parquet"
    ours = pl.read_parquet(eng).select(
        ["plant", "gt_code", "machine", "start_ts", "qty"])

    plant = duck().execute("""
        SELECT plant, itemCode AS gt_code, machineCode AS machine,
               event_ts AS start_ts, 1.0 AS qty
        FROM v_build
        WHERE stage = 2 AND itemCode IS NOT NULL AND machineCode IS NOT NULL
          AND date_trunc('month', event_ts) = ?::DATE
    """, [date(y, m, 1)]).pl()

    for name, df in (("PLANT", plant), ("OURS ", ours)):
        print("=" * 82)
        print(f"{name}   {month}   rows={df.height:,}  tyres={df['qty'].sum():,.0f}")
        print("=" * 82)
        r = runs_from(df, "machine", "start_ts")
        for p in ("PCR", "TBR"):
            s = r.join(df.select(["machine", "plant"]).unique(), on="machine",
                       how="left").filter(pl.col("plant") == p)
            if s.height == 0:
                continue
            sz = s["size"]
            # runs per GT per month
            rpg = s.group_by("gt_code").len()["len"]
            # replenishment gap between consecutive runs of a GT
            g = s.sort(["gt_code", "t0"])
            g = g.with_columns(
                ((pl.col("t0") - pl.col("t1").shift(1).over("gt_code"))
                 .dt.total_seconds() / 3600.0).alias("gap_h"))
            gaps = g.filter(pl.col("gap_h").is_not_null()
                            & (pl.col("gap_h") >= 0))["gap_h"]
            # burstiness + spread
            byday = (df.filter(pl.col("plant") == p)
                     .with_columns(pl.col("start_ts").dt.date().alias("d"))
                     .group_by(["gt_code", "d"]).agg(pl.col("qty").sum().alias("q")))
            tot = byday.group_by("gt_code").agg(pl.col("q").sum().alias("t"),
                                                pl.col("q").max().alias("mx"),
                                                pl.len().alias("nd"))
            burst = (tot["mx"] / tot["t"])
            print(f"\n  {p}")
            print(f"    RUN LENGTH      p50 {sz.median():7,.0f}  p90 "
                  f"{sz.quantile(.9):7,.0f}  max {sz.max():7,.0f}")
            print(f"    RUNS PER GT     p50 {rpg.median():7.0f}  max {rpg.max():7.0f}")
            if gaps.len():
                print(f"    REPLENISH GAP   p50 {gaps.median():7.1f}h p90 "
                      f"{gaps.quantile(.9):7.1f}h")
            print(f"    BURSTINESS      biggest day = {100*burst.median():5.1f}% of "
                  f"a GT's month (p90 {100*burst.quantile(.9):.0f}%)")
            print(f"    SPREAD          built on {tot['nd'].median():.0f} days "
                  f"(p90 {tot['nd'].quantile(.9):.0f})")

    # intra-day shape: how much of a machine-day lands in one contiguous block
    print("\n" + "=" * 82)
    print("INTRA-DAY SHAPE  -- one block per day, or spread across shifts?")
    print("=" * 82)
    for name, df in (("PLANT", plant), ("OURS ", ours)):
        d = (df.with_columns(pl.col("start_ts").dt.date().alias("d"),
                             pl.col("start_ts").dt.hour().alias("h"))
             .group_by(["plant", "machine", "d", "gt_code"])
             .agg(pl.col("h").min().alias("h0"), pl.col("h").max().alias("h1"),
                  pl.col("qty").sum().alias("q")))
        for p in ("PCR", "TBR"):
            s = d.filter(pl.col("plant") == p)
            if s.height == 0:
                continue
            span = (s["h1"] - s["h0"])
            print(f"  {name} {p}: a (machine, GT, day) block spans "
                  f"{span.median():.0f}h (p90 {span.quantile(.9):.0f}h), "
                  f"{s['q'].median():,.0f} tyres")
    log.info("compare_lotting.done")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "2026-07"))
