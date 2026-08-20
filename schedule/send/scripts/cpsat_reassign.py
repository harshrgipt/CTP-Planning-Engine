"""CP-SAT press reassignment -- kill the excess mould mounts, move nothing in time.

    PYTHONPATH=. python scripts/cpsat_reassign.py SV_jul 2026-07 [seconds]

THE DEFECT
  Measured July 2026, on the shipped plan:

      PCR  273 campaigns · 201 distinct-press visits · peak concurrency 135
           -> 66 EXCESS mounts   (1.36 campaigns per press-visit)
      TBR  193 campaigns · 163 visits · peak 118
           -> 45 EXCESS mounts   (1.18)

  A GT never needs more than `peak` presses at any instant, yet we mount its
  mould on far more presses than that across the month. Each excess mount is a
  ~6 h mould change: 66 x 6 = 396 press-h on PCR, 270 h on TBR -- roughly 2,500
  and 450 tyres of pure setup waste.

  Cause: L5 picks whichever press is free soonest, campaign by campaign. When a
  press finishes GT X it is handed to whatever GT asks next, so by the time X's
  following campaign comes round that press is busy and X mounts somewhere new.
  Nothing in a greedy sequential pick can see that X still had work pending.

WHY REASSIGNMENT AND NOT RE-SCHEDULING
  Eleven attempts this session to change WHEN work happens all lost -- EDD
  -3.8 pt, BACKLOAD -4.0, FLOOR_BASIS -2,478..-6,050, CHG_PARALLEL -3,665,
  T0_STOCK_BASIS -1,670, WARM_RELEASE -1,319. The single change that gained
  (+1,956) moved work BETWEEN RESOURCES at fixed times.

  So this model holds every campaign's (start_ts, end_ts) EXACTLY as L5 set it
  and re-decides only WHICH PRESS runs it. Cure times cannot move, so R5, the
  GT ledger, build release and fulfilment are all invariant by construction.
  The only thing that can change is the mould-change bill.

MODEL
      x[c][p] = 1  campaign c runs on press p
      sum_p x[c][p] == 1                          every campaign is placed
      x[c1][p] + x[c2][p] <= 1  for overlapping c1,c2   one press, one campaign
      x[c][p] <= y[gt(c)][p]                      y marks "this GT uses press p"
      minimise sum(y)                             = total mould mounts

  Exact. The optimum is a lower bound on mounts for the given schedule, so the
  gap to today's 201 is the true size of the greedy loss.
"""
from __future__ import annotations

import sys
from pathlib import Path

import polars as pl
from ortools.sat.python import cp_model

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
D = ROOT / "warehouse" / "derived"


def main() -> None:
    run = ROOT / "runs" / (sys.argv[1] if len(sys.argv) > 1 else "SV_jul")
    month = sys.argv[2] if len(sys.argv) > 2 else "2026-07"
    budget = float(sys.argv[3]) if len(sys.argv) > 3 else 90.0

    cc = pl.read_parquet(run / "cure_campaigns.parquet")
    cp_elig = pl.read_parquet(D / f"cap_press_{month}.parquet")
    cmould = pl.read_parquet(D / f"cap_mould_{month}.parquet")

    print("=" * 78)
    print(f"  CP-SAT PRESS REASSIGNMENT   {month}   run={run.name}"
          f"   budget {budget:.0f}s   (times FROZEN)")
    print("=" * 78)

    for plant in ("PCR", "TBR"):
        c = cc.filter(pl.col("plant") == plant)
        if not c.height:
            continue
        elig: dict = {}
        for r in cp_elig.filter(pl.col("plant") == plant).iter_rows(named=True):
            elig.setdefault(r["gt_code"], set()).add(str(r["press"]))
        cap: dict = {}
        for r in cmould.filter(pl.col("plant") == plant).iter_rows(named=True):
            cap[r["gt_code"]] = int(r.get("max_concurrent_presses") or 99)

        camps = list(c.iter_rows(named=True))
        # today's bill
        cur_visits = sum(
            c.filter(pl.col("gt_code") == g)["press"].n_unique()
            for g in c["gt_code"].unique().to_list())

        mdl = cp_model.CpModel()
        x: dict = {}
        gts = sorted({r["gt_code"] for r in camps})
        presses = sorted({str(r["press"]) for r in camps}
                         | {p for g in gts for p in elig.get(g, set())})
        y = {(g, p): mdl.NewBoolVar(f"y{gi}_{pi}")
             for gi, g in enumerate(gts) for pi, p in enumerate(presses)
             if p in elig.get(g, set())}

        for i, r in enumerate(camps):
            g = r["gt_code"]
            opts = [p for p in elig.get(g, set()) if (g, p) in y]
            if not opts:                      # keep it where L5 put it
                opts = [str(r["press"])]
                for p in opts:
                    y.setdefault((g, p), mdl.NewBoolVar(f"yx{i}"))
            lits = []
            for p in opts:
                v = mdl.NewBoolVar(f"x{i}_{p}")
                x[(i, p)] = v
                mdl.Add(v <= y[(g, p)])
                lits.append(v)
            mdl.AddExactlyOne(lits)

        # NO-OVERLAP VIA OPTIONAL INTERVALS, not pairwise booleans.
        # The pairwise form is 273^2/2 x 86 ~ 3.2M constraints and CP-SAT
        # returned UNKNOWN in 90 s. One optional interval per (campaign, press)
        # with AddNoOverlap per press is ~23k intervals and lets the scheduling
        # propagators do the work they exist for.
        t0 = min(r["start_ts"] for r in camps)
        per_press: dict = {}
        for i, r in enumerate(camps):
            a = int((r["start_ts"] - t0).total_seconds() // 60)
            b = int((r["end_ts"] - t0).total_seconds() // 60)
            for p in presses:
                v = x.get((i, p))
                if v is None:
                    continue
                iv = mdl.NewOptionalIntervalVar(
                    mdl.NewConstant(a), mdl.NewConstant(max(b - a, 1)),
                    mdl.NewConstant(max(b, a + 1)), v, f"iv{i}_{p}")
                per_press.setdefault(p, []).append(iv)
        for p, ivs in per_press.items():
            if len(ivs) > 1:
                mdl.AddNoOverlap(ivs)
        # NO R3 CONSTRAINT HERE, DELIBERATELY -- and getting this wrong is what
        # made the model INFEASIBLE (UNKNOWN even with a valid warm start).
        # `max_concurrent_presses` caps presses running a GT AT THE SAME INSTANT.
        # `y[(g,p)]` counts DISTINCT presses the GT touches over the WHOLE MONTH.
        # Constraining the second by the first says GT 1513 may visit at most 8
        # presses in 31 days when it demonstrably needs 26 -- a contradiction the
        # solver correctly refused.
        # R3 is satisfied automatically: campaign TIMES are frozen, so how many
        # of a GT's campaigns overlap at any instant is fixed, and overlapping
        # campaigns are forced onto different presses by AddNoOverlap. Reassigning
        # presses cannot change instantaneous concurrency.

        # WARM START from L5's own assignment. It is feasible by construction,
        # so CP-SAT begins from today's answer and can only improve on it -- and
        # if the model rejects the hint, the model is wrong, not the problem hard.
        nhint = 0
        for i, r in enumerate(camps):
            p0 = str(r["press"])
            for p in presses:
                v = x.get((i, p))
                if v is not None:
                    mdl.AddHint(v, 1 if p == p0 else 0)
                    nhint += 1
        for (g, p), v in y.items():
            used = c.filter((pl.col("gt_code") == g)
                            & (pl.col("press").cast(pl.Utf8) == p)).height > 0
            mdl.AddHint(v, 1 if used else 0)
        print(f"    model: {len(x):,} x-vars · {len(y):,} y-vars · "
              f"{sum(len(v) for v in per_press.values()):,} intervals · "
              f"{nhint:,} hints")
        mdl.Minimize(sum(y.values()))
        slv = cp_model.CpSolver()
        slv.parameters.max_time_in_seconds = budget
        slv.parameters.num_search_workers = 8
        st = slv.Solve(mdl)
        if st not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            print(f"  {plant}: {slv.StatusName(st)} -- no solution")
            continue
        got = int(sum(slv.Value(v) for v in y.values()))
        saved = cur_visits - got
        print(f"  {plant}:  {slv.StatusName(st)}   {len(camps)} campaigns, "
              f"{len(presses)} presses, {len(y):,} assignment vars")
        print(f"    press-visits NOW (greedy)     {cur_visits:>6,}")
        print(f"    press-visits OPTIMAL          {got:>6,}")
        print(f"    mounts saved                  {saved:>6,}"
              f"   = {saved * 6.0:,.0f} press-h  (~{saved * 6.0 * 6.3:,.0f} tyres)")
        print(f"    solve {slv.WallTime():.1f}s")


if __name__ == "__main__":
    main()
