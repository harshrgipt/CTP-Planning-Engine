"""INDEPENDENT verifier for an exported schedule pack.

    python scripts/verify_export.py output/2026_07_schedule 2026-07

Reads ONLY the exported CSVs and re-derives every check from scratch. It
deliberately imports nothing from `planner/` -- same principle as
`planner/validate/violations.py`: a verifier that calls planner internals only
proves the planner agrees with itself. Everything upstream has validated the
PLAN; this validates the FILE that reaches the shop floor.

Severities
  HARD   physically impossible -- the plan cannot be run as printed
  SOFT   breaks a business rule but is executable
  EXPORT the file misrepresents the plan (dropped rows, bad sums, bad labels)
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import polars as pl

SHELF_H = 72.0
SHIFT_START_H = 7
F = []          # findings: (severity, check, detail)


def add(sev, check, detail):
    F.append({"severity": sev, "check": check, "detail": detail})


def overlaps(df, key, s="start_ts", e="end_ts", tol_s=1.0):
    """Pairs on the same resource whose intervals overlap by more than tol."""
    bad = []
    for k, g in df.group_by(key):
        g = g.sort(s)
        st = g[s].to_list()
        en = g[e].to_list()
        for i in range(1, len(st)):
            prev_end = max(en[:i])
            if (prev_end - st[i]).total_seconds() > tol_s:
                bad.append((k, str(st[i]), str(prev_end)))
    return bad


def main() -> int:
    out = Path(sys.argv[1])
    month = sys.argv[2]
    y, m = int(month[:4]), int(month[5:7])
    t0 = datetime(y, m, 1, SHIFT_START_H)
    # THE HORIZON IS THE PLANT MONTH, NOT THE CALENDAR MONTH. The plant day runs
    # 07:00 -> 07:00, so day 31 ends at 07:00 on the 1st of the next month.
    # Testing against the calendar boundary flagged the whole of day 31's C shift
    # as "outside the month" while `carry_out` (correctly, plant-day based) said
    # it was inside -- two different definitions disagreeing about the same rows.
    ndays = (datetime(y + (m == 12), (m % 12) + 1, 1) - datetime(y, m, 1)).days
    hzn_lo = t0
    hzn_hi = t0 + timedelta(days=ndays)
    H = ndays * 24
    C = out / "csv"

    b = pl.read_csv(C / "1_build_schedule_shift.csv", try_parse_dates=True)
    c = pl.read_csv(C / "2_cure_schedule_shift.csv", try_parse_dates=True)
    mach = pl.read_csv(C / "5_machine_summary.csv")
    press = pl.read_csv(C / "6_press_summary.csv")
    daily = pl.read_csv(C / "7_daily_summary.csv")
    dvp = pl.read_csv(C / "8_demand_vs_plan.csv")
    kpi = pl.read_csv(C / "9a_kpi_summary.csv")
    inv = pl.read_csv(C / "9b_l11_invariants.csv")

    print("=" * 78)
    print(f"INDEPENDENT VERIFICATION  --  {out}  ({month})")
    print("=" * 78)
    print(f"  build rows {b.height:,} · cure rows {c.height:,} · "
          f"machines {mach.height} · presses {press.height}")

    # ---------- QUANTITIES -------------------------------------------------
    for name, d in (("build", b), ("cure", c)):
        q = d["qty"]
        if q.null_count():
            add("EXPORT", f"{name}.qty null", f"{q.null_count()} null quantities")
        neg = d.filter(pl.col("qty") < 0)
        if neg.height:
            add("HARD", f"{name}.qty < 0", f"{neg.height} rows")
        # A zero on the CURE sheet is not an empty row. Sheet 2 is one row per
        # (plant-day, shift, press, GT) and `qty` is the tyres HANDED to that
        # press in that shift -- a delivery event. A press working through a
        # delivery it received two shifts ago holds real press hours and takes no
        # new tyres, so qty 0 with hours > 0 is the normal state of a running
        # press: 8,992 of 14,682 July rows. What would be a defect is a row that
        # occupies no press time and moves no tyres, so that is what is checked.
        z = d.filter((pl.col("qty") == 0) &
                     (pl.col("hours").fill_null(0) <= 0
                      if "hours" in d.columns else pl.lit(True)))
        if z.height:
            add("EXPORT", f"{name}.qty == 0 with no press time",
                f"{z.height} rows carry neither tyres nor hours")
        frac = d.filter((pl.col("qty") % 1) != 0)
        if frac.height:
            add("HARD", f"{name}.qty fractional",
                f"{frac.height} rows -- a fractional tyre corrupts the ledger "
                f"(MEMORY §12); e.g. {frac.head(1).to_dicts()}")

    # ---------- TIME WINDOW + SHIFT LABEL ----------------------------------
    # Rows past month end are LEGITIMATE under PLANNER_CARRY_OUT (a campaign may
    # start inside the horizon and finish after). They are only a defect if the
    # export does not FLAG them, which is what a supervisor actually needs.
    # THE MONTH IS A CLOSED BOX (plant ruling, 2026-08-09): "only demand which is
    # filled within the month is considered fulfilled -- after that, discard".
    # No exported row may start OR end outside the plant month. This is HARD so
    # the ruling cannot silently regress if PLANNER_HORIZON_MODE is changed.
    for name, d in (("build", b), ("cure", c)):
        late_s = d.filter((pl.col("start_ts") < hzn_lo) | (pl.col("start_ts") >= hzn_hi))
        late_e = d.filter(pl.col("end_ts") > hzn_hi)
        if late_s.height or late_e.height:
            ex = (late_e if late_e.height else late_s).head(1).to_dicts()[0]
            add("HARD", f"{name} row outside the plant month",
                f"{late_s.height} start outside and {late_e.height} end after "
                f"{hzn_hi:%Y-%m-%d %H:%M}; e.g. {ex.get('gt_code')} "
                f"{ex.get('start_ts')} -> {ex.get('end_ts')}")
        else:
            print(f"  {name}: all {d.height:,} rows inside the plant month "
                  f"({hzn_lo:%m-%d %H:%M} -> {hzn_hi:%m-%d %H:%M})  OK")
    exp = ((pl.col("start_ts") - pl.lit(t0)).dt.total_seconds() / 3600.0 % 24) // 8
    lab = (b.with_columns(exp.alias("_e"))
           .with_columns(pl.when(pl.col("_e") == 0).then(pl.lit("A"))
                         .when(pl.col("_e") == 1).then(pl.lit("B"))
                         .otherwise(pl.lit("C")).alias("_lab")))
    bad = lab.filter(pl.col("shift") != pl.col("_lab"))
    if bad.height:
        add("EXPORT", "shift label mismatch",
            f"{bad.height} build rows labelled with the wrong shift")

    # ---------- MACHINE / PRESS DOUBLE-BOOKING -----------------------------
    ov = overlaps(b, "machine")
    if ov:
        add("HARD", "machine double-booked",
            f"{len(ov)} overlapping runs, e.g. {ov[0]}")
    ovp = overlaps(c, "press")
    if ovp:
        add("HARD", "press double-booked",
            f"{len(ovp)} overlapping campaigns, e.g. {ovp[0]}")

    # ---------- MOULD DOUBLE-BOOKING ---------------------------------------
    # Moulds are per (plant, gt_code, press) -- labelled <mould>@<press>.
    # Collapsing to one mould per GT once produced 416 K phantom violations.
    cm = c.with_columns(
        (pl.col("mould_set").cast(pl.Utf8) + "@" + pl.col("press").cast(pl.Utf8))
        .alias("mould_at_press"))
    ovm = overlaps(cm, "mould_at_press")
    if ovm:
        add("HARD", "mould double-booked",
            f"{len(ovm)} overlaps on <mould>@<press>, e.g. {ovm[0]}")

    # ---------- CAPACITY ----------------------------------------------------
    # CLIP TO THE HORIZON. This check used to sum raw (end - start), so a cure
    # campaign that STARTS on day 25 and finishes in the next month contributed
    # its whole duration and the resource read as over-capacity. That produced a
    # standing "12 presses exceed 744 h" HARD finding which was entirely a
    # false positive -- and a permanent false HARD hides the real ones.
    # PLANNER_CARRY_OUT makes cross-boundary campaigns legitimate; only the hours
    # that fall INSIDE the month may be charged against the month's capacity.
    for nm, d, key in (("machine", b, "machine"), ("press", c, "press")):
        busy = (d.with_columns(
            ((pl.min_horizontal(pl.col("end_ts"), pl.lit(hzn_hi))
              - pl.max_horizontal(pl.col("start_ts"), pl.lit(hzn_lo)))
             .dt.total_seconds() / 3600.0).clip(lower_bound=0).alias("h"))
            .group_by(key).agg(pl.col("h").sum().alias("h")))
        over = busy.filter(pl.col("h") > H + 1e-6)
        if over.height:
            add("HARD", f"{nm} over {H} h (in-month hours)",
                f"{over.height} {nm}s exceed the month, max "
                f"{over['h'].max():.1f} h")
        print(f"  {nm} in-month hours: max {busy['h'].max():.1f} of {H} h")

    # ---------- SETUP RESERVATION + MACHINE-DAY FEASIBILITY ----------------
    # AN OVERLAP CHECK IS NOT A FEASIBILITY CHECK. Two runs of different GTs can
    # sit back-to-back with a ZERO gap and never overlap -- which is exactly the
    # defect this verifier passed for the whole project (July: 350 of 856 PCR
    # transitions had a zero gap; 55 PCR machine-days needed more than 24 h).
    # The resource must be RESERVED, not merely not-collided-with.
    #
    # Changeover minutes come from the PLANT MASTER (cap_changeover.parquet,
    # loaded from Master_Building_ChangeoverTime_*.csv), not from the planner --
    # this stays independent of the code that built the schedule.
    co_f = (Path(__file__).resolve().parent.parent / "warehouse" / "derived"
            / "cap_changeover.parquet")
    if not co_f.exists():
        add("EXPORT", "setup check skipped", f"{co_f.name} missing")
    else:
        _co = pl.read_parquet(co_f)
        SAME = {r["machine"]: float(r["same_min"]) for r in _co.iter_rows(named=True)}
        DIFF = {r["machine"]: float(r["diff_min"]) for r in _co.iter_rows(named=True)}
        short, short_h, ntrans = [], 0.0, 0
        setup_by_md: dict[tuple, float] = {}
        for (mk,), g in b.group_by(["machine"]):
            g = g.sort("start_ts")
            gt = g["gt_code"].to_list(); rm = g["rim"].to_list()
            s = g["start_ts"].to_list(); e = g["end_ts"].to_list()
            pd_ = g["plant_day"].to_list()
            for i in range(1, len(gt)):
                if gt[i] == gt[i - 1]:
                    continue
                ntrans += 1
                need = SAME[mk] if rm[i] == rm[i - 1] else DIFF[mk]
                # The changeover occupies [prev_end, prev_end + need]. CLIP IT
                # INTO THE DAYS IT ACTUALLY SPANS -- charging the whole setup to
                # the day the NEXT run starts moves work across the 07:00
                # boundary and manufactures a false >24 h day, the same error as
                # bucketing a straddling run by its start day.
                _ss = e[i - 1]
                _se = _ss + timedelta(minutes=need)
                _d0 = int((_ss - t0).total_seconds() // 86400)
                _d1 = int(((_se - t0).total_seconds() - 1e-9) // 86400)
                for _dd in range(_d0, _d1 + 1):
                    _lo = max(_ss, t0 + timedelta(days=_dd))
                    _hi = min(_se, t0 + timedelta(days=_dd + 1))
                    _mn = (_hi - _lo).total_seconds() / 60.0
                    if _mn > 0:
                        k2 = (mk, _dd + 1)
                        setup_by_md[k2] = setup_by_md.get(k2, 0.0) + _mn
                gap = (s[i] - e[i - 1]).total_seconds() / 60.0
                if gap + 1e-6 < need:
                    short.append((mk, str(s[i]), round(gap, 1), need))
                    short_h += (need - gap) / 60.0
        if short:
            add("HARD", "changeover time not reserved",
                f"{len(short)} of {ntrans} GT transitions start before the "
                f"machine could be ready ({short_h:.1f} h short), e.g. "
                f"{short[0]} (gap min, needs min)")
        else:
            print(f"  setup reserved on all {ntrans:,} GT transitions  OK")
        # MACHINE-DAY: production + setup must fit 24 h.
        # HOURS ARE CLIPPED INTO THE DAY THEY ACTUALLY FALL IN. A run legitimately
        # straddles the 07:00 boundary, so bucketing its WHOLE duration into its
        # START day counts hours that are physically spent the next day and
        # manufactures a false >24 h finding -- the same clipping error as the
        # press-capacity check above. Attribute each slice to every day it
        # touches, in proportion to the time it actually spends there.
        md: dict[tuple, float] = {}
        for r in b.iter_rows(named=True):
            s, e = r["start_ts"], r["end_ts"]
            d0 = int((s - t0).total_seconds() // 86400)
            d1 = int(((e - t0).total_seconds() - 1e-9) // 86400)
            for dd in range(d0, d1 + 1):
                lo = max(s, t0 + timedelta(days=dd))
                hi = min(e, t0 + timedelta(days=dd + 1))
                h = (hi - lo).total_seconds() / 3600.0
                if h > 0:
                    k = (r["plant"], r["machine"], dd + 1)
                    md[k] = md.get(k, 0.0) + h
        rows_md = []
        for (pp, mk, dd), ph in md.items():
            sh = setup_by_md.get((mk, dd), 0.0) / 60.0
            rows_md.append({"plant": pp, "machine": mk, "plant_day": dd,
                            "prod_h": ph, "setup_h": sh, "total_h": ph + sh})
        prod = pl.DataFrame(rows_md)
        bad_md = prod.filter(pl.col("total_h") > 24.0 + 1e-6)
        if bad_md.height:
            w = bad_md.sort("total_h", descending=True).head(1).to_dicts()[0]
            add("HARD", "machine-day over 24 h (production + setup)",
                f"{bad_md.height} of {prod.height} machine-days need more than "
                f"24 h; worst {w['machine']} day {w['plant_day']} = "
                f"{w['total_h']:.2f} h (prod {w['prod_h']:.2f} + setup {w['setup_h']:.2f})")
        else:
            print(f"  all {prod.height:,} machine-days fit 24 h incl. setup  OK "
                  f"(max {prod['total_h'].max():.2f} h)")

    # ---------- BUILD BEFORE CURE (per-tyre GT balance) --------------------
    # Every cured tyre needs a built tyre at or before its cure time. Build the
    # signed event stream per (plant, gt_code) and look for a negative balance.
    ev = pl.concat([
        b.select(["plant", "gt_code", pl.col("end_ts").alias("ts"),
                  pl.col("qty").alias("d")]),
        b.select(["plant", "gt_code", pl.col("cure_ts").alias("ts"),
                  (-pl.col("qty")).alias("d")]),
    ]).sort("ts")
    worst = {}
    for (p, g), grp in ev.group_by(["plant", "gt_code"]):
        bal = np.cumsum(grp.sort("ts")["d"].to_numpy())
        if bal.min() < -1e-9:
            worst[(p, g)] = float(bal.min())
    if worst:
        k = min(worst, key=worst.get)
        add("HARD", "negative GT balance (cure before build)",
            f"{len(worst)} (plant,GT) streams go negative, worst {worst[k]:.0f} "
            f"on {k}")

    # ---------- R5 SHELF LIFE ----------------------------------------------
    w = (b.with_columns(((pl.col("cure_ts") - pl.col("end_ts"))
                         .dt.total_seconds() / 3600.0).alias("w")))
    br = w.filter(pl.col("w") > SHELF_H)
    if br.height:
        add("HARD", f"R5 shelf life > {SHELF_H} h",
            f"{br.height} rows, max {br['w'].max():.1f} h")
    neg_w = w.filter(pl.col("w") < 0)
    if neg_w.height:
        add("HARD", "cure before build (negative wait)", f"{neg_w.height} rows")
    for p in b["plant"].unique().sort():
        wp = w.filter(pl.col("plant") == p)["w"]
        print(f"  R5 re-derived {p}: max {wp.max():.1f} h  p95 {np.percentile(wp,95):.1f} h")

    # ---------- B16 TT/TL ---------------------------------------------------
    tf = Path(__file__).resolve().parent.parent.parent.parent / "INPUT" / "derived" / "tt_tl.parquet"
    if tf.exists():
        tt = pl.read_parquet(tf)
        dem_map = {}
        try:
            d8 = pl.read_parquet(Path(__file__).resolve().parent.parent /
                                 "masters" / "demand" / f"demand_{month}.parquet")
            tmap = tt.filter(pl.col("sku") != "").select(["sku", "tt_tl"]).unique(subset=["sku"])
            for r in d8.join(tmap, on="sku", how="left").iter_rows(named=True):
                if r["tt_tl"] and r["plant"] == "TBR":
                    dem_map[r["gt_code"]] = r["tt_tl"]
        except Exception as exc:
            add("EXPORT", "B16 check skipped", str(exc))
        if dem_map:
            tb = b.filter(pl.col("plant") == "TBR").with_columns(
                pl.col("gt_code").replace_strict(dem_map, default=None).alias("tag"))
            mixed = (tb.filter(pl.col("tag").is_not_null())
                     .group_by("machine").agg(pl.col("tag").n_unique().alias("n")))
            bad_m = mixed.filter(pl.col("n") > 1)
            if bad_m.height:
                add("SOFT", "B16 TT/TL mixed on a machine",
                    f"{bad_m.height} TBR machines carry both TT and TL: "
                    f"{bad_m['machine'].to_list()}")
            else:
                print(f"  B16: no TBR machine mixes TT and TL  OK "
                      f"({tb.filter(pl.col('tag').is_not_null()).height:,} tagged rows)")

    # ---------- INTERNAL RECONCILIATION ------------------------------------
    bq = b.group_by("plant").agg(pl.col("qty").sum().alias("sheet1"))
    dq = daily.group_by("plant").agg(pl.col("built").sum().alias("sheet7"))
    mq = mach.group_by("plant").agg(pl.col("tyres").sum().alias("sheet5"))
    rec = bq.join(dq, on="plant").join(mq, on="plant")
    print("\n  RECONCILIATION (build side)")
    for r in rec.iter_rows(named=True):
        print(f"    {r['plant']}: sheet1 {r['sheet1']:>10,.0f} | sheet7 "
              f"{r['sheet7']:>10,.0f} | sheet5 {r['sheet5']:>10,.0f}")
        if abs(r["sheet1"] - r["sheet5"]) > 0.5:
            add("EXPORT", "sheet1 vs sheet5 mismatch",
                f"{r['plant']}: {r['sheet1']:,.0f} vs {r['sheet5']:,.0f}")
        if abs(r["sheet1"] - r["sheet7"]) > 0.5:
            add("EXPORT", "sheet1 vs sheet7 mismatch",
                f"{r['plant']}: {r['sheet1']:,.0f} vs {r['sheet7']:,.0f}")
    # cure side
    cq = c.group_by("plant").agg(pl.col("qty").sum().alias("cure_sheet2"))
    cd = daily.group_by("plant").agg(pl.col("cured").sum().alias("cure_sheet7"))
    pq = press.group_by("plant").agg(pl.col("tyres").sum().alias("cure_sheet6"))
    rec2 = cq.join(cd, on="plant").join(pq, on="plant")
    print("  RECONCILIATION (cure side)")
    for r in rec2.iter_rows(named=True):
        print(f"    {r['plant']}: sheet2 {r['cure_sheet2']:>10,.0f} | sheet7 "
              f"{r['cure_sheet7']:>10,.0f} | sheet6 {r['cure_sheet6']:>10,.0f}")
        if abs(r["cure_sheet2"] - r["cure_sheet6"]) > 0.5:
            add("EXPORT", "sheet2 vs sheet6 mismatch", f"{r['plant']}")
    # headline
    # The KPI row is labelled "tyres fed (incl. opening stock)"; an exact-match
    # lookup on "tyres fed" silently returned an EMPTY dict, so this whole
    # reconciliation was comparing against 0 and reporting a phantom mismatch.
    fed = {r["plant"]: r["value"] for r in kpi.filter(
        pl.col("metric").str.starts_with("tyres fed")).iter_rows(named=True)}
    if not fed:
        add("EXPORT", "headline 'tyres fed' row not found", "KPI label changed?")
    # `fed` INCLUDES OPENING STOCK, which is by definition not built this month
    # and so is absent from sheet 1. Comparing the two directly reported the
    # entire opening inventory as "missing rows" -- the identity is
    # fed == built + opening, and that is what is checked.
    op = {r["plant"]: float(str(r["value"]).replace(",", "")) for r in kpi.filter(
        pl.col("metric").str.contains("OPENING STOCK")).iter_rows(named=True)}
    print("  RECONCILIATION (headline fed == sheet1 built + opening stock)")
    for p, v in fed.items():
        f_ = float(str(v).replace(",", ""))
        s1 = float(bq.filter(pl.col("plant") == p)["sheet1"][0])
        o_ = op.get(p, 0.0)
        gap = f_ - s1 - o_
        print(f"    {p}: fed {f_:>10,.0f} | sheet1 {s1:>10,.0f} + opening "
              f"{o_:>8,.0f} | diff {gap:>8,.0f}")
        if abs(gap) > 0.5:
            add("EXPORT", "headline fed != built + opening",
                f"{p}: {gap:,.0f} tyres unaccounted")
    # demand vs plan internal
    if abs(dvp["fed"].sum() - sum(float(str(v).replace(',', '')) for v in fed.values())) > 0.5:
        add("EXPORT", "sheet8 fed vs headline",
            f"{dvp['fed'].sum():,.0f} vs {sum(float(str(v).replace(',','')) for v in fed.values()):,.0f}")

    # ---------- REPORT ------------------------------------------------------
    print("\n" + "=" * 78)
    if not F:
        print("  NO VIOLATIONS OF ANY SEVERITY")
    for sev in ("HARD", "SOFT", "EXPORT"):
        rows = [f for f in F if f["severity"] == sev]
        print(f"  {sev}: {len(rows)}")
        for r in rows:
            print(f"     - {r['check']}: {r['detail']}")
    hard = sum(1 for f in F if f["severity"] == "HARD")
    print("=" * 78)
    print(f"  VERDICT: plan is {'NOT ' if hard else ''}physically executable "
          f"({hard} hard violation(s))")
    pl.DataFrame(F if F else {"severity": [], "check": [], "detail": []}
                 ).write_csv(out / "verification_report.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
