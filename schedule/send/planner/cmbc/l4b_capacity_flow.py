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
    ap.add_argument("--run", default=None,
                    help="with --timed: the run whose cure_campaigns supply deadlines")
    ap.add_argument("--timed", action="store_true",
                    help="deadline-aware ALLOCATION only; runs after L5, before L7")
    a = ap.parse_args()

    if a.timed:
        if not a.run:
            raise SystemExit("--timed needs --run")
        al, s = allocate_timed(a.month, a.run)
        al.write_parquet(paths.wh_derived(f"l4b_alloc_{a.month}.parquet"))
        print(f"\n  L4b DEADLINE-AWARE ALLOCATION  {a.month}  (from runs/{a.run})")
        for p, v in s.items():
            print(f"    {p}: {v['machines_used']} machines · families/machine "
                  f"mean {v['families_per_machine_mean']} max "
                  f"{v['families_per_machine_max']} · unplaced {v['unplaced_h']:,.0f} h"
                  f" · deadline span {v['deadline_span_h']:,.0f} h")
        print(f"  -> l4b_alloc_{a.month}.parquet ({al.height} rows)\n")
        return

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



# ==========================================================================
# B -- SCARCITY-FIRST, SAME-RIM ALLOCATION
# ==========================================================================
# Max-flow proves the month CAN be built. It does not say WHERE, and any feasible
# flow will do -- so it spreads one rim across every machine that can take it.
#
# THE DEFECT THIS REPLACES (found 2026-08-12, July PCR)
#   The first version ordered by RIM FAMILY SIZE and filled machines greedily, so
#   widely-eligible machines claimed the work first and narrow ones got nothing:
#
#       machine        GTs L4b allocated    GTs L7 actually built
#       TBMPCR7                        0                        1
#       TBMPCR10                       1                        7
#
#   TBMPCR7 may build only 6 of July's 48 demanded PCR GTs. Allocating it LAST
#   meant its few eligible GTs had already gone elsewhere, so it idled 287 h --
#   and L7 then dumped 32,939 tyres of a single GT on it anyway. The allocator
#   and the placer disagreed about the same machine.
#
# THE RULE, WHICH IS L7'S OWN AND WAS MISSING HERE
#   Most-constrained-first. L7 already assigns "a GT with 3 options must claim
#   before one with 11"; the allocator now does the same from the MACHINE side --
#   a machine few GTs can reach gets first pick of the GTs that can reach it.
#   Flexible machines are then left free to absorb the variety, which is also
#   what keeps their family count (and their changeover bill) down.
#
# ORDER WITHIN A MACHINE -- the plant's own sequencing preference
#   1. anchor: the largest eligible GT, i.e. the work this machine exists for
#   2. remaining GTs of the SAME RIM            -> same-size changeover
#   3. inside that rim, same sister / construction cluster ranked first
#   4. other rims only when nothing same-rim remains
#   ties break on largest remaining shortfall, so scarce hours buy the most.
#
#   Sister and cluster rank ONLY INSIDE A RIM. Cross-rim similarity reordering
#   was measured on this codebase and cost 25,549 tyres -- see l5_cure_master.
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

    # sister / construction cluster -- used ONLY to rank inside a rim
    fam_of: dict[tuple, str] = {}
    f = paths.input_derived("gt_sister_group.parquet")
    if f.exists():
        for r in pl.read_parquet(f).iter_rows(named=True):
            fam_of[(r["plant"], r["gt_code"])] = str(r.get("sister_id") or "")
    f = paths.input_derived("sku_con_cluster.parquet")
    if f.exists():
        for r in pl.read_parquet(f).iter_rows(named=True):
            k = (r["plant"], r["gt_code"])
            if not fam_of.get(k):
                fam_of[k] = "cl" + str(r.get("con_cluster"))
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

        need_h: dict[str, float] = {}
        need_q: dict[str, float] = {}
        for r in rq.iter_rows(named=True):
            g, q = r["gt_code"], float(r["gross_build"])
            ms = elig.get(g, [])
            if not ms:
                continue
            cts = [c for c in (PCT.build_ct_s(plant, g, mc) or 0.0 for mc in ms) if c]
            cts = cts or [60.0]
            need_h[g] = q * (sum(cts) / len(cts)) / 3600.0
            need_q[g] = q

        reach: dict[str, list[str]] = {mc: [] for mc in machines}
        for g, ms in elig.items():
            if g in need_h:
                for mc in ms:
                    reach[mc].append(g)
        free = {mc: cap_h for mc in machines}
        fams: dict[str, set] = {mc: set() for mc in machines}
        left = dict(need_h)

        # MACHINES IN ORDER OF INCREASING FLEXIBILITY -- narrow ones pick first
        for mc in sorted(machines, key=lambda x: (len(reach[x]), x)):
            mine = [g for g in reach[mc] if left.get(g, 0.0) > 1e-9]
            if not mine:
                continue
            mine.sort(key=lambda g: -left[g])
            anchor = mine[0]
            a_rim = rim_of.get((plant, anchor), "?")
            a_fam = fam_of.get((plant, anchor), "")

            # PRIORITY, in the plant's stated order:
            #   hard feasibility  (already applied -- `mine` is eligible only)
            #   -> GT scarcity / risk of stranding
            #   -> same rim
            #   -> largest shortfall
            #   -> sister / construction cluster, INSIDE the rim
            #   -> load balance (the machine loop itself)
            #
            # GT SCARCITY COMES BEFORE SAME-RIM, and that ordering is the point.
            # A GT with 2 eligible machines that loses both to same-rim
            # preference is STRANDED -- it has nowhere else to go -- whereas a
            # GT with 9 machines can always be placed later. Ranking rim above
            # scarcity buys a same-size changeover and pays for it with volume
            # that becomes unbuildable.
            #
            # Cure deadline is NOT in this key: L4b runs BEFORE L5, so no cure
            # seat exists yet to read a deadline from. Adding it means moving the
            # allocation after L5, which reorders the pipeline -- worth doing, but
            # as its own measured change, not smuggled in here.
            def _key(g: str, _a=anchor, _ar=a_rim, _af=a_fam) -> tuple:
                r = rim_of.get((plant, g), "?")
                fm = fam_of.get((plant, g), "")
                return (0 if g == _a else 1,               # anchor
                        len(elig.get(g, [])),              # scarcity: fewest first
                        0 if r == _ar else 1,              # same rim
                        -left[g],                          # largest shortfall
                        0 if (r == _ar and fm and fm == _af) else 1)  # sister/cluster

            for g in sorted(mine, key=_key):
                if free[mc] <= 1e-9:
                    break
                take = min(left[g], free[mc])
                if take <= 1e-9:
                    continue
                free[mc] -= take
                left[g] -= take
                fams[mc].add(rim_of.get((plant, g), "?"))
                rows.append({"plant": plant, "gt_code": g, "machine": mc,
                             "family": rim_of.get((plant, g), "?"),
                             "alloc_h": round(take, 3),
                             "alloc_qty": round(need_q[g] * take / need_h[g], 1)
                             if need_h[g] else 0.0})

        unplaced = sum(v for v in left.values() if v > 1e-9)
        fpm = [len(v) for mc, v in fams.items() if cap_h - free[mc] > 1e-9]
        summary[plant] = {
            "machines_used": len(fpm),
            "families": len({rim_of.get((plant, g), "?") for g in need_h}),
            "families_per_machine_mean": round(sum(fpm) / max(len(fpm), 1), 2),
            "families_per_machine_max": max(fpm) if fpm else 0,
            "unplaced_h": round(unplaced, 1),
        }
    return pl.DataFrame(rows), summary

# ==========================================================================
# B2 -- DEADLINE-AWARE ALLOCATION.  Runs AFTER L5, before L7.
# ==========================================================================
# WHY THE PRE-L5 ALLOCATION WAS NOT ENOUGH
#   `allocate()` runs before L5 exists, so it distributes machine HOURS with no
#   idea WHEN those hours are needed. Measured on July PCR: TBMPCR7 was handed
#   707 h -- ample in total -- and still finished with 96 h idle while its own
#   anchor GT was 1,539 tyres short. 20 of its 27 idle gaps were individually
#   large enough for a legal run; they were simply in days 2-29 while every
#   unfed GT 1513 seat sat at day 1 07:00 or day 30.
#
#   Hours adequate in TOTAL and useless in POSITION. That is the defect.
#
# WHAT CHANGES
#   Once L5 has run, every GT has real cure seats with real timestamps. The
#   allocation can then rank by CURE DEADLINE -- a GT whose first seat is on day
#   2 must claim machine hours before one whose first seat is on day 20, however
#   large the latter is. That is the term the pre-L5 version structurally could
#   not have.
#
# FULL PRIORITY ORDER (the plant's, now all of it)
#   1 hard feasibility        -- only allowable (GT, machine) pairs are candidates
#   2 GT scarcity / stranding -- fewest eligible machines claims first
#   3 same rim                -- same-size changeover
#   4 cure deadline           -- earliest first-seat wins            <- NEW
#   5 largest shortfall       -- scarce hours buy the most volume
#   6 same GT                 -- whole-GT allocation keeps runs intact
#   7 sister / cluster        -- ranked only INSIDE the rim
#   8 lowest changeover       -- rim continuity against the machine's anchor
#   9 load balance            -- the machine loop, narrow machines first
#
# It emits the SAME schema as `allocate()`, so L7 consumes it unchanged and
# PLANNER_L4B_ALLOC=0 still disables the whole mechanism.
def allocate_timed(month: str, run: str) -> tuple[pl.DataFrame, dict]:
    import calendar
    y, m = int(month[:4]), int(month[5:7])
    cap_h = calendar.monthrange(y, m)[1] * 24.0 * UTIL_CAP

    camp = pl.read_parquet(paths.RUNS / run / "cure_campaigns.parquet")
    # time-positioned cure demand: what each GT actually needs, and by when
    dl = (camp.group_by(["plant", "gt_code"])
              .agg(pl.col("start_ts").min().alias("first_seat"),
                   pl.col("qty").sum().alias("cure_qty")))
    t0 = camp["start_ts"].min()
    dead = {(r["plant"], r["gt_code"]):
            (r["first_seat"] - t0).total_seconds() / 3600.0
            for r in dl.iter_rows(named=True)}

    cm = allowable.restrict(
        pl.read_parquet(paths.wh_derived(f"cap_machine_{month}.parquet")),
        label="cap_machine", quiet=True)
    cm = allowable.restrict_rimlock(cm, label="cap_machine", quiet=True)
    sz = (pl.read_parquet(paths.input_derived("gt_size.parquet"))
          .select(["plant", "gt_code", "rim"]).unique(subset=["plant", "gt_code"]))
    rim_of = {(r["plant"], r["gt_code"]): (r["rim"] or "?")
              for r in sz.iter_rows(named=True)}
    fam_of: dict[tuple, str] = {}
    f = paths.input_derived("gt_sister_group.parquet")
    if f.exists():
        for r in pl.read_parquet(f).iter_rows(named=True):
            fam_of[(r["plant"], r["gt_code"])] = str(r.get("sister_id") or "")
    f = paths.input_derived("sku_con_cluster.parquet")
    if f.exists():
        for r in pl.read_parquet(f).iter_rows(named=True):
            k = (r["plant"], r["gt_code"])
            if not fam_of.get(k):
                fam_of[k] = "cl" + str(r.get("con_cluster"))
    PCT = plant_ct.get()

    rows, summary = [], {}
    for plant in ("PCR", "TBR"):
        el = cm.filter(pl.col("plant") == plant)
        cq = dl.filter(pl.col("plant") == plant)
        if not el.height or not cq.height:
            continue
        elig: dict[str, list[str]] = {}
        for r in el.iter_rows(named=True):
            elig.setdefault(r["gt_code"], []).append(r["machine"])
        machines = sorted(el["machine"].unique().to_list())

        # requirement in HOURS, from the cure quantity L5 actually seated
        need_h: dict[str, float] = {}
        need_q: dict[str, float] = {}
        for r in cq.iter_rows(named=True):
            g, q = r["gt_code"], float(r["cure_qty"])
            ms = elig.get(g, [])
            if not ms:
                continue
            cts = [c for c in (PCT.build_ct_s(plant, g, mc) or 0.0 for mc in ms) if c]
            cts = cts or [60.0]
            need_h[g] = q * (sum(cts) / len(cts)) / 3600.0
            need_q[g] = q

        reach: dict[str, list[str]] = {mc: [] for mc in machines}
        for g, ms in elig.items():
            if g in need_h:
                for mc in ms:
                    reach[mc].append(g)
        free = {mc: cap_h for mc in machines}
        fams: dict[str, set] = {mc: set() for mc in machines}
        left = dict(need_h)

        for mc in sorted(machines, key=lambda x: (len(reach[x]), x)):
            mine = [g for g in reach[mc] if left.get(g, 0.0) > 1e-9]
            if not mine:
                continue
            # anchor = earliest-deadline among this machine's largest work, so a
            # narrow machine anchors on what it must feed FIRST, not merely on
            # what is biggest.
            mine.sort(key=lambda g: (dead.get((plant, g), 1e9), -left[g]))
            anchor = mine[0]
            a_rim = rim_of.get((plant, anchor), "?")
            a_fam = fam_of.get((plant, anchor), "")

            def _key(g, _a=anchor, _ar=a_rim, _af=a_fam):
                r = rim_of.get((plant, g), "?")
                fm = fam_of.get((plant, g), "")
                return (0 if g == _a else 1,                       # 6 same GT
                        len(elig.get(g, [])),                      # 2 scarcity
                        0 if r == _ar else 1,                      # 3/8 same rim
                        dead.get((plant, g), 1e9),                 # 4 deadline
                        -left[g],                                  # 5 shortfall
                        0 if (r == _ar and fm and fm == _af) else 1)  # 7 sister
            for g in sorted(mine, key=_key):
                if free[mc] <= 1e-9:
                    break
                take = min(left[g], free[mc])
                if take <= 1e-9:
                    continue
                free[mc] -= take
                left[g] -= take
                fams[mc].add(rim_of.get((plant, g), "?"))
                rows.append({"plant": plant, "gt_code": g, "machine": mc,
                             "family": rim_of.get((plant, g), "?"),
                             "alloc_h": round(take, 3),
                             "alloc_qty": round(need_q[g] * take / need_h[g], 1)
                             if need_h[g] else 0.0})

        unplaced = sum(v for v in left.values() if v > 1e-9)
        fpm = [len(v) for mc, v in fams.items() if cap_h - free[mc] > 1e-9]
        summary[plant] = {
            "machines_used": len(fpm),
            "families_per_machine_mean": round(sum(fpm) / max(len(fpm), 1), 2),
            "families_per_machine_max": max(fpm) if fpm else 0,
            "unplaced_h": round(unplaced, 1),
            "deadline_span_h": round(max(dead.values()) if dead else 0.0, 1),
        }
    return pl.DataFrame(rows), summary

if __name__ == "__main__":
    main()
