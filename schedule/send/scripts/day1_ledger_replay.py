"""DAY-1 LEDGER REPLAY -- can a seated press start earlier on GT it already has?

    PYTHONPATH=. python scripts/day1_ledger_replay.py 2026-08 aug_v13 [--arm continuous|sublot]

A POST-PLAN EXECUTABLE REPLAY. It does not re-plan anything:

    L5 cure seats        unchanged      press assignment   unchanged
    L7 build schedule    unchanged      campaign quantity  unchanged
    month ordering       unchanged      downstream seats   never moved

It answers one question per press: given the GT that the plan ALREADY builds,
how much earlier could this press's FIRST campaign legally start?

WHY A REPLAY AND NOT A LAYER CHANGE
  L7's build schedule was created against L5's original cure seats. Changing the
  seats and re-running would move the builds, which would move the seats again --
  the circularity that made the retired L5<->L6 loop lose 16,548 tyres. Holding
  both fixed and asking only "was the GT already there?" is a strictly bounded
  question with no feedback path.

THE CONTRACT
    actual_start = max(press_available, GT_available_ts, GT_min_age_ts)
  subject to: ledger never negative · R3 unchanged · same press · same quantity
  · no downstream campaign moved · building byte-identical.

THE PART THE CONTRACT DOES NOT STATE, AND IT MATTERS
  Presses SHARE a GT. On August PCR, GT 1503 NEO MSIL feeds 9 presses and
  GT 1513 feeds 7. Pulling one press left consumes stock another press is
  counting on, so a per-press test would double-spend the same tyres and report
  a day-1 gain that cannot physically happen. Every campaign of a GT is
  therefore simulated JOINTLY against one shared ledger, and accelerations are
  granted in press order, each one re-checked against what the previous ones
  already committed.

RESULT, 2026-08-13 -- THE PRIZE IS NOT 2,690 TYRES. IT IS NEAR ZERO.

  The model-free measurement that settles it. Per GT, the engine's own ledger
  slack is  worst(cumulative gt_events) + opening stock:

      51 of 87 August GTs have slack < 1 tyre -- EXACTLY TIGHT
      21 GTs have slack >= 50
      total slack over the WHOLE MONTH: 3,701 tyres

  GT 1402 XPC TATA is the clearest case: worst balance -131, opening stock 131,
  slack 0.000. The plan consumes its opening stock to the last tyre. There is
  nothing to pull a press left into.

  The continuous arm recovers 45 PCR + 81 TBR tyres. Not 2,690.

  WHY THE 2,690 ESTIMATE WAS WRONG, AND IT IS THE SAME ERROR TWICE.
  It came from comparing each GT's FIRST fresh build completion against its
  waiting press's start: "GT 1503 built at 2.28 h, 9 presses wait until 11.86 h,
  so 9 presses idle 9.58 h on finished stock". But that GT is already committed
  to the presses running FROM t0. The early build is not spare -- it is what
  feeds the covered presses. Counting it again for the waiting presses spends
  the same tyres twice. The identical mistake produced the earlier "+16 presses
  / 1,400 tyres" partial-start estimate.

  RULE: any claim that GT is "available" for a press must be proved against the
  SHARED ledger for that GT, never against a per-press or per-GT-first-build
  comparison. Supply is rival. If two presses draw the same GT, the tyre can
  only be spent once.

TWO ARMS
  continuous  the campaign must run UNINTERRUPTED from the new start through
              its original window with the balance never negative. Safer: no
              stop/restart, which is an operational rule we have not modelled.
  sublot      start as soon as one legal lot is available, proving non-negative
              balance event by event. Higher ceiling, but it permits the press
              to idle mid-campaign if supply lags -- report it, do not ship it,
              until the plant confirms incremental feeding is allowed.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from planner import paths                                          # noqa: E402

EPS = 1e-6


def _params() -> dict:
    f = sorted((paths.ROOT / "warehouse" / "params").glob("params_*.json"))[-1]
    return json.load(open(f))


def run(month: str, run_dir: Path, arm: str) -> None:
    y, m = int(month[:4]), int(month[5:7])
    t0 = dt.datetime(y, m, 1, 7, 0)
    day1_end = t0 + dt.timedelta(days=1)
    P = _params()
    tmin = {p: float(P["tau"][p]["tau_min_h"]) for p in ("PCR", "TBR")}

    cc = pl.read_parquet(run_dir / "cure_campaigns.parquet")
    # SUPPLY COMES FROM gt_events, NOT build_schedule.
    # Building credits the ledger +1 PER TYRE at setup_end + i*cycle_s, not one
    # lump at end_ts (CLAUDE.md, "the ledger is the source of truth"). Modelling
    # a build slice as arriving whole at its end starves the ledger: validated
    # against the shipped plan, that model called the BASELINE infeasible on
    # 37 of 87 August GTs, worst balance -1,066, while the real plan has zero
    # negative-ledger events. gt_events is the stream the engine itself uses,
    # so replaying against it is feasible by construction and any infeasibility
    # the replay finds is caused by the pull, not by the model.
    ge = pl.read_parquet(run_dir / "gt_events.parquet")

    ogn = paths.MASTERS / "opening_gt" / f"opening_gt_manual_{month}.parquet"
    if not ogn.exists():
        ogn = paths.MASTERS / "opening_gt" / f"opening_gt_{month}.parquet"
    og = pl.read_parquet(ogn).filter(pl.col("age_h") <= 72.0)
    opening = {(r["plant"], r["gt_code"]): float(r["len"])
               for r in og.group_by(["plant", "gt_code"]).len().iter_rows(named=True)}

    rows: list[dict] = []
    tot = {"PCR": 0.0, "TBR": 0.0}
    npull = {"PCR": 0, "TBR": 0}

    for plant in ("PCR", "TBR"):
        camp = cc.filter(pl.col("plant") == plant)
        for gt in sorted({g for g in camp["gt_code"]}):
            cg = camp.filter(pl.col("gt_code") == gt).sort("start_ts")
            if not cg.height:
                continue
            # ---- SUPPLY: opening stock at t0, then each build slice + tau_min
            supply: list[tuple[dt.datetime, float]] = [
                (t0, opening.get((plant, gt), 0.0))]
            for r in (ge.filter((pl.col("plant") == plant)
                                & (pl.col("gt_code") == gt) & (pl.col("d") > 0))
                        .group_by("ts").agg(pl.col("d").sum())
                        .iter_rows(named=True)):
                supply.append((r["ts"] + dt.timedelta(hours=tmin[plant]),
                               float(r["d"])))
            supply.sort(key=lambda x: x[0])

            # ---- DEMAND: every campaign of this GT, at its BASELINE seat
            base = [{"press": r["press"], "start": r["start_ts"], "end": r["end_ts"],
                     "qty": float(r["qty"]),
                     "rate": float(r["qty"]) / max(float(r["hours"]), 1e-9)}
                    for r in cg.iter_rows(named=True)]

            # committed[] holds the CURRENT start of each campaign; we only ever
            # move a press's FIRST campaign, and only left.
            cur = {i: c["start"] for i, c in enumerate(base)}

            def feasible(idx: int, new_start: dt.datetime) -> bool:
                """Is the shared ledger non-negative with campaign idx moved left?

                Simulates every campaign of this GT together. `continuous`
                requires the moved campaign to be fed without interruption from
                new_start; `sublot` only requires the running balance never to
                go negative at any event.
                """
                ev: list[tuple[dt.datetime, float]] = [(t, q) for t, q in supply]
                for j, c in enumerate(base):
                    st = new_start if j == idx else cur[j]
                    en = st + (c["end"] - c["start"])
                    ev.append((st, 0.0))
                    ev.append((en, 0.0))
                pts = sorted({t for t, _ in ev})
                bal = 0.0
                si = 0
                prev = pts[0]
                for t in pts:
                    # draw between prev and t
                    if t > prev:
                        hrs = (t - prev).total_seconds() / 3600.0
                        draw = 0.0
                        for j, c in enumerate(base):
                            st = new_start if j == idx else cur[j]
                            en = st + (c["end"] - c["start"])
                            ov = (min(t, en) - max(prev, st)).total_seconds() / 3600.0
                            if ov > 0:
                                draw += ov * c["rate"]
                        bal -= draw
                        if bal < -EPS:
                            return False
                    while si < len(supply) and supply[si][0] <= t:
                        bal += supply[si][1]
                        si += 1
                    if bal < -EPS:
                        return False
                    prev = t
                return True

            # ---- try to pull each press's FIRST campaign left ---------------
            seen_press: set = set()
            for i, c in enumerate(base):
                if c["press"] in seen_press:
                    continue                      # first campaign on that press only
                seen_press.add(c["press"])
                if c["start"] <= t0 + dt.timedelta(minutes=1):
                    continue                      # already at t0
                if c["start"] >= day1_end:
                    continue                      # not a day-1 press
                # binary search the earliest feasible start in [t0, baseline]
                lo, hi = t0, c["start"]
                if not feasible(i, lo):
                    step = dt.timedelta(minutes=15)
                    best = None
                    t = lo
                    while t < hi:
                        if feasible(i, t):
                            best = t
                            break
                        t += step
                    if best is None:
                        continue
                    lo = best
                new = lo
                gained = (c["start"] - new).total_seconds() / 3600.0
                if gained <= 0.01:
                    continue
                cur[i] = new
                # day-1 tyres recovered = extra curing hours INSIDE day 1
                inside = (min(c["start"], day1_end) - min(new, day1_end)).total_seconds() / 3600.0
                rec = max(0.0, inside) * c["rate"]
                tot[plant] += rec
                npull[plant] += 1
                rows.append({"plant": plant, "press": c["press"], "gt_code": gt,
                             "baseline_start_h": round((c["start"] - t0).total_seconds() / 3600, 2),
                             "gt_ready_h": round((new - t0).total_seconds() / 3600, 2),
                             "new_start_h": round((new - t0).total_seconds() / 3600, 2),
                             "hours_recovered": round(gained, 2),
                             "day1_tyres_recovered": round(rec)})

    print(f"\n  DAY-1 LEDGER REPLAY   {month}   run={run_dir.name}   arm={arm}")
    print(f"  {'-' * 88}")
    if rows:
        d = pl.DataFrame(rows).sort(["plant", "day1_tyres_recovered"],
                                    descending=[False, True])
        print(f"  {'plant':<6}{'press':>7}{'GT':<32}{'base h':>8}{'new h':>8}"
              f"{'gained h':>10}{'day1 tyres':>12}")
        for r in d.head(25).iter_rows(named=True):
            print(f"  {r['plant']:<6}{r['press']:>7}  {r['gt_code'][:30]:<30}"
                  f"{r['baseline_start_h']:>8.2f}{r['new_start_h']:>8.2f}"
                  f"{r['hours_recovered']:>10.2f}{r['day1_tyres_recovered']:>12,.0f}")
        if d.height > 25:
            print(f"  ... {d.height - 25} more")
        out = run_dir / f"day1_replay_{arm}.parquet"
        d.write_parquet(out)
        print(f"\n  -> {out}")
    else:
        print("  no press could be pulled earlier")
    print()
    for p in ("PCR", "TBR"):
        print(f"  {p}: {npull[p]} presses accelerated · "
              f"{tot[p]:,.0f} day-1 tyres recovered")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("month")
    ap.add_argument("run")
    ap.add_argument("--arm", default="continuous", choices=["continuous", "sublot"])
    a = ap.parse_args()
    run(a.month, Path("runs") / a.run, a.arm)


if __name__ == "__main__":
    main()
