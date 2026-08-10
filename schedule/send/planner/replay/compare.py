"""Compare planner-produced schedule vs actual historical outcome for a month."""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import date, timedelta

from planner.data.warehouse import duck
from planner.replay.kpi import KPI


@dataclass
class Comparison:
    month: str
    planner: dict
    actual: dict
    delta: dict
    wins: int

    def to_dict(self) -> dict:
        return asdict(self)


def _actual_kpis(month_start: date, month_end: date) -> dict:
    con = duck()
    # Split into 3 focused queries so DuckDB can partition-prune each and no
    # accidental cartesian join can appear.
    r_prod = con.execute(
        """
        WITH b AS (
            SELECT plant, itemCode AS gt_code, machineCode AS machine, event_ts
            FROM v_build WHERE stage = 2 AND QualityStatus = '1'
              AND date BETWEEN ? AND ?
        ),
        ord AS (
            SELECT machine, gt_code,
                   lag(gt_code) OVER (PARTITION BY machine ORDER BY event_ts) AS prev_gt
            FROM b
        )
        SELECT (SELECT count(*) FROM b) AS scheduled_qty,
               (SELECT sum(CASE WHEN prev_gt IS NOT NULL AND prev_gt <> gt_code THEN 1 ELSE 0 END) FROM ord) AS changeovers
        """,
        [month_start, month_end],
    ).fetchone()
    scheduled_qty = float(r_prod[0] or 0)
    changeovers = int(r_prod[1] or 0)

    # Historical machine utilization for the month: sum of build-event intervals
    # per (plant, machine) divided by span × n_machines, matching planner metric.
    r_util = con.execute(
        """
        WITH gaps AS (
            SELECT machineCode AS machine,
                   date_diff('second',
                       lag(event_ts) OVER (PARTITION BY machineCode ORDER BY event_ts),
                       event_ts) AS gap_s
            FROM v_build
            WHERE stage = 2 AND QualityStatus = '1'
              AND date BETWEEN ? AND ?
        ),
        active AS (
            SELECT machine, sum(CASE WHEN gap_s BETWEEN 1 AND 3600 THEN gap_s ELSE 0 END) AS occ_s
            FROM gaps GROUP BY 1
        ),
        span AS (
            SELECT date_diff('second', min(event_ts), max(event_ts)) AS span_s,
                   count(DISTINCT machineCode) AS n_machines
            FROM v_build WHERE stage = 2 AND QualityStatus = '1' AND date BETWEEN ? AND ?
        )
        SELECT (SELECT sum(occ_s) FROM active) / NULLIF((SELECT span_s * n_machines FROM span), 0) * 100.0
        """,
        [month_start, month_end, month_start, month_end],
    ).fetchone()
    util_pct = float(r_util[0]) if r_util and r_util[0] is not None else 0.0

    # Curing wait per built tyre: proper join via productionID = gtbarCode.
    r_wait = con.execute(
        """
        SELECT quantile_cont(date_diff('second', b.event_ts, c.event_ts), 0.95) AS p95
        FROM v_build b
        JOIN v_curing c ON b.productionID = c.gtbarCode
        WHERE b.stage = 2 AND b.QualityStatus = '1'
          AND b.date BETWEEN ? AND ?
          AND c.statuscritical = 'Normal'
          AND c.event_ts >= b.event_ts
        """,
        [month_start, month_end],
    ).fetchone()
    wait_p95 = float(r_wait[0]) if r_wait and r_wait[0] is not None else 0.0
    scheduled_qty = float(scheduled_qty or 0)
    return {
        "demand_qty": scheduled_qty,  # actual production == "actual demand" proxy
        "scheduled_qty": scheduled_qty,
        "demand_fulfillment_rate": 1.0,
        "changeover_count": int(changeovers or 0),
        "machine_util_pct": util_pct,
        "avg_wip": 0.0,
        "curing_wait_p95_s": float(wait_p95 or 0.0),
        "starvation_events": 0,
        "schedule_stability": 1.0,
        "soft_violation_score": 0.0,
    }


def compare(month_start: date, planner_kpi: KPI) -> Comparison:
    # month_end = last day of month_start
    if month_start.month == 12:
        next_first = date(month_start.year + 1, 1, 1)
    else:
        next_first = date(month_start.year, month_start.month + 1, 1)
    month_end = next_first - timedelta(days=1)

    actual = _actual_kpis(month_start, month_end)
    planner = planner_kpi.to_dict()

    delta: dict[str, float] = {}
    wins = 0
    # Higher is better
    for k in ("demand_fulfillment_rate", "machine_util_pct", "schedule_stability"):
        dv = float(planner[k]) - float(actual[k])
        delta[k] = dv
        if dv >= 0:
            wins += 1
    # Lower is better
    for k in ("changeover_count", "avg_wip", "curing_wait_p95_s", "starvation_events", "soft_violation_score"):
        dv = float(actual[k]) - float(planner[k])
        delta[k] = dv
        if dv >= 0:
            wins += 1

    return Comparison(
        month=month_start.strftime("%Y-%m"),
        planner=planner, actual=actual, delta=delta, wins=wins,
    )
