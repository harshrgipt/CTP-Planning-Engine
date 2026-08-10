"""Discover HOW THE PLANT HOLDS GREEN-TYRE INVENTORY, from 8 months of MES.

    python -m scripts.analyse_plant_gt

Answers, per month and per plant:
  A. build vs cure balance          -- is the plant in steady state?
  B. daily GT inventory trajectory  -- level, oscillation, drift
  C. WHICH GTs are held, and how much
  D. concentration                  -- few GTs or spread?
  E. the sizing rule                -- what does stock scale with?

Everything is per-tyre, joined on the documented barcode key
`v_build.productionID = v_curing.gtbarCode` (MEMORY s3). Plant day boundary is
07:00, matching the shift grid.
"""
from __future__ import annotations

import statistics as st

import polars as pl

from planner.data.warehouse import duck, set_cutoff
from planner.runs.logger import log

DAY_H = 7


def main() -> int:
    set_cutoff(None)
    con = duck()

    print("Building per-tyre build/cure pairs (this is the big join)...")
    con.execute("""
        CREATE OR REPLACE TEMP TABLE pairs AS
        SELECT b.plant,
               b.itemCode                AS gt,
               b.event_ts                AS b_ts,
               c.event_ts                AS c_ts,
               date_trunc('month', c.event_ts)::DATE AS mo,
               date_diff('second', b.event_ts, c.event_ts)/3600.0 AS lag_h
        FROM v_build b
        JOIN v_curing c ON b.productionID = c.gtbarCode
        WHERE b.stage = 2 AND b.QualityStatus = '1'
          AND c.statuscritical = 'Normal' AND c.event_ts >= b.event_ts
          AND b.itemCode IS NOT NULL
    """)
    n = con.execute("SELECT count(*) FROM pairs").fetchone()[0]
    print(f"  {n:,} matched tyres\n")

    # ---------- A. balance -------------------------------------------------
    print("=" * 78)
    print("A. BUILD vs CURE BALANCE  (is the plant in steady state?)")
    print("=" * 78)
    bal = con.execute("""
        WITH b AS (SELECT plant, date_trunc('month', event_ts)::DATE mo, count(*) built
                   FROM v_build WHERE stage=2 AND QualityStatus='1' GROUP BY 1,2),
             c AS (SELECT plant, date_trunc('month', event_ts)::DATE mo, count(*) cured
                   FROM v_curing WHERE statuscritical='Normal' GROUP BY 1,2)
        SELECT b.plant, b.mo, built, cured, built::DOUBLE/cured AS ratio
        FROM b JOIN c ON b.plant=c.plant AND b.mo=c.mo ORDER BY 1,2
    """).pl()
    for p in ["PCR", "TBR"]:
        s = bal.filter(pl.col("plant") == p)
        r = s["ratio"].to_list()
        print(f"  {p}: ratio by month " + " ".join(f"{x:.3f}" for x in r))
        print(f"       mean {st.mean(r):.4f}   -> "
              f"{'STEADY STATE' if abs(st.mean(r)-1) < 0.02 else 'DRIFTS'}")

    # ---------- B. daily inventory ----------------------------------------
    print("\n" + "=" * 78)
    print("B. DAILY GT INVENTORY  (plant-day 07:00, cumulative build - cure)")
    print("=" * 78)
    inv = con.execute(f"""
        WITH ev AS (
            SELECT plant, CAST(event_ts - INTERVAL {DAY_H} HOUR AS DATE) d, 1 q
            FROM v_build WHERE stage=2 AND QualityStatus='1'
            UNION ALL
            SELECT plant, CAST(event_ts - INTERVAL {DAY_H} HOUR AS DATE) d, -1 q
            FROM v_curing WHERE statuscritical='Normal'
        )
        SELECT plant, d, sum(q) net FROM ev GROUP BY 1,2 ORDER BY 1,2
    """).pl()
    inv = inv.with_columns(pl.col("net").cum_sum().over("plant").alias("wip"))
    for p in ["PCR", "TBR"]:
        s = inv.filter(pl.col("plant") == p)
        w = s["wip"].to_list()
        # de-trend: the absolute level depends on the unknown pre-Dec balance
        d = [w[i] - w[i - 1] for i in range(1, len(w))]
        print(f"  {p}: daily CHANGE  mean {st.mean(d):+.0f}  sd {st.pstdev(d):.0f}  "
              f"min {min(d):+,}  max {max(d):+,}")
        print(f"       cumulative drift over {len(w)} days = {w[-1]-w[0]:+,} tyres "
              f"({(w[-1]-w[0])/len(w):+.0f}/day)")
        print(f"       oscillation band (sd of level about its own trend) = "
              f"{st.pstdev([w[i] - (w[0] + (w[-1]-w[0])*i/len(w)) for i in range(len(w))]):.0f}")

    # ---------- C/D. which GTs are held -----------------------------------
    print("\n" + "=" * 78)
    print("C. WHICH GTs ARE HELD AS OPENING STOCK, AND HOW MUCH")
    print("=" * 78)
    rows = con.execute("""
        SELECT mo, plant, gt, count(*) cured,
               count(DISTINCT CAST(c_ts AS DATE)) cure_days,
               quantile_cont(lag_h, 0.5) lag_p50,
               quantile_cont(lag_h, 0.95) lag_p95
        FROM pairs GROUP BY 1,2,3
    """).pl()

    months = sorted(rows["mo"].unique().to_list())
    allrec = []
    for mo in months[1:]:
        as_of = f"{mo.year}-{mo.month:02d}-01 07:00:00"
        op = con.execute("""
            WITH built AS (SELECT plant, itemCode gt, productionID pid
                           FROM v_build WHERE stage=2 AND QualityStatus='1'
                             AND event_ts < ?::TIMESTAMP AND itemCode IS NOT NULL),
                 cured AS (SELECT gtbarCode pid FROM v_curing
                           WHERE event_ts < ?::TIMESTAMP AND statuscritical='Normal')
            SELECT b.plant, b.gt, count(*) opening FROM built b
            LEFT JOIN cured c ON b.pid=c.pid WHERE c.pid IS NULL GROUP BY 1,2
        """, [as_of, as_of]).pl()
        # bound by the p99 lag, as the ledger does
        m = rows.filter(pl.col("mo") == mo).select(
            ["plant", "gt", "cured", "cure_days", "lag_p50"])
        j = m.join(op, on=["plant", "gt"], how="left").with_columns(
            pl.col("opening").fill_null(0),
            pl.lit(str(mo)[:7]).alias("month"))
        j = j.with_columns(
            (pl.col("cured") / pl.col("cure_days")).alias("draw_per_day"))
        j = j.with_columns(
            (24.0 * pl.col("opening") / pl.col("draw_per_day")).alias("cover_h"))
        allrec.append(j)
    rec = pl.concat(allrec)

    for p in ["PCR", "TBR"]:
        s = rec.filter((pl.col("plant") == p) & (pl.col("opening") > 0))
        z = rec.filter((pl.col("plant") == p) & (pl.col("opening") == 0))
        print(f"\n  {p}:  {s.height} (month,GT) rows HOLD stock, "
              f"{z.height} hold NONE")
        print(f"     GTs holding stock: monthly cured p50 = {s['cured'].median():,.0f}"
              f"   |  GTs holding none: p50 = {z['cured'].median():,.0f}")
        print(f"     cover (hours of own draw): p25 {s['cover_h'].quantile(.25):.1f}h  "
              f"p50 {s['cover_h'].median():.1f}h  p75 {s['cover_h'].quantile(.75):.1f}h")
        print(f"     opening qty:  p50 {s['opening'].median():,.0f}  "
              f"max {s['opening'].max():,.0f}")
        # E. what does stock scale with?
        x = s["draw_per_day"].to_list()
        y = s["opening"].to_list()
        if len(x) > 5:
            mx, my = st.mean(x), st.mean(y)
            cov = sum((a-mx)*(b-my) for a, b in zip(x, y))
            vx = sum((a-mx)**2 for a in x)
            slope = cov/vx if vx else 0
            r = cov / ((vx**0.5) * (sum((b-my)**2 for b in y)**0.5) or 1)
            print(f"     REGRESSION opening = {slope:.3f} x draw_per_day"
                  f"   (r = {r:.3f})   => cover = {24*slope:.1f} h")

    # ---------- D. concentration ------------------------------------------
    print("\n" + "=" * 78)
    print("D. CONCENTRATION  (is stock spread or in a few GTs?)")
    print("=" * 78)
    for p in ["PCR", "TBR"]:
        for mo in ["2026-03", "2026-07"]:
            s = (rec.filter((pl.col("plant") == p) & (pl.col("month") == mo)
                            & (pl.col("opening") > 0))
                 .sort("opening", descending=True))
            if s.height == 0:
                continue
            tot = s["opening"].sum()
            top = s.head(10)["opening"].sum()
            print(f"  {p} {mo}: {s.height} GTs hold {tot:,}; "
                  f"top-10 = {top:,} ({100*top/tot:.0f}%)")

    # ---------- E. lag ------------------------------------------------------
    print("\n" + "=" * 78)
    print("E. BUILD -> CURE LAG (what the stock is actually FOR)")
    print("=" * 78)
    lag = con.execute("""
        SELECT plant, quantile_cont(lag_h,0.5) p50, quantile_cont(lag_h,0.9) p90,
               quantile_cont(lag_h,0.95) p95, quantile_cont(lag_h,0.99) p99,
               avg(CASE WHEN lag_h<=8 THEN 1.0 ELSE 0 END) within_shift,
               avg(CASE WHEN lag_h<=24 THEN 1.0 ELSE 0 END) within_day
        FROM pairs GROUP BY 1
    """).pl()
    print(lag)
    log.info("analyse.done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
