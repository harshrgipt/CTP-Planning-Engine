"""Measure the PLANT's own build-run distribution from MES. READ-ONLY.

    PYTHONPATH=. python scripts/plant_run_baseline.py 2026-07

Same run definition the planner now emits: a maximal consecutive same-GT block
on one machine. Quoting a single p50 from a doc is not enough to judge a lot
size -- the min and max decide whether a plan is runnable, and the plant's own
spread is the only fair target.

Build stage 2 `itemCode` IS the GT code. Filter on the Hive partition column
`date`, never `event_ts`, or the scan does not prune.
"""
from __future__ import annotations

import sys

import numpy as np

from planner.data.warehouse import duck

Q = """
WITH b AS (
  SELECT plant, machineCode AS m, itemCode AS gt, event_ts, date
  FROM v_build
  WHERE stage = 2 AND date >= ?::DATE AND date < ?::DATE
    AND itemCode IS NOT NULL AND machineCode IS NOT NULL
),
f AS (
  SELECT *, CASE WHEN gt IS DISTINCT FROM
                 lag(gt) OVER (PARTITION BY plant, m ORDER BY event_ts)
            THEN 1 ELSE 0 END AS newrun
  FROM b
),
r AS (
  SELECT plant, m, gt, event_ts, date,
         sum(newrun) OVER (PARTITION BY plant, m ORDER BY event_ts
             ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS rid
  FROM f
)
SELECT plant, m, rid, gt, count(*) AS qty,
       epoch(max(event_ts) - min(event_ts)) / 3600.0 AS hrs,
       min(event_ts) AS t0, max(event_ts) AS t1
FROM r GROUP BY 1, 2, 3, 4
"""

MD = """
SELECT plant, count(DISTINCT machineCode || '|' || CAST(date AS VARCHAR)) AS mdays
FROM v_build
WHERE stage = 2 AND date >= ?::DATE AND date < ?::DATE AND machineCode IS NOT NULL
GROUP BY 1
"""


def main() -> None:
    month = sys.argv[1] if len(sys.argv) > 1 else "2026-07"
    y, m = int(month[:4]), int(month[5:7])
    lo = f"{y:04d}-{m:02d}-01"
    hi = f"{y + (m == 12):04d}-{(m % 12) + 1:02d}-01"
    con = duck()
    rows = con.execute(Q, [lo, hi]).fetchall()
    mdays = dict(con.execute(MD, [lo, hi]).fetchall())

    print(f"PLANT ACTUAL BUILD RUNS -- {month}  (MES stage 2, "
          f"maximal consecutive same-GT block per machine)\n")
    hdr = (f"{'plant':<6}{'runs':>7}{'tyres':>10}{'min':>6}{'p05':>7}{'p50':>7}"
           f"{'p95':>8}{'max':>8}{'hours p50':>11}{'chg/mach-day':>14}"
           f"{'machines':>10}{'GTs':>6}")
    print(hdr)
    print("-" * len(hdr))
    for plant in ("PCR", "TBR"):
        rs = [r for r in rows if r[0] == plant]
        if not rs:
            continue
        q = np.array([r[4] for r in rs], float)
        h = np.array([r[5] for r in rs], float)
        machines = len({r[1] for r in rs})
        md = mdays.get(plant, 1)
        chg = (len(rs) - machines) / max(md, 1)
        print(f"{plant:<6}{len(rs):>7,}{int(q.sum()):>10,}{q.min():>6.0f}"
              f"{np.percentile(q,5):>7.0f}{np.percentile(q,50):>7.0f}"
              f"{np.percentile(q,95):>8.0f}{q.max():>8.0f}"
              f"{np.percentile(h,50):>11.2f}{chg:>14.2f}"
              f"{machines:>10}{len({r[3] for r in rs}):>6}")
        # how much of the plant's own output sits below our policy floor
        floor = 150 if plant == "PCR" else 70
        below = int((q < floor).sum())
        print(f"      runs below the {floor}-tyre B12 floor: {below:,} of "
              f"{len(rs):,} ({100*below/len(rs):.1f}%), carrying "
              f"{int(q[q < floor].sum()):,} tyres "
              f"({100*q[q < floor].sum()/q.sum():.1f}% of volume)")


if __name__ == "__main__":
    main()
