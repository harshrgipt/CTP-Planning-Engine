"""L3 -- TRUE BOTTLENECK & THROUGHPUT CEILING (RCCP).

    python -m planner.cmbc.l3_ceiling --month 2026-07

Publishes MAX_FEASIBLE per line per week. Every downstream plan is scored
against it, so this is the denominator the whole programme reports into.

    cure_ceiling  = SUM over presses ( cavities x 1440/cycle_min x availability )
    mould_ceiling = SUM over GTs     ( moulds x cavities x 1440/cycle_min x avail )
    build_ceiling = SUM over machines( 1440/cadence_min x avail / (1+co_frac) )
    MAX_FEASIBLE  = min(cure, mould, build, prep)

WHY THE MOULD CEILING IS SEPARATE FROM THE PRESS CEILING
  A press with no mould in it cures nothing. A GT with 2 moulds cannot occupy
  5 presses however many are free. Press count and mould count are different
  constraints and the smaller binds -- `runs/july_v4` violates this in 4 places
  (GT 2167 RAN HPE planned on 6 presses against 2 moulds), which the old engine
  had no way to see.

DO NOT APPLY AVAILABILITY ON TOP OF A DERIVED RATE
  Effective cavities are derived from OBSERVED tyres/day, which already contains
  every stoppage. Multiplying that by L0's availability removes downtime twice
  and produced a ceiling BELOW actual production (PCR 102.1%, TBR 111.4% "load"
  against a month the plant actually built). A ceiling under demonstrated output
  is self-refuting, which is how the error surfaced.

  The demonstrated-capacity ceiling is therefore the p90 press-day, times a full
  week: the plant has shown it can run at that rate, so it is achievable by
  construction and needs no availability haircut.

CAVITIES ARE DERIVED, AND SAID TO BE
  The mould master's `Full_Load` is 100% NULL, so tyres-per-cycle is not
  available from any master. It is derived here as

      cavities = tyres per press-day / (1440 / cycle_min)

  i.e. observed output divided by theoretical cycles. That is a MEASUREMENT of
  effective cavity count including any part-loading, not the physical cavity
  count, and the two are not interchangeable -- the physical number would give a
  higher, unachievable ceiling. Provenance is carried in the output.

AVAILABILITY IS L0's, NOT CALENDAR
  An earlier cut used calendar time and called the result a lower bound. With
  L0's measured MTBF/MTTR the ceiling is honest: PCR 0.8897, TBR 0.8282.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import polars as pl

from planner.cmbc import plant_ct

ROOT = Path(__file__).resolve().parent.parent.parent
D = ROOT / "warehouse" / "derived"
PARAMS = ROOT / "warehouse" / "params"
PLANTS = ["PCR", "TBR"]
WEEK_MIN = 7 * 24 * 60.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", default="2026-07")
    a = ap.parse_args()
    from planner.data.warehouse import duck, set_cutoff
    set_cutoff(None)

    pj = sorted(PARAMS.glob("params_*.json"))
    if not pj:
        raise SystemExit("no L0 parameter file -- run l0_learn first")
    P = json.loads(pj[-1].read_text())
    avail = {p: float(P["press_availability"][p]["availability"]) for p in PLANTS}

    print("=" * 92)
    print(f"L3  THROUGHPUT CEILING (RCCP)  --  {a.month}")
    print("=" * 92)
    print(f"  L0 params {pj[-1].name}   availability "
          f"PCR {avail['PCR']:.4f}  TBR {avail['TBR']:.4f}\n")

    # ---- derived cavities ----------------------------------------------
    cav = duck().execute("""
        WITH d AS (
          SELECT plant, CAST(wcID AS VARCHAR) press, date,
                 count(*) AS tyres,
                 median(date_diff('second', event_ts, cycleStart)) AS cyc_s
          FROM v_curing
          WHERE wcID IS NOT NULL AND cycleStart IS NOT NULL
            AND date_diff('second', event_ts, cycleStart) BETWEEN 60 AND 14400
          GROUP BY 1,2,3 HAVING count(*) >= 5)
        SELECT plant, press,
               count(*) AS days,
               median(tyres) AS tyres_per_day,
               quantile_cont(tyres, 0.90) AS tyres_per_day_p90,
               median(cyc_s) AS cycle_s
        FROM d GROUP BY 1,2""").pl()
    cav = cav.with_columns(
        (86400.0 / pl.col("cycle_s")).alias("cycles_per_day"))
    cav = cav.with_columns(
        (pl.col("tyres_per_day") / pl.col("cycles_per_day")).alias("cavities"))
    # The ceiling must use the presses that actually run this month, not every
    # press that has ever run. Planning against 92 PCR presses when the plant
    # operated 86 inflates capacity and makes any comparison with the plant
    # meaningless -- the same roster L2 restricts eligibility to.
    roster_f = ROOT / "masters" / f"press_list_{a.month}.json"
    if roster_f.exists():
        roster = json.loads(roster_f.read_text())
        keep = pl.DataFrame(
            [{"plant": p, "press": x} for p, v in roster.items() for x in v])
        n0 = {p: cav.filter(pl.col("plant") == p).height for p in PLANTS}
        cav = cav.join(keep, on=["plant", "press"], how="inner")
        print("  press roster applied: " + "  ".join(
            f"{p} {n0[p]}->{cav.filter(pl.col('plant') == p).height}"
            for p in PLANTS))
    cav.write_parquet(D / "l3_cavities.parquet")

    print("  derived cavities (effective, not physical -- Full_Load is NULL)")
    print(f"    {'plant':<6}{'presses':>9}{'cycle s':>10}{'cycles/day':>12}"
          f"{'tyres/day p50':>15}{'p90':>7}{'cavities p50':>14}")
    for p in PLANTS:
        s = cav.filter(pl.col("plant") == p)
        print(f"    {p:<6}{s.height:>9}{float(s['cycle_s'].median()):>10.0f}"
              f"{float(s['cycles_per_day'].median()):>12.1f}"
              f"{float(s['tyres_per_day'].median()):>15.0f}"
              f"{float(s['tyres_per_day_p90'].median()):>7.0f}"
              f"{float(s['cavities'].median()):>14.2f}")

    # ---- cure ceiling (press-limited) -----------------------------------
    rows = []
    for p in PLANTS:
        s = cav.filter(pl.col("plant") == p)
        # DEMONSTRATED capacity: p90 press-day x 7. No availability multiplier --
        # the observed day already contains downtime (see module docstring).
        per_press = s["tyres_per_day_p90"] * 7.0
        rows.append({"plant": p, "presses": s.height,
                     "cure_press_ceiling": float(per_press.sum())})
    cure = pl.DataFrame(rows)

    # ---- mould ceiling ---------------------------------------------------
    mo = pl.read_parquet(D / f"cap_mould_{a.month}.parquet")
    cyc = duck().execute("""
        SELECT b.plant, b.itemCode AS gt_code,
               median(date_diff('second', c.event_ts, c.cycleStart)) AS cycle_s
        FROM v_curing c JOIN v_build b ON c.gtbarCode = b.productionID
        WHERE b.stage=2 AND b.itemCode IS NOT NULL AND c.cycleStart IS NOT NULL
          AND date_diff('second', c.event_ts, c.cycleStart) BETWEEN 60 AND 14400
        GROUP BY 1,2""").pl()
    cav_p = {p: float(cav.filter(pl.col("plant") == p)["cavities"].median())
             for p in PLANTS}
    m = (mo.join(cyc, on=["plant", "gt_code"], how="left")
         .with_columns(pl.col("cycle_s").fill_null(
             pl.col("plant").replace_strict(
                 {p: float(cav.filter(pl.col("plant") == p)["cycle_s"].median())
                  for p in PLANTS}))))
    # PLANT CURE TIMES for the mould ceiling.
    # `cycle_s` here is `cycleStart - event_ts`, which is NOT the press cycle:
    # measured on July 2026 the true press cycle (inter-load gap, loads clustered
    # at 120 s) is p50 17.1 min on PCR and 59.6 on TBR, while this dwell reads
    # 30.1 and 85.1. The derived `cav_p` (3.40 / 2.41) then absorbs the error, so
    # the two are wrong in opposite directions and the product came out roughly
    # right. The plant's own file plus the MEASURED 2-cavity load (92.5 % / 92.3 %
    # of loads are exactly 2 tyres) replaces both with quantities that are
    # individually true. Fallback keeps the old pair.
    _PCT = plant_ct.get()
    if _PCT.ok:
        _pc = {(r["plant"], r["gt_code"]): _PCT.press_cycle_min(r["plant"], r["gt_code"])
               for r in m.select(["plant", "gt_code"]).unique().iter_rows(named=True)}
        m = m.with_columns([
            pl.struct(["plant", "gt_code"]).map_elements(
                lambda x: _pc.get((x["plant"], x["gt_code"])),
                return_dtype=pl.Float64).alias("_plant_cyc_min")])
        m = m.with_columns([
            pl.when(pl.col("_plant_cyc_min").is_not_null())
              .then(pl.lit(plant_ct.CAVITIES))
              .otherwise(pl.col("plant").replace_strict(cav_p)).alias("cav"),
            pl.when(pl.col("_plant_cyc_min").is_not_null())
              .then(WEEK_MIN / pl.col("_plant_cyc_min"))
              .otherwise(WEEK_MIN / (pl.col("cycle_s") / 60.0)).alias("cycles_wk")])
    else:
        m = m.with_columns(
            pl.col("plant").replace_strict(cav_p).alias("cav"),
            (WEEK_MIN / (pl.col("cycle_s") / 60.0)).alias("cycles_wk"))
    # a GT can occupy at most `moulds` presses at once
    m = m.with_columns(
        (pl.col("moulds") * pl.col("cav") * pl.col("cycles_wk")
         * pl.col("plant").replace_strict(avail)).alias("gt_ceiling"))
    # mould ceiling IS theoretical (moulds x cavities x cycles), so availability
    # belongs here -- unlike the press ceiling above, nothing observed feeds it.
    mould_ceil = m.group_by("plant").agg(
        pl.col("gt_ceiling").sum().alias("mould_ceiling"),
        pl.col("moulds").sum().alias("total_moulds"))

    # ---- build ceiling ---------------------------------------------------
    cad = pl.read_parquet(PARAMS / P["tables"]["build_cadence"])
    co = pl.read_parquet(D / "cap_changeover.parquet")
    cie = pl.read_parquet(D / f"cie_proposals_{a.month}.parquet")
    # DEMONSTRATED build capacity, same basis as cure: p90 machine-day x 7.
    # The earlier version multiplied by `avail`, which is PRESS availability
    # measured from cure gaps -- a different resource entirely. Applied to build
    # machines it pushed the TBR ceiling (21,340/wk) BELOW actual output
    # (22,132/wk). Observed machine-days already contain build downtime and
    # changeover, so neither an availability factor nor a (1+co_frac) divisor
    # belongs on top.
    bd = duck().execute("""
        WITH d AS (SELECT plant, machineCode m, date, count(*) AS tyres
                   FROM v_build WHERE stage=2 AND machineCode IS NOT NULL
                   GROUP BY 1,2,3 HAVING count(*) >= 5)
        SELECT plant, m, quantile_cont(tyres, 0.90) AS p90
        FROM d GROUP BY 1,2""").pl()
    brows = []
    for p in PLANTS:
        s = cad.filter(pl.col("plant") == p)
        n_mach = s.height
        # Plant building CT where available -- the demand-weighted mean over
        # the GTs this plant actually runs, not the mined per-machine median.
        cad_s = float(s["cadence_s_p50"].median())
        if _PCT.ok:
            _bs = [v for (q, _g, _mk), v in _PCT._b.items() if q == p]
            if _bs:
                cad_s = float(sorted(_bs)[len(_bs) // 2])
        camps = float(cie.filter(pl.col("plant") == p)["n_campaigns"].sum())
        setup = float(co.filter(co["plant"] == p)["same_min"].mean())
        weeks = 744 / 168.0
        co_frac = (camps * setup / max(weeks, 1)) / max(n_mach * WEEK_MIN, 1)
        dem_cap = float(bd.filter(pl.col("plant") == p)["p90"].sum()) * 7.0
        theo = n_mach * (WEEK_MIN / (cad_s / 60.0))
        brows.append({"plant": p, "machines": n_mach, "cadence_s": cad_s,
                      "co_frac": co_frac, "theoretical": theo,
                      "build_ceiling": dem_cap})
    build = pl.DataFrame(brows)

    # ---- combine ---------------------------------------------------------
    x = cure.join(mould_ceil, on="plant").join(build, on="plant")
    x = x.with_columns(
        pl.min_horizontal("cure_press_ceiling", "mould_ceiling", "build_ceiling")
        .alias("max_feasible"))
    x = x.with_columns(
        pl.when(pl.col("max_feasible") == pl.col("build_ceiling")).then(pl.lit("BUILD"))
        .when(pl.col("max_feasible") == pl.col("mould_ceiling")).then(pl.lit("MOULD"))
        .otherwise(pl.lit("CURE")).alias("binding"))

    net = D / f"net_requirement_{a.month}.parquet"
    req = pl.read_parquet(net) if net.exists() else None

    print("\n  CEILINGS  (tyres per week)")
    print(f"    {'plant':<6}{'cure/press':>13}{'mould':>12}{'build':>12}"
          f"{'MAX_FEASIBLE':>15}{'binding':>9}")
    for r in x.iter_rows(named=True):
        print(f"    {r['plant']:<6}{r['cure_press_ceiling']:>13,.0f}"
              f"{r['mould_ceiling']:>12,.0f}{r['build_ceiling']:>12,.0f}"
              f"{r['max_feasible']:>15,.0f}{r['binding']:>9}")
    print(f"\n    build changeover fraction: "
          + "  ".join(f"{r['plant']} {100*r['co_frac']:.1f}%"
                      for r in build.iter_rows(named=True)))

    if req is not None:
        print("\n  DEMAND vs CEILING  (month)")
        print(f"    {'plant':<6}{'gross build':>13}{'max feasible/mo':>17}"
              f"{'load':>8}{'verdict':>12}")
        for r in x.iter_rows(named=True):
            need = float(req.filter(pl.col("plant") == r["plant"])["gross_build"].sum())
            mo_cap = r["max_feasible"] * 744 / 168.0
            load = need / max(mo_cap, 1)
            v = "OK" if load <= 0.95 else "TIGHT" if load <= 1.0 else "INFEASIBLE"
            print(f"    {r['plant']:<6}{need:>13,.0f}{mo_cap:>17,.0f}"
                  f"{100*load:>7.1f}%{v:>12}")

    x.write_parquet(D / f"l3_ceiling_{a.month}.parquet")
    print(f"\n  -> l3_ceiling_{a.month}.parquet · l3_cavities.parquet")


if __name__ == "__main__":
    main()
