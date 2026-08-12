"""L4b -- CAPACITY-FLOW FEASIBILITY.  Can building feed this month at all?

    python -m planner.cmbc.l4b_capacity_flow --month 2026-08

RUNS BEFORE L5, AND GATES IT.
  L5 seats cure campaigns first and L7 then discovers, one slice at a time, that
  building cannot feed some of them. That is a late and expensive way to learn a
  fact that is decidable up front: given the plant's allowable machine matrix and
  the real GT x machine cycle times, is there ANY assignment of the month's
  demand to machines that fits in the available hours?

  This layer answers exactly that, and nothing else. It does not schedule.

WHY MAX-FLOW AND NOT A GREEDY LOAD SUM
  A greedy "total demand hours <= total machine hours" check passes constantly
  while the plan is infeasible, because eligibility is SPARSE. With the allowable
  matrix hard, a PCR GT has a median of 2 machines. Demand can sit comfortably
  under the plant total and still be impossible, because the GTs that need
  machine M are collectively bigger than M -- a subset constraint (Hall's
  condition), not a total.

  So the question is posed as a flow:

      source --(required hours)--> GT --(inf)--> machine --(available hours)--> sink

  If max-flow < total required hours, the month is infeasible by the difference,
  and the MIN-CUT names the machine subset that binds. That is the diagnosis the
  brief asks for: overloaded subsets, GTs starved of eligible hours, and
  capacity that is present but unreachable.

  networkx is already a dependency; this is a flow algorithm, not a MILP or
  CP-SAT solver, so it is inside the project's locked-in constraints.

FOUR THINGS IT REPORTS
  1. overloaded machine subsets    -- from the min-cut
  2. unusable capacity             -- machine hours no eligible GT can consume
  3. GTs with insufficient hours   -- per-GT shortfall after the flow
  4. secondary-machine reliance    -- demand that cannot fit on its home machine

EXIT CODE
  0 feasible · 1 infeasible. `PLANNER_FLOW_GATE=0` downgrades to a warning so an
  infeasible month can still be planned deliberately (and its shortfall is then
  an expected, named number rather than a surprise in L7).
"""
from __future__ import annotations

import argparse
import calendar
import os
import sys
from pathlib import Path

import networkx as nx
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from planner import paths                                          # noqa: E402
from planner.cmbc import allowable, plant_ct                       # noqa: E402
from planner.config import CONFIG                                  # noqa: E402

UTIL_CAP = float(os.environ.get("PLANNER_MACH_UTIL_CAP", "0.95"))


def analyse(month: str) -> tuple[pl.DataFrame, pl.DataFrame, dict]:
    y, m = int(month[:4]), int(month[5:7])
    days = calendar.monthrange(y, m)[1]
    horizon_h = days * 24.0
    cap_h = horizon_h * UTIL_CAP

    req = pl.read_parquet(paths.wh_derived(f"net_requirement_{month}.parquet"))
    req = req.filter(~pl.col("residual")).select(
        ["plant", "gt_code", "gross_build"])

    # ELIGIBILITY = exactly what L7 will use. Same two hard filters, same order.
    cm = allowable.restrict(
        pl.read_parquet(paths.wh_derived(f"cap_machine_{month}.parquet")),
        label="cap_machine", quiet=True)
    cm = allowable.restrict_rimlock(cm, label="cap_machine", quiet=True)
    home = dict(zip(zip(cm["plant"], cm["gt_code"]), cm["home"])) \
        if "home" in cm.columns else {}

    PCT = plant_ct.get()
    per_gt, per_mach, summary = [], [], {}

    for plant in ("PCR", "TBR"):
        rq = req.filter(pl.col("plant") == plant)
        el = cm.filter(pl.col("plant") == plant)
        if rq.height == 0 or el.height == 0:
            continue
        machines = sorted(el["machine"].unique().to_list())
        elig: dict[str, list[str]] = {}
        for r in el.iter_rows(named=True):
            elig.setdefault(r["gt_code"], []).append(r["machine"])

        # required hours per (GT, machine) uses the REAL per-machine cadence; the
        # GT's requirement in hours therefore depends on where it runs. The flow
        # needs one number per GT, so use the mean over its eligible machines --
        # the same estimator L7 uses for its ordering key (`_est_cad`).
        need_h: dict[str, float] = {}
        ct_of: dict[tuple, float] = {}
        for r in rq.iter_rows(named=True):
            g, q = r["gt_code"], float(r["gross_build"])
            ms = elig.get(g, [])
            if not ms:
                need_h[g] = float("inf")          # no machine at all
                continue
            cts = []
            for mc in ms:
                c = PCT.build_ct_s(plant, g, mc) or 0.0
                if c:
                    ct_of[(g, mc)] = c
                    cts.append(c)
            cad = (sum(cts) / len(cts)) if cts else 60.0
            need_h[g] = q * cad / 3600.0

        G = nx.DiGraph()
        SRC, SNK = "_src", "_snk"
        total_need = 0.0
        for g, h in need_h.items():
            if h == float("inf"):
                continue
            total_need += h
            G.add_edge(SRC, f"g::{g}", capacity=h)
            for mc in elig.get(g, []):
                G.add_edge(f"g::{g}", f"m::{mc}", capacity=float("inf"))
        for mc in machines:
            G.add_edge(f"m::{mc}", SNK, capacity=cap_h)

        flow_val, flow = nx.maximum_flow(G, SRC, SNK)
        cut_val, (reach, unreach) = nx.minimum_cut(G, SRC, SNK)
        # machines on the sink side of the cut are the binding (saturated) set
        binding = sorted(n[3:] for n in unreach if n.startswith("m::"))

        alloc_m: dict[str, float] = {mc: 0.0 for mc in machines}
        for g in need_h:
            for mc, v in flow.get(f"g::{g}", {}).items():
                if mc.startswith("m::"):
                    alloc_m[mc[3:]] += v
        for g, h in need_h.items():
            got = 0.0 if h == float("inf") else sum(
                v for k, v in flow.get(f"g::{g}", {}).items() if k.startswith("m::"))
            per_gt.append({
                "plant": plant, "gt_code": g,
                "n_eligible": len(elig.get(g, [])),
                "need_h": None if h == float("inf") else round(h, 2),
                "alloc_h": round(got, 2),
                "short_h": None if h == float("inf") else round(max(h - got, 0.0), 2),
                "home": home.get((plant, g)),
                "no_machine": h == float("inf"),
            })
        for mc in machines:
            reachable = sum(need_h.get(g, 0.0) for g, ms in elig.items()
                            if mc in ms and need_h.get(g) != float("inf"))
            per_mach.append({
                "plant": plant, "machine": mc,
                "cap_h": round(cap_h, 2),
                "alloc_h": round(alloc_m[mc], 2),
                "load_pct": round(100 * alloc_m[mc] / cap_h, 1),
                "reachable_need_h": round(reachable, 2),
                "unusable_h": round(max(cap_h - reachable, 0.0), 2),
                "binding": mc in binding,
                "n_gts_eligible": sum(1 for ms in elig.values() if mc in ms),
            })
        summary[plant] = {
            "need_h": round(total_need, 1),
            "capacity_h": round(cap_h * len(machines), 1),
            "max_flow_h": round(flow_val, 1),
            "short_h": round(max(total_need - flow_val, 0.0), 1),
            "feasible": total_need - flow_val < 1e-6,
            "binding_machines": binding,
            "n_machines": len(machines),
            "no_machine_gts": [g for g, h in need_h.items() if h == float("inf")],
        }
    return pl.DataFrame(per_gt), pl.DataFrame(per_mach), summary


def main() -> None:
    ap = argparse.ArgumentParser(description="pre-L5 building feasibility, by max-flow")
    ap.add_argument("--month", default="2026-08")
    a = ap.parse_args()

    gt, mach, s = analyse(a.month)
    gt.write_parquet(paths.wh_derived(f"l4b_flow_gt_{a.month}.parquet"))
    mach.write_parquet(paths.wh_derived(f"l4b_flow_machine_{a.month}.parquet"))

    print(f"\n  L4b CAPACITY-FLOW FEASIBILITY  {a.month}")
    print(f"  {'-' * 72}")
    infeasible = False
    for plant, v in s.items():
        ok = "FEASIBLE" if v["feasible"] else "INFEASIBLE"
        infeasible |= not v["feasible"]
        print(f"  {plant}  {ok}")
        print(f"     need {v['need_h']:>9,.0f} h · capacity {v['capacity_h']:>9,.0f} h "
              f"over {v['n_machines']} machines · max-flow {v['max_flow_h']:>9,.0f} h")
        if v["short_h"] > 0:
            print(f"     SHORT {v['short_h']:,.0f} h  ({100*v['short_h']/max(v['need_h'],1):.1f}% "
                  f"of required build hours cannot be placed on ANY allowable machine)")
        if v["binding_machines"]:
            print(f"     binding (saturated) machines: {', '.join(v['binding_machines'])}")
        if v["no_machine_gts"]:
            print(f"     GTs with NO eligible machine: {len(v['no_machine_gts'])}")
        mm = mach.filter(pl.col("plant") == plant)
        un = mm.filter(pl.col("unusable_h") > 0)
        if un.height:
            print(f"     unusable capacity: {un['unusable_h'].sum():,.0f} h across "
                  f"{un.height} machines (no eligible GT can reach it)")
            for r in un.sort("unusable_h", descending=True).head(4).iter_rows(named=True):
                print(f"        {r['machine']:<16} cap {r['cap_h']:>7,.0f} h · reachable "
                      f"{r['reachable_need_h']:>7,.0f} h · idle-by-eligibility "
                      f"{r['unusable_h']:>7,.0f} h · {r['n_gts_eligible']} GTs")
        gg = gt.filter((pl.col("plant") == plant) & (pl.col("short_h") > 0))
        if gg.height:
            print(f"     GTs short of eligible hours: {gg.height} "
                  f"({gg['short_h'].sum():,.0f} h)")
            for r in gg.sort("short_h", descending=True).head(4).iter_rows(named=True):
                print(f"        {r['gt_code']:<30} need {r['need_h']:>7,.0f} h · got "
                      f"{r['alloc_h']:>7,.0f} h · {r['n_eligible']} machines")
        sec = gt.filter((pl.col("plant") == plant) & (pl.col("n_eligible") > 1))
        print(f"     {sec.height} GTs have a secondary allowable machine; "
              f"{gt.filter((pl.col('plant') == plant) & (pl.col('n_eligible') == 1)).height} "
              f"are single-machine (no alternative if it is busy)")
        print()

    if infeasible and os.environ.get("PLANNER_FLOW_GATE", "1") != "0":
        print("  REFUSING TO PROCEED TO CURING -- building cannot support this demand.")
        print("  Set PLANNER_FLOW_GATE=0 to plan anyway (the shortfall is then expected).\n")
        sys.exit(1)
    print("  building allocation can support the demand -> proceed to L5\n")
    _ = CONFIG


if __name__ == "__main__":
    main()


# ==========================================================================
# B -- FAMILY-MINIMISING ALLOCATION
# ==========================================================================
# Max-flow proves the month CAN be built. It does not say WHERE it should be,
# and any feasible flow will do -- so the flow happily spreads one rim across
# every machine that can take it. That is the structural cause of PCR's setup
# bill: L7 inherits a scattered allocation and can only reorder inside it.
#
# This pass re-allocates the SAME hours with a second objective: minimise the
# number of rim families a machine has to carry. Fewer families per machine ->
# fewer different-size changeovers -> setup time returns as CONTIGUOUS hours,
# which is what the rim-fill result showed is worth more than average hours
# (weighted CO 129.2 -> 112.8 min/machine-day and fulfilment UP, both plants).
#
# GREEDY, FAMILY-FIRST, CAPACITY-RESPECTING -- no MILP (project rule):
#   1. order rim families by total hours, largest first
#   2. for each family, order its GTs largest first
#   3. place each GT on the eligible machine that ALREADY carries its family and
#      still has room; else the emptiest eligible machine
#   4. split across machines only when one cannot hold the GT
#
# It never widens eligibility: every candidate comes from the same allowable-
# and-rimlock-filtered set the flow used, so allocation cannot introduce a
# violation. A GT that does not fit anywhere is reported, not forced.
def allocate(month: str) -> tuple[pl.DataFrame, dict]:
    import calendar
    y, m = int(month[:4]), int(month[5:7])
    cap_h = calendar.monthrange(y, m)[1] * 24.0 * UTIL_CAP

    req = (pl.read_parquet(paths.wh_derived(f"net_requirement_{month}.parquet"))
           .filter(~pl.col("residual")).select(["plant", "gt_code", "gross_build"]))
    cm = allowable.restrict(
        pl.read_parquet(paths.wh_derived(f"cap_machine_{month}.parquet")),
        label="cap_machine", quiet=True)
    cm = allowable.restrict_rimlock(cm, label="cap_machine", quiet=True)
    sz = (pl.read_parquet(paths.input_derived("gt_size.parquet"))
          .select(["plant", "gt_code", "rim"]).unique(subset=["plant", "gt_code"]))
    rim_of = {(r["plant"], r["gt_code"]): (r["rim"] or "?")
              for r in sz.iter_rows(named=True)}
    PCT = plant_ct.get()

    rows, summary = [], {}
    for plant in ("PCR", "TBR"):
        rq = req.filter(pl.col("plant") == plant)
        el = cm.filter(pl.col("plant") == plant)
        if rq.height == 0 or el.height == 0:
            continue
        elig: dict[str, list[str]] = {}
        for r in el.iter_rows(named=True):
            elig.setdefault(r["gt_code"], []).append(r["machine"])
        machines = sorted(el["machine"].unique().to_list())
        free = {mc: cap_h for mc in machines}
        fams: dict[str, set] = {mc: set() for mc in machines}

        need: list[tuple[str, str, float, float]] = []   # fam, gt, hours, qty
        for r in rq.iter_rows(named=True):
            g, q = r["gt_code"], float(r["gross_build"])
            ms = elig.get(g, [])
            if not ms:
                continue
            cts = [PCT.build_ct_s(plant, g, mc) or 0.0 for mc in ms]
            cts = [c for c in cts if c] or [60.0]
            need.append((rim_of.get((plant, g), "?"), g,
                         q * (sum(cts) / len(cts)) / 3600.0, q))

        fam_h: dict[str, float] = {}
        for fam, _g, h, _q in need:
            fam_h[fam] = fam_h.get(fam, 0.0) + h
        need.sort(key=lambda x: (-fam_h[x[0]], x[0], -x[2], x[1]))

        unplaced = 0.0
        for fam, g, h, q in need:
            cands = elig[g]
            left = h
            while left > 1e-9:
                # prefer a machine already carrying this family, then the emptiest
                pool = [mc for mc in cands if free[mc] > 1e-9]
                if not pool:
                    unplaced += left
                    break
                pool.sort(key=lambda mc: (fam not in fams[mc], -free[mc], mc))
                mc = pool[0]
                take = min(left, free[mc])
                free[mc] -= take
                fams[mc].add(fam)
                rows.append({"plant": plant, "gt_code": g, "machine": mc,
                             "family": fam, "alloc_h": round(take, 3),
                             "alloc_qty": round(q * take / h, 1) if h else 0.0})
                left -= take
        fpm = [len(v) for mc, v in fams.items() if cap_h - free[mc] > 1e-9]
        summary[plant] = {
            "machines_used": len(fpm),
            "families": len(fam_h),
            "families_per_machine_mean": round(sum(fpm) / max(len(fpm), 1), 2),
            "families_per_machine_max": max(fpm) if fpm else 0,
            "unplaced_h": round(unplaced, 1),
        }
    return pl.DataFrame(rows), summary
