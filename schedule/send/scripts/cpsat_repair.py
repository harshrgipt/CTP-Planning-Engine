"""CP-SAT REPAIR ON TOP OF GREEDY -- assignment AND placement, warm-started.

    PYTHONPATH=. python scripts/cpsat_repair.py SV_jul 2026-07 [seconds] [slackH]

WHY THIS SHAPE
  Three CP-SAT formulations were measured on July 2026 PCR:

    model                    freedom              warm start   result
    reassignment             press only, t frozen     yes      BEAT greedy (201->186 mounts)
    cure-only from raw       press + time              no      99.1% but constraints missing
    joint from raw           everything                no      77.8% -- WORSE than greedy

  More freedom without guidance made it monotonically worse. The from-scratch
  joint model could not even find a first solution inside a closed box in 420 s,
  because 548 operations with free start times over 816 hours is a search space
  no solver walks unaided.

  So this model does what the evidence says works: START FROM GREEDY'S ANSWER and
  let CP-SAT move things a bounded distance. Both dimensions are free --
  which press, and when -- but `when` only within +/- SLACK hours of where L5 put
  it. That keeps the horizon structure L5 got right (it loses 0.02% at this
  stage) and re-opens exactly the two decisions greedy makes locally and cannot
  revisit.

  Because the incumbent is feasible by construction, the solver can only improve
  on it. There is no arm where this is worse than shipping greedy alone.

OBJECTIVE
  maximise  in-month tyres  -  MOUNT_TYRES x (distinct presses per GT)

  in tyres, one unit, so the setup trade is legible in the answer.
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

MOUNT_TYRES = 38


def main() -> None:
    run = ROOT / "runs" / (sys.argv[1] if len(sys.argv) > 1 else "SV_jul")
    month = sys.argv[2] if len(sys.argv) > 2 else "2026-07"
    budget = float(sys.argv[3]) if len(sys.argv) > 3 else 180.0
    slack = int(sys.argv[4]) if len(sys.argv) > 4 else 24
    y, m = int(month[:4]), int(month[5:7])
    MEND = calendar.monthrange(y, m)[1] * 24

    cc = pl.read_parquet(run / "cure_campaigns.parquet")
    cpe = pl.read_parquet(D / f"cap_press_{month}.parquet")
    cmo = pl.read_parquet(D / f"cap_mould_{month}.parquet")

    print("=" * 78)
    print(f"  CP-SAT REPAIR ON GREEDY   {month}   run={run.name}")
    print(f"  press free · start time +/-{slack} h · budget {budget:.0f}s")
    print("=" * 78)

    for plant in ("PCR", "TBR"):
        c = cc.filter(pl.col("plant") == plant)
        if not c.height:
            continue
        t0 = c["start_ts"].min()
        elig: dict = {}
        for r in cpe.filter(pl.col("plant") == plant).iter_rows(named=True):
            elig.setdefault(r["gt_code"], []).append(str(r["press"]))
        moulds = {r["gt_code"]: max(int(r.get("moulds") or 1), 1)
                  for r in cmo.filter(pl.col("plant") == plant).iter_rows(named=True)}

        camps = list(c.iter_rows(named=True))
        base_in = sum(float(r["qty"]) for r in camps
                      if (r["end_ts"] - t0).total_seconds() / 3600 <= MEND)
        base_mounts = sum(c.filter(pl.col("gt_code") == g)["press"].n_unique()
                          for g in c["gt_code"].unique().to_list())

        mdl = cp_model.CpModel()
        press_iv: dict = {}
        gt_iv: dict = {}
        ypair: dict = {}
        xlit: dict = {}
        svar: dict = {}
        inmonth = []
        hints = []

        for i, r in enumerate(camps):
            g = r["gt_code"]
            s0 = int((r["start_ts"] - t0).total_seconds() // 3600)
            e0 = int((r["end_ts"] - t0).total_seconds() // 3600)
            dur = max(1, e0 - s0)
            lo, hi = max(0, s0 - slack), s0 + slack
            st = mdl.NewIntVar(lo, hi + dur, f"s{i}")
            mdl.Add(st >= lo)
            mdl.Add(st <= hi)
            en = mdl.NewIntVar(0, hi + dur, f"e{i}")
            mdl.Add(en == st + dur)
            fin = mdl.NewBoolVar(f"f{i}")
            mdl.Add(en <= MEND).OnlyEnforceIf(fin)
            mdl.Add(en > MEND).OnlyEnforceIf(fin.Not())
            inmonth.append((fin, float(r["qty"])))
            ps = elig.get(g, []) or [str(r["press"])]
            lits = []
            for p in ps:
                v = mdl.NewBoolVar(f"x{i}_{p}")
                xlit[(i, p)] = v
                press_iv.setdefault(p, []).append(
                    mdl.NewOptionalIntervalVar(st, dur, en, v, f"iv{i}_{p}"))
                k = (g, p)
                if k not in ypair:
                    ypair[k] = mdl.NewBoolVar(f"y{g}_{p}")
                mdl.Add(v <= ypair[k])
                lits.append(v)
                hints.append((v, 1 if p == str(r["press"]) else 0))
            mdl.AddExactlyOne(lits)
            svar[i] = st
            gt_iv.setdefault(g, []).append(mdl.NewIntervalVar(st, dur, en, f"g{i}"))
            hints.append((st, s0))
            hints.append((fin, 1 if e0 <= MEND else 0))

        for p, ivs in press_iv.items():
            if len(ivs) > 1:
                mdl.AddNoOverlap(ivs)
        for g, ivs in gt_iv.items():
            capg = max(moulds.get(g, 1), 1)
            if len(ivs) > capg:
                mdl.AddCumulative(ivs, [1] * len(ivs), capg)
        used = {(r["gt_code"], str(r["press"])) for r in camps}
        for (g, p), v in ypair.items():
            mdl.AddHint(v, 1 if (g, p) in used else 0)
        for v, val in hints:
            mdl.AddHint(v, val)

        mdl.Maximize(sum(int(q) * f for f, q in inmonth)
                     - MOUNT_TYRES * sum(ypair.values()))

        class _T(cp_model.CpSolverSolutionCallback):
            def __init__(self):
                super().__init__()
                self.rows = []
            def on_solution_callback(self):
                self.rows.append((self.WallTime(), self.ObjectiveValue(),
                                  self.BestObjectiveBound()))
        tr = _T()
        slv = cp_model.CpSolver()
        slv.parameters.max_time_in_seconds = budget
        slv.parameters.num_search_workers = 8
        st_ = slv.Solve(mdl, tr)
        print(f"  {plant}: {slv.StatusName(st_)}   {len(camps)} campaigns")
        if st_ not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            print("    no solution")
            continue
        got = sum(q for f, q in inmonth if slv.Value(f))
        mounts = int(sum(slv.Value(v) for v in ypair.values()))
        print(f"    in-month  greedy {base_in:>9,.0f}  ->  CP-SAT {got:>9,.0f}"
              f"   ({got - base_in:>+7,.0f})")
        print(f"    mounts    greedy {base_mounts:>9,}  ->  CP-SAT {mounts:>9,}"
              f"   ({mounts - base_mounts:>+7,})")
        if tr.rows:
            for w, o, b in tr.rows[-5:]:
                print(f"      {w:>7.1f}s  obj {o:>12,.0f}  bound {b:>12,.0f}"
                      f"  gap {100.0 * (b - o) / max(abs(o), 1.0):>6.2f}%")
        print(f"    solve {slv.WallTime():.1f}s")
        # WRITE THE REPAIRED SCHEDULE BACK so L7/L10/L11 can score it for real.
        # Until this runs through the build release, "+14,524" is a cure-seating
        # number, not fulfilment -- the model has no build coupling, and moving
        # cures earlier is exactly what creates starvation.
        import datetime as _dt
        out = []
        for i, r in enumerate(camps):
            g = r["gt_code"]
            s0 = int((r["start_ts"] - t0).total_seconds() // 3600)
            e0 = int((r["end_ts"] - t0).total_seconds() // 3600)
            dur = max(1, e0 - s0)
            newp, news = str(r["press"]), s0
            for p in (elig.get(g, []) or [str(r["press"])]):
                v = xlit.get((i, p))
                if v is not None and slv.Value(v):
                    newp = p
                    break
            sv_ = svar.get(i)
            if sv_ is not None:
                news = slv.Value(sv_)
            d = dict(r)
            d["press"] = newp
            d["start_ts"] = t0 + _dt.timedelta(hours=int(news))
            d["end_ts"] = t0 + _dt.timedelta(hours=int(news + dur))
            out.append(d)
        pl.DataFrame(out).write_parquet(
            ROOT / "runs" / f"cpsat_repaired_{plant}_{month}.parquet")
        print(f"    -> runs/cpsat_repaired_{plant}_{month}.parquet")


if __name__ == "__main__":
    main()
