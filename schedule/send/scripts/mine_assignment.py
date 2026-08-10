"""L1 -- HOW DOES THE PLANT ASSIGN GTs TO BUILDING MACHINES?

    python scripts/mine_assignment.py

This is the missing piece for the dominant open defect: our build stickiness is
41% against the plant's 99.8%. Before designing a building-window rule from
theory (b_g = q_g / c_m), measure what the plant actually does. The answer is
sitting in 8 months of MES.

Six questions:
  A. Does a MACHINE own a set of GTs?          (concentration per machine-month)
  B. Does a GT own a set of MACHINES?          (concentration per GT-month)
  C. Is the assignment STABLE across months?   (Jaccard of GT sets per machine)
  D. Is a (machine, GT) run CONTIGUOUS in time or scattered across the month?
  E. Does machine count scale with volume?     (the b_g = q_g/c_m test)
  F. What does a machine's day look like?      (GTs per machine-day, run length)
"""
from __future__ import annotations

import sys

import polars as pl

from planner.data.warehouse import duck, set_cutoff
from planner.runs.logger import log


def main() -> int:
    set_cutoff(None)
    con = duck()
    con.execute("""
        CREATE OR REPLACE TEMP TABLE bm AS
        SELECT plant, machineCode AS machine, itemCode AS gt,
               date_trunc('month', event_ts)::DATE AS mo,
               CAST(event_ts AS DATE) AS d, event_ts
        FROM v_build
        WHERE stage = 2 AND itemCode IS NOT NULL AND machineCode IS NOT NULL
    """)

    print("=" * 88)
    print("A. DOES A MACHINE OWN A SET OF GTs?  (per machine-month)")
    print("=" * 88)
    a = con.execute("""
        WITH mg AS (SELECT plant, mo, machine, gt, count(*) n FROM bm GROUP BY 1,2,3,4),
             tot AS (SELECT plant, mo, machine, sum(n) t FROM mg GROUP BY 1,2,3),
             rk AS (SELECT mg.*, t, n::DOUBLE/t AS shr,
                    row_number() OVER (PARTITION BY mg.plant, mg.mo, mg.machine
                                       ORDER BY n DESC) r
                    FROM mg JOIN tot USING (plant, mo, machine))
        SELECT plant,
               avg(CASE WHEN r=1 THEN shr END) top1,
               avg(CASE WHEN r<=3 THEN shr END)*3 top3,
               avg(CASE WHEN r=1 THEN 1.0 ELSE 0 END)*count(*)/count(DISTINCT mo||machine) gts_per_machine_month
        FROM rk GROUP BY 1
    """).pl()
    for r in a.iter_rows(named=True):
        print(f"  {r['plant']}: top GT = {100*r['top1']:.1f}% of a machine's month, "
              f"top-3 = {100*r['top3']:.1f}%, "
              f"{r['gts_per_machine_month']:.1f} GTs per machine-month")

    print("\n" + "=" * 88)
    print("B. DOES A GT OWN A SET OF MACHINES?  (per GT-month)")
    print("=" * 88)
    b = con.execute("""
        WITH gm AS (SELECT plant, mo, gt, machine, count(*) n FROM bm GROUP BY 1,2,3,4),
             tot AS (SELECT plant, mo, gt, sum(n) t, count(*) nm FROM gm GROUP BY 1,2,3),
             rk AS (SELECT gm.*, t, nm, n::DOUBLE/t AS shr,
                    row_number() OVER (PARTITION BY gm.plant, gm.mo, gm.gt
                                       ORDER BY n DESC) r
                    FROM gm JOIN tot USING (plant, mo, gt))
        SELECT plant, avg(CASE WHEN r=1 THEN shr END) top1,
               quantile_cont(nm, 0.5) machines_p50, max(nm) machines_max
        FROM rk GROUP BY 1
    """).pl()
    for r in b.iter_rows(named=True):
        print(f"  {r['plant']}: a GT's top machine takes {100*r['top1']:.1f}% of its "
              f"month; uses {r['machines_p50']:.0f} machines (max {r['machines_max']})")

    print("\n" + "=" * 88)
    print("C. IS THE ASSIGNMENT STABLE MONTH TO MONTH?  (Jaccard of a machine's GT set)")
    print("=" * 88)
    sets = con.execute(
        "SELECT plant, mo, machine, gt FROM bm GROUP BY 1,2,3,4").pl()
    for plant in sorted(sets["plant"].unique().to_list()):
        s = sets.filter(pl.col("plant") == plant)
        months = sorted(s["mo"].unique().to_list())
        prev: dict[str, set] = {}
        line = []
        for mo in months:
            cur: dict[str, set] = {}
            for r in s.filter(pl.col("mo") == mo).iter_rows(named=True):
                cur.setdefault(r["machine"], set()).add(r["gt"])
            if prev:
                js = []
                for m, g in cur.items():
                    o = prev.get(m)
                    if o:
                        js.append(len(g & o) / max(1, len(g | o)))
                if js:
                    line.append(f"{str(mo)[5:7]}:{sum(js)/len(js):.2f}")
            prev = cur
        print(f"  {plant}: month-over-month Jaccard  {'  '.join(line)}")

    print("\n" + "=" * 88)
    print("D. IS A (MACHINE, GT) RUN CONTIGUOUS OR SCATTERED?")
    print("=" * 88)
    d = con.execute("""
        WITH mgd AS (SELECT plant, mo, machine, gt, d FROM bm GROUP BY 1,2,3,4,5),
             sp AS (SELECT plant, mo, machine, gt,
                      count(*) AS active_days,
                      date_diff('day', min(d), max(d)) + 1 AS span_days
                    FROM mgd GROUP BY 1,2,3,4)
        SELECT plant, quantile_cont(active_days,0.5) act_p50,
               quantile_cont(span_days,0.5) span_p50,
               avg(active_days::DOUBLE/span_days) density
        FROM sp WHERE active_days >= 2 GROUP BY 1
    """).pl()
    for r in d.iter_rows(named=True):
        print(f"  {r['plant']}: a (machine,GT) pair is active {r['act_p50']:.0f} days "
              f"over a {r['span_p50']:.0f}-day span -> density "
              f"{r['density']:.2f}   ({'CONTIGUOUS' if r['density']>0.7 else 'SCATTERED'})")

    print("\n" + "=" * 88)
    print("E. DOES MACHINE COUNT SCALE WITH VOLUME?   b_g = q_g / c_m ?")
    print("=" * 88)
    e = con.execute("""
        WITH gm AS (SELECT plant, mo, gt, count(*) n, count(DISTINCT machine) nm,
                           count(DISTINCT d) nd FROM bm GROUP BY 1,2,3)
        SELECT plant, gt, mo, n, nm, nd, n::DOUBLE/nd AS per_day FROM gm WHERE n > 200
    """).pl()
    for plant in sorted(e["plant"].unique().to_list()):
        s = e.filter(pl.col("plant") == plant)
        x = s["per_day"].to_list()
        y = s["nm"].to_list()
        mx, my = sum(x) / len(x), sum(y) / len(y)
        cov = sum((a - mx) * (bb - my) for a, bb in zip(x, y))
        vx = sum((a - mx) ** 2 for a in x)
        vy = sum((bb - my) ** 2 for bb in y)
        r = cov / ((vx ** .5) * (vy ** .5) or 1)
        slope = cov / vx if vx else 0
        print(f"  {plant}: machines vs daily rate  r = {r:.3f}, slope = {slope:.5f} "
              f"=> c_m ~ {1/slope if slope else 0:,.0f} tyres/machine-day")
        for lo, hi in [(0, 200), (200, 500), (500, 1000), (1000, 99999)]:
            b2 = s.filter((pl.col("per_day") >= lo) & (pl.col("per_day") < hi))
            if b2.height:
                print(f"      draw {lo:>5}-{hi if hi<9999 else '+':>5}/day : "
                      f"{b2.height:4d} GT-months, machines p50 "
                      f"{b2['nm'].median():.0f} max {b2['nm'].max()}")

    print("\n" + "=" * 88)
    print("F. WHAT DOES A MACHINE-DAY LOOK LIKE?")
    print("=" * 88)
    f = con.execute("""
        WITH md AS (SELECT plant, machine, d, count(DISTINCT gt) k, count(*) n
                    FROM bm GROUP BY 1,2,3)
        SELECT plant, avg(k) gts_per_day, quantile_cont(k,0.5) k_p50,
               max(k) k_max, avg(n) tyres_per_day
        FROM md GROUP BY 1
    """).pl()
    for r in f.iter_rows(named=True):
        print(f"  {r['plant']}: {r['gts_per_day']:.2f} GTs per machine-day "
              f"(p50 {r['k_p50']:.0f}, max {r['k_max']}), "
              f"{r['tyres_per_day']:.0f} tyres/machine-day")
    log.info("mine_assignment.done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
