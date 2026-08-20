"""CP-SAT cure scheduling FROM RAW -- assignment and placement together.

    PYTHONPATH=. python scripts/cpsat_cure_master.py 2026-07 [seconds] [plant]

WHAT THIS IS
  Not a post-pass. This replaces L5's greedy seating entirely: it takes the L4.5
  lot list and decides, for every lot, WHICH PRESS and WHEN -- jointly, in one
  model -- against the same constraints L5 honours.

  The reassignment experiment (scripts/cpsat_reassign.py) held L5's times frozen
  and re-chose presses only. It found 15 + 12 avoidable mould mounts (~1,000
  tyres). That bounds what press choice alone is worth. This model relaxes the
  times too, so it can find schedules greedy cannot reach at all -- at the cost
  of a far larger search.

MODEL
    job j  = one cure lot (plant, gt, qty) from l45_lots_<M>.parquet
    dur_j  = qty_j / press_rate(gt)          hours, one press
    for each eligible press p:  optional interval (start, dur, end, lit[j,p])
      exactly one p per job                      -- every lot is placed once
      AddNoOverlap per press                     -- a press runs one lot at a time
      AddCumulative per GT, capacity = moulds    -- R3, concurrent presses
      end_j <= HORIZON                           -- the planning box
    maximise  sum( qty_j  where end_j <= month_end )   -- IN-MONTH tyres
              - MOUNT_TYRES x (distinct presses per GT)

  The objective is in tyres, one unit, so the setup trade is visible in the
  answer rather than hidden in a weight.

WHAT TO COMPARE IT AGAINST
  L5 greedy on the same lot list, same month: PCR 273 campaigns, 380,762 tyres
  cured in-month (with PLANNER_L5_SCARCE_PRESS on). If CP-SAT cannot beat that
  it is not a modelling failure -- it is evidence the greedy seating is already
  near-optimal for the cure stage, which the phase RCA already suggested
  (L5 loses 63 tyres of 393,646, 0.02%).
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import polars as pl
from ortools.sat.python import cp_model

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
D = ROOT / "warehouse" / "derived"

MOUNT_TYRES = 38          # a 6 h mould change at ~6.3 tyres/press-h


def main() -> None:
    month = sys.argv[1] if len(sys.argv) > 1 else "2026-07"
    budget = float(sys.argv[2]) if len(sys.argv) > 2 else 300.0
    only = sys.argv[3] if len(sys.argv) > 3 else None
    y, m = int(month[:4]), int(month[5:7])
    t0 = dt.datetime(y, m, 1, 7)
    import calendar
    ndays = calendar.monthrange(y, m)[1]
    MEND = ndays * 24                                  # hours from t0
    HORIZON = MEND + 72                                # planning tail

    lots = pl.read_parquet(D / f"l45_lots_{month}.parquet").filter(
        pl.col("n_lots") > 0)
    cpe = pl.read_parquet(D / f"cap_press_{month}.parquet")
    cmo = pl.read_parquet(D / f"cap_mould_{month}.parquet")

    print("=" * 78)
    print(f"  CP-SAT CURE MASTER (assignment + placement FROM RAW)   {month}")
    print(f"  horizon {MEND} h month + 72 h tail · budget {budget:.0f}s")
    print("=" * 78)

    for plant in (["PCR", "TBR"] if not only else [only]):
        L = lots.filter(pl.col("plant") == plant)
        if not L.height:
            continue
        elig: dict = {}
        for r in cpe.filter(pl.col("plant") == plant).iter_rows(named=True):
            elig.setdefault(r["gt_code"], []).append(str(r["press"]))
        moulds: dict = {}
        rate: dict = {}
        for r in cmo.filter(pl.col("plant") == plant).iter_rows(named=True):
            moulds[r["gt_code"]] = max(int(r.get("moulds") or 1), 1)
        for r in L.iter_rows(named=True):
            mo = max(moulds.get(r["gt_code"], 1), 1)
            mx = float(r["max_lot"] or 0)
            rate[r["gt_code"]] = (mx / (mo * 72.0)) if mx > 0 else 6.0

        jobs = []
        for r in L.iter_rows(named=True):
            sz = r.get("lot_sizes")
            sz = list(sz) if sz is not None and len(sz) else [
                float(r["lot_qty"])] * int(r["n_lots"])
            for q in sz:
                if q > 0:
                    jobs.append((r["gt_code"], float(q)))
        total = sum(q for _, q in jobs)

        mdl = cp_model.CpModel()
        lit: dict = {}
        per_press: dict = {}
        per_gt: dict = {}
        gt_press: dict = {}
        inmonth_terms = []

        for i, (g, q) in enumerate(jobs):
            ps = elig.get(g, [])
            if not ps:
                continue
            r1 = max(rate.get(g, 6.0), 0.1)
            dur = max(1, int(round(q / r1)))            # hours on one press
            st = mdl.NewIntVar(0, HORIZON, f"s{i}")
            en = mdl.NewIntVar(0, HORIZON, f"e{i}")
            mdl.Add(en == st + dur)
            fin = mdl.NewBoolVar(f"f{i}")               # ends inside the month
            mdl.Add(en <= MEND).OnlyEnforceIf(fin)
            mdl.Add(en > MEND).OnlyEnforceIf(fin.Not())
            inmonth_terms.append((fin, q))
            lits = []
            for p in ps:
                v = mdl.NewBoolVar(f"x{i}_{p}")
                lit[(i, p)] = v
                iv = mdl.NewOptionalIntervalVar(st, dur, en, v, f"iv{i}_{p}")
                per_press.setdefault(p, []).append(iv)
                lits.append(v)
                k = (g, p)
                if k not in gt_press:
                    gt_press[k] = mdl.NewBoolVar(f"y{g}_{p}")
                mdl.Add(v <= gt_press[k])
            mdl.AddExactlyOne(lits)
            # R3: concurrent presses on this GT <= its mould count
            per_gt.setdefault(g, []).append(
                mdl.NewIntervalVar(st, dur, en, f"g{i}"))

        for p, ivs in per_press.items():
            if len(ivs) > 1:
                mdl.AddNoOverlap(ivs)
        for g, ivs in per_gt.items():
            capg = max(moulds.get(g, 1), 1)
            if len(ivs) > capg:
                mdl.AddCumulative(ivs, [1] * len(ivs), capg)

        mdl.Maximize(
            sum(int(q) * f for f, q in inmonth_terms)
            - MOUNT_TYRES * sum(gt_press.values()))

        slv = cp_model.CpSolver()
        slv.parameters.max_time_in_seconds = budget
        slv.parameters.num_search_workers = 8
        slv.parameters.log_search_progress = False
        st_ = slv.Solve(mdl)
        print(f"  {plant}: {slv.StatusName(st_)}  {len(jobs):,} lots · "
              f"{len(lit):,} assignment vars · {len(gt_press):,} GT-press vars")
        if st_ not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            print(f"    no solution in {budget:.0f}s")
            continue
        got = sum(q for f, q in inmonth_terms if slv.Value(f))
        mounts = int(sum(slv.Value(v) for v in gt_press.values()))
        print(f"    lots offered                 {total:>9,.0f} tyres")
        print(f"    CP-SAT completes IN-MONTH    {got:>9,.0f} "
              f"({100 * got / max(total, 1):.1f}%)")
        print(f"    press mounts used            {mounts:>9,}")
        print(f"    solve {slv.WallTime():.1f}s")
        # DUMP THE SOLUTION so lot size / changeovers can be measured, not inferred.
        out = []
        for i, (g, q) in enumerate(jobs):
            for p in elig.get(g, []):
                v = lit.get((i, p))
                if v is not None and slv.Value(v):
                    st_i = slv.Value(mdl.GetIntVarFromProtoIndex(0)) if False else None
                    out.append({"plant": plant, "gt_code": g, "press": p,
                                "qty": q, "job": i})
                    break
        pl.DataFrame(out).write_parquet(
            ROOT / "runs" / f"cpsat_cure_{plant}_{month}.parquet")
        print(f"    -> runs/cpsat_cure_{plant}_{month}.parquet ({len(out)} rows)")


if __name__ == "__main__":
    main()
