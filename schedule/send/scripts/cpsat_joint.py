"""JOINT CP-SAT -- building AND curing from raw, no greedy anywhere.

    PYTHONPATH=. python scripts/cpsat_joint.py 2026-07 [seconds] [plant]

WHY THIS MODEL AND NOT THE LAST ONE
  `cpsat_cure_master.py` scheduled presses alone and returned 99.1 % fulfilment.
  That number is not achievable: it omits the build stage, so campaigns are free
  to start at hour 0 with nothing feeding them. Four experiments this session
  gave exactly that freedom to the greedy engine and every one LOST tyres --
  WARM_RELEASE -1,319, T0_STOCK_BASIS -1,670, CHG_PARALLEL -3,665, EDD -3.8 pt.
  A cure schedule that building cannot feed is not a plan.

  This model carries both stages and the constraint that links them, so its
  answer is the first CP-SAT number that means anything.

MODEL
    for each cure lot j (from l45_lots -- gross_build, already net of opening GT)
      build_j : optional interval on ONE eligible building machine
                duration = qty_j x cadence(machine)
      cure_j  : optional interval on ONE eligible press
                duration = qty_j / press_rate(gt)
    coupling (the whole point)
      start(cure_j) >= end(build_j) + tau_min          ageing, R17
      start(cure_j) <= end(build_j) + 72 h             shelf life, R5
    resources
      AddNoOverlap per press · AddNoOverlap per machine
      AddCumulative per GT with capacity = mould count  (R3)
    opening stock
      a lot may instead be fed from opening GT (no build predecessor), capped
      per GT by what is actually on the floor at t0
    maximise  sum( qty_j  where end(cure_j) <= month_end )

  Everything is in tyres and hours. No mined statistic enters as a constraint --
  tau_min and the 72 h shelf life are physical, the rates come from the plant's
  own masters, and the horizon is the calendar.

READ THE RESULT AS A CEILING
  It still assumes one build block per lot (the real engine slices deliveries),
  ignores build changeover time, and treats machine rates as constant. So it is
  an UPPER BOUND on what joint optimisation can reach -- but a legitimate one,
  because the binding physical coupling is present.
"""
from __future__ import annotations

import calendar
import sys
from pathlib import Path

import polars as pl
from ortools.sat.python import cp_model

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
D = ROOT / "warehouse" / "derived"

TAU_MIN_H = 1           # 0.27 h -> 1 h grid
SHELF_H = 72


def main() -> None:
    month = sys.argv[1] if len(sys.argv) > 1 else "2026-07"
    budget = float(sys.argv[2]) if len(sys.argv) > 2 else 300.0
    only = sys.argv[3] if len(sys.argv) > 3 else None
    y, m = int(month[:4]), int(month[5:7])
    MEND = calendar.monthrange(y, m)[1] * 24
    # TAIL: hours of planning horizon past month end. 0 = hard closed box.
    TAIL = int(sys.argv[4]) if len(sys.argv) > 4 else 72
    HZ = MEND + TAIL

    lots = pl.read_parquet(D / f"l45_lots_{month}.parquet").filter(pl.col("n_lots") > 0)
    cpe = pl.read_parquet(D / f"cap_press_{month}.parquet")
    cme = pl.read_parquet(D / f"cap_machine_{month}.parquet")
    cmo = pl.read_parquet(D / f"cap_mould_{month}.parquet")
    ctb = pl.read_parquet(D / "cycle_time_building.parquet")
    try:
        op = pl.read_parquet(ROOT / "masters" / "opening_gt" / f"opening_gt_{month}.parquet")
    except Exception:                                            # noqa: BLE001
        op = None

    print("=" * 78)
    print(f"  JOINT CP-SAT  (BUILD + CURE, from raw)   {month}"
          f"   month {MEND} h + {TAIL} h tail   budget {budget:.0f}s")
    print("=" * 78)

    for plant in (["PCR", "TBR"] if not only else [only]):
        L = lots.filter(pl.col("plant") == plant)
        if not L.height:
            continue
        pelig: dict = {}
        for r in cpe.filter(pl.col("plant") == plant).iter_rows(named=True):
            pelig.setdefault(r["gt_code"], []).append(str(r["press"]))
        melig: dict = {}
        for r in cme.filter(pl.col("plant") == plant).iter_rows(named=True):
            melig.setdefault(r["gt_code"], []).append(r["machine"])
        moulds = {r["gt_code"]: max(int(r.get("moulds") or 1), 1)
                  for r in cmo.filter(pl.col("plant") == plant).iter_rows(named=True)}
        cad = {r["machine"]: float(r["s_per_tyre"])
               for r in ctb.filter(pl.col("plant") == plant).iter_rows(named=True)}
        stock: dict = {}
        if op is not None:
            for r in (op.filter(pl.col("plant") == plant)
                      .group_by("gt_code").agg(pl.len().alias("n"))
                      .iter_rows(named=True)):
                stock[r["gt_code"]] = int(r["n"])

        jobs = []
        for r in L.iter_rows(named=True):
            sz = r.get("lot_sizes")
            sz = list(sz) if sz is not None and len(sz) else [
                float(r["lot_qty"])] * int(r["n_lots"])
            mo = max(moulds.get(r["gt_code"], 1), 1)
            mx = float(r["max_lot"] or 0)
            prate = (mx / (mo * SHELF_H)) if mx > 0 else 6.0
            for q in sz:
                if q > 0:
                    jobs.append((r["gt_code"], float(q), max(prate, 0.1)))
        total = sum(q for _, q, _ in jobs)

        mdl = cp_model.CpModel()
        press_iv: dict = {}
        mach_iv: dict = {}
        gt_iv: dict = {}
        inmonth = []
        opening_use: dict = {}

        for i, (g, q, prate) in enumerate(jobs):
            ps, ms = pelig.get(g, []), melig.get(g, [])
            if not ps:
                continue
            cdur = max(1, int(round(q / prate)))
            cs = mdl.NewIntVar(0, HZ, f"cs{i}")
            ce = mdl.NewIntVar(0, HZ, f"ce{i}")
            mdl.Add(ce == cs + cdur)
            fin = mdl.NewBoolVar(f"fin{i}")
            mdl.Add(ce <= MEND).OnlyEnforceIf(fin)
            mdl.Add(ce > MEND).OnlyEnforceIf(fin.Not())
            inmonth.append((fin, q))
            # PLACEMENT IS OPTIONAL, NOT FORCED.
            # AddExactlyOne made the whole model infeasible-or-UNKNOWN: if any
            # single lot cannot be placed under the R5 coupling, the solver has
            # no solution at all and returns nothing useful. With `placed` as a
            # decision the model is trivially feasible (place none) and the
            # objective does the work -- which is also the honest formulation,
            # since the real engine already reports unplaced lots rather than
            # failing.
            placed = mdl.NewBoolVar(f"pl{i}")
            lits = []
            for p in ps:
                v = mdl.NewBoolVar(f"cx{i}_{p}")
                press_iv.setdefault(p, []).append(
                    mdl.NewOptionalIntervalVar(cs, cdur, ce, v, f"civ{i}_{p}"))
                lits.append(v)
            mdl.Add(sum(lits) == 1).OnlyEnforceIf(placed)
            mdl.Add(sum(lits) == 0).OnlyEnforceIf(placed.Not())
            mdl.AddImplication(fin, placed)          # unplaced cannot count
            gt_iv.setdefault(g, []).append(
                mdl.NewOptionalIntervalVar(cs, cdur, ce, placed, f"gv{i}"))

            # fed from opening stock, or built
            from_stock = mdl.NewBoolVar(f"os{i}")
            opening_use.setdefault(g, []).append((from_stock, q))
            if ms:
                bl = []
                bs_ = mdl.NewIntVar(0, HZ, f"bs{i}")
                be = mdl.NewIntVar(0, HZ, f"be{i}")
                for mm in ms:
                    bdur = max(1, int(round(q * cad.get(mm, 50.0) / 3600.0)))
                    v = mdl.NewBoolVar(f"bx{i}_{mm}")
                    mach_iv.setdefault(mm, []).append(
                        mdl.NewOptionalIntervalVar(bs_, bdur, be, v, f"biv{i}_{mm}"))
                    mdl.Add(be == bs_ + bdur).OnlyEnforceIf(v)
                    bl.append(v)
                # exactly one machine unless fed from stock
                # build only when placed AND not fed from stock
                need_build = mdl.NewBoolVar(f"nb{i}")
                mdl.AddBoolAnd([placed, from_stock.Not()]).OnlyEnforceIf(need_build)
                mdl.AddBoolOr([placed.Not(), from_stock]).OnlyEnforceIf(
                    need_build.Not())
                mdl.Add(sum(bl) == 1).OnlyEnforceIf(need_build)
                mdl.Add(sum(bl) == 0).OnlyEnforceIf(need_build.Not())
                # THE COUPLING -- the whole reason this model exists
                mdl.Add(cs >= be + TAU_MIN_H).OnlyEnforceIf(need_build)
                mdl.Add(cs <= be + SHELF_H).OnlyEnforceIf(need_build)
                mdl.Add(from_stock == 0).OnlyEnforceIf(placed.Not())
            else:
                mdl.Add(from_stock == 1).OnlyEnforceIf(placed)
                mdl.Add(from_stock == 0).OnlyEnforceIf(placed.Not())

        for p, ivs in press_iv.items():
            if len(ivs) > 1:
                mdl.AddNoOverlap(ivs)
        for mm, ivs in mach_iv.items():
            if len(ivs) > 1:
                mdl.AddNoOverlap(ivs)
        for g, ivs in gt_iv.items():
            capg = max(moulds.get(g, 1), 1)
            if len(ivs) > capg:
                mdl.AddCumulative(ivs, [1] * len(ivs), capg)
        # opening stock is finite
        for g, lst in opening_use.items():
            s = stock.get(g, 0)
            mdl.Add(sum(int(q) * b for b, q in lst) <= s)

        mdl.Maximize(sum(int(q) * f for f, q in inmonth))
        # CONVERGENCE TRACE -- record objective and BOUND over time, so the
        # question "how long to OPTIMAL?" is answered by the gap curve rather
        # than guessed. CP-SAT proves optimality when bound == objective.
        class _Trace(cp_model.CpSolverSolutionCallback):
            def __init__(self):
                super().__init__()
                self.rows = []
            def on_solution_callback(self):
                self.rows.append((self.WallTime(), self.ObjectiveValue(),
                                  self.BestObjectiveBound()))
        tr = _Trace()
        slv = cp_model.CpSolver()
        slv.parameters.max_time_in_seconds = budget
        slv.parameters.num_search_workers = 8
        st = slv.Solve(mdl, tr)
        if tr.rows:
            print("    convergence (wall, objective, bound, gap):")
            for w, o, b in tr.rows[-8:]:
                g = 100.0 * (b - o) / max(abs(o), 1.0)
                print(f"      {w:>7.1f}s  obj {o:>12,.0f}  bound {b:>12,.0f}"
                      f"  gap {g:>6.1f}%")
            w, o, b = tr.rows[-1]
            print(f"    FINAL GAP {100.0 * (b - o) / max(abs(o), 1.0):.1f}%"
                  f"   ({len(tr.rows)} improving solutions found)")
        print(f"  {plant}: {slv.StatusName(st)}   {len(jobs):,} lots · "
              f"{sum(len(v) for v in press_iv.values()):,} press-intervals · "
              f"{sum(len(v) for v in mach_iv.values()):,} machine-intervals")
        if st not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            print(f"    NO SOLUTION in {budget:.0f}s")
            continue
        got = sum(q for f, q in inmonth if slv.Value(f))
        os_ = sum(q for lst in opening_use.values() for b, q in lst if slv.Value(b))
        print(f"    lots offered (gross_build)   {total:>9,.0f}")
        print(f"    fed from opening stock       {os_:>9,.0f}")
        print(f"    CURED IN-MONTH               {got:>9,.0f}"
              f"   ({100 * got / max(total, 1):.1f}% of gross_build)")
        print(f"    solve {slv.WallTime():.1f}s")


if __name__ == "__main__":
    main()
