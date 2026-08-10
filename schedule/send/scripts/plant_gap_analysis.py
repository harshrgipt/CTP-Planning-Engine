"""WHERE ARE WE LAGGING THE PLANT? -- July 2026, whole chain. READ-ONLY.

    PYTHONPATH=. python scripts/plant_gap_analysis.py 2026-07 runs/exp_base

Measures the plant's own July on the SAME definitions the planner emits, so the
comparison is like-for-like at every stage:

  build side   runs, lot size, changeovers
  cure side    campaigns per press (break on mouldNo -- the physical event, at
               100% population; a time-gap cutoff is an assumption and L0 showed
               the bands are sensitive to it), n_active GTs, presses per GT
  coupling     GT wait per tyre via the barcode join
               v_build.productionID = v_curing.gtbarCode  (99.6% hit rate)

Curing `cycleStart` is press-OPEN, i.e. the cycle END; `event_ts` is press-close.
The source names are backwards and are deliberately not "fixed".
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import polars as pl

from planner.data.warehouse import duck

ROOT = Path(__file__).resolve().parent.parent

# cure campaigns: consecutive same-GT block on one press, broken on mould change
CURE = """
WITH c AS (
  SELECT plant, CAST(wcID AS VARCHAR) AS press, mouldNo, event_ts, date,
         CAST(recipeID AS VARCHAR) AS item
  FROM v_curing
  WHERE date >= ?::DATE AND date < ?::DATE AND wcID IS NOT NULL
),
f AS (
  SELECT *, CASE WHEN item IS DISTINCT FROM lag(item) OVER w
                  OR mouldNo IS DISTINCT FROM lag(mouldNo) OVER w
            THEN 1 ELSE 0 END AS newc
  FROM c WINDOW w AS (PARTITION BY plant, press ORDER BY event_ts)
),
r AS (
  SELECT *, sum(newc) OVER (PARTITION BY plant, press ORDER BY event_ts
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS cid
  FROM f
)
SELECT plant, press, cid, item, count(*) AS qty,
       min(event_ts) AS t0, max(event_ts) AS t1,
       epoch(max(event_ts) - min(event_ts))/3600.0 AS hrs
FROM r GROUP BY 1,2,3,4
"""

# per-tyre GT wait: built at v_build.event_ts, cured at v_curing.event_ts
WAIT = """
SELECT b.plant,
       epoch(c.event_ts - b.event_ts)/3600.0 AS wait_h
FROM v_build b
JOIN v_curing c ON b.productionID = c.gtbarCode
WHERE b.stage = 2 AND b.date >= ?::DATE AND b.date < ?::DATE
  AND c.date >= ?::DATE AND c.date < ?::DATE
"""

PRESS_DAYS = """
SELECT plant, count(DISTINCT CAST(wcID AS VARCHAR) || '|' || CAST(date AS VARCHAR)) AS pd,
       count(DISTINCT CAST(wcID AS VARCHAR)) AS presses, count(*) AS tyres
FROM v_curing WHERE date >= ?::DATE AND date < ?::DATE AND wcID IS NOT NULL
GROUP BY 1
"""


def n_active(iv: list, horizon_h: float) -> float:
    """Mean number of distinct GTs live, from the union of each GT's intervals."""
    by_gt: dict = {}
    for gt, s, e in iv:
        by_gt.setdefault(gt, []).append((s, e))
    live = 0.0
    for gt, xs in by_gt.items():
        xs.sort()
        merged = []
        for s, e in xs:
            if merged and s <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], e))
            else:
                merged.append((s, e))
        live += sum((e - s).total_seconds() / 3600 for s, e in merged)
    return live / horizon_h


def main() -> None:
    month = sys.argv[1] if len(sys.argv) > 1 else "2026-07"
    ours = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "runs" / "exp_base"
    y, m = int(month[:4]), int(month[5:7])
    lo = f"{y:04d}-{m:02d}-01"
    hi = f"{y + (m == 12):04d}-{(m % 12) + 1:02d}-01"
    H = 744.0
    con = duck()

    cure = con.execute(CURE, [lo, hi]).fetchall()
    wait = con.execute(WAIT, [lo, hi, lo, hi]).fetchall()
    pdays = {r[0]: (r[1], r[2], r[3]) for r in con.execute(PRESS_DAYS, [lo, hi]).fetchall()}

    ocamp = pl.read_parquet(ours / "cure_campaigns.parquet")
    obuild = pl.read_parquet(ours / "build_schedule.parquet")

    print(f"PLANT vs OURS -- {month}   (ours = {ours.name})\n")
    for plant in ("PCR", "TBR"):
        cs = [r for r in cure if r[0] == plant]
        ws = np.array([r[1] for r in wait if r[0] == plant], float)
        ws = ws[(ws >= 0) & (ws < 24 * 30)]
        pd_, npress, ctyres = pdays.get(plant, (1, 1, 1))
        q = np.array([r[4] for r in cs], float)
        h = np.array([r[7] for r in cs], float)
        pl_n = n_active([(r[3], r[5], r[6]) for r in cs], H)
        # presses per GT, plant
        gtp: dict = {}
        for r in cs:
            gtp.setdefault(r[3], set()).add(r[1])
        pl_ppg = np.mean([len(v) for v in gtp.values()])

        oc = ocamp.filter(pl.col("plant") == plant)
        ob = obuild.filter(pl.col("plant") == plant)
        o_n = n_active(list(zip(oc["gt_code"].to_list(), oc["start_ts"].to_list(),
                                oc["end_ts"].to_list())), H)
        o_ppg = float(oc.group_by("gt_code").agg(
            pl.col("press").n_unique()).mean()["press"][0])
        ow = np.array(ob["wait_h"], float)
        o_ph = float(oc["hours"].sum())
        pl_ph = float(h.sum())

        print("=" * 88)
        print(f"{plant}")
        print("=" * 88)
        print(f"  {'':<32}{'PLANT':>16}{'OURS':>16}{'gap':>14}")
        rows = [
            ("cure presses used", npress, oc["press"].n_unique(), "n"),
            ("tyres cured", ctyres, int(oc["qty"].sum()), "n"),
            ("press-hours occupied", pl_ph, o_ph, "n"),
            ("press util % of 744h", 100 * pl_ph / (npress * H),
             100 * o_ph / (oc["press"].n_unique() * H), "pct"),
            ("cure campaigns", len(cs), oc.height, "n"),
            ("campaign tyres p50", np.percentile(q, 50),
             float(oc["qty"].median()), "n"),
            ("campaign hours p50", np.percentile(h, 50),
             float(oc["hours"].median()), "n"),
            ("presses per GT (distinct)", pl_ppg, o_ppg, "f"),
            ("n_active GTs", pl_n, o_n, "f"),
            ("GT wait p50 h", np.percentile(ws, 50), np.percentile(ow, 50), "f"),
            ("GT wait mean h", ws.mean(), ow.mean(), "f"),
            ("GT wait p95 h", np.percentile(ws, 95), np.percentile(ow, 95), "f"),
        ]
        for label, a, b, kind in rows:
            fmt = "{:,.0f}" if kind == "n" else ("{:.1f}%" if kind == "pct" else "{:.2f}")
            gap = (b - a) / a * 100 if a else 0
            print(f"  {label:<32}{fmt.format(a):>16}{fmt.format(b):>16}"
                  f"{gap:>13.0f}%")


if __name__ == "__main__":
    main()
