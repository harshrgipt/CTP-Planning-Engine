"""Study every month before changing any master. Measure, do not assume.

    python scripts/study_months.py

For each of the 8 months, independently:
  A. how many presses / machines were actually active
  B. real throughput per press-day and per machine-day
  C. would the allowable matrices have covered that month's real pairs?
  D. how many (GT, resource) pairs were NEW that month
  E. capacity model check: eff_CT vs actual

The point is to find out whether a master is WRONG or merely NARROW, and
whether its error is stable or drifts. Those need different fixes.
"""
from __future__ import annotations

import sys

import polars as pl

from planner.config import CONFIG
from planner.data.warehouse import duck, set_cutoff
from planner.runs.logger import log

DER = CONFIG.paths.warehouse / "derived"


def main() -> int:
    set_cutoff(None)
    con = duck()
    con.execute("""
        CREATE OR REPLACE TEMP TABLE pr AS
        SELECT b.plant, b.itemCode gt, b.machineCode machine,
               c.wcID::VARCHAR press,
               date_trunc('month', c.event_ts)::DATE mo,
               CAST(c.event_ts AS DATE) cd, CAST(b.event_ts AS DATE) bd
        FROM v_build b JOIN v_curing c ON b.productionID = c.gtbarCode
        WHERE b.stage=2 AND b.QualityStatus='1' AND c.statuscritical='Normal'
          AND b.itemCode IS NOT NULL
    """)

    print("=" * 92)
    print("A/B. ACTIVE RESOURCES AND REAL THROUGHPUT, PER MONTH")
    print("=" * 92)
    d = con.execute("""
        SELECT mo, plant,
               count(DISTINCT press) presses, count(DISTINCT machine) machines,
               count(DISTINCT gt) gts, count(*) tyres,
               count(DISTINCT cd) AS n_days
        FROM pr GROUP BY 1,2 ORDER BY 1,2
    """).pl()
    d = d.with_columns(
        (pl.col("tyres") / pl.col("n_days") / pl.col("presses")).round(1).alias("per_press_day"),
        (pl.col("tyres") / pl.col("n_days") / pl.col("machines")).round(0).alias("per_mach_day"))
    for r in d.iter_rows(named=True):
        print(f"  {str(r['mo'])[:7]} {r['plant']:4s} presses={r['presses']:3d} "
              f"machines={r['machines']:2d} gts={r['gts']:3d} "
              f"tyres={r['tyres']:7,d} /press-day={r['per_press_day']:6.1f} "
              f"/machine-day={r['per_mach_day']:6.0f}")
    for p in ("PCR", "TBR"):
        s = d.filter(pl.col("plant") == p)
        print(f"  --> {p}: presses {s['presses'].min()}-{s['presses'].max()}   "
              f"per-press-day {s['per_press_day'].min():.1f}-{s['per_press_day'].max():.1f} "
              f"(median {s['per_press_day'].median():.1f})")

    print("\n" + "=" * 92)
    print("C. WOULD THE ALLOWABLE MATRICES HAVE COVERED EACH MONTH?")
    print("=" * 92)
    ap = pl.read_parquet(DER / "allowed_press_matrix.parquet").select(
        ["plant", "gt_code", "press"]).unique()
    am = pl.read_parquet(DER / "allowed_machine_matrix.parquet").select(
        ["plant", "gt_code", "machine"]).unique()
    realp = con.execute(
        "SELECT DISTINCT mo, plant, gt, press FROM pr").pl()
    realm = con.execute(
        "SELECT DISTINCT mo, plant, gt, machine FROM pr").pl()
    for mo in sorted(realp["mo"].unique().to_list()):
        rp = realp.filter(pl.col("mo") == mo).rename({"gt": "gt_code"})
        rm = realm.filter(pl.col("mo") == mo).rename({"gt": "gt_code"})
        mp = rp.join(ap, on=["plant", "gt_code", "press"], how="anti")
        mm = rm.join(am, on=["plant", "gt_code", "machine"], how="anti")
        print(f"  {str(mo)[:7]}  press pairs used={rp.height:5d} "
              f"NOT in matrix={mp.height:4d} ({100*mp.height/rp.height:4.1f}%)   "
              f"machine pairs used={rm.height:4d} NOT in matrix={mm.height:4d} "
              f"({100*mm.height/rm.height:4.1f}%)")

    print("\n" + "=" * 92)
    print("D. NEW (GT, RESOURCE) PAIRS EACH MONTH  -- is history a feasibility set?")
    print("=" * 92)
    seen_p: set = set()
    seen_m: set = set()
    for mo in sorted(realp["mo"].unique().to_list()):
        rp = {(r["plant"], r["gt"], r["press"])
              for r in realp.filter(pl.col("mo") == mo).iter_rows(named=True)}
        rm = {(r["plant"], r["gt"], r["machine"])
              for r in realm.filter(pl.col("mo") == mo).iter_rows(named=True)}
        np_, nm_ = rp - seen_p, rm - seen_m
        if seen_p:
            print(f"  {str(mo)[:7]}  NEW press pairs {len(np_):4d}/{len(rp):5d} "
                  f"({100*len(np_)/len(rp):4.1f}%)   "
                  f"NEW machine pairs {len(nm_):4d}/{len(rm):4d} "
                  f"({100*len(nm_)/len(rm):4.1f}%)")
        seen_p |= rp
        seen_m |= rm

    print("\n" + "=" * 92)
    print("E. CAPACITY MODEL vs REALITY, PER MONTH")
    print("=" * 92)
    cap = con.execute("""
        WITH dw AS (
            SELECT plant, date_trunc('month', event_ts)::DATE mo,
                   quantile_cont(date_diff('second', event_ts, cycleStart)/60.0, 0.5) raw_min
            FROM v_curing WHERE statuscritical='Normal' AND cycleStart > event_ts
            GROUP BY 1,2),
        pday AS (
            SELECT plant, date_trunc('month', event_ts)::DATE mo,
                   wcID::VARCHAR p, CAST(event_ts AS DATE) d, count(*) n
            FROM v_curing WHERE statuscritical='Normal' GROUP BY 1,2,3,4),
        busy AS (
            SELECT plant, mo, quantile_cont(n, 0.95) busy_p95,
                   quantile_cont(n, 0.5) busy_p50
            FROM pday GROUP BY 1,2)
        SELECT dw.plant, dw.mo, raw_min, busy_p95, busy_p50 FROM dw JOIN busy USING (plant, mo)
        ORDER BY 2,1
    """).pl()
    cap = cap.with_columns(
        ((pl.col("raw_min") + 2.3) / 0.94).round(2).alias("eff_ct"))
    cap = cap.with_columns((480.0 / pl.col("eff_ct")).floor().alias("cyc_shift"))
    for r in cap.iter_rows(named=True):
        c3, c4 = 3 * r["cyc_shift"] * 3, 3 * r["cyc_shift"] * 4
        print(f"  {str(r['mo'])[:7]} {r['plant']:4s} dwell={r['raw_min']:5.1f}min "
              f"eff_CT={r['eff_ct']:5.1f} cyc/shift={r['cyc_shift']:3.0f} -> "
              f"cap/day: 3slots={c3:4.0f} 4slots={c4:4.0f}   "
              f"ACTIVE-DAY actual p50={r['busy_p50']:4.0f} p95={r['busy_p95']:4.0f}")
    print("\n  --> the honest comparison is against BUSY-DAY p95, not the monthly")
    print("      average: a press that is idle half the month drags the mean but")
    print("      says nothing about its capacity.")
    log.info("study.done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
