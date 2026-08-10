"""KPI computations on a planner-produced schedule + inventory ledger."""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import date

import polars as pl

from planner.plan.ledger import GreenTireLedger


@dataclass
class KPI:
    demand_qty:              float
    scheduled_qty:           float
    demand_fulfillment_rate: float
    changeover_count:        int
    machine_util_pct:        float
    avg_wip:                 float
    curing_wait_p95_s:       float
    starvation_events:       int
    schedule_stability:      float  # 0..1; higher = more stable
    soft_violation_score:    float

    def to_dict(self) -> dict:
        return asdict(self)


def compute(demand: pl.DataFrame,
            build_df: pl.DataFrame,
            cure_df: pl.DataFrame,
            ledger: GreenTireLedger,
            prev_build_df: pl.DataFrame | None = None) -> KPI:
    demand_qty = float(demand["qty"].sum()) if demand.height else 0.0
    scheduled_qty = float(build_df["qty"].sum()) if build_df.height else 0.0

    fulfillment = scheduled_qty / demand_qty if demand_qty > 0 else 0.0

    # Changeovers: count transitions per machine
    if build_df.height:
        ord_df = build_df.sort(["machine", "start_ts"])
        prev = ord_df.select(
            pl.col("gt_code").shift(1).over("machine").alias("prev_gt"),
            pl.col("gt_code"),
        )
        chg = int(
            (prev["prev_gt"].is_not_null() & (prev["prev_gt"] != prev["gt_code"])).sum()
        )
    else:
        chg = 0

    # Utilization: schedule seconds / horizon
    util = 0.0
    if build_df.height:
        span = (build_df["end_ts"].max() - build_df["start_ts"].min()).total_seconds()
        occupied = (build_df["end_ts"] - build_df["start_ts"]).dt.total_seconds().sum()
        n_machines = build_df["machine"].n_unique()
        denom = span * n_machines
        if denom > 0:
            util = 100.0 * float(occupied) / float(denom)

    # WIP: mean absolute GT inventory. Compute daily via per-day aggregation
    # rather than per-event window (avoids 800K-row window in :memory:).
    if build_df.height:
        daily = ledger.con.execute("""
            SELECT plant, gt_code, CAST(ts AS DATE) AS d, sum(qty_delta) AS delta
            FROM gt_events GROUP BY 1,2,3
        """).pl().sort(["plant", "gt_code", "d"])
        if daily.height:
            daily = daily.with_columns(
                pl.col("delta").cum_sum().over(["plant", "gt_code"]).alias("bal")
            )
            wip = float(daily["bal"].abs().mean())
        else:
            wip = 0.0
    else:
        wip = 0.0

    # Curing wait per tyre: pair kth cure event with kth supply event on the
    # ledger (FIFO), then measure cure_ts - supply_ts.
    if cure_df.height:
        wait_row = ledger.con.execute("""
            WITH ranked_supply AS (
                SELECT plant, gt_code, ts,
                       row_number() OVER (PARTITION BY plant, gt_code ORDER BY ts) AS rk
                FROM gt_events WHERE source IN ('build','opening') AND qty_delta > 0
            ),
            ranked_cure AS (
                SELECT plant, gt_code, ts,
                       row_number() OVER (PARTITION BY plant, gt_code ORDER BY ts) AS rk
                FROM gt_events WHERE source = 'cure'
            )
            SELECT quantile_cont(date_diff('second', s.ts, c.ts), 0.95) AS p95
            FROM ranked_cure c
            JOIN ranked_supply s USING (plant, gt_code, rk)
            WHERE c.ts >= s.ts
        """).fetchone()
        p95 = float(wait_row[0]) if wait_row and wait_row[0] is not None else 0.0
    else:
        p95 = 0.0

    starve = ledger.starvations().height

    # Stability vs prior plan: fraction of (machine, gt_code, date) tuples that
    # match between plans. If no prior, treat as 1.0 (perfect).
    if prev_build_df is not None and prev_build_df.height and build_df.height:
        cur_set = set(
            build_df.with_columns(pl.col("start_ts").cast(pl.Date).alias("d"))
                    .select(["machine", "gt_code", "d"])
                    .rows()
        )
        prev_set = set(
            prev_build_df.with_columns(pl.col("start_ts").cast(pl.Date).alias("d"))
                         .select(["machine", "gt_code", "d"])
                         .rows()
        )
        overlap = len(cur_set & prev_set)
        stability = overlap / max(1, len(cur_set | prev_set))
    else:
        stability = 1.0

    return KPI(
        demand_qty=demand_qty,
        scheduled_qty=scheduled_qty,
        demand_fulfillment_rate=fulfillment,
        changeover_count=chg,
        machine_util_pct=util,
        avg_wip=wip,
        curing_wait_p95_s=p95,
        starvation_events=starve,
        schedule_stability=stability,
        soft_violation_score=float(starve),
    )
