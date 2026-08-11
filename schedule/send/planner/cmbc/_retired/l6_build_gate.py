"""L6 -- BUILDING FEASIBILITY GATE.  Curing proposes; building disposes.

    python -m planner.cmbc.l6_build_gate --month 2026-07

Plant step 5 · R2, R6, R8/B16, R10.

WHAT THIS LAYER IS FOR
  L5 decided the cure campaigns without consulting building at all -- deliberately,
  because the plant sets its rhythm on the presses. L6 asks the only question that
  matters next: CAN BUILDING FEED THIS?

  If the answer is no, the correct response is to RESHAPE THE CURE CAMPAIGN AT L5,
  not to patch the build schedule downstream. Downstream repair is exactly what
  the old engine's Phase 5 did, and it is why that engine churned: building would
  quietly slide, curing would follow, and the coupling buffer would drift.

THIS LAYER DOES NOT SCHEDULE. IT GATES.
  No lot is placed on a machine here -- that is L7. L6 only tests whether a
  feasible build plan EXISTS for the cure plan as given. Keeping the test separate
  from the placement means an infeasible cure plan is caught before any build
  decision has been made and has to be unwound.

THE FOUR TESTS
  R2   every GT in the cure plan has at least one eligible building machine
  B16  a TT GT has an eligible machine inside the TT group, and vice versa
  R10  in every time bucket, required build hours <= available machine hours
  rate a campaign's GT can be supplied at the rate the campaign consumes it,
       given how many machines may run that GT at once

THE BUILD WINDOW COMES FROM THE PULL EQUATION
  A tyre cured at time t must be built at t - tau*, so a cure campaign spanning
  [t0, t1] must be fed by building over [t0 - tau*, t1 - tau*]. That window --
  not the campaign itself -- is what building capacity is tested against.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import polars as pl

from planner.cmbc import plant_ct

from planner.config import GT_SHELF_LIFE_H
from planner import paths

ROOT = paths.ROOT   # depth-independent; this file moved one level deeper
D = ROOT / "warehouse" / "derived"
PARAMS = ROOT / "warehouse" / "params"
BUCKET_H = 24.0                     # capacity is tested per day
# See l7_pull_release.CAD_BASIS. "plant" restores the flat-cadence bug for A/B.
CAD_BASIS = os.environ.get("PLANNER_CAD_BASIS", "machine")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", default="2026-07")
    ap.add_argument("--run", default=None)
    a = ap.parse_args()
    run = ROOT / "runs" / (a.run or f"cmbc_{a.month}")

    P = json.loads(sorted(PARAMS.glob("params_*.json"))[-1].read_text())
    tau = {p: float(P["tau"][p]["tau_star_h"]) for p in ["PCR", "TBR"]}
    cad = pl.read_parquet(PARAMS / P["tables"]["build_cadence"])
    camp = pl.read_parquet(run / "cure_campaigns.parquet")
    cm = pl.read_parquet(D / f"cap_machine_{a.month}.parquet")
    grp = pl.read_parquet(D / f"cap_ttl_groups_{a.month}.parquet")
    tt = pl.read_parquet(paths.INPUT_DERIVED / "tt_tl.parquet")
    dem = pl.read_parquet(ROOT / "masters" / "demand" / f"demand_{a.month}.parquet")

    # PLANT-MEDIAN cadence, kept ONLY as the fallback when a GT has no eligible
    # machine carrying a mined cadence. Everything that measures LOAD must use
    # the GT's own machines -- see `gt_cad` below and EXPERT_AUDIT §4b. PCR
    # machines run 49-78 s against this 62 s median (-21 % to +26 %), so a flat
    # charge makes Hall's condition below test the wrong quantity: it inflates
    # demand on fast machines and deflates it on slow ones, which is one concrete
    # reason this gate "structurally cannot detect the failure it exists to
    # detect".
    cad_s = {p: float(cad.filter(pl.col("plant") == p)["cadence_s_p50"].median())
             for p in ["PCR", "TBR"]}
    cad_m = {r["machine"]: float(r["cadence_s_p50"]) for r in cad.iter_rows(named=True)}
    n_mach = {p: cad.filter(pl.col("plant") == p).height for p in ["PCR", "TBR"]}
    elig: dict[tuple, set] = {}
    for r in cm.iter_rows(named=True):
        elig.setdefault((r["plant"], r["gt_code"]), set()).add(r["machine"])
    group_of = {r["machine"]: r["group"] for r in grp.iter_rows(named=True)}
    tmap = (tt.filter(pl.col("sku") != "").select(["sku", "tt_tl"])
            .unique(subset=["sku"]))
    gt_tag = {}
    for r in dem.join(tmap, on="sku", how="left").iter_rows(named=True):
        if r["tt_tl"] and r["plant"] == "TBR":
            gt_tag[r["gt_code"]] = r["tt_tl"]

    def _elig_of(p: str, gt: str) -> set:
        """Eligible machines, inside the TT/TL group where B16 applies."""
        e = elig.get((p, gt), set())
        if p == "TBR" and gt_tag.get(gt):
            e = {mm for mm in e if group_of.get(mm) == gt_tag[gt]} or e
        return e

    PCT = plant_ct.get()

    def _mach_cad(p: str, gt: str, mm: str) -> float:
        """One machine's seconds/tyre FOR THIS GT. The plant's building file is
        per GT x machine MAKE (CONTI/BJ on PCR), which is finer than the mined
        per-machine median it replaces; the mined value is the fallback."""
        v = PCT.build_ct_s(p, gt, mm)
        return v if v else cad_m.get(mm, cad_s[p])

    def gt_cad(p: str, gt: str) -> float:
        """Mean cadence over the GT's OWN eligible machines. The load a GT places
        depends on which machines can run it, not on the plant's median."""
        if CAD_BASIS == "plant":            # A/B only -- this is the bug
            return cad_s[p]
        ms = list(_elig_of(p, gt))
        return (sum(_mach_cad(p, gt, mm) for mm in ms) / len(ms)) if ms else cad_s[p]

    y, m = int(a.month[:4]), int(a.month[5:7])
    t0 = datetime(y, m, 1, 7, 0)
    ndays = (datetime(y + (m == 12), (m % 12) + 1, 1) - datetime(y, m, 1)).days

    print("=" * 92)
    print(f"L6  BUILDING FEASIBILITY GATE  --  {a.month}")
    print("=" * 92)
    print(f"  tau*  PCR {tau['PCR']:.2f} h   TBR {tau['TBR']:.2f} h   "
          f"cadence PCR {cad_s['PCR']:.0f} s  TBR {cad_s['TBR']:.0f} s\n")

    fail = []

    # ---- R2 / B16 : eligibility -----------------------------------------
    for r in camp.select(["plant", "gt_code"]).unique().iter_rows(named=True):
        p, gt = r["plant"], r["gt_code"]
        e = elig.get((p, gt), set())
        if not e:
            fail.append({"test": "R2", "plant": p, "gt_code": gt,
                         "detail": "no eligible building machine"})
            continue
        tag = gt_tag.get(gt)
        if p == "TBR" and tag:
            same = {mm for mm in e if group_of.get(mm) == tag}
            if not same:
                fail.append({"test": "B16", "plant": p, "gt_code": gt,
                             "detail": f"{tag} GT has no eligible machine in the "
                                       f"{tag} group"})

    # ---- rate : can building supply the campaign as fast as it is consumed
    rate_rows = []
    for r in camp.iter_rows(named=True):
        p, gt = r["plant"], r["gt_code"]
        hrs = max(float(r["hours"]), 1e-9)
        need_rate = float(r["qty"]) / hrs                    # tyres per hour
        e = _elig_of(p, gt)
        # Sum each machine's OWN rate, not machines x the plant median rate.
        cap_rate = (len(e) * 3600.0 / cad_s[p] if CAD_BASIS == "plant"
                    else sum(3600.0 / _mach_cad(p, gt, mm) for mm in e))
        rate_rows.append({"plant": p, "gt_code": gt, "need_rate": need_rate,
                          "machines": len(e), "cap_rate": cap_rate,
                          "ok": need_rate <= cap_rate})
    rr = pl.DataFrame(rate_rows)
    for r in rr.filter(~pl.col("ok")).iter_rows(named=True):
        fail.append({"test": "rate", "plant": r["plant"], "gt_code": r["gt_code"],
                     "detail": f"needs {r['need_rate']:.1f} tyres/h, "
                               f"{r['machines']} machines give {r['cap_rate']:.1f}"})

    # ---- R10 : per-day build hours vs machine hours ---------------------
    rows = []
    for r in camp.iter_rows(named=True):
        p = r["plant"]
        s = r["start_ts"] - timedelta(hours=tau[p])
        e = r["end_ts"] - timedelta(hours=tau[p])
        span = max((e - s).total_seconds() / 3600.0, 1e-9)
        bh = float(r["qty"]) * gt_cad(p, r["gt_code"]) / 3600.0   # build hours needed
        d0 = max(0, int((s - t0).total_seconds() // 86400))
        d1 = min(ndays - 1, int((e - t0).total_seconds() // 86400))
        n = max(1, d1 - d0 + 1)
        for d in range(d0, d1 + 1):
            rows.append({"plant": p, "day": d, "build_h": bh / n,
                         "gt_code": r["gt_code"]})
    bd = (pl.DataFrame(rows).group_by(["plant", "day"])
          .agg(pl.col("build_h").sum().alias("need_h"))
          .sort(["plant", "day"]))
    bd = bd.with_columns(
        pl.col("plant").replace_strict({p: n_mach[p] * 24.0 for p in n_mach})
        .alias("avail_h"))
    bd = bd.with_columns((pl.col("need_h") / pl.col("avail_h")).alias("load"))
    # R10 MUST BE TESTED OVER A ROLLING WINDOW, NOT PER DAY.
    # A tyre does not have to be built on a particular day. It must exist before
    # its cure and no earlier than the shelf life allows:
    #     build(tyre) in [t_cure - GT_SHELF_LIFE_H, t_cure - tau_min]
    # That is a 72 h window, so building can pull work forward up to 3 days.
    # Testing each day in isolation manufactures violations the plant absorbs by
    # building slightly early: PCR day 15 read 100.3% while days 13-15 together
    # were 718 h against 792 h available, and the month averaged 81.7%.
    # The binding condition is therefore: in EVERY window of tau_hard, required
    # hours <= available hours.
    win = max(1, int(GT_SHELF_LIFE_H // 24))
    bd = bd.with_columns(
        pl.col("need_h").rolling_sum(win, min_samples=1).over("plant").alias("need_win"),
        pl.col("avail_h").rolling_sum(win, min_samples=1).over("plant").alias("avail_win"))
    bd = bd.with_columns((pl.col("need_win") / pl.col("avail_win")).alias("load_win"))
    over = bd.filter(pl.col("load_win") > 1.0)
    for r in over.iter_rows(named=True):
        fail.append({"test": "R10", "plant": r["plant"],
                     "gt_code": f"days {int(r['day'])+2-win}-{int(r['day'])+1}",
                     "detail": f"{win}-day build load {100*r['load_win']:.0f}% "
                               f"({r['need_win']:.0f} h needed, "
                               f"{r['avail_win']:.0f} available)"})

    # ---- ALLOCATION FEASIBILITY (Hall's condition, by machine-set) --------
    # Aggregate load can pass while a SUBSET of GTs is infeasible: TBR runs at
    # 75.6% overall, yet 6,271 tyres went unfed because the median TBR GT reaches
    # only 3 of 9 machines and those 3 were taken. Total capacity is the wrong
    # test -- what matters is whether every GROUP of GTs sharing a machine set
    # fits inside that set's capacity. This is Hall's condition, checked per
    # distinct eligible-machine signature rather than over all 2^n subsets.
    alloc_rows = []
    need_h: dict[tuple, float] = {}
    for r in camp.iter_rows(named=True):
        p = r["plant"]
        need_h[(p, r["gt_code"])] = need_h.get((p, r["gt_code"]), 0.0) \
            + float(r["qty"]) * gt_cad(p, r["gt_code"]) / 3600.0
    sig: dict[tuple, list] = {}
    for (p, gt), h in need_h.items():
        e = elig.get((p, gt), set())
        if p == "TBR" and gt_tag.get(gt):
            e = {mm for mm in e if group_of.get(mm) == gt_tag[gt]} or e
        sig.setdefault((p, frozenset(e)), []).append((gt, h))
    # a machine set's capacity must cover every GT that can ONLY reach it or a
    # subset of it -- so accumulate demand from all signatures contained in it
    for (p, ms), items in sorted(sig.items(), key=lambda kv: len(kv[0][1])):
        if not ms:
            continue
        reach = sum(h for (p2, ms2), it2 in sig.items() if p2 == p and ms2 <= ms
                    for _g, h in it2)
        capacity = len(ms) * 24.0 * ndays
        alloc_rows.append({"plant": p, "n_machines": len(ms),
                           "gts": len(items), "need_h": reach,
                           "cap_h": capacity, "load": reach / max(capacity, 1)})
        if reach > capacity:
            fail.append({"test": "alloc", "plant": p,
                         "gt_code": f"{len(items)} GTs on {len(ms)} machines",
                         "detail": f"need {reach:,.0f} h, those machines give "
                                   f"{capacity:,.0f} h ({100*reach/capacity:.0f}%)"})
    al = pl.DataFrame(alloc_rows) if alloc_rows else pl.DataFrame()
    if al.height:
        print("  ALLOCATION FEASIBILITY (tightest machine sets)")
        print(f"    {'plant':<6}{'machines':>10}{'GTs':>6}{'need h':>10}"
              f"{'cap h':>10}{'load':>8}")
        for r in al.sort("load", descending=True).head(6).iter_rows(named=True):
            print(f"    {r['plant']:<6}{r['n_machines']:>10}{r['gts']:>6}"
                  f"{r['need_h']:>10,.0f}{r['cap_h']:>10,.0f}"
                  f"{100*r['load']:>7.1f}%")
        print()

    # ---- timing feasibility ---------------------------------------------
    # Aggregate load and per-slice timing are DIFFERENT tests. L6 passed a plan
    # at 91%/95% rolling load that L7 could not release 1,048 slices of, because
    # a campaign starting at t0 needs its GT finished at t0 - tau* -- before the
    # horizon exists. A gate must not read PASS while carrying unreleaseable
    # work, so the timing condition is checked here too.
    build_band = {p: float(P["campaign_bands"][p]["build"]["hours_p50"])
                  for p in ["PCR", "TBR"]}
    early = 0
    for r in camp.iter_rows(named=True):
        p = r["plant"]
        hrs = max(float(r["hours"]), 1e-9)
        n = max(1, int(round(hrs / max(build_band[p], 1e-9))))
        build_h = (float(r["qty"]) / n) * gt_cad(p, r["gt_code"]) / 3600.0
        if r["start_ts"] < t0 + timedelta(hours=tau[p] + build_h):
            early += 1
    if early:
        fail.append({"test": "timing", "plant": "-", "gt_code": "-",
                     "detail": f"{early} campaigns start before "
                               f"t0 + tau* + first-slice build time -- L7 cannot "
                               f"release them"})

    # ---- report ----------------------------------------------------------
    print("  BUILD LOAD per day (pull window = cure window shifted by tau*)")
    print(f"    {'plant':<6}{'machines':>9}{'need h/day p50':>16}{'avail h/day':>13}"
          f"{'load p50':>10}{'load max':>10}{'days >100%':>12}")
    for p in ["PCR", "TBR"]:
        s = bd.filter(pl.col("plant") == p)
        if not s.height:
            continue
        print(f"    {p:<6}{n_mach[p]:>9}{float(s['need_h'].median()):>16,.0f}"
              f"{float(s['avail_h'][0]):>13,.0f}"
              f"{100*float(s['load'].median()):>9.1f}%"
              f"{100*float(s['load'].max()):>9.1f}%"
              f"{s.filter(pl.col('load_win') > 1.0).height:>12}")
        print(f"          {win}-day rolling: p50 {100*float(s['load_win'].median()):.1f}%"
              f"   max {100*float(s['load_win'].max()):.1f}%"
              f"   <- the binding test (72 h build flexibility)")

    print("\n  RATE FEASIBILITY (can building match the campaign's consumption?)")
    print(f"    {'plant':<6}{'campaigns':>11}{'infeasible':>12}"
          f"{'need rate p50':>15}{'cap rate p50':>14}")
    for p in ["PCR", "TBR"]:
        s = rr.filter(pl.col("plant") == p)
        if not s.height:
            continue
        print(f"    {p:<6}{s.height:>11}{s.filter(~pl.col('ok')).height:>12}"
              f"{float(s['need_rate'].median()):>15.1f}"
              f"{float(s['cap_rate'].median()):>14.1f}")

    fd = pl.DataFrame(fail) if fail else pl.DataFrame(
        schema={"test": pl.Utf8, "plant": pl.Utf8, "gt_code": pl.Utf8,
                "detail": pl.Utf8})
    fd.write_parquet(run / "l6_infeasible.parquet")
    bd.write_parquet(run / "l6_build_load.parquet")

    print("\n  GATE")
    for t in ["R2", "B16", "rate", "R10", "timing", "alloc"]:
        n = fd.filter(pl.col("test") == t).height if fd.height else 0
        print(f"    {t:<6}{'PASS' if n == 0 else f'FAIL  {n} violations':<30}")
    print()
    if fd.height:
        print(f"  >>> INFEASIBLE -- {fd.height} findings. RESHAPE AT L5, do not "
              f"patch downstream.")
        for r in fd.head(8).iter_rows(named=True):
            print(f"      [{r['test']:<4}] {r['plant']} {r['gt_code'][:34]:<34} "
                  f"{r['detail']}")
    else:
        print("  >>> FEASIBLE -- building can supply this cure plan. Proceed to L7.")
    print(f"\n  -> {run.name}/l6_infeasible.parquet · l6_build_load.parquet")


if __name__ == "__main__":
    main()
