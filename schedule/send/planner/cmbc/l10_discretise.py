"""L10 -- TIME DISCRETISATION & SEQUENCING.

    python -m planner.cmbc.l10_discretise --month 2026-07

R13 · plant step 13.

WHY DISCRETISE HERE AND NOT EARLIER
  L5 through L7 plan in continuous time because the pull equation is continuous:
  release = t_cure - tau* - build_duration. Rounding that to shifts before the
  coupling is solved would quantise tau* itself and destroy the buffer the whole
  architecture is built around.
  But the floor runs on shifts (A 07-15, B 15-23, C 23-07), so the plan has to
  land on that grid before it can be issued. Discretising last -- rather than
  optimising at campaign aggregate and bolting time on at the end, which the old
  Phase 7 did -- means the shift plan inherits a schedule already known feasible.

MOULD CHANGES ARE THE THING BEING SLOTTED
  A mould change costs PCR 210-430 min and TBR 361 min: 3.5-7 press-hours each,
  which dwarfs every build changeover (10-60 min). Where it lands matters as much
  as how many there are -- a change spanning a shift boundary occupies two crews
  instead of one.

R13 -- CREW IS A SHARED FINITE RESOURCE
  Each change needs 2 fitters (same size) or 3 (different size), from the plant's
  own changeover master. Crew is shared across every press in the plant, so two
  simultaneous changes need two crews. This is the constraint that is
  "universally forgotten" (architecture §6) and it is computed here.

  We can compute crew DEMAND. We cannot yet check it against capacity: GAP-4 gave
  us crew SIZE per change but no roster, so the shift-by-shift crew availability
  is unknown. Demand is reported and the check is named as blocked rather than
  quietly assumed satisfied.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import polars as pl
from planner import paths

ROOT = Path(__file__).resolve().parent.parent.parent
D = ROOT / "warehouse" / "derived"
SHIFTS = ["A", "B", "C"]
SHIFT_H = 8
DAY_START_H = 7                       # the plant day starts at 07:00, not midnight


def shift_of(ts: datetime, t0: datetime) -> tuple[int, str]:
    h = (ts - t0).total_seconds() / 3600.0
    day = int(h // 24)
    k = int((h % 24) // SHIFT_H)
    return day + 1, SHIFTS[min(k, 2)]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", default="2026-07")
    ap.add_argument("--run", default=None)
    a = ap.parse_args()
    run = ROOT / "runs" / (a.run or f"cmbc_{a.month}")

    camp = pl.read_parquet(run / "cure_campaigns.parquet")
    bs = pl.read_parquet(run / "build_schedule.parquet").filter(
        pl.col("machine") != "OPENING_STOCK")
    chg = pl.read_parquet(D / "cap_changeover.parquet")
    pmc = pl.read_parquet(D / "press_mould_change.parquet")
    sz = pl.read_parquet(paths.INPUT_DERIVED / "gt_size.parquet")
    rim = {r["gt_code"]: r["rim"] for r in sz.iter_rows(named=True)
           if r.get("gt_code")}

    y, m = int(a.month[:4]), int(a.month[5:7])
    t0 = datetime(y, m, 1, DAY_START_H, 0)
    mch = {p: float(pmc.filter(pl.col("plant") == p)["mould_change_min"].median())
           for p in ["PCR", "TBR"]}
    mch_press = {r["wc_id"]: float(r["mould_change_min"])
                 for r in pmc.iter_rows(named=True) if r.get("wc_id")}
    crew_same = {r["plant"]: int(r["crew"]) for r in chg.iter_rows(named=True)}

    print("=" * 92)
    print(f"L10  TIME DISCRETISATION & SEQUENCING  --  {a.month}   (R13)")
    print("=" * 92)
    print(f"  day starts {DAY_START_H:02d}:00 · shifts A 07-15 · B 15-23 · C 23-07")
    print(f"  mould change PCR {mch['PCR']:.0f} min  TBR {mch['TBR']:.0f} min\n")

    # ---- discretise both stages ----------------------------------------
    # SPREAD ACROSS OCCUPIED SHIFTS, do not bucket by start.
    # A cure campaign runs 135-160 h and therefore spans ~20 shifts. Attributing
    # its whole quantity to the shift it STARTS in put 221,825 tyres in one
    # bucket and left 41 of 93 shifts empty -- peak/p50 read 93x (PCR) and 126x
    # (TBR) while build, whose slices fit inside a shift, read 1.1x. The ratio
    # was measuring the bucketing, not the plan.
    def spread(df):
        rows = []
        for r in df.iter_rows(named=True):
            st, en = r["start_ts"], r["end_ts"]
            total = max((en - st).total_seconds(), 1.0)
            cur = st
            while cur < en:
                dd, ss = shift_of(cur, t0)
                # end of this shift bucket
                h = (cur - t0).total_seconds() / 3600.0
                nxt = t0 + timedelta(hours=(int(h // SHIFT_H) + 1) * SHIFT_H)
                seg = min(nxt, en)
                frac = max((seg - cur).total_seconds(), 0.0) / total
                rows.append({**{k: r[k] for k in
                                ("plant", "gt_code", "press") if k in r},
                             "day": dd, "shift": ss,
                             "qty": float(r["qty"]) * frac,
                             "hours": (seg - cur).total_seconds() / 3600.0})
                cur = seg
        return pl.DataFrame(rows)

    def bucket(df, tcol):
        d, sh = [], []
        for ts in df[tcol].to_list():
            dd, ss = shift_of(ts, t0)
            d.append(dd)
            sh.append(ss)
        return df.with_columns(pl.Series("day", d), pl.Series("shift", sh))

    cb = spread(camp)                      # long campaigns -> many shifts
    bb = bucket(bs, "start_ts")            # build slices fit inside one shift
    # ---- REPORT ONLY THE MONTH (PLANNER_HORIZON_MODE=extend) --------------
    # L5 may PLAN past hour 744 so the presses are not tapered at the boundary
    # and building keeps a downstream pull to the last hour. None of that work is
    # this month's output, so it is dropped HERE -- at the single point where the
    # continuous plan becomes the shift grid the floor is issued from.
    # `spread()` has already cut each campaign into per-shift pieces, so the
    # in-month portion of a campaign that crosses the boundary survives intact
    # and only the pieces past day `ndays` are dropped. That is the same
    # in-month/out-of-month split L7 applies to fulfilment, on the same
    # boundary, so the export and the KPI cannot disagree.
    #
    # BUILD IS CUT ON end_ts, NOT ON `day`. A slice keeps its real start/end on
    # the exported sheet (the cure sheet's times are synthesised shift windows),
    # so a slice that STARTS on day 31 and finishes after hour 744 would print a
    # timestamp outside the month -- which `scripts/verify_export.py` fails HARD,
    # correctly. Whole-slice is also the ledger's own convention: `gt_events`
    # credits a slice whole at `end_ts`, so a slice finishing next month is next
    # month's green tyre and is neither exported here nor carried forward.
    ndays = (datetime(y + (m == 12), (m % 12) + 1, 1) - datetime(y, m, 1)).days
    _mend = t0 + timedelta(days=ndays)
    _cb_all, _bb_all = cb.height, bb.height
    cb = cb.filter(pl.col("day") <= ndays)
    bb = bb.filter((pl.col("day") <= ndays) & (pl.col("end_ts") <= pl.lit(_mend)))
    if _cb_all != cb.height or _bb_all != bb.height:
        print(f"  REPORT WINDOW: days 1-{ndays} only. Dropped "
              f"{_cb_all - cb.height} cure and {_bb_all - bb.height} build "
              f"shift-rows planned past the month boundary (next month's work).")
    cb.write_parquet(run / "cure_by_shift.parquet")
    bb.write_parquet(run / "build_by_shift.parquet")

    print("  SHIFT PLAN")
    print(f"  {'plant':<6}{'stage':<8}{'shifts used':>13}{'per shift p50':>15}"
          f"{'peak':>9}{'peak/p50':>10}")
    for p in ["PCR", "TBR"]:
        for lbl, df in [("cure", cb), ("build", bb)]:
            s = df.filter(pl.col("plant") == p)
            if not s.height:
                continue
            g = s.group_by(["day", "shift"]).agg(pl.col("qty").sum().alias("q"))
            v = np.array(g["q"], float)
            print(f"  {p:<6}{lbl:<8}{g.height:>13}{np.percentile(v,50):>15,.0f}"
                  f"{v.max():>9,.0f}{v.max()/max(np.percentile(v,50),1):>9.1f}x")

    # ---- mould-change events -------------------------------------------
    ev = []
    for (pr,), g in camp.sort(["press", "start_ts"]).group_by("press",
                                                              maintain_order=True):
        last = None
        for r in g.iter_rows(named=True):
            if last is not None and r["gt_code"] != last["gt_code"]:
                p = r["plant"]
                # a change ends when the next campaign starts
                end = r["start_ts"]
                mm = mch_press.get(pr, mch[p])
                start = end - timedelta(minutes=mm)
                same_rim = (rim.get(r["gt_code"]) is not None
                            and rim.get(r["gt_code"]) == rim.get(last["gt_code"]))
                dd, ss = shift_of(start, t0)
                de, se = shift_of(end - timedelta(seconds=1), t0)
                ev.append({"plant": p, "press": pr, "from_gt": last["gt_code"],
                           "to_gt": r["gt_code"], "start_ts": start,
                           "end_ts": end, "minutes": mm,
                           "crew": crew_same.get(p, 3),
                           "same_rim": same_rim,
                           "day": dd, "shift": ss,
                           "spans_boundary": (dd, ss) != (de, se)})
            last = r
    mc = pl.DataFrame(ev) if ev else pl.DataFrame()
    if mc.height:                          # same report window as the shift grid
        mc = mc.filter(pl.col("day") <= ndays)
    mc.write_parquet(run / "mould_changes.parquet")

    print(f"\n  MOULD CHANGES  ({mc.height} events)")
    print(f"  {'plant':<6}{'changes':>9}{'press-h lost':>14}{'same rim':>10}"
          f"{'spanning a shift boundary':>27}")
    for p in ["PCR", "TBR"]:
        s = mc.filter(pl.col("plant") == p)
        if not s.height:
            continue
        span = s.filter(pl.col("spans_boundary")).height
        print(f"  {p:<6}{s.height:>9}{float(s['minutes'].sum())/60:>14,.0f}"
              f"{s.filter(pl.col('same_rim')).height:>10}"
              f"{span:>20} ({100*span/s.height:.0f}%)")

    # ---- R13 crew levelling ---------------------------------------------
    cl = (mc.group_by(["plant", "day", "shift"])
          .agg(pl.len().alias("changes"), pl.col("crew").sum().alias("fitters"))
          .sort(["plant", "day", "shift"]))
    cl.write_parquet(run / "crew_load.parquet")
    print("\n  R13 CREW LOAD per shift")
    print(f"  {'plant':<6}{'shifts w/ change':>18}{'changes p50':>13}{'peak':>7}"
          f"{'fitters p50':>13}{'peak':>7}{'levelling gain':>16}")
    for p in ["PCR", "TBR"]:
        s = cl.filter(pl.col("plant") == p)
        if not s.height:
            continue
        c = np.array(s["changes"], float)
        fpk = np.array(s["fitters"], float)
        # if the peak shift's excess were spread over the median, how much falls?
        gain = (fpk.max() - np.percentile(fpk, 50)) / max(fpk.max(), 1)
        print(f"  {p:<6}{s.height:>18}{np.percentile(c,50):>13.0f}{c.max():>7.0f}"
              f"{np.percentile(fpk,50):>13.0f}{fpk.max():>7.0f}"
              f"{100*gain:>15.0f}%")

    print("\n  R13 CAPACITY CHECK: BLOCKED")
    print("    crew SIZE per change is known (2 same-size / 3 different-size,")
    print("    from the plant's changeover master) but the ROSTER is not -- how")
    print("    many fitters are on shift is GAP-4. Demand is computed above; the")
    print("    check against capacity cannot run and is not assumed to pass.")

    print(f"\n  -> {run.name}/cure_by_shift · build_by_shift · mould_changes "
          f"· crew_load")


if __name__ == "__main__":
    main()
