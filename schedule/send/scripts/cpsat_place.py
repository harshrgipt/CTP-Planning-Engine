"""CP-SAT multi-gap build placement -- the refused runs, with setup costs.

    PYTHONPATH=. python scripts/cpsat_place.py FLOW_jul 2026-07 [seconds]

WHAT THIS SOLVES, AND WHY IT IS NOT A HEURISTIC
  `_place` in L7 demands ONE CONTIGUOUS block per build run. The runs it refuses
  have 11.3 h of cumulative free time inside their own R5 window against a 3.21 h
  need -- but in ~1.46 h shards, so no single hole fits.

  `scripts/flow_place_bound.py` already proved the CEILING exactly: max-flow
  (Horn's preemptive formulation, polynomial and exact) says all 4,350 PCR tyres
  are simultaneously placeable, with ZERO contention loss. What the flow model
  cannot represent is that resuming a run after another GT ran on that machine
  costs a build changeover (PCR 22-42 min). Adding that makes the problem
  NP-hard, which is where CP-SAT earns its place.

MODEL
  For each refused run r, each eligible machine m, each free gap g inside r's
  [R5 lower, ideal] window:
      piece(r, m, g)  optional interval, size = tyres placed x cadence
  subject to
      sum over pieces of run r        <= qty_r          (never overbuild)
      NoOverlap per machine           (existing occupancy enters as fixed)
      each piece >= MIN_PIECE tyres   (a 3-tyre fragment is not a real setup)
  maximise
      tyres placed  -  SETUP_TYRES x (number of pieces)
  so a piece must repay its own changeover in tyres or the solver drops it.

  The objective is deliberately in ONE unit (tyres). A weighted sum of tyres and
  minutes hides the trade; expressing setup as the tyres it costs makes the
  trade visible in the answer.

READ THE RESULT AS A BOUND, NOT A PLAN
  This places the refused runs against a FROZEN calendar of everything L7 already
  scheduled. A real integration would re-solve jointly and could do better or
  worse. If CP-SAT cannot get close to the 4,350 flow bound here, joint solving
  will not rescue it -- that is the decision this script exists to inform.
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

MIN_PIECE = 30          # tyres -- below this a piece is not worth a setup
SETUP_MIN = 32.0        # PCR build changeover, midpoint of same(22)/diff(42)


def main() -> None:
    run = ROOT / "runs" / (sys.argv[1] if len(sys.argv) > 1 else "FLOW_jul")
    month = sys.argv[2] if len(sys.argv) > 2 else "2026-07"
    budget = float(sys.argv[3]) if len(sys.argv) > 3 else 120.0
    y, m = int(month[:4]), int(month[5:7])
    t0 = dt.datetime(y, m, 1, 7)

    diag = pl.read_parquet(run / "l7_place_diag.parquet")
    bs = pl.read_parquet(run / "build_schedule.parquet").filter(
        pl.col("machine") != "OPENING_STOCK")
    cm = pl.read_parquet(D / f"cap_machine_{month}.parquet")

    print("=" * 78)
    print(f"  CP-SAT MULTI-GAP PLACEMENT   {month}   run={run.name}"
          f"   budget {budget:.0f}s")
    print("=" * 78)

    for plant in ("PCR", "TBR"):
        dg = diag.filter(pl.col("plant") == plant)
        cand = dg.filter((pl.col("ideal_slack_h") > 0)
                         & (pl.col("r5_n_fit") == 0)
                         & (pl.col("r5_free_in_window_h") >= pl.col("dur_h")))
        if not cand.height:
            print(f"  {plant}: no addressable runs")
            continue

        elig: dict = {}
        for r in cm.filter(pl.col("plant") == plant).iter_rows(named=True):
            elig.setdefault(r["gt_code"], set()).add(r["machine"])
        occ: dict = {}
        for r in bs.filter(pl.col("plant") == plant).iter_rows(named=True):
            occ.setdefault(r["machine"], []).append((r["start_ts"], r["end_ts"]))
        for k in occ:
            occ[k].sort()
        cad: dict = {}
        for r in (bs.filter(pl.col("plant") == plant).group_by("machine")
                  .agg(pl.col("qty").sum().alias("q"),
                       ((pl.col("end_ts") - pl.col("start_ts"))
                        .dt.total_seconds().sum()).alias("s"))
                  .iter_rows(named=True)):
            cad[r["machine"]] = r["s"] / max(r["q"], 1e-9)      # sec per tyre

        mdl = cp_model.CpModel()
        HOR = 40 * 24 * 60                                       # minutes
        setup_tyres = int(round(SETUP_MIN * 60.0
                                / max(sum(cad.values()) / max(len(cad), 1), 1.0)))
        per_mach: dict = {}
        placed_terms, piece_lits, total_q = [], [], 0.0

        for i, r in enumerate(cand.iter_rows(named=True)):
            q = int(r["qty"])
            total_q += q
            lo = r["cure_first"] - dt.timedelta(hours=float(r["r5_window_h"]))
            hi = r["cure_first"]
            run_pieces = []
            for mach in sorted(elig.get(r["gt_code"], set())):
                if mach not in cad:
                    continue
                cur, gaps = lo, []
                for s2, e2 in occ.get(mach, []):
                    if e2 <= lo or s2 >= hi:
                        continue
                    if s2 > cur:
                        gaps.append((cur, min(s2, hi)))
                    cur = max(cur, e2)
                if cur < hi:
                    gaps.append((cur, hi))
                for gi, (gs, ge) in enumerate(gaps):
                    span = int((ge - gs).total_seconds() // 60)
                    if span < 10:
                        continue
                    cap = int(span * 60.0 / cad[mach])
                    if cap < MIN_PIECE:
                        continue
                    nm = f"p{i}_{mach}_{gi}"
                    lit = mdl.NewBoolVar(nm)
                    qty = mdl.NewIntVar(0, min(cap, q), nm + "q")
                    mdl.Add(qty >= MIN_PIECE).OnlyEnforceIf(lit)
                    mdl.Add(qty == 0).OnlyEnforceIf(lit.Not())
                    dur = mdl.NewIntVar(0, span, nm + "d")
                    # minutes = tyres x cadence(sec)/60, linearised
                    mdl.Add(dur * 60 == qty * int(round(cad[mach])))
                    st = mdl.NewIntVar(int((gs - t0).total_seconds() // 60),
                                       int((ge - t0).total_seconds() // 60), nm + "s")
                    en = mdl.NewIntVar(0, HOR, nm + "e")
                    iv = mdl.NewOptionalIntervalVar(st, dur, en, lit, nm + "i")
                    mdl.Add(en <= int((ge - t0).total_seconds() // 60))
                    per_mach.setdefault(mach, []).append(iv)
                    run_pieces.append((lit, qty))
                    piece_lits.append(lit)
                    placed_terms.append(qty)
            if run_pieces:
                mdl.Add(sum(v for _, v in run_pieces) <= q)

        # existing occupancy is immovable
        for mach, ivs in per_mach.items():
            fixed = []
            for s2, e2 in occ.get(mach, []):
                a = int((s2 - t0).total_seconds() // 60)
                b = int((e2 - t0).total_seconds() // 60)
                if b <= a:
                    continue
                fixed.append(mdl.NewIntervalVar(
                    mdl.NewConstant(a), mdl.NewConstant(b - a),
                    mdl.NewConstant(b), f"fx{mach}{a}"))
            mdl.AddNoOverlap(ivs + fixed)

        mdl.Maximize(sum(placed_terms) - setup_tyres * sum(piece_lits))
        slv = cp_model.CpSolver()
        slv.parameters.max_time_in_seconds = budget
        slv.parameters.num_search_workers = 8
        stt = slv.Solve(mdl)
        got = sum(slv.Value(v) for v in placed_terms) if stt in (
            cp_model.OPTIMAL, cp_model.FEASIBLE) else 0
        npc = sum(slv.Value(l) for l in piece_lits) if stt in (
            cp_model.OPTIMAL, cp_model.FEASIBLE) else 0
        print(f"  {plant}:  {slv.StatusName(stt)}   "
              f"{len(piece_lits):,} candidate pieces over {cand.height} runs")
        print(f"    refused tyres offered        {total_q:>8,.0f}")
        print(f"    CP-SAT PLACED                {got:>8,.0f}   "
              f"({100 * got / max(total_q, 1):.0f}%)")
        print(f"    pieces used                  {npc:>8,}   "
              f"(setup charged {setup_tyres} tyres each = "
              f"{npc * setup_tyres:,} tyres of overhead)")
        print(f"    NET of setup                 {got - npc * setup_tyres:>8,.0f}")
        print(f"    solve {slv.WallTime():.1f}s")


if __name__ == "__main__":
    main()
