"""Measure every constant the planner uses, per plant, across ALL 8 months.

A number is only safe to FIX as a constant if the plant holds it steady across
months. If it drifts, the planner must derive it at run time from the trailing
window instead -- otherwise it is fitted to whichever month it was read off, and
that cannot be detected from that month's own KPIs.

    STABLE  CV <= 0.05   -> safe to hard-code (still prefer deriving)
    DRIFTS  CV >  0.05   -> MUST be derived per month

Read-only. Uses the full history deliberately: the question is whether a
quantity is stable, which is a property of all months, not of one. The planner
itself still derives these under the as-of cutoff, so nothing leaks into a plan.
"""
from __future__ import annotations

import statistics as st
import sys

from planner.data.warehouse import duck, set_cutoff
from planner.runs.logger import log

Q = {
    # (name, unit, sql) -> one row per (plant, month, value)
    "cure_cadence_s": ("s/tyre", """
        WITH s AS (
            SELECT plant, date_trunc('month', event_ts) AS mo,
                   wcID::VARCHAR AS p, CAST(event_ts AS DATE) AS d,
                   count(*) AS n
            FROM v_curing WHERE statuscritical = 'Normal' GROUP BY 1,2,3,4
        )
        SELECT plant, mo, 28800.0 / (quantile_cont(n, 0.5) / 3.0) FROM s GROUP BY 1,2
    """),
    "press_active_days": ("days/31", """
        WITH pd AS (
            SELECT plant, date_trunc('month', event_ts) AS mo,
                   wcID::VARCHAR AS p, count(DISTINCT date) AS d
            FROM v_curing WHERE statuscritical = 'Normal' GROUP BY 1,2,3
        )
        SELECT plant, mo, quantile_cont(d, 0.5) FROM pd GROUP BY 1,2
    """),
    "presses_used": ("count", """
        SELECT plant, date_trunc('month', event_ts) AS mo,
               count(DISTINCT wcID::VARCHAR)
        FROM v_curing WHERE statuscritical = 'Normal' GROUP BY 1,2
    """),
    "gt_cure_window_D": ("days", """
        WITH gd AS (
            SELECT b.plant, date_trunc('month', c.event_ts) AS mo,
                   b.itemCode AS g, count(DISTINCT c.date) AS d
            FROM v_build b JOIN v_curing c ON b.productionID = c.gtbarCode
            WHERE b.stage = 2 AND c.statuscritical = 'Normal' GROUP BY 1,2,3
        )
        SELECT plant, mo, quantile_cont(d, 0.5) FROM gd WHERE d >= 2 GROUP BY 1,2
    """),
    "gts_active": ("count", """
        SELECT plant, date_trunc('month', event_ts) AS mo,
               count(DISTINCT itemCode)
        FROM v_build WHERE stage = 2 AND itemCode IS NOT NULL GROUP BY 1,2
    """),
    "cured_per_day": ("tyres", """
        WITH d AS (
            SELECT plant, date_trunc('month', event_ts) AS mo,
                   CAST(event_ts AS DATE) AS dd, count(*) AS n
            FROM v_curing WHERE statuscritical = 'Normal' GROUP BY 1,2,3
        )
        SELECT plant, mo, quantile_cont(n, 0.5) FROM d GROUP BY 1,2
    """),
    "curing_changeovers": ("count", """
        WITH seq AS (
            SELECT c.plant, date_trunc('month', c.event_ts) AS mo,
                   c.wcID::VARCHAR AS p, c.event_ts AS ts, b.itemCode AS g,
                   lag(b.itemCode) OVER (PARTITION BY c.plant, c.wcID
                                         ORDER BY c.event_ts) AS prev
            FROM v_curing c JOIN v_build b ON b.productionID = c.gtbarCode
            WHERE b.stage = 2 AND c.statuscritical = 'Normal'
        )
        SELECT plant, mo, count(*) FROM seq
        WHERE prev IS NOT NULL AND prev <> g GROUP BY 1,2
    """),
    "build_machines": ("count", """
        SELECT plant, date_trunc('month', event_ts) AS mo,
               count(DISTINCT machineCode)
        FROM v_build WHERE stage = 2 GROUP BY 1,2
    """),
    "skus_per_machine_day": ("count", """
        WITH md AS (
            SELECT plant, date_trunc('month', event_ts) AS mo, machineCode AS m,
                   CAST(event_ts AS DATE) AS d, count(DISTINCT itemCode) AS k
            FROM v_build WHERE stage = 2 GROUP BY 1,2,3,4
        )
        SELECT plant, mo, avg(k) FROM md GROUP BY 1,2
    """),
    "build_cure_lag_p50_h": ("h", """
        SELECT b.plant, date_trunc('month', c.event_ts) AS mo,
               quantile_cont(date_diff('second', b.event_ts, c.event_ts), 0.5) / 3600.0
        FROM v_build b JOIN v_curing c ON b.productionID = c.gtbarCode
        WHERE b.stage = 2 AND c.statuscritical = 'Normal'
          AND c.event_ts > b.event_ts GROUP BY 1,2
    """),
    "build_cure_lag_p95_h": ("h", """
        SELECT b.plant, date_trunc('month', c.event_ts) AS mo,
               quantile_cont(date_diff('second', b.event_ts, c.event_ts), 0.95) / 3600.0
        FROM v_build b JOIN v_curing c ON b.productionID = c.gtbarCode
        WHERE b.stage = 2 AND c.statuscritical = 'Normal'
          AND c.event_ts > b.event_ts GROUP BY 1,2
    """),
}


def main() -> int:
    set_cutoff(None)
    con = duck()
    verdicts: list[tuple[str, str, str, float, float, str]] = []
    for name, (unit, sql) in Q.items():
        try:
            rows = con.execute(sql).fetchall()
        except Exception as e:  # noqa: BLE001
            print(f"\n{name}: QUERY FAILED -- {e}")
            continue
        by_plant: dict[str, list[tuple[str, float]]] = {}
        for plant, mo, val in rows:
            if val is None or plant is None:
                continue
            by_plant.setdefault(plant, []).append((str(mo)[:7], float(val)))
        print(f"\n=== {name}  [{unit}]")
        for plant in sorted(by_plant):
            series = sorted(by_plant[plant])
            vals = [v for _m, v in series]
            if len(vals) < 2:
                continue
            mean = st.mean(vals)
            cv = (st.pstdev(vals) / mean) if mean else 0.0
            verdict = "STABLE" if cv <= 0.05 else "DRIFTS"
            verdicts.append((name, plant, unit, mean, cv, verdict))
            cells = "  ".join(f"{m[5:]}:{v:,.1f}" for m, v in series)
            print(f"  {plant:4s} {cells}")
            print(f"       mean {mean:,.2f}  min {min(vals):,.1f}  "
                  f"max {max(vals):,.1f}  CV {cv:.3f}  -> {verdict}")

    print("\n" + "=" * 74)
    print("SUMMARY -- what may be hard-coded, what must be derived per month")
    print("=" * 74)
    for name, plant, unit, mean, cv, verdict in sorted(
            verdicts, key=lambda t: (t[5], -t[4])):
        print(f"  {verdict:7s} CV {cv:5.3f}  {plant:4s} {name:24s} "
              f"mean {mean:>10,.2f} {unit}")
    log.info("calibrate.done", quantities=len(Q), series=len(verdicts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
