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
from datetime import datetime, timedelta
import json
import os
from pathlib import Path

import numpy as np
import polars as pl

from planner.cmbc import holiday, r3_cap
from planner.config import CONFIG, GT_SHELF_LIFE_H, PRESS_ROSTER
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
    holiday.load(a.month)              # rule G3; empty = every call is identity
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
    # THE PLANT DAY STARTS AT 07:00 (rule S1) and every exported sheet buckets
    # on it. Derived once here so no invariant below re-derives it -- and so
    # nothing reaches for `.dt.date()`, which is the wall-clock day and a
    # different partition of the month. See the machine-day block below.
    t0 = datetime(int(a.month[:4]), int(a.month[5:7]), 1, 7, 0)

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
        # DEFECT 4ag -- p95 is a RELEASE statistic and must exclude the
        # OPENING_STOCK pseudo-rows for exactly the reason the R17 comment 20
        # lines below already spells out: those rows carry
        # start_ts == end_ts == t0, so their wait_h is measured from the
        # horizon start, not from when the tyre was actually built (last
        # month). 35 PCR / 32 TBR of them, 3,951 / 855 tyres. Including them
        # FLIPPED this gate: TBR read 28.20 h FAIL against a 28 h cap where
        # the released population is 27.90 h PASS. The independent verifier
        # (`verify_export.py`, re-derived from sheet 1, which has no
        # OPENING_STOCK rows) has been printing 27.8 h against L11's 28.2 h
        # for the whole project -- the pack contradicted itself.
        _rel = np.array(bs.filter((pl.col("plant") == p)
                                  & (pl.col("machine") != "OPENING_STOCK"))["wait_h"], float)
        if not len(_rel):
            _rel = w
        check(f"{p} GT wait p95", f"{np.percentile(_rel,95):.1f} h", "<= 28 h",
              np.percentile(_rel, 95) <= 28.0, "L0 observed p95, RELEASED rows only")
        # ---- R5 IS GRADED ON THE FIRST TYRE OF THE SLICE, NOT THE LAST -----
        #      A measured defect, found 2026-08-21.
        #   `wait_h` is `cure_ts - end_ts`: the wait of the LAST tyre off the
        #   drum. A slice is built continuously from `start_ts` to `end_ts`, so
        #   the FIRST tyre waits `wait_h + slice hours` -- and it is the first
        #   tyre that expires. The comment 250 lines below in l7's `_place`
        #   records the same error one level up ("checking t_last - run_end
        #   looked at the wrong endpoint"); it was fixed from the RUN to the
        #   SLICE and the slice's own span was left in.
        #
        #     grade at   Jul PCR   Jul TBR   Aug PCR   Aug TBR
        #     slice end    71.23     69.6     *63.27    *71.71
        #     first tyre   74.59     70.4     *65.58    *73.45
        #   * re-measured this session on a fresh August arm; the July column
        #     is QUOTED from the forensics report and was NOT re-run (no July
        #     arm exists -- the partition on disk is stamped 2026-08).
        #
        #   118 PCR (Jul) and 26 TBR (Aug) tyres are past the 72 h shelf life
        #   and the old form of this gate could not see any of them. 0.03 % of
        #   volume -- small, and a passing check that is not a correct check.
        #   NOT behind a flag: this is what the rule says. The PLAN is only made
        #   to honour it under PLANNER_L7_R5_FIRST_TYRE (l7, default off,
        #   measured) -- so expect this line to FAIL until that ships.
        _w1 = np.array(((bs.filter(pl.col("plant") == p)["cure_ts"]
                         - bs.filter(pl.col("plant") == p)["start_ts"])
                        .dt.total_seconds() / 3600.0), float)
        check(f"{p} GT wait max (R5, first tyre)", f"{_w1.max():.1f} h",
              f"<= {GT_SHELF_LIFE_H:.0f} h", _w1.max() <= GT_SHELF_LIFE_H,
              f"cure_ts - start_ts; graded at slice END this reads "
              f"{w.max():.1f} h")
        # THE COUNT, BECAUSE A MAX CANNOT BE ACTED ON. Tyres inside a slice are
        # built at a constant cadence, so the share of a slice that is already
        # past the shelf life at its cure is (wait_first - 72) / slice_span,
        # clipped to [0, 1]. This is the number that says whether a breach is a
        # rounding artefact or scrap.
        _bp5 = bs.filter(pl.col("plant") == p)
        _we5 = np.array(((_bp5["cure_ts"] - _bp5["end_ts"])
                         .dt.total_seconds() / 3600.0), float)
        _span5 = np.maximum(_w1 - _we5, 1e-12)
        _over5 = float((np.clip((_w1 - GT_SHELF_LIFE_H) / _span5, 0.0, 1.0)
                        * np.array(_bp5["qty"], float)).sum())
        check(f"{p} tyres past R5 shelf life", f"{_over5:,.0f}", "0",
              _over5 < 0.5, "per-tyre, pro-rated inside the slice")
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
        # ---- PLANT HOLIDAY (rule G3) -------------------------------------
        # THE BAND IS ABOUT CAMPAIGN LENGTH, NOT ELAPSED TIME. A campaign that
        # pauses over a closure has the SAME press-hours and a wall-clock span
        # 24 h longer, so grading `hours` would fail a campaign for a reason
        # that has nothing to do with how it was sized -- and on PCR, whose
        # band is 40-75 h, a single closed day is a third of the band. Grade
        # the working hours. Column arithmetic is untouched when no calendar is
        # configured, so this cannot move an existing result.
        if holiday.ACTIVE:
            _wh = [holiday.work_seconds(p, r["start_ts"], r["end_ts"]) / 3600.0
                   for r in ch.iter_rows(named=True)]
            ch = ch.with_columns(pl.Series("hours", _wh))
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
            # ---- THE MACHINE-DAY DENOMINATOR IS THE PLANT DAY, NOT THE
            #      WALL-CLOCK DATE -- a measured defect, found 2026-08-21.
            #   This was `pl.col("start_ts").dt.date()`. The plant day runs
            #   07:00 -> 07:00 and every exported sheet buckets on it
            #   (`plant_day` in export_shift_schedule.py, whose own docstring
            #   records that wall-clock labelling once mislabelled 28.7 % of
            #   build rows). A calendar date splits the C shift in two, so a
            #   machine running through 07:00 was counted as TWO machine-days
            #   and every `per machine-day` rate below it read LOW.
            #
            #     machine-days     PCR Jul  TBR Jul  PCR Aug  TBR Aug
            #     calendar (old)      351      281     *355     *283
            #     plant-day (now)     345      278     *343     *272
            #   * re-measured this session on a fresh August arm. The July
            #     column is QUOTED from the forensics report and was NOT
            #     re-run -- no July arm exists, the partition on disk is
            #     stamped 2026-08 and must not be rebuilt.
            #
            #   Understated by 1.7-4.0 %. IT FLIPS A GATE: August PCR WEIGHTED
            #   build changeover reads 73.6 PASS on 355 calendar days and
            #   76.2 FAIL on 343 plant-days, against the 74.0 plant benchmark.
            #   The shipped pack's "32 PASS of 50" for August is really 31.
            #   NOT behind a flag -- a denominator is either the one the rest of
            #   the pack uses or it is wrong, and there is no arm in which the
            #   old one is the right answer.
            _dnum = ((pl.col("start_ts") - pl.lit(t0)).dt.total_seconds()
                     // 86400).alias("d")
            mdays = (bp.with_columns(_dnum)
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

        # --- DAILY CURE vs THE PRESS FLEET --------------------------------
        # NOTHING GRADED THIS, AND THE PACK SHIPPED AN IMPOSSIBLE DAY.
        # Measured 2026-08-21 on the shipped August pack: `7_daily_summary.cured`
        # read 16,696 on plant-day 3, which needs 2,389 press-hours against a
        # fleet of 86 x 24 = 2,064 -- 115.7 % of every press in the plant. Four
        # days were over 100 % (16,696 / 15,482 / 15,001 / 14,749).
        #
        # The PLAN was feasible -- the campaign-prorated curve peaks at 14,286 =
        # 99.1 % and never breaches. What was wrong is that `cured` buckets
        # tyres on `cure_ts` with no capacity constraint, and no invariant
        # existed to notice. A supervisor reads that column as a daily target.
        #
        # Graded on the SAME basis the pack publishes (`cure_ts` bucketing on
        # the plant-day) so the gate sees what the reader sees. The denominator
        # is derived from this run's own campaigns -- tyres per press-hour x
        # roster x 24 h -- never a mined constant. It is a physical ceiling, so
        # 100 % is the target, not a tuned threshold.
        try:
            _ccf = pl.read_parquet(run / "cure_campaigns.parquet").filter(
                pl.col("plant") == p)
            _pph = float((_ccf["end_ts"] - _ccf["start_ts"])
                         .dt.total_seconds().sum()) / 3600.0 if _ccf.height else 0.0
            _rate = (float(_ccf["qty"].sum()) / _pph) if _pph > 0 else 0.0
            _capd = _rate * PRESS_ROSTER.get(p, 0) * 24.0
            _mend = t0 + timedelta(hours=_nh)      # plant month end, 07:00
            # GRADE THE PHYSICAL CURVE, NOT THE STAMP. `cure_ts` stamps a whole
            # slice at one instant, so bucketing on it invents peaks that no
            # press schedule contains -- Aug PCR day 3 stamped 16,696 while the
            # presses ran 1,944 h = 13,589. Spread each campaign over the hours
            # its press is actually occupied inside the plant-day; that is what
            # the plant experiences and what sheet 7's `cured` now publishes.
            _nd = int(_nh // 24)
            _dq = [0.0] * (_nd + 1)
            for _r in _ccf.iter_rows(named=True):
                _sp, _ep = _r["start_ts"], _r["end_ts"]
                _sec = (_ep - _sp).total_seconds()
                if _sec <= 0:
                    continue
                _q = float(_r["qty"])
                _d0 = int((_sp - t0).total_seconds() // 86400)
                _d1 = int((_ep - t0 - timedelta(microseconds=1)).total_seconds() // 86400)
                for _d in range(max(_d0, 0), min(_d1, _nd - 1) + 1):
                    _a = max(_sp, t0 + timedelta(days=_d))
                    _b = min(_ep, t0 + timedelta(days=_d + 1))
                    if _b > _a:
                        _dq[_d] += _q * (_b - _a).total_seconds() / _sec
            if _capd > 0 and any(_dq):
                _mx = max(_dq)
                _nov = sum(1 for _v in _dq if _v > _capd)
                check(f"{p} peak daily cure vs press fleet",
                      f"{_mx:,.0f} = {100*_mx/_capd:.1f}% of {_capd:,.0f}",
                      "<= 100%", _mx <= _capd,
                      f"{_nov} day(s) over the whole fleet -- PHYSICAL, "
                      "sheet 7 'cured' is what the plant reads")
        except Exception as _exc:                                # noqa: BLE001
            # NEVER SILENT. A capacity gate that cannot run must say so --
            # a swallowed exception here reads exactly like a passing check.
            print(f"  [l11] !! peak-daily-cure check could not run for {p}: {_exc}")

        # --- moulds ------------------------------------------------------
        # SAME DERIVATION L5 PLANNED TO. `cap_mould.moulds` counts mould HALVES
        # and the plant ruling is 2 per press; the divisor lives in r3_cap and
        # nowhere else. Grading against the raw `moulds` while l5 seats against
        # `moulds / 2` is the duplicated-constant defect PARTITION §1g records --
        # the invariant would pass by construction for any divisor.
        cap = {k[1]: v for k, v in r3_cap.table(
            mo.filter(pl.col("plant") == p).iter_rows(named=True)).items()}
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
                _d = d0 + timedelta(days=k)
                # HOLIDAY: a paused campaign still HOLDS the press across the
                # closure, but the plant is shut -- counting the closed day as
                # an occupied press-day inflates the denominator and makes the
                # changeover rate look better than it is. Fourth denominator
                # defect in this project if left in (PARTITION §1).
                if holiday.ACTIVE and holiday.is_blocked(
                        p, datetime(_d.year, _d.month, _d.day, 12, 0)):
                    continue
                rd.add((r["press"], _d))
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
        # LOOKAHEAD MUST LEAVE BOTH SIDES OF THE RATIO, NOT ONE.
        # `_sc` is filtered above (~lookahead) so the DENOMINATOR excludes
        # next-month GTs. The NUMERATOR reads `rec`, which still contains their
        # cure campaigns -- so arming --lookahead-days would raise fulfilment by
        # construction, with no extra tyre made. Sixth-and-a-half instance of the
        # basis class; caught before the flag was ever measured.
        _la_gts: set = set()
        if "lookahead" in req.columns:
            _la_gts = {(r["plant"], r["gt_code"])
                       for r in req.filter(pl.col("lookahead")).iter_rows(named=True)}
        # AND CAP EACH GT AT ITS OWN IN-MONTH REQUIREMENT.
        # Dropping lookahead-ONLY GTs is not enough: a GT demanded in BOTH months
        # keeps its next-month cures in the numerator while `gross_build_la` has
        # already removed them from the denominator -- output counted once,
        # requirement counted zero times. Measured 2026-08-14, July + 3 d
        # lookahead: PCR read 98.4 % with 9,169 of its 388,262 cures (2.4 %)
        # ABOVE July's own demand, TBR 97.8 % with 3,312 (3.4 %). Capped, the
        # real figures are PCR +338 tyres and TBR -1,853 against the base.
        # A cure beyond this month's requirement for that GT serves NEXT month;
        # it is real output and belongs in BUILT, never in this ratio.
        _cap = {}
        if "gross_build" in req.columns:
            for _r in req.iter_rows(named=True):
                _gb = float(_r["gross_build"] or 0.0) - float(
                    _r.get("gross_build_la") or 0.0)
                _cap[(_r["plant"], _r["gt_code"])] = max(_gb, 0.0)
        if _cap and _fedcol in rec.columns:
            rec = rec.with_columns(
                pl.struct(["plant", "gt_code"]).map_elements(
                    lambda x: _cap.get((x["plant"], x["gt_code"]), 1e18),
                    return_dtype=pl.Float64).alias("_cap"))
            _byg = (rec.group_by(["plant", "gt_code"])
                    .agg(pl.col(_fedcol).sum().alias("_tot"),
                         pl.col("_cap").first()))
            _over = float((_byg["_tot"] - _byg["_cap"]).clip(lower_bound=0).sum())
            if _over > 0:
                _sc_f = (_byg.with_columns(
                    (pl.min_horizontal(pl.col("_tot"), pl.col("_cap"))
                     / pl.col("_tot").clip(lower_bound=1e-9)).alias("_f"))
                    .select(["plant", "gt_code", "_f"]))
                rec = (rec.join(_sc_f, on=["plant", "gt_code"], how="left")
                       .with_columns((pl.col(_fedcol)
                                      * pl.col("_f").fill_null(1.0)).alias(_fedcol)))
                print(f"  fulfilment numerator capped at this month's own "
                      f"requirement: {_over:,.0f} tyres of NEXT-month cure "
                      f"excluded (they are real output, counted in BUILT)")
            rec = rec.drop([c for c in ("_cap", "_f") if c in rec.columns])
        if _la_gts:
            rec = rec.filter(~pl.struct(["plant", "gt_code"]).map_elements(
                lambda x: (x["plant"], x["gt_code"]) in _la_gts,
                return_dtype=pl.Boolean))
            print(f"  lookahead: {len(_la_gts)} next-month GTs excluded from the "
                  f"fulfilment NUMERATOR as well as the denominator")
        _tot_n = 0.0
        _tot_g = 0.0
        for _p in ["PCR", "TBR"]:
            _sp = _sc.filter(pl.col("plant") == _p)
            # Subtract the lookahead QUANTITY, do not drop whole GTs -- a GT
            # demanded in both months belongs partly to each.
            # NUMERATOR AND DENOMINATOR MUST COUNT THE SAME TYRES.
            # `_g` below is `qty_fed[_in_month]` -- green tyres DELIVERED INTO
            # PRESSES, which includes the opening floor stock. `gross_build`
            # EXCLUDES it: it is what building must make after opening stock is
            # netted off. Dividing one by the other overstated fulfilment by the
            # whole opening-stock term -- +1.16 / +1.20 pt (Jul PCR/TBR) and
            # +0.94 / +0.94 (Aug), on every arm this project has ever scored.
            # The ratio also cancelled `cure_yield` out entirely, so the R15
            # gross-up appeared in no reported number.
            # `cure_requirement` is the same universe as `qty_fed`: green tyres
            # the presses must consume, opening stock included.
            _dcol = ("cure_requirement" if "cure_requirement" in _sp.columns
                     else "gross_build")
            _n = float(_sp[_dcol].sum())
            _lacol = _dcol + "_la"
            if _lacol in _sp.columns:
                _n -= float(_sp[_lacol].sum())
            elif "gross_build_la" in _sp.columns:
                _n -= float(_sp["gross_build_la"].sum())
            _rp = rec.filter(pl.col("plant") == _p)
            _g = float(_rp[_fedcol].sum())
            _tot_n += _n
            _tot_g += _g
            if _n > 0:
                check(f"{_p} demand fulfilment", f"{100*_g/_n:.1f}%", ">= 99%",
                      _g / _n >= 0.99,
                      "IN-MONTH output vs plannable requirement, THIS PLANT")
                if "qty_fed_in_month" in rec.columns:
                    _tail = float(_rp["qty_fed"].sum()) - _g
                    check(f"{_p} carry-out tail (excluded from fulfilment)",
                          f"{_tail:,.0f}", "reported", True,
                          "cured after month end -- next month's output")
        # DEFECT 4af -- the plant TOTAL was the ONE line the 2026-08-14
        # denominator fix above did not reach. It divided an
        # opening-stock-INCLUSIVE numerator (`qty_fed_in_month`) by an
        # opening-stock-EXCLUSIVE denominator (`gross_build` = requirement
        # MINUS opening stock) and skipped the look-ahead subtraction, so the
        # total came out ABOVE BOTH of its own components -- Jul BASE printed
        # PCR 94.8 / TBR 96.1 / TOTAL 96.2. A weighted mean cannot exceed both
        # parts; that impossibility is the proof, no arithmetic needed.
        # Orphaned opening-stock term: 6,071 tyres, +1.17 pp overstated.
        # Now it is literally the sum of the per-plant pairs, so it is bounded
        # by them by construction and can never drift from them again.
        need = _tot_n
        got = _tot_g
        if need > 0:
            check("demand fulfilment", f"{100*got/need:.1f}%", ">= 99%",
                  got / need >= 0.99, "plant TOTAL = sum of the per-plant "
                  "numerators/denominators -- never judge a change on this "
                  "alone, see DO-NOT #14")
    resid = req.filter(pl.col("residual"))
    check("residual demand flagged not dropped",
          f"{resid.height} GTs / {int(resid['demand'].sum()):,} tyres",
          "100% flagged", True, "B12 step 6")

    # ---- PLANT-OBSERVED CEILING -- never measure ourselves against ourselves
    #
    # THE ERROR THIS PREVENTS, made 2026-08-14 and repeated across a whole
    # session. The press ceiling was computed as
    #     roster x 744 h - mould hours,  x  OUR OWN campaign rate (qty/hours)
    # and July was declared "over-committed by 502 tyres, physically infeasible".
    # It is not. That rate is a property of OUR plan's GT mix and cycle times, so
    # the test asks "can our plan achieve our plan's rate" -- circular, and it
    # silently converts every scheduling shortfall into a capacity excuse.
    #
    # `l3_cavities.parquet` holds what the plant's OWN presses demonstrably do:
    #     PCR 13,179 tyres/day median over 86 presses  -> 408,549 / 31 d
    #     TBR  3,403 tyres/day median over 79 presses  -> 105,493 / 31 d
    # against July demand of 398,405 / 98,020. There is ~10,000 PCR of headroom,
    # not a deficit. Print BOTH the plant rate and ours on every run so the
    # comparison is never reconstructed by hand again.
    _ROSTER = dict(PRESS_ROSTER)          # plant file, see config.py
    try:
        _cav = pl.read_parquet(D / "l3_cavities.parquet")
        _cpf = pl.read_parquet(D / f"cap_press_{a.month}.parquet")
    except Exception:
        _cav = None
    if _cav is not None:
        _y2, _m2 = int(a.month[:4]), int(a.month[5:7])
        _nd2 = calendar.monthrange(_y2, _m2)[1]
        for _p in ("PCR", "TBR"):
            _use = set(_cpf.filter(pl.col("plant") == _p)["press"].unique().to_list())
            _c = (_cav.filter((pl.col("plant") == _p)
                              & pl.col("press").is_in(list(_use)))
                  .sort("tyres_per_day", descending=True).head(_ROSTER[_p]))
            if not _c.height:
                continue
            _pl_day = float(_c["tyres_per_day"].sum())
            _ceil = _pl_day * _nd2
            _need = float(req.filter((pl.col("plant") == _p)
                                     & ~pl.col("residual"))["demand"].sum())
            _got = float(rec.filter(pl.col("plant") == _p)[_fedcol].sum())                 if _fedcol in rec.columns else 0.0
            check(f"{_p} demand within plant-observed ceiling",
                  f"{_need:,.0f} vs {_ceil:,.0f}", "<= ceiling", _need <= _ceil,
                  "l3_cavities = the plant's OWN per-press output. NEVER derive "
                  "a ceiling from our own campaign rate -- that is circular")
            check(f"{_p} throughput vs plant per-press rate",
                  f"{_got / _nd2:,.0f}/day vs plant {_pl_day:,.0f}/day "
                  f"({100 * (_got / _nd2) / _pl_day:.1f}%)",
                  ">= 95% of plant", (_got / _nd2) / _pl_day >= 0.95,
                  "same 86/79 presses -- any gap here is OURS, not capacity")

    # ---- THE CARRY METER -- the boundary debt, in HOURS OF PRODUCTION -------
    #
    # WHY THIS EXISTS. A rolling boundary contract over an over-committed month
    # does not settle, it accumulates. Measured on the Jul->Aug->Sep chain:
    #     Jul -> Aug   PCR  7,899   TBR 2,256
    #     Aug -> Sep   PCR 24,110   TBR 4,093     <- 3.05x, and WITHOUT carry-in
    # The debt is August being over-committed by ~25,000 tyres; carry-in adds
    # ~11 % on top of it, it does not create it. Nothing detected this, and
    # l4b_capacity_flow is building-only so it structurally cannot.
    #
    # WHY HOURS, NOT A RATIO. `carry_out <= 1.25 x carry_in` breaks at the head
    # of a chain -- the first month has carry_in = 0, so the test is either
    # vacuous or a divide-by-zero -- and a ratio never says HOW BAD. Hours of
    # production is comparable across plants and months and is the unit the
    # constraint is actually in.
    #
    # WHY THE TAIL IS THE THRESHOLD, and why this is not an always-failing guard
    # (EXPERT_AUDIT's fourth failure mode). The plan runs [t0, month_end + T]
    # with T = HORIZON_TAIL_H. A carried campaign is representable in the next
    # month's carry-in only if it fits inside that tail. So carry-out > T is a
    # genuine correctness bound, not a preference -- past it the contract breaks
    # silently. Measured today: Jul->Aug 14.6 h PCR / 17.1 h TBR, Aug->Sep 48.2 h
    # / 39.2 h. Both months PASS against a 72 h tail, and August at 48.2 h --
    # two-thirds of the entire tail handed over before September plans anything
    # -- is exactly the number that should be on the scorecard.
    #
    # SHIPPED BEFORE the Rule-T numerator fix, deliberately: once carried cures
    # count toward fulfilment, this debt starts reading as output. Ship the meter
    # before the thing it measures.
    _tail_h = float(os.environ.get("PLANNER_HORIZON_TAIL_H", "72"))
    # PLANT RULING 2026-08-14: roster is 86 PCR / 79 TBR, every month.
    _ROSTER = dict(PRESS_ROSTER)          # plant file, see config.py
    _cof = run / "carry_out.parquet"
    if _cof.exists() and _tail_h > 0:
        _co = pl.read_parquet(_cof)
        if "busy_at_t0" in _co.columns:
            _co = _co.filter(pl.col("busy_at_t0"))
        for _p in ["PCR", "TBR"]:
            _cp = camp.filter(pl.col("plant") == _p)
            if not _cp.height:
                continue
            # Rate from THIS run's own realised mix, not from the fast early
            # days: PCR runs 6.55 t/press-h on days 2-5 but 6.31 over the month
            # (L5 sorts by -qty and PCR's biggest GTs are also its fastest), and
            # extrapolating the head rate overstates the ceiling by ~15,000.
            _rate_pph = float(_cp["qty"].sum()) / max(float(_cp["hours"].sum()), 1e-9)
            _plant_th = _rate_pph * _ROSTER[_p]
            _q = float(_co.filter(pl.col("plant") == _p)["qty"].sum())
            _h = _q / max(_plant_th, 1e-9)
            check(f"{_p} carry-out debt (production handed forward)",
                  f"{_h:.1f} h  ({_q:,.0f} tyres @ {_plant_th:,.0f}/h)",
                  f"<= {_tail_h:.0f} h tail", _h <= _tail_h,
                  "a carried campaign is only representable in the next month's "
                  "carry-in if it fits the horizon tail; past that the rolling "
                  "contract breaks silently. Rising month-on-month = debt spiral")
    # ---- OPENING STOCK THAT EXPIRED UNUSED ---------------------------------
    #
    # WHAT THIS GRADES / WHY IT EXISTS -- a measured defect, found 2026-08-21
    #   The month opens with green tyres on the floor. They are already built,
    #   they are inside R5 on arrival (August age p50 6-15 h, max 55.9 h, ZERO
    #   rows over 72 h) and every one the plan does not cure is a tyre building
    #   has to make again. August: PCR held 5,132, drew 3,453, EXPIRED 1,679;
    #   TBR held 1,266, drew 794, EXPIRED 472. July: 869 / 442. Nothing in the
    #   engine graded it and the one L7 line that named it could not fire (it
    #   compared the residual against the draw -- see the fixed guard in
    #   l7_pull_release). Four always-passing guards and a silent 3,462-tyre loss
    #   have the same root cause: nobody grades what nobody prints.
    #
    # THE POPULATION IS THE ADDRESSABLE ONE, and that choice is the whole
    # invariant. Stock sitting on a GT with NO cure campaign this month is a
    # DEMAND fact -- there is nothing to cure it into and no placement change can
    # reach it (August 656 of 1,679 PCR, 240 of 472 TBR, i.e. ~40 % of the
    # headline). Grading the total would make this a gate on the order book that
    # the planner can never pass, which is EXPERT_AUDIT's fourth failure mode
    # (an always-failing guard) and just as useless as an always-passing one.
    #
    # WHY 0 IS REACHABLE AND NOT ASPIRATIONAL. Every GT in this population has a
    # cure campaign in the plan; the only thing wrong with it is WHEN. A plan
    # that seats each such GT's first campaign inside its own stock's remaining
    # shelf life scores 0 here, and the campaign is placed by L5's greedy, which
    # is free to order its queue any way it likes. The count is `qty`, not a
    # share, so it is comparable across arms and months and cannot be flattered
    # by a denominator (DO-NOT #32).
    _ogf = paths.opening_gt(a.month)
    if _ogf.exists():
        _og = pl.read_parquet(_ogf).filter(pl.col("age_h") <= GT_SHELF_LIFE_H)
        _stk = (_og.group_by(["plant", "gt_code"])
                .agg(pl.len().alias("held")))
        _drawn = (bs.filter(pl.col("machine") == "OPENING_STOCK")
                  .group_by(["plant", "gt_code"]).agg(pl.col("qty").sum().alias("drawn")))
        _planned = camp.select(["plant", "gt_code"]).unique().with_columns(
            pl.lit(True).alias("has_campaign"))
        _x = (_stk.join(_drawn, on=["plant", "gt_code"], how="left")
              .with_columns(pl.col("drawn").fill_null(0.0))
              .with_columns((pl.col("held") - pl.col("drawn")).alias("expired"))
              .join(_planned, on=["plant", "gt_code"], how="left")
              .with_columns(pl.col("has_campaign").fill_null(False)))
        for _p in ["PCR", "TBR"]:
            _xp = _x.filter(pl.col("plant") == _p)
            if not _xp.height:
                continue
            _held = float(_xp["held"].sum())
            _exp = float(_xp["expired"].sum())
            _addr = float(_xp.filter(pl.col("has_campaign"))["expired"].sum())
            _ngt = _xp.filter(pl.col("has_campaign") & (pl.col("expired") > 0.5)).height
            check(f"{_p} opening stock expired on a planned GT",
                  f"{_addr:,.0f} of {_held:,.0f} held  ({_ngt} GTs)",
                  "0 tyres", _addr < 0.5,
                  "already-built green inside R5 at t0 whose GT the plan DOES "
                  "cure, but not before the stock expires. Building has to make "
                  f"these again. Total expired incl. undemanded GTs: {_exp:,.0f}")

    _cif = Path(os.environ.get("PLANNER_CARRY_IN", "")) if os.environ.get(
        "PLANNER_CARRY_IN") else (ROOT / "masters" / "carry_in"
                                  / f"carry_in_{a.month}.parquet")
    if _cif.exists():
        _ci = pl.read_parquet(_cif)
        if "busy_at_t0" in _ci.columns:
            _ci = _ci.filter(pl.col("busy_at_t0"))
        for _p in ["PCR", "TBR"]:
            _cp = camp.filter(pl.col("plant") == _p)
            if not _cp.height:
                continue
            _rate_pph = float(_cp["qty"].sum()) / max(float(_cp["hours"].sum()), 1e-9)
            _plant_th = _rate_pph * _ROSTER[_p]
            _q = float(_ci.filter(pl.col("plant") == _p)["qty"].sum())
            check(f"{_p} carry-in debt (production received)",
                  f"{_q / max(_plant_th, 1e-9):.1f} h  ({_q:,.0f} tyres)",
                  "reported", True,
                  "the other half of the meter -- compare with LAST month's "
                  "carry-out debt; they must be the same number")

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
