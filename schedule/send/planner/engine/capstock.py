"""HARD CAP on realized daily GT inventory. PCR 5,000 / TBR 1,500.

Not a setpoint. A cap that cannot be exceeded, enforced on the LEDGER -- the
only place it can actually bind. The controller's own projection already runs
1.75x under reality (it projects 4,113 for PCR and the ledger realizes 7,197),
because it is fed press POTENTIAL rather than realized cure, so a limit imposed
there constrains a number that is not the one being measured.

THE CONSTRAINT IS AN EXACT ENVELOPE, not a search. Inventory is cumulative
build minus cumulative cure, so:

    I(t) = cumbuild(t) - cumcure(t) <= cap
    =>  t_k >= (time cumcure reaches k - cap)       for the k-th built tyre

So every tyre has a REQUIRED EARLIEST build time, read off the drawdown curve
by index. No iteration, no feasibility search.

THE ENVELOPE MUST BE EXOGENOUS -- USE DEMAND, NEVER REALIZED CURE. Driving it
off the realized cure schedule is a self-amplifying deadlock: capping build
shrinks cure, which shrinks the envelope, which caps build harder. Measured,
it collapsed in three passes -- 490,133 -> 79,329 -> 16,582 tyres cured,
fulfilment 3.34%, and inventory ended at 30,796 (SIX TIMES the cap it was
enforcing) because the plan could no longer cure what it had already built.
This is the same failure as feeding realized cure to the controller; cure is
never an input to a decision that constrains its own supply.

Demand is fixed by the order book, so the envelope is stable across passes.

Each tyre also has a LATEST build time from its own FIFO-paired cure:

    t_k <= paired_cure(k) - tau_min

DEFER, NEVER DROP: the shift is min(required, allowed). Where the cap would
demand a deferral past the press deadline, the tyre is built on time and the
cap is BREACHED AND REPORTED. A cap enforced by destroying production is not a
cap on inventory, it is a cap on output -- and the residual breach is the
honest measure of how much of the cap the press campaign structure will not
permit.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import polars as pl

from planner.plan.shift_grid import TAU_MIN_H
from planner.runs.logger import log


def _per_tyre(ledger: pl.DataFrame) -> pl.DataFrame:
    return (ledger
            .filter(pl.col("source").is_in(["build", "opening"])
                    & (pl.col("qty_delta") > 0))
            .with_columns(pl.col("qty_delta").cast(pl.Int64))
            .with_columns(pl.int_ranges(pl.col("qty_delta")).alias("_i"))
            .explode("_i")
            .select(["plant", "gt_code", "ts", "lot_id", "source"]))


def enforce_cap(build_df: pl.DataFrame, cure_df: pl.DataFrame,
                demand: pl.DataFrame, ledger: pl.DataFrame,
                caps: dict[str, int], origin: datetime,
                horizon_end: datetime) -> tuple[pl.DataFrame, dict]:
    """Return (build_df with cap_shift_h, stats). Lots defer; nothing is dropped."""
    if build_df.height == 0 or demand.height == 0:
        return build_df, {"note": "empty"}

    tyres = _per_tyre(ledger)
    stats: dict = {"caps": caps, "plants": {}}
    shift_of: dict[str, float] = {}

    for plant in sorted(caps):
        cap = int(caps[plant])
        tp = tyres.filter(pl.col("plant") == plant)
        cp = cure_df.filter(pl.col("plant") == plant)
        if tp.height == 0:
            continue

        # ---- EXOGENOUS drawdown: demand per day, spread within the day -----
        # Demand is a daily quantity; presses draw it continuously, so a tyre
        # ranked r within day d is treated as drawn at d + (r/qty_d) of a day.
        # Step-per-day instead would let a whole day's build land at midnight
        # inside the cap and breach it for 23 hours.
        dd = (demand.filter(pl.col("plant") == plant)
              .group_by("due_date").agg(pl.col("qty").sum().alias("q"))
              .sort("due_date"))
        draw_ts: list[datetime] = []
        for r in dd.iter_rows(named=True):
            q = int(r["q"])
            if q <= 0:
                continue
            base = datetime(r["due_date"].year, r["due_date"].month,
                            r["due_date"].day)
            for i in range(q):
                draw_ts.append(base + timedelta(seconds=86400.0 * i / q))
        M = len(draw_ts)

        # ---- required earliest build time, by plant-wide build rank --------
        tp = tp.sort(["ts", "gt_code", "lot_id"]).with_columns(
            pl.int_range(1, pl.len() + 1).alias("k"))
        # tyre k needs cumcure >= k - cap, i.e. the (k-cap)-th draw to have run.
        tp = tp.with_columns((pl.col("k") - cap).alias("need"))
        req = [None if i < 1 else (draw_ts[i - 1] if i <= M else horizon_end)
               for i in tp["need"].to_list()]
        tp = tp.with_columns(pl.Series("req_ts", req, dtype=pl.Datetime("us")),
                             (pl.col("need") > M).alias("unsat"))

        # ---- latest build time, from the tyre's own FIFO-paired cure -------
        bk = tp.sort(["gt_code", "ts", "lot_id"]).with_columns(
            pl.int_range(pl.len()).over("gt_code").alias("gk"))
        ck = (cp.select(["gt_code", "start_ts"]).sort(["gt_code", "start_ts"])
              .with_columns(pl.int_range(pl.len()).over("gt_code").alias("gk")))
        bk = bk.join(ck, on=["gt_code", "gk"], how="left").with_columns(
            (pl.col("start_ts") - pl.duration(seconds=TAU_MIN_H * 3600))
            .alias("late_ts"))

        # ---- per lot: required shift vs allowed shift ----------------------
        bk = bk.with_columns(
            ((pl.col("req_ts") - pl.col("ts")).dt.total_seconds() / 3600.0)
            .fill_null(0.0).clip(lower_bound=0.0).alias("need_h"),
            ((pl.col("late_ts") - pl.col("ts")).dt.total_seconds() / 3600.0)
            .alias("allow_h"))
        # a tyre with no paired cure is never cured anyway; it cannot breach a
        # deadline, so it is deferrable without limit.
        bk = bk.with_columns(pl.col("allow_h").fill_null(1e9))
        # THE DEFERRAL IS min(required, allowed) AND NEVER NEGATIVE. Capped at
        # the press deadline so no tyre is ever made late for its own press;
        # floored at 0 so the cap can only push build later, never earlier.
        bk = bk.with_columns(
            pl.min_horizontal(pl.col("need_h"), pl.col("allow_h"))
            .clip(lower_bound=0.0).alias("shift_h"),
            (pl.col("need_h") > pl.col("allow_h") + 1e-9).alias("blocked"))

        lot = (bk.filter(pl.col("source") == "build")
               .group_by("lot_id")
               # MIN over the lot's tyres, not max: the lot moves as one unit,
               # so its shift is bounded by its most urgent tyre's deadline.
               .agg(pl.col("shift_h").min().alias("sh"),
                    pl.col("blocked").any().alias("bl"))
               .sort("lot_id"))
        for r in lot.iter_rows(named=True):
            if r["sh"] > 0:
                shift_of[r["lot_id"]] = float(r["sh"])

        nbad = int(bk.filter(pl.col("blocked")).height)
        moved = lot.filter(pl.col("sh") > 0)
        stats["plants"][plant] = {
            "cap": cap,
            "tyres": tp.height,
            "deferred_lots": moved.height,
            "mean_defer_h": round(float(moved["sh"].mean()), 2) if moved.height else 0.0,
            "max_defer_h": round(float(moved["sh"].max()), 2) if moved.height else 0.0,
            # tyres the press deadline will not let us defer far enough -- the
            # cap's residual, and the honest measure of what the campaign
            # structure refuses to give up.
            "cap_blocked_tyres": nbad,
            "cap_blocked_pct": round(100.0 * nbad / max(tp.height, 1), 2),
            "beyond_demand_tyres": int(bk.filter(pl.col("unsat")).height),
        }

    # ---- apply: defer only. Volume is never destroyed. -------------------
    out = build_df.with_columns(
        pl.col("lot_id").replace_strict(shift_of, default=0.0)
        .cast(pl.Float64).alias("cap_shift_h"))
    out = out.with_columns(
        (pl.col("start_ts") + pl.duration(seconds=pl.col("cap_shift_h") * 3600)),
        (pl.col("end_ts") + pl.duration(seconds=pl.col("cap_shift_h") * 3600)))

    stats["deferred_lots"] = len(shift_of)
    log.info("engine.cap_stock", **{k: v for k, v in stats.items() if k != "plants"})
    return out, stats
