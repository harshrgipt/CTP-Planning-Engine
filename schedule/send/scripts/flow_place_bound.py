"""EXACT upper bound on multi-gap build placement, by max-flow.

    PYTHONPATH=. python scripts/flow_place_bound.py FLOW_jul 2026-07

WHY A FLOW AND NOT A HEURISTIC
  `_place` in L7 requires ONE CONTIGUOUS block for a whole build run. Measured on
  July 2026, the runs it refuses have, inside their own R5 window:

      window                71.7 h
      cumulative free       11.3 h      <- 3.5x what the run needs
      largest single gap     1.46 h
      run needs              3.21 h
      machines that fit          0

  So the hours exist and the contiguity requirement is what refuses them. The
  obvious question -- "how many tyres would multi-gap placement recover?" -- was
  answered per-run as 4,907 by comparing each run's cumulative free against its
  own duration. That figure is an OVERSTATEMENT: it treats every run in
  isolation, so two runs competing for the same 1.46 h gap are both counted.

  Preemptive scheduling on parallel machines with release times and deadlines is
  solvable EXACTLY in polynomial time by a flow formulation (Horn 1974). This is
  not a metaheuristic and not an approximation -- max-flow == total demand iff a
  feasible preemptive schedule exists, and the flow value IS the maximum
  placeable quantity when it does not.

      source --qty--> run_r --> (machine, interval) --> sink
                               edge only where the interval lies inside run r's
                               [R5 lower bound, ideal release] window AND the
                               machine is eligible for r's GT
                               cap(interval -> sink) = length x machine rate

  The result is the CEILING for any multi-gap algorithm, contention included.
  It deliberately ignores setup-on-resumption, which is why it is an upper bound
  rather than a plan: a real schedule pays a build changeover (PCR 22-42 min)
  each time it resumes, and that is an NP-hard problem needing CP-SAT. Knowing
  the exact ceiling first tells us whether the CP-SAT work is worth doing.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import networkx as nx
import polars as pl

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
D = ROOT / "warehouse" / "derived"


def main() -> None:
    run = ROOT / "runs" / (sys.argv[1] if len(sys.argv) > 1 else "FLOW_jul")
    month = sys.argv[2] if len(sys.argv) > 2 else "2026-07"
    y, m = int(month[:4]), int(month[5:7])
    t0 = dt.datetime(y, m, 1, 7)

    diag = pl.read_parquet(run / "l7_place_diag.parquet")
    bs = pl.read_parquet(run / "build_schedule.parquet").filter(
        pl.col("machine") != "OPENING_STOCK")
    cm = pl.read_parquet(D / f"cap_machine_{month}.parquet")

    print("=" * 78)
    print(f"  MAX-FLOW BOUND ON MULTI-GAP PLACEMENT   {month}   run={run.name}")
    print("=" * 78)

    for plant in ("PCR", "TBR"):
        dg = diag.filter(pl.col("plant") == plant)
        # Only runs that are (a) not cold-start and (b) whose window holds enough
        # cumulative free time -- the population multi-gap placement can address.
        cand = dg.filter((pl.col("ideal_slack_h") > 0)
                         & (pl.col("r5_n_fit") == 0)
                         & (pl.col("r5_free_in_window_h") >= pl.col("dur_h")))
        if not cand.height:
            print(f"  {plant}: no addressable runs")
            continue

        elig: dict = {}
        for r in cm.filter(pl.col("plant") == plant).iter_rows(named=True):
            elig.setdefault(r["gt_code"], set()).add(r["machine"])

        # machine occupancy -> free intervals
        occ: dict = {}
        for r in bs.filter(pl.col("plant") == plant).iter_rows(named=True):
            occ.setdefault(r["machine"], []).append((r["start_ts"], r["end_ts"]))
        for k in occ:
            occ[k].sort()

        # per-machine tyre rate, from what it actually built
        rate: dict = {}
        for r in (bs.filter(pl.col("plant") == plant)
                  .group_by("machine")
                  .agg(pl.col("qty").sum().alias("q"),
                       ((pl.col("end_ts") - pl.col("start_ts"))
                        .dt.total_seconds().sum() / 3600).alias("h"))
                  .iter_rows(named=True)):
            rate[r["machine"]] = r["q"] / max(r["h"], 1e-9)

        G = nx.DiGraph()
        SRC, SNK = "SRC", "SNK"
        total = 0.0
        node_cap: dict = {}
        for i, r in enumerate(cand.iter_rows(named=True)):
            q = float(r["qty"])
            total += q
            rn = f"run{i}"
            G.add_edge(SRC, rn, capacity=q)
            # the run's legal band: [ideal - window, ideal]
            lo = r["cure_first"] - dt.timedelta(hours=float(r["r5_window_h"]))
            hi = r["cure_first"]
            for mach in elig.get(r["gt_code"], set()):
                if mach not in occ and mach not in rate:
                    continue
                cur = lo
                gaps = []
                for s2, e2 in occ.get(mach, []):
                    if e2 <= lo or s2 >= hi:
                        continue
                    if s2 > cur:
                        gaps.append((cur, min(s2, hi)))
                    cur = max(cur, e2)
                if cur < hi:
                    gaps.append((cur, hi))
                for gs, ge in gaps:
                    h = (ge - gs).total_seconds() / 3600.0
                    if h <= 0.01:
                        continue
                    cap = h * rate.get(mach, 0.0)
                    if cap <= 0:
                        continue
                    iv = f"{mach}|{gs.isoformat()}"
                    node_cap[iv] = max(node_cap.get(iv, 0.0), cap)
                    G.add_edge(rn, iv, capacity=cap)
        for iv, cap in node_cap.items():
            G.add_edge(iv, SNK, capacity=cap)

        if SRC not in G or SNK not in G:
            print(f"  {plant}: no feasible edges")
            continue
        val, _ = nx.maximum_flow(G, SRC, SNK)
        naive = float(cand["qty"].sum())
        print(f"  {plant}:")
        print(f"    addressable runs                     {cand.height:>7,}")
        print(f"    per-run bound (ignores contention)   {naive:>7,.0f}  tyres")
        print(f"    MAX-FLOW bound (contention included) {val:>7,.0f}  tyres"
              f"   ({100 * val / max(naive, 1):.0f}% of it)")
        print(f"    lost purely to contention            {naive - val:>7,.0f}")
        print(f"    network: {G.number_of_nodes():,} nodes / "
              f"{G.number_of_edges():,} edges")


if __name__ == "__main__":
    main()
