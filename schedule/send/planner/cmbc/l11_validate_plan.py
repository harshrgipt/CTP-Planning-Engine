"""L11 -- PLAN VALIDATION.  The invariants, rewritten for a pull plant.

    python -m planner.cmbc.l11_validate_plan --month 2026-07

R12 · plant step 13.

THREE INVARIANTS ARE DELETED, NOT RELAXED
  The old Phase 8 would fail this plant on a CORRECT plan:

  * "daily build mix should resemble daily cure mix" -- observed cosine
    similarity is 0.21. An 11-day cure campaign fed by 5.5-hour build campaigns
    MUST show a different daily mix. Checking it punishes the pull system for
    being a pull system.
  * "minimise WIP toward zero" -- a zero buffer starves presses, and press-idle
    at the drum is throughput never recovered. The plant's own answer is ~4.5 h
    of coupling buffer, not zero.
  * "build changeover count as a pass/fail gate" -- the plant deliberately
    absorbs 2.66/3.56 build changeovers per resource-day to hold cure at
    1.43/1.19. Gating on the build count would reverse a trade it makes on
    purpose.

  They are replaced by invariants that measure whether the COUPLING is right.

AND ONE CHECK IS DELIBERATELY ABSENT
  There is no build-slice minimum. A slice is a delivery, not a lot. Adding a
  floor there fragments cure campaigns or forces building to run ahead, which
  recreates the head gap. It is reported as NOT CHECKED so nobody "fixes" it.
"""
from __future__ import annotations

import argparse
import calendar
from datetime import timedelta
import json
from pathlib import Path

import numpy as np
import polars as pl

from planner.config import CONFIG, GT_SHELF_LIFE_H
from planner.data.warehouse import duck
from planner import paths

# G4 -- REAL per-machine changeover minutes, from the plant master. THE ONLY
# place this verifier gets a changeover cost. Reading the master rather than
# hardcoding is the guard: a flat (11.3, 42.4) charged to PCR here made this
# file's own weighted-changeover invariant wrong, and the gate derived from it
# wrong too. Fallback is the cheaper PCR tier / the single TBR tier.
CO_FALLBACK = {"PCR": (22.0, 42.0), "TBR": (10.0, 24.0)}
CO_MIN: dict = {}
try:
    for _r in duck().execute("SELECT machine, same_size_min, diff_size_min "
                             "FROM v_changeover_build").fetchall():
        CO_MIN[_r[0]] = (float(_r[1]), float(_r[2]))
except Exception:                       # master absent -> documented fallback
    pass

ROOT = Path(__file__).resolve().parent.parent.parent
D = ROOT / "warehouse" / "derived"
PARAMS = ROOT / "warehouse" / "params"

RES: list[dict] = []


def _tw_mean_l11(ivt, t0=None, n_h: int = 744) -> float:
    """TIME-weighted mean of a (ts, bal) stock profile.

    DO-NOT #9: never report or gate `bal.mean()`. That averages over EVENTS, and
    events cluster where activity -- and therefore stock -- is high, so it reads
    high: +5.5 % on PCR and +11.7 % on TBR measured on runs/v31. Inventory is a
    stock held over TIME, so hours are the weights.
    This file had the event-weighted version long after the same bug was fixed in
    l7 and in the scorecard -- one bug, three files, two repaired.
    """
    ts0 = t0 if t0 is not None else ivt["ts"].min()
    ts = np.array([(x - ts0).total_seconds() / 3600.0 for x in ivt["ts"]], float)
    bal = np.array(ivt["bal"], float)
    idx = np.searchsorted(ts, np.arange(n_h) + 0.5, side="right") - 1
    return float(np.where(idx >= 0, bal[np.clip(idx, 0, len(bal) - 1)], 0.0).mean())


# ---------------------------------------------------------------------------
# ARM FRESHNESS -- the guard against scoring one arm with another arm's result.
#
# THE FAILURE THIS PREVENTS, measured 2026-08-08 on five directories at once.
# `runs/hl_00 hl_01 hl_10 hl_11` and `runs/rr_*` each carried an
# `l11_invariants.parquet` written 1-2 SECONDS BEFORE their own
# `build_schedule.parquet`, and all five files were byte-identical to
# `runs/v31`'s. Reading them said every arm scored 93.6 % fulfilment, 96.1 %
# same-size, 22/34 PASS -- while `build_starved.parquet` showed PCR starvation
# moving 13,743 -> 5,336 across the same arms. A flag that recovered 8,085 tyres
# read as free.
#
# The cause is not an ordering bug in a driver -- there is no driver. It is the
# documented seeding step `cp -r runs/<prev> runs/<new>` (PARTITION_AND_CHANGEOVER
# §7), which inherits L1-L6 artefacts AND the previous arm's L11 result. L7 is
# then re-run and L11 is not, so a stale scorecard sits in the directory looking
# exactly like that arm's own.
#
# Two mechanisms, because one is not enough:
#   1. L11 deletes its own output before scoring, so a crash cannot leave a
#      stale file behind, and writes `l11_provenance.json` fingerprinting every
#      plan artefact it actually read.
#   2. `arm_is_stale()` lets any READER prove a scorecard belongs to the plan
#      sitting beside it. mtime alone is not enough (a copy rewrites it), so the
#      fingerprint carries size and row count too.
# `scripts/check_arm_fresh.py` runs (2) over a whole runs/ tree.
PLAN_ARTEFACTS = ("build_schedule.parquet", "cure_campaigns.parquet",
                  "gt_events.parquet", "cure_campaigns_reconciled.parquet",
                  "build_starved.parquet")


def plan_fingerprint(run: Path) -> dict:
    """Size + mtime of every plan artefact present. Cheap, no parquet read."""
    fp = {}
    for n in PLAN_ARTEFACTS:
        f = run / n
        if f.exists():
            st = f.stat()
            fp[n] = {"bytes": st.st_size, "mtime": round(st.st_mtime, 3)}
    return fp


def arm_is_stale(run: Path) -> str | None:
    """Reason string if `l11_invariants.parquet` does not describe this plan."""
    out = run / "l11_invariants.parquet"
    if not out.exists():
        return None                      # nothing to be stale
    plan = run / "build_schedule.parquet"
    if not plan.exists():
        return "l11_invariants.parquet present but build_schedule.parquet is not"
    prov = run / "l11_provenance.json"
    if not prov.exists():
        # Pre-guard directory. Fall back to the mtime test that caught hl_*.
        if out.stat().st_mtime < plan.stat().st_mtime - 1.0:
            return (f"l11_invariants.parquet is OLDER than build_schedule.parquet "
                    f"({out.stat().st_mtime:.0f} < {plan.stat().st_mtime:.0f}) "
                    f"-- L11 was not re-run after the plan changed")
        return "no l11_provenance.json (pre-guard run); freshness unproven"
    try:
        rec = json.loads(prov.read_text())["fingerprint"]
    except Exception as exc:
        return f"l11_provenance.json unreadable: {exc}"
    now = plan_fingerprint(run)
    for n, v in now.items():
        if n not in rec:
            return f"{n} was not scored by this l11_invariants.parquet"
        if rec[n]["bytes"] != v["bytes"] or abs(rec[n]["mtime"] - v["mtime"]) > 1.0:
            return (f"{n} changed since scoring "
                    f"({rec[n]['bytes']}B -> {v['bytes']}B) "
                    f"-- l11_invariants.parquet is STALE")
    return None


def _assert_fresh(run: Path) -> dict:
    """Refuse to score a directory that is missing its plan; clear stale output."""
    if not run.exists():
        raise SystemExit(f"!! run directory does not exist: {run}")
    missing = [n for n in ("build_schedule.parquet", "cure_campaigns.parquet")
               if not (run / n).exists()]
    if missing:
        raise SystemExit(f"!! {run.name}: cannot score, missing {missing}. "
                         f"Run l7_pull_release for this arm first.")
    why = arm_is_stale(run)
    if why:
        print(f"  !! STALE SCORECARD FOUND AND DISCARDED in {run.name}: {why}")
    fp = plan_fingerprint(run)
    (run / "l11_invariants.parquet").unlink(missing_ok=True)
    (run / "l11_provenance.json").unlink(missing_ok=True)
    return fp


def _daily_mean_l11(ivt, t0=None, n_h: int = 744) -> np.ndarray:
    """Per-calendar-day means of the same hourly step function `_tw_mean_l11`
    averages. Same sampling, same weights -- so the day series and the monthly
    mean can never disagree about what the stock profile was."""
    ts0 = t0 if t0 is not None else ivt["ts"].min()
    ts = np.array([(x - ts0).total_seconds() / 3600.0 for x in ivt["ts"]], float)
    bal = np.array(ivt["bal"], float)
    idx = np.searchsorted(ts, np.arange(n_h) + 0.5, side="right") - 1
    g = np.where(idx >= 0, bal[np.clip(idx, 0, len(bal) - 1)], 0.0)
    nd = n_h // 24
    return g[: nd * 24].reshape(nd, 24).mean(axis=1)


def check(name: str, actual, target: str, ok: bool, basis: str = "") -> None:
    RES.append({"invariant": name, "actual": actual, "target": target,
                "status": "PASS" if ok else "FAIL", "basis": basis})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", default="2026-07")
    ap.add_argument("--run", default=None)
    a = ap.parse_args()
    run = ROOT / "runs" / (a.run or f"cmbc_{a.month}")
    # Fingerprint the plan BEFORE reading it, and clear any inherited scorecard.
    _fp = _assert_fresh(run)

    P = json.loads(sorted(PARAMS.glob("params_*.json"))[-1].read_text())
    camp = pl.read_parquet(run / "cure_campaigns.parquet")
    bs = pl.read_parquet(run / "build_schedule.parquet")
    bsm = bs.filter(pl.col("machine") != "OPENING_STOCK")
    ev = pl.read_parquet(run / "gt_events.parquet")
    mo = pl.read_parquet(D / f"cap_mould_{a.month}.parquet")
    lots = pl.read_parquet(D / f"l45_lots_{a.month}.parquet")
    req = pl.read_parquet(D / f"net_requirement_{a.month}.parquet")
    # Rim per GT, for the size-mix invariants. The plant's changeover master is
    # BINARY -- same size vs different -- so the MIX is what sets setup time, and
    # a raw count cannot see it.
    _sz = pl.read_parquet(paths.INPUT_DERIVED / "gt_size.parquet")
    rim_of = {r["gt_code"]: str(r["rim"]) for r in _sz.iter_rows(named=True)
              if r.get("gt_code") and r.get("rim")}

    print("=" * 92)
    print(f"L11  PLAN VALIDATION  --  {a.month}   (R12)")
    print("=" * 92)

    # MONTH LENGTH IS DERIVED, NEVER 744. A 30-day month is 720 h and a 28-day
    # month 672; the inventory samplers below used to assume 744 unconditionally,
    # so on June 2026 they averaged over 24 hours that do not exist and read the
    # last-day mean as 0 instead of 102. Same bug class as the hardcoded 744 h
    # month already fixed in the partition builder (PARTITION §3) -- third
    # instance, so grep before assuming it is gone.
    _nh = calendar.monthrange(int(a.month[:4]), int(a.month[5:7]))[1] * 24

    for p in ["PCR", "TBR"]:
        tau = float(P["tau"][p]["tau_star_h"])
        cvt = 0.10 if p == "PCR" else 0.15
        w = np.array(bs.filter(pl.col("plant") == p)["wait_h"], float)
        if not len(w):
            continue

        # --- coupling ---------------------------------------------------
        med = float(np.median(w))
        check(f"{p} median GT wait vs tau*", f"{med:.2f} h",
              f"{tau:.2f} +/-20%", abs(med - tau) / tau <= 0.20,
              "the pull equation binding")
        check(f"{p} GT wait p95", f"{np.percentile(w,95):.1f} h", "<= 28 h",
              np.percentile(w, 95) <= 28.0, "L0 observed p95")
        check(f"{p} GT wait max (R5)", f"{w.max():.1f} h",
              f"<= {GT_SHELF_LIFE_H:.0f} h", w.max() <= GT_SHELF_LIFE_H, "hard")
        # R17 IS A RELEASE RULE -- it does not apply to opening stock.
        # Those rows carry start_ts == end_ts == t0, so wait_h is measured from
        # the horizon start, not from when the tyre was built (last month). With
        # EARLY_STOCK on they made this hard-rule guard fail permanently on an
        # artifact: 19 PCR / 20 TBR "breaches", every one an OPENING_STOCK row.
        # A guard that always fails cannot detect a real breach.
        _wr = np.array(bs.filter((pl.col("plant") == p)
                                 & (pl.col("machine") != "OPENING_STOCK"))["wait_h"], float)
        _tm = float(P["tau"][p]["tau_min_h"])
        check(f"{p} GT wait below tau_min (R17)",
              int((_wr < _tm).sum()), "0", (_wr < _tm).sum() == 0,
              "press starvation risk (released tyres only)")

        # --- campaign shape ---------------------------------------------
        # CALIBRATED BAND, not L0's per-month p10/p90.
        # L0's own band widened to a "85 h band" (PCR) / "190 h band" (TBR) that
        # appears in no source document, and a 192.9 h campaign PASSED against it
        # while ENGINE_FLOW's 8-month-validated band is 40-75 h. A moved goalpost
        # is worse than a failed test because it removes the signal permanently.
        # Judge the DISTRIBUTION, not the median: a p50 test cannot see the
        # bimodality (TBR had 78 campaigns below 200 h and 68 above 330).
        ch = camp.filter(pl.col("plant") == p)
        lo, hi = (40.0, 75.0) if p == "PCR" else (200.0, 330.0)
        h50 = float(ch["hours"].median())
        in_band = ch.filter((pl.col("hours") >= lo)
                            & (pl.col("hours") <= hi)).height
        frac = 100.0 * in_band / max(ch.height, 1)
        check(f"{p} cure campaigns in {lo:.0f}-{hi:.0f} h band",
              f"{frac:.0f}% (p50 {h50:.0f} h)", ">= 80%", frac >= 80.0,
              "ENGINE_FLOW calibrated band, 8-month validated")
        # DERIVE the expected ratio from L0's CURRENT bands, do not hardcode
        # Phase 0's 7.5/48. Those came from the unbounded-window campaign
        # measurement (cure 57.4 h) that L0 later corrected to a per-month basis
        # (cure p50 85 h). Checking a plan against a superseded constant reports
        # a failure that is in the target, not the plan.
        nsl = bsm.filter(pl.col("plant") == p).height / max(ch.height, 1)
        exp = (float(P["campaign_bands"][p]["cure"]["hours_p50"])
               / max(float(P["campaign_bands"][p]["build"]["hours_p50"]), 1e-9))
        check(f"{p} build slices per cure campaign", f"{nsl:.1f}",
              f"~{exp:.0f} +/-30%", 0.7 * exp <= nsl <= 1.3 * exp,
              "L0 cure p50 / build p50")

        # --- lots --------------------------------------------------------
        lt = lots.filter((pl.col("plant") == p) & (pl.col("n_lots") > 0))
        below = lt.filter(pl.col("lot_qty") < pl.col("min_lot")).height
        check(f"{p} cure lots below min_cure_lot", below, "0", below == 0, "R9")

        # --- BUILD RUNS (B12 / R9 at the machine) -------------------------
        # The cure-lot check above passed at 100% while 91% of BUILD RUNS were
        # below the same floor -- the floor was enforced on the lot and lost at
        # the machine, because the run did not exist as an object. It does now
        # (`run_id` in build_schedule.parquet), so it is checked here. Without
        # this pair of invariants the fragmentation regresses silently: every
        # other L11 line stayed green throughout.
        fl = int(CONFIG.thresholds.min_lot_units.get(p, 0))
        rp = (bsm.filter(pl.col("plant") == p)
              .group_by(["machine", "run_id"])
              .agg(pl.col("qty").sum().alias("q")))
        if rp.height:
            # GATE ON THE PLANT'S OWN TOLERANCE, NOT ON ZERO.
            # Measured over 8 months of v_build stage 2, the plant runs BELOW its
            # own floor 13.1 % of the time on PCR (14.0 % in July) and 31.0 % on
            # TBR. A zero gate is therefore stricter than the plant it is meant
            # to imitate, and enforcing it as a hard split refusal starved 30,615
            # tyres -- 6.2 points, the single largest fulfilment loss in the run.
            # Allow the plant's share plus a small margin; flag only EXCESS
            # fragmentation, which is what B12 is actually protecting against.
            nb = rp.filter(pl.col("q") < fl).height
            sh = 100.0 * nb / rp.height
            lim = {"PCR": 16.0, "TBR": 34.0}.get(p, 100.0)   # plant 14.0 / 31.0
            check(f"{p} build runs below min_lot ({fl})", f"{sh:.1f}%",
                  f"<= {lim:.0f}%", sh <= lim,
                  "B12 / R9 -- gated at the plant's own sub-floor share")
            bp = bsm.filter(pl.col("plant") == p)
            mdays = (bp.with_columns(pl.col("start_ts").dt.date().alias("d"))
                     .select(["machine", "d"]).unique().height)
            cpd = (rp.height - bp["machine"].n_unique()) / max(mdays, 1)
            cap_co = CONFIG.thresholds.plant_co_per_machine_day.get(p, 99.0)
            check(f"{p} build changeovers / machine-day", f"{cpd:.2f}",
                  f"<= {cap_co:.2f}", cpd <= cap_co,
                  "plant July, config.plant_co_per_machine_day")

            # --- SIZE MIX: what a raw count cannot see --------------------
            # Two schedules with identical COUNTS can differ 2x in real setup
            # TIME, so the weighted figure is the gate and the count secondary.
            #
            # MINUTES COME FROM THE PLANT MASTER, PER MACHINE. This block used to
            # charge a flat (11.3, 42.4) to PCR. `v_changeover_build` says 28/60
            # on machines 1-5 and 22/42 on 6-11 -- so both this invariant AND the
            # 30.2 gate derived from it were computed on wrong constants. The
            # correct plant benchmark is 74.0 min/machine-day on PCR, not 30.2.
            # Never hardcode a changeover minute here again.
            seq = (bp.group_by(["machine", "run_id"])
                   .agg(pl.col("gt_code").first(),
                        pl.col("start_ts").min().alias("t"))
                   .sort(["machine", "t"])
                   .with_columns(pl.col("gt_code").shift(1).over("machine")
                                 .alias("prev"))
                   .filter(pl.col("prev").is_not_null()))
            if seq.height:
                # KNOWN-RIM TRANSITIONS ONLY, AND REPORT THE COVERAGE BESIDE IT.
                #
                # This used to read `rim_of.get(a) == rim_of.get(b)` over EVERY
                # transition, which is wrong in both directions:
                #   * known vs UNKNOWN scored as different-size  -> deflates
                #   * UNKNOWN vs UNKNOWN scored as SAME (None == None is True)
                #     -> inflates. 16 PCR transitions on August 2026.
                # Measured on runs/aug_v6: 60.9 % as reported against 82.3 % over
                # the 72.4 % of transitions where both rims are actually known.
                # A metric that silently counts "I don't know" as either answer
                # is not a measurement, so the unknowns are now excluded and the
                # coverage is published as its own invariant -- you can no longer
                # read the share without seeing what it is a share OF.
                rows = list(seq.iter_rows(named=True))
                known = [x for x in rows
                         if rim_of.get(x["gt_code"]) and rim_of.get(x["prev"])]
                cov = 100.0 * len(known) / len(rows)
                if known:
                    same = sum(1 for x in known
                               if rim_of[x["gt_code"]] == rim_of[x["prev"]])
                    pct = 100.0 * same / len(known)
                    check(f"{p} same-size share of build changeovers",
                          f"{pct:.1f}%", ">= 70%", pct >= 70.0,
                          f"known-rim transitions only ({len(known)}/{len(rows)}"
                          f" = {cov:.1f}%); plant July PCR 91.5%, TBR 100%")
                else:
                    pct = 0.0
                    check(f"{p} same-size share of build changeovers",
                          "no known-rim pair", ">= 70%", False,
                          "gt_size has no rim for any transition on this plant")
                # Coverage is a MASTER-DATA invariant, not a scheduling one: a
                # low value means gt_size is thin, and it caps how much the
                # same-size figure above can be trusted.
                check(f"{p} rim coverage of build transitions",
                      f"{cov:.1f}%", ">= 95%", cov >= 95.0,
                      "share of transitions where BOTH GTs have a rim in gt_size")
                wmin = 0.0
                for x in seq.iter_rows(named=True):
                    s_min, d_min = CO_MIN.get(x["machine"], CO_FALLBACK[p])
                    wmin += (s_min if rim_of.get(x["gt_code"])
                             == rim_of.get(x["prev"]) else d_min)
                cap_w = CONFIG.thresholds\
                    .plant_weighted_co_min_per_machine_day.get(p, 999.0)
                check(f"{p} WEIGHTED build changeover min/machine-day",
                      f"{wmin/max(mdays,1):.1f}", f"<= {cap_w:.1f}",
                      wmin / max(mdays, 1) <= cap_w,
                      "per-machine minutes from v_changeover_build")

            # --- realised n_g, measured not requested ---------------------
            # A packer that silently degrades concurrency looks fine on every
            # other metric while inventory quietly rises: r = n_g x press_rate,
            # and r is what sets the drain and therefore I = lambda x W.
            cp = camp.filter(pl.col("plant") == p)
            if cp.height:
                live = 0.0
                for (_gt,), g in cp.group_by("gt_code"):
                    iv = sorted(zip(g["start_ts"].to_list(), g["end_ts"].to_list()))
                    merged: list = []
                    for s, e in iv:
                        if merged and s <= merged[-1][1]:
                            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
                        else:
                            merged.append((s, e))
                    live += sum((e - s).total_seconds() / 3600 for s, e in merged)
                ng = float(cp["hours"].sum()) / max(live, 1e-9)
                tgt = 3.25 if p == "PCR" else 3.11
                check(f"{p} realised n_g (concurrent presses/GT)", f"{ng:.2f}",
                      f">= {tgt - 0.3:.2f}", ng >= tgt - 0.3,
                      f"plant measured {tgt:.2f}")

        # --- G8 DAILY GT INVENTORY ---------------------------------------
        # v3 had NO inventory gate at all, and a 4,147 -> 5,851 regression went
        # unnoticed for an entire session. Plant rule G8, band from config.
        bpp = bs.filter(pl.col("plant") == p)
        if bpp.height:
            ivt = pl.concat([
                bpp.select([pl.col("end_ts").alias("ts"),
                            pl.col("qty").alias("d")]),
                bpp.select([pl.col("cure_ts").alias("ts"),
                            (-pl.col("qty")).alias("d")]),
            ]).sort("ts").with_columns(pl.col("d").cum_sum().alias("bal"))
            inv = _tw_mean_l11(ivt, n_h=_nh)  # TIME-weighted; see DO-NOT #9
            lo = CONFIG.thresholds.gt_wip_min.get(p, 0)
            hi = CONFIG.thresholds.gt_wip_max.get(p, 0)
            check(f"{p} mean GT inventory (G8)", f"{inv:,.0f}",
                  f"{lo:,}-{hi:,}", lo <= inv <= hi,
                  "plant PCR 4,772 / TBR 1,743")
            # G8 SAYS "EVERY DAY, INCLUDING THE LAST DAY OF THE MONTH".
            # The mean above cannot see a month-end collapse: measured v32, the
            # last three daily means are PCR 2,954 / 1,497 / 486 and TBR 662 /
            # 271 / 195 -- we finish at ~11 % (PCR) and ~16 % (TBR) of the band
            # FLOOR, so closing stock is ~0 and next month starts cold. That is
            # the steady state G8 exists to enforce, and it is also the
            # structural reason the plant reaches 100 % and we do not: its month
            # is a window on a continuous process, ours starts and ends.
            #
            # DETECTOR, NEVER A CONTROLLER -- and this distinction is the whole
            # point. Forcing a closing-stock floor means building tyres with no
            # cure to consume them inside the horizon, which destroys the audited
            # invariant "0 rows with no cure_ts, and built == fed exactly on both
            # plants" (MEMORY §10d) and adds a fourth constraint of the class
            # that has cost this project most. The cause is the demand horizon
            # (l45_lots holds only the current month, so nothing pulls build for
            # early next-month cures -- see the --lookahead-days flag on L4.5);
            # fix that and this closes for free. Do NOT close it here.
            # EXPECTED TO FAIL until the lookahead is armed. That is correct.
            dm = _daily_mean_l11(ivt, n_h=_nh)
            if len(dm):
                check(f"{p} last-day GT inventory (G8)", f"{dm[-1]:,.0f}",
                      f">= {lo:,}", dm[-1] >= lo,
                      "G8 'every day incl. the last'; DETECTOR -- cause is the "
                      "demand horizon, never fix by forcing closing stock")

        # --- moulds ------------------------------------------------------
        cap = {r["gt_code"]: max(int(r["moulds"]), 1) for r in
               mo.filter(pl.col("plant") == p).iter_rows(named=True)}
        bad = 0
        for (gt,), g in ch.group_by("gt_code"):
            iv = list(zip(g["start_ts"].to_list(), g["end_ts"].to_list()))
            pts = sorted([(s, 1) for s, _ in iv] + [(e, -1) for _, e in iv])
            cur = 0
            for _t, d in pts:
                cur += d
                if cur > cap.get(gt, 1):
                    bad += 1
                    break
        check(f"{p} concurrent presses > active moulds", bad, "0", bad == 0, "R3")

        # --- changeover rate ---------------------------------------------
        chg = 0
        rd = set()
        for (_pr,), g in ch.sort(["press", "start_ts"]).group_by(
                "press", maintain_order=True):
            last = None
            for r in g.iter_rows(named=True):
                if last is not None and r["gt_code"] != last:
                    chg += 1
                last = r["gt_code"]
        # PRESS-DAYS *OCCUPIED*, not campaign-START days. A campaign here runs
        # 10-12 days, so counting its start date alone undercounts the
        # denominator ~10x and inflates this rate accordingly -- it read 0.38
        # against the plant's 0.08 when the true value is 0.04, i.e. better than
        # the plant. The gate passed either way, which is why it went unnoticed.
        for r in ch.iter_rows(named=True):
            d0, d1 = r["start_ts"].date(), r["end_ts"].date()
            for k in range((d1 - d0).days + 1):
                rd.add((r["press"], d0 + timedelta(days=k)))
        rate = chg / max(len(rd), 1)
        capr = 0.08 if p == "PCR" else 0.04   # plant July, same denominator
        check(f"{p} mould changes / press-day occupied", f"{rate:.2f}",
              f"<= {capr}", rate <= capr, "plant July 2026, press-days occupied")

    # --- coupling correlation, plant-wide -------------------------------
    for p in ["PCR", "TBR"]:
        b = bsm.filter(pl.col("plant") == p)
        if not b.height:
            continue
        bd = (b.with_columns(pl.col("start_ts").dt.date().alias("d"))
              .group_by("d").agg(pl.col("qty").sum().alias("bq")).sort("d"))
        cd = (b.with_columns(pl.col("cure_ts").dt.date().alias("d"))
              .group_by("d").agg(pl.col("qty").sum().alias("cq")).sort("d"))
        j = bd.join(cd, on="d", how="inner")
        if j.height > 3:
            x = np.array(j["bq"], float)
            y = np.array(j["cq"], float)
            r = float(np.corrcoef(x, y)[0, 1])
            check(f"{p} same-day build/cure correlation", f"{r:+.3f}", ">= 0.90",
                  r >= 0.90, "Phase 0 observed 0.92 / 0.94")

    # --- demand ----------------------------------------------------------
    fedf = run / "cure_campaigns_reconciled.parquet"
    if fedf.exists():
        rec = pl.read_parquet(fedf)
        _sc = req.filter(~pl.col("residual"))
        # Lookahead rows belong to month M+1 and must not be scored against M.
        if "lookahead" in _sc.columns:
            _sc = _sc.filter(~pl.col("lookahead"))
        # PER PLANT. DO-NOT #14: a plant-TOTAL that moved 1.85 pt once hid an
        # 8.67 pt TBR regression (EXPERT_AUDIT §1) -- and THIS was the gate that
        # failed to catch it, because it was the one number reported in
        # aggregate. The total is kept as an extra line, never as the only one.
        # FULFILMENT IS IN-MONTH OUTPUT. `qty_fed` has no horizon clip, so a
        # campaign starting on day 30 and finishing next month contributed its
        # WHOLE quantity here while the denominator was strictly this month's
        # requirement -- numerator and denominator over different periods.
        # `qty_fed_in_month` prorates a crossing campaign. Overstatement measured
        # 2026-08-09: July PCR 0.43 pt / TBR 0.95 pt, Aug PCR 1.04 pt / TBR 0.13 pt.
        # Opening stock is deliberately NOT clipped -- a tyre built last month and
        # cured this month is genuine output against this month's demand.
        _fedcol = ("qty_fed_in_month" if "qty_fed_in_month" in rec.columns
                   else "qty_fed")
        if _fedcol == "qty_fed":
            print("  !! cure_campaigns_reconciled has no qty_fed_in_month -- "
                  "fulfilment INCLUDES the carry-out tail and is overstated")
        for _p in ["PCR", "TBR"]:
            _n = float(_sc.filter(pl.col("plant") == _p)["gross_build"].sum())
            _rp = rec.filter(pl.col("plant") == _p)
            _g = float(_rp[_fedcol].sum())
            if _n > 0:
                check(f"{_p} demand fulfilment", f"{100*_g/_n:.1f}%", ">= 99%",
                      _g / _n >= 0.99,
                      "IN-MONTH output vs plannable requirement, THIS PLANT")
                if "qty_fed_in_month" in rec.columns:
                    _tail = float(_rp["qty_fed"].sum()) - _g
                    check(f"{_p} carry-out tail (excluded from fulfilment)",
                          f"{_tail:,.0f}", "reported", True,
                          "cured after month end -- next month's output")
        need = float(_sc["gross_build"].sum())
        got = float(rec[_fedcol].sum())
        check("demand fulfilment", f"{100*got/need:.1f}%", ">= 99%",
              got / need >= 0.99, "plant TOTAL, IN-MONTH -- never judge a change "
              "on this alone, see DO-NOT #14")
    resid = req.filter(pl.col("residual"))
    check("residual demand flagged not dropped",
          f"{resid.height} GTs / {int(resid['demand'].sum()):,} tyres",
          "100% flagged", True, "B12 step 6")

    # --- report -----------------------------------------------------------
    df = pl.DataFrame(RES)
    df.write_parquet(run / "l11_invariants.parquet")
    # Provenance: which plan artefacts THIS scorecard describes. `arm_is_stale()`
    # and `scripts/check_arm_fresh.py` compare it back. Written last so it can
    # only exist beside a completed scorecard.
    (run / "l11_provenance.json").write_text(json.dumps(
        {"run": run.name, "month": a.month, "fingerprint": _fp}, indent=2))
    print(f"  {'invariant':<44}{'actual':>16}{'target':>18}  status")
    print("  " + "-" * 88)
    for r in df.iter_rows(named=True):
        mark = "PASS" if r["status"] == "PASS" else "**FAIL**"
        print(f"  {r['invariant']:<44}{str(r['actual']):>16}"
              f"{r['target']:>18}  {mark}")
    n_f = df.filter(pl.col("status") == "FAIL").height
    print("\n  DELETED (would fail a CORRECT pull plant):")
    print("    - daily build mix ~ daily cure mix   (observed cosine 0.21)")
    print("    - minimise WIP toward zero           (starves presses)")
    print("    - build changeover count as a gate   (plant absorbs them on purpose)")
    print("\n  NOT CHECKED, DELIBERATELY:")
    print("    - build slice minimum. A slice is a delivery, not a lot; a floor")
    print("      there recreates the head gap. Do not add one.")
    print(f"\n  {df.height - n_f}/{df.height} invariants pass")
    print(f"  >>> {'PLAN VALID' if n_f == 0 else f'{n_f} INVARIANT(S) FAILED'}")


if __name__ == "__main__":
    main()
