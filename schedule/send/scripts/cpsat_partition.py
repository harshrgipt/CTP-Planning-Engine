"""CP-SAT GT -> MACHINE PARTITION, built from committed masters only.

    PYTHONPATH=. python scripts/cpsat_partition.py 2026-07 [seconds]
    -> INPUT/derived/gt_machine_partition.parquet   (month-stamped)

WHY THIS EXISTS
  `scripts/build_gt_machine_partition.py` is a 4-tier greedy (LPT + a booking
  order copied from the plant's 8-month machine x size matrix). It is a good
  design -- it copies the plant's STRUCTURE rather than wiring a mined median in
  as a constraint -- but it has two problems here:

    1. it mines `v_build`, so it needs the 4.4 GB raw MES drop, which a clone
       does not have. The file on disk is stamped 2026-08 and cannot be rebuilt
       for July.
    2. so every run set PLANNER_PARTITION_PLANTS= (empty) and the partition was
       not used at all. THAT IS NO LONGER TRUE -- the default is "PCR" as of
       2026-08-17 and this file's own measurements below were taken with it on.

  What the partition exists to do is hold a GT on one machine for the month and
  so cut DIFFERENT-SIZE changeovers. The gap it is closing: the plant keeps
  66.7 % of PCR GTs on exactly ONE machine and holds 91.5 % same-size
  changeovers; we were at 27.5 % one-machine and 86.9 % same-size. (An earlier
  version of this paragraph read "our same-size share is 27.5 %" -- that is the
  ONE-MACHINE-GT figure, not same-size, and it overstated the gap by ~60 points.
  PARTITION_AND_CHANGEOVER section on P7 has the table.)

  This script rebuilds the same object WITHOUT the MES. The machine x rim matrix
  is reconstructed from `cap_machine_<M>.parquet` (basis OBSERVED carries
  `n_used`, mined from the same MES and committed to the repo) joined to
  `gt_size`. So a clone can build a July-stamped partition and the question
  becomes testable.

WHY CP-SAT HERE AND NOT ELSEWHERE
  Eleven attempts this session to re-TIME work all lost. The one change that
  gained (+1,956) re-ALLOCATED work between resources at fixed times. This is
  purely the second kind: it decides WHICH MACHINE builds each GT and never
  touches a clock. L7 still does all the timing.

  The model is small -- ~41 GTs x 11 machines -- so CP-SAT closes it properly
  rather than returning a feasible-but-unfinished answer, which is what went
  wrong every time it was pointed at the scheduling problem.

MODEL   (rewritten 2026-08-18 -- the previous block described neither the
         objective nor the variables the code actually builds)
    x[g][m]     1 if GT g uses machine m           (eligibility-restricted)
    u[g][m]     quantity, in lots of GRAN tyres    SPLITS ARE REQUIRED, see below
    r[m][k]     1 if machine m carries rim k
      sum_m u[g][m] == units(g)                    all of g lands somewhere
      sum_g u[g][m] * sec(m) <= UTIL * month_hours no machine over-booked
      x[g][m] <= r[m][rim(g)]                      a GT drags its rim in
      hi_G - lo_G <= IMB_CAP_H                     per B16 GROUP, not plant-wide
      sum_m r[m][k] >= ceil(...)                   rim-count cuts, OFF by default
    minimise  sum_m diff_min(m) * rims_on_m
            + sum_g diff_min * (machines_used(g) - 1)

  Every extra rim on a machine is a different-size changeover, priced at THAT
  machine's own rate (PCR 1-5 cost 60 min, 6-11 cost 42, TBR all 24 -- so a
  second rim belongs on a cheap machine). The second term prices run
  fragmentation and was swept: x1 is the peak, see the table at the split cost.

  IMBALANCE IS A CONSTRAINT, NOT AN OBJECTIVE TERM. It used to be
  `+ LOAD_W * (hi - lo)` and that is why the model never closed -- see the
  comment at the Minimize call. `LOAD_W` is dead.

G4 REPAIR PASS -- NOT PORTED HERE, AND THE REASON MATTERS
  `PARTITION_AND_CHANGEOVER.md` section 2 documents a G4 repair pass in the GREEDY
  builder ("prefer size changes where they cost least"): it measured 60.0 -> 48.9
  min per different-size changeover, against the plant's own 55.7, with +0.9 pt of
  fulfilment as a side effect. That pass belongs to `build_gt_machine_partition.py`
  and did NOT come across when CP-SAT became the default builder on 2026-08-17.
  July now measures 57.2 min -- the gain is gone and we are worse than the plant --
  and `l7_pull_release.py` still carries a comment ASSUMING the pass runs.

  A port was started on 2026-08-19 and abandoned mid-edit; this note replaced a
  docstring that promised a `_g4_repair` function and a `PLANNER_PART_G4` flag,
  NEITHER OF WHICH EXISTS. Do not trust that pair if you see it referenced.

  Before porting it, settle this: the objective below already prices each extra
  rim at THAT machine's own `diff_min`, and on a partition proved OPTIMAL no
  cost-reducing relocation can exist -- G4 would be a no-op by construction. So if
  the realised 57.2 min is worse than the greedy's 48.9, the loss is NOT in the
  partition, it is in how L7 SEQUENCES and spills against it. Port the pass only
  after measuring where the cost actually accrues.
"""
from __future__ import annotations

import calendar
import hashlib
import json
import os
import sys
from pathlib import Path

import polars as pl
from ortools.sat.python import cp_model

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from planner import paths                                        # noqa: E402
from planner.cmbc import allowable                                # noqa: E402
from planner.config import CONFIG                                 # noqa: E402

D = ROOT / "warehouse" / "derived"
DER = paths.INPUT_DERIVED
# Share of calendar hours a machine may be booked to. An ENFORCED LIMIT, and it
# was hardcoded -- against PARTITION section 8's "add a cap in config.py or
# nowhere". It is also the SECOND 0.95 in the pipeline: `l2_ttl.LOAD_CAP` gates
# B16 group load against 100 % of calendar, this caps each machine at 95 % of it,
# and the same August TBR TL group therefore reads 92.9 % there and 97.4 % here.
# Two denominators, both labelled 0.95, in one run. Env-readable now so an arm can
# move it; reconciling the two bases needs a plant ruling on what 95 % means.
UTIL = float(os.environ.get("PLANNER_PART_UTIL", "0.95"))
# LOAD_W was the imbalance weight in the objective. Imbalance is a CONSTRAINT
# now (see the Minimize call), so this has no reader. Kept only so a search
# for it lands on this note rather than on nothing.
LOAD_W = None                     # DEAD -- see above
# THE DEFAULT IS DELIBERATELY LARGE: IT MUST NOT BIND.
#
# This started at 60 work units, chosen as "enough". It was not enough, and it was
# the whole reason PCR looked intractable: every run stopped at 51.6 % gap and
# reported FEASIBLE, and I concluded from that the model could not be closed --
# blaming bin-packing LP weakness. Wrong. Let the solver finish and PCR proves
# OPTIMAL at objective 1,044 (rims 16, better than the 1,092/17 the cut-off was
# producing), and two runs taking 965 s and 1,627 s of wall clock return
# BYTE-IDENTICAL partitions, because a closed model's answer does not depend on
# the clock.
#
# So the stopping rule is now: don't stop, close it. Both limits stay as safety
# nets and the wall-clock one shouts if it ever binds, since a bound cut-off is
# exactly what makes this file non-reproducible.
DET_BUDGET = float(os.environ.get("PLANNER_PARTITION_DET_TIME", "1e9"))
# Max hours between the busiest and idlest machine WITHIN a B16 group. A LIMIT,
# not a preference -- see the Minimize() call.
#
# THIS BINDS. An earlier version of this comment claimed "observed optima sit at
# 0-3 h, so this is slack, not a binding wall". Measured 2026-08-18 across four
# (plant, month) cells, the group spread sits at EXACTLY 8.0 h on two of them
# (July PCR, August TBR TT group). It is shaping the optimum, not bounding it
# loosely -- and because it binds FEASIBLY it will never trigger the "raise it if
# a month comes back INFEASIBLE" escape the old comment offered. The value is
# unmeasured: nobody has run 4 h or 16 h. Treat 8 as an untested constant.
IMB_CAP_H = float(os.environ.get("PLANNER_PARTITION_IMB_CAP_H", "8"))
_SPLIT_COST_USED = float(os.environ.get("PLANNER_PART_SPLIT_COST", "1"))
CAD_DEFAULT = {"PCR": 58.0, "TBR": 204.0}
DIFF_DEFAULT = {"PCR": 42.0, "TBR": 24.0}


def main() -> None:
    month = sys.argv[1] if len(sys.argv) > 1 else "2026-07"
    # 120 s was the old default and it CUT THE MODEL OFF -- `main.py` invokes this
    # script with no budget argument, so every pipeline run got the 120 s version
    # and therefore a FEASIBLE, arbitrary, non-reproducible PCR partition. PCR
    # needs ~1,000-1,650 s at one worker to prove optimality; TBR takes 2 s. This
    # runs once a month, so minutes are free and a wrong partition is not.
    budget = float(sys.argv[2]) if len(sys.argv) > 2 else 3600.0
    y, m = int(month[:4]), int(month[5:7])
    ndays = calendar.monthrange(y, m)[1]
    CAPH = UTIL * ndays * 24

    sz = pl.read_parquet(DER / "gt_size.parquet")
    rim = {r["gt_code"]: str(r["rim"]) for r in sz.iter_rows(named=True)
           if r.get("gt_code") and r.get("rim")}
    nr = pl.read_parquet(D / f"net_requirement_{month}.parquet").filter(
        ~pl.col("residual"))
    cm = allowable.restrict(pl.read_parquet(D / f"cap_machine_{month}.parquet"),
                            label="partition", quiet=True)
    ctb = pl.read_parquet(D / "cycle_time_building.parquet")
    try:
        cch = pl.read_parquet(D / "cap_changeover.parquet")
    except Exception:                                            # noqa: BLE001
        cch = None

    print("=" * 78)
    print(f"  CP-SAT GT->MACHINE PARTITION   {month}   budget {budget:.0f}s")
    print("=" * 78)

    # ---- B16 IS AN INPUT TO THIS MODEL, NOT SOMETHING DOWNSTREAM FIXES ------
    # This partition is built from `cap_machine` and `allowed_machine_matrix`,
    # neither of which carries a TT/TL dimension -- so it had no idea B16 existed
    # and assigned TBR GTs across the boundary freely. L7 applies the group filter
    # in `_cand()`, but the partition has ALREADY chosen the machine by then, so
    # the filter never gets to bind.
    #
    # Measured 2026-08-17 on July, with the partition switched on by default:
    #   8 partition rows (10,487 tyres) assigned a TL GT to a TT machine
    #   367 build lots / 9,537 tyres were then actually built across the boundary
    # every one of them on TBMTBR4 -- the plant's designated flex machine, which
    # the July split had placed in the TT group.
    #
    # This was invisible in the KPI: TBR moved 96.3 -> 96.2 %, which reads as
    # noise. It is the "aggregate metric hiding a per-segment regression" failure
    # mode from EXPERT_AUDIT.md, and it was introduced by switching the partition
    # on -- before that, B16 chose the machine because nothing else did.
    #
    # INERT AT THE SHIPPED DEFAULT -- read this before crediting it with anything.
    # `PLANNER_PARTITION_PLANTS` defaults to "PCR", and `l7_pull_release.py`
    # filters `if r["plant"] in PARTITION_PLANTS`, so the TBR rows this block
    # restricts are discarded by L7 before they reach a plan. The boundary
    # violation it fixes existed only while the default was "PCR,TBR" -- a state
    # that shipped for one day (2026-08-17) and was reverted on measurement. Keep
    # the restriction: it is what makes re-enabling TBR safe. Do not attribute a
    # KPI movement to it.
    #
    # ALSO: the mechanism sentence above is wrong about the month. July's split
    # puts TBMTBR4 in TL, not TT (verified against cap_ttl_groups_2026-07 in the
    # working tree AND at commit b98d434); TBMTBR4 is TT in AUGUST. The counts
    # (8 rows / 10,487 tyres, 367 lots / 9,537 tyres) were measured; the story
    # attached to them was not.
    #
    # NOTE the open question underneath: `plant-authority` ruled on 2026-08-17
    # that B16 is over-hardened -- the plant's own R8 says "wherever possible" and
    # certifies 6 of 9 TBR machines for both tube types, with TBMTBR4 and TBMTBR9
    # as designated flex machines. So the spill above may not be WRONG so much as
    # UNINTENDED. Enforcing it here makes the engine consistent with its own
    # declared rule; relaxing the rule is a separate, plant-ruled decision.
    ttl_group: dict[str, str] = {}
    gt_tag: dict[str, str] = {}
    try:
        _g = pl.read_parquet(D / f"cap_ttl_groups_{month}.parquet")
        ttl_group = {r["machine"]: r["group"] for r in _g.iter_rows(named=True)}
        _tt = pl.read_parquet(DER / "tt_tl.parquet").filter(pl.col("sku") != "")
        _dm = pl.read_parquet(ROOT / "masters" / "demand"
                              / f"demand_{month}.parquet")
        _j = _dm.filter(pl.col("plant") == "TBR").join(
            _tt.select(["sku", "tt_tl"]).unique(subset=["sku"]),
            on="sku", how="left")
        gt_tag = {r["gt_code"]: r["tt_tl"] for r in _j.iter_rows(named=True)
                  if r["tt_tl"]}
    except Exception as exc:                                     # noqa: BLE001
        print(f"  !! B16 groups unreadable ({exc}) -- partition will NOT respect "
              f"the TT/TL boundary on TBR")

    out_rows = []
    all_optimal = True
    for plant in ("PCR", "TBR"):
        elig: dict = {}
        for r in cm.filter(pl.col("plant") == plant).iter_rows(named=True):
            elig.setdefault(r["gt_code"], []).append(r["machine"])
        if plant == "TBR" and ttl_group and gt_tag:
            _drop = _kept = 0
            for g in list(elig):
                tag = gt_tag.get(g)
                if not tag:
                    continue
                keep = [m for m in elig[g] if ttl_group.get(m, tag) == tag]
                if not keep:
                    # l2_ttl's `uncovered` gate is supposed to make this
                    # impossible. If it happens, say so and leave the GT
                    # unpartitioned rather than emitting an illegal row or an
                    # INFEASIBLE model -- L7 will assign it dynamically.
                    print(f"  !! B16 leaves {g} ({tag}) with no in-group machine "
                          f"-- left out of the partition")
                    del elig[g]
                    continue
                _drop += len(elig[g]) - len(keep)
                _kept += len(keep)
                elig[g] = keep
            print(f"  [B16] TBR eligibility restricted to TT/TL groups: "
                  f"{_drop} (GT, machine) pairs dropped, {_kept} kept")
        cad = {r["machine"]: float(r["s_per_tyre"])
               for r in ctb.filter(pl.col("plant") == plant).iter_rows(named=True)}
        diff = {}
        if cch is not None and "diff_min" in cch.columns:
            for r in cch.filter(pl.col("plant") == plant).iter_rows(named=True):
                diff[r["machine"]] = float(r["diff_min"])

        gts = []
        for r in (nr.filter(pl.col("plant") == plant)
                  .group_by("gt_code").agg(pl.col("gross_build").sum().alias("q"))
                  .iter_rows(named=True)):
            g = r["gt_code"]
            if g not in elig or not elig[g]:
                continue
            gts.append((g, float(r["q"])))
        # SORT, OR THE MODEL IS NOT THE SAME MODEL TWICE.
        # `group_by` above is maintain_order=False, so `gts` comes out in an
        # arbitrary order and the CP-SAT variables are therefore CREATED in an
        # arbitrary order -- which changes the search, which changes the answer.
        # No solver seed can fix that, because the seed makes the SEARCH
        # reproducible, not the MODEL. Two rebuilds of July gave rims-on-machines
        # 17 vs 20 and moved the plan 0.1 pt with num_search_workers=1, a fixed
        # random_seed AND a deterministic time limit all set. `elig` is sorted for
        # the same reason: it decides the order the per-machine literals appear in.
        gts.sort()
        for _g in elig:
            elig[_g] = sorted(set(elig[_g]))
        machines = sorted({mm for g, _ in gts for mm in elig[g]})
        if not gts or not machines:
            continue

        mdl = cp_model.CpModel()
        x, rvar = {}, {}
        rims = sorted({rim.get(g, "?") for g, _ in gts})
        for mm in machines:
            for k in rims:
                rvar[(mm, k)] = mdl.NewBoolVar(f"r_{mm}_{k}")
        # SPLITTING IS REQUIRED, NOT OPTIONAL.
        # Whole-GT assignment made PCR INFEASIBLE: GT 1513 alone is 55,224 tyres
        # = ~890 machine-hours against a 707 h machine, so no single machine can
        # hold it. The greedy script hits the same wall and splits exactly 2 PCR
        # GTs for the same reason. Model quantity per (GT, machine) and let the
        # solver decide where a split is needed, with a fixed charge per extra
        # machine so it never splits for free.
        # QUANTITY IS MODELLED IN LOTS OF `GRAN` TYRES, NOT SINGLE TYRES.
        #
        # Modelled per tyre, PCR could not be closed: objective 1,092 against a
        # bound of 528, a 51.6 % gap after 110 s, while TBR proved OPTIMAL in 4 s.
        # The asymmetry is domain size. The objective depends only on the BOOLEANS
        # (which rims sit on which machine, how many splits) -- the integers exist
        # solely to satisfy the capacity constraint -- but PCR's GT 1513 alone
        # carries a 0..55,224 domain, and CP-SAT still has to reason across it to
        # prove no cheaper rim layout fits. That is a large search that cannot
        # improve the objective at all.
        #
        # One tyre of resolution is meaningless anyway: B12 forbids a build run
        # below 150 (PCR) / 70 (TBR), so a partition that distinguishes 12,001
        # from 12,000 tyres on a machine is describing something the plant cannot
        # execute. Rounding UP is deliberate -- the modelled load is then never
        # less than the real one, so the capacity constraint stays conservative.
        # Exact totals are restored at emit time.
        # MEASURED AND REJECTED, 2026-07 PCR. Default 1 = no granularity.
        #   GRAN   PCR rims   PCR objective / bound   gap     TBR
        #     1       17         1,092 / 528          51.6%   OPTIMAL 240
        #    25       19         1,170 / 522          55.4%   OPTIMAL 216
        # The hypothesis was that PCR's huge quantity domains (GT 1513 alone is
        # 0..55,224) were what stopped the model closing. They were not: coarsening
        # them left the bound flat and made the SOLUTION worse. TBR improved, but
        # TBR already closes in seconds either way, and rim count is a proxy that
        # has already been shown to disagree with fulfilment on this plant.
        # Kept as a knob because it is sound and costs nothing switched off.
        GRAN = max(1, int(os.environ.get("PLANNER_PARTITION_GRAN", "1")))
        load, qty, units, tot_u = {}, {}, {}, {}
        for g, q in gts:
            qi = int(round(q))
            ug = max(1, -(-qi // GRAN))          # ceil
            tot_u[g] = ug
            parts = []
            for mm in elig[g]:
                v = mdl.NewBoolVar(f"x_{g}_{mm}")
                uv = mdl.NewIntVar(0, ug, f"u_{g}_{mm}")
                x[(g, mm)] = v
                units[(g, mm)] = uv
                qty[(g, mm)] = uv                # load terms scale by GRAN below
                mdl.Add(uv <= ug * v)
                mdl.Add(uv >= 1).OnlyEnforceIf(v)
                mdl.Add(v <= rvar[(mm, rim.get(g, "?"))])
                parts.append(uv)
            mdl.Add(sum(parts) == ug)
        # LOAD IN SECONDS, NOT HOURS.
        # `hv * 3600 == qty * sec` demands qty*sec be divisible by 3600 -- it
        # almost never is, so that constraint made BOTH plants INFEASIBLE. Sum
        # seconds directly: a plain linear expression with no division.
        CAPS = int(CAPH * 3600)
        for mm in machines:
            sec = int(round(cad.get(mm, CAD_DEFAULT[plant]))) * GRAN
            terms = [qty[(g, mm)] * sec for g, _ in gts if (g, mm) in qty]
            lv = mdl.NewIntVar(0, CAPS, f"L_{mm}")
            mdl.Add(lv == sum(terms))
            mdl.Add(lv <= CAPS)
            load[mm] = lv
        # VALID CUT: HOW MANY MACHINES EACH RIM MUST OCCUPY, AT MINIMUM.
        #
        # Without this the solver's lower bound is the trivial one -- one rim per
        # machine, nothing split -- which for PCR is 522 against solutions of
        # ~1,100, a 52 % gap that never moves however long it runs. That bound is
        # not merely loose, it is UNREACHABLE: a rim carrying more tyres than one
        # machine can build in a month needs two machines no matter how the rest
        # is arranged, and nothing in the model said so.
        #
        # For rim k: every tyre of that rim must be built on a machine carrying k,
        # each machine offers at most CAPH hours, and no tyre takes less than the
        # FASTEST eligible cadence. So
        #     sum_m r[m][k]  >=  ceil( qty_k * fastest_cadence / CAPH )
        # is implied by the existing constraints -- it adds no restriction, it
        # only tells CP-SAT something it could not derive. Using the fastest
        # cadence keeps it valid; a slower one would over-count and could cut off
        # the true optimum.
        # MEASURED AND REJECTED, 2026-07 PCR. Default OFF.
        #   cuts   PCR rims   objective / bound   gap
        #    off      17        1,092 / 528       51.6%
        #    on       19        1,164 / 576       50.5%
        # The cut did what it was designed to do -- it lifted the bound 522 -> 576
        # -- and the gap still did not close, because the remaining slack is not
        # in this constraint. It is valid and cannot exclude the true optimum, so
        # landing on 19 rims instead of 17 is the search taking a different path,
        # not a worse model. But "different path" is not a reason to ship, and the
        # 17-rim partition is the one that MEASURED 96.2 % on July. Off until a
        # variant beats that on fulfilment rather than on the proxy.
        _cuts = 0
        if os.environ.get("PLANNER_PARTITION_RIM_CUTS", "0") != "0":
            _fast = min(cad.values()) if cad else CAD_DEFAULT[plant]
            _rim_q: dict = {}
            for g, q in gts:
                _rim_q[rim.get(g, "?")] = _rim_q.get(rim.get(g, "?"), 0.0) + q
            for k, qk in _rim_q.items():
                need = -(-int(qk * _fast) // int(CAPH * 3600))     # ceil
                if need > 1:
                    mdl.Add(sum(rvar[(mm, k)] for mm in machines) >= need)
                    _cuts += 1
        if _cuts:
            print(f"        {_cuts} rim-count cuts added")

        # IMBALANCE IS MEASURED WITHIN A B16 GROUP, NOT ACROSS THE PLANT.
        #
        # It was global, over all machines of the plant, and that made August TBR
        # INFEASIBLE the moment B16 was enforced here. The two groups do not carry
        # equal work and cannot lend hours to each other -- that is what "closed
        # group" means -- so on August TT sits at 86.9 % of cap (~614 h/machine)
        # and TL at 97.4 % (~689 h/machine), a 75 h gap against an 8 h cap. The
        # constraint was demanding something B16 forbids.
        #
        # Balancing only makes sense among machines that could actually take each
        # other's work. PCR has one group, so nothing changes there.
        _groups: list[list[str]] = []
        if plant == "TBR" and ttl_group:
            for tag in ("TT", "TL"):
                ms = [mm for mm in machines if ttl_group.get(mm) == tag]
                if ms:
                    _groups.append(ms)
            _rest = [mm for mm in machines if mm not in ttl_group]
            if _rest:
                _groups.append(_rest)
        if not _groups:
            _groups = [list(machines)]
        _spread = []
        for _gi, _ms in enumerate(_groups):
            _h = mdl.NewIntVar(0, CAPS, f"hi{_gi}")
            _l = mdl.NewIntVar(0, CAPS, f"lo{_gi}")
            mdl.AddMaxEquality(_h, [load[mm] for mm in _ms])
            mdl.AddMinEquality(_l, [load[mm] for mm in _ms])
            _spread.append((_h, _l))
        hi, lo = _spread[0]

        cost = []
        # SPLIT COST -- SWEPT 2026-08-17 ON JULY. x1 IS THE PEAK. DO NOT RE-TUNE
        # WITHOUT RE-MEASURING.
        #
        #   cost   multi-machine GTs (PCR)   rims   ful% PCR   ful% TBR   unfed PCR
        #    x0            15                 17      95.8       96.3      10,071
        #    x1             6                 17      96.6       97.2       3,519
        #    x3             6                 17      96.0       96.4       9,305
        #
        # x0 was tried on the argument that this term DOUBLE-COUNTS: the real
        # changeover is a rim change and `dm * n_r` below already prices every rim
        # a machine carries, so a GT joining a machine that already holds its rim
        # should be free. The plant's own 1-year matrix supported it -- 34 of 49
        # PCR GTs run on more than one machine there against our 6 -- and 65 % of
        # our PCR starvation sits on single-machine GTs.
        #
        # It lost 0.8 pt and nearly TRIPLED unfed. The term is not double-counting
        # RIMS, it is pricing RUN FRAGMENTATION, which the rim term cannot see:
        # rims-on-machines stayed at 17 while splits went 6 -> 15. Splitting a GT
        # across more machines shortens each machine's run of it, and a run below
        # the B12 lot floor (PCR 150 / TBR 70) cannot form a legal lot at all.
        #
        # x3 confirms the peak is interior, and kills the tempting proxy: TBR at
        # x3 reached rims-on-machines 9, the theoretical minimum, and still lost
        # 0.8 pt. MINIMISING RIMS IS NOT MAXIMISING FULFILMENT.
        #
        # PLANNER_PART_SPLIT_COST rescales it.
        _sc = float(os.environ.get("PLANNER_PART_SPLIT_COST", "1"))
        globals()["_SPLIT_COST_USED"] = _sc
        if _sc:
            for g, q in gts:
                nm = mdl.NewIntVar(1, len(elig[g]), f"nm_{g}")
                mdl.Add(nm == sum(x[(g, mm)] for mm in elig[g]))
                cost.append(int(round(DIFF_DEFAULT[plant] * _sc)) * (nm - 1))
        for mm in machines:
            dm = int(round(diff.get(mm, DIFF_DEFAULT[plant])))
            n_r = mdl.NewIntVar(0, len(rims), f"nr_{mm}")
            mdl.Add(n_r == sum(rvar[(mm, k)] for k in rims))
            cost.append(dm * n_r)
        # IMBALANCE IS A CONSTRAINT, NOT AN OBJECTIVE TERM.
        #
        # It used to be `Minimize(60 * sum(cost) + (hi - lo))`, described as
        # "changeover dominant, imbalance a tiebreak". It was the opposite: `cost`
        # is minutes, so one extra rim prices at 60 x 42 = 2,520, while `hi - lo`
        # is SECONDS over a 0..2,545,200 range. The tiebreak outweighed the
        # objective by three orders of magnitude.
        #
        # It also stopped the model closing. Across that integer range the LP
        # relaxation is far too weak to prune, so CP-SAT never proved optimality
        # -- every run returned FEASIBLE and simply stopped wherever the budget
        # ran out. Measured: deterministic budget 60 -> 20 rims, 120 -> 19,
        # 8-worker runs wandered 17/19/20, against a lower bound of 11. Making
        # that reproducible (see the sort above) only fixed WHICH arbitrary point
        # it stopped at, not that it was arbitrary.
        #
        # Bounding imbalance instead leaves a pure changeover objective -- small
        # integers, tight bounds, closable -- and states the balance requirement
        # as what it actually is: a limit, not a preference.
        for _h, _l in _spread:
            mdl.Add(_h - _l <= int(IMB_CAP_H * 3600))
        mdl.Minimize(sum(cost))

        slv = cp_model.CpSolver()
        # DETERMINISM IS REQUIRED HERE, SPEED IS NOT.
        #
        # First attempt: num_search_workers=8. CP-SAT's portfolio search is
        # non-reproducible across workers, and the plan moved with it -- two
        # rebuilds of the SAME month from the SAME inputs gave July PCR 96.0 % vs
        # 95.9 % and L11 30/48 vs 31/48.
        #
        # Second attempt: one worker + fixed seed. STILL NOT REPRODUCIBLE, and the
        # reason is the stopping rule, not the search: `max_time_in_seconds` is
        # WALL CLOCK, this model returns FEASIBLE rather than OPTIMAL inside it,
        # so the answer is wherever the search had got to when the clock ran out
        # -- which depends on what else the machine was doing. Two runs: 10 vs 7
        # multi-machine PCR GTs, different sha1.
        #
        # `max_deterministic_time` counts the solver's own work units instead, so
        # the cutoff lands in the same place every time. The wall-clock limit stays
        # as a safety net ONLY; if it ever binds, the run is non-reproducible again
        # and says so out loud rather than silently emitting a different partition.
        slv.parameters.num_search_workers = int(
            os.environ.get("PLANNER_PARTITION_WORKERS", "1"))
        slv.parameters.random_seed = CONFIG.seed
        slv.parameters.max_deterministic_time = DET_BUDGET
        slv.parameters.max_time_in_seconds = budget
        # DIAGNOSTIC ONLY. `PLANNER_PARTITION_TRACE=1` prints the incumbent and
        # the bound at every improvement, which is the only way to answer "how
        # long would proving optimality take?" with a measurement instead of an
        # opinion. Pair with PLANNER_PARTITION_WORKERS>1 and a long budget: for
        # the diagnostic, reproducibility does not matter and speed does.
        _cb = None
        if os.environ.get("PLANNER_PARTITION_TRACE", "0") != "0":
            slv.parameters.max_deterministic_time = 1e9      # let wall clock rule

            class _Tr(cp_model.CpSolverSolutionCallback):
                def __init__(self):
                    super().__init__()
                    self.n = 0

                def on_solution_callback(self):
                    self.n += 1
                    o, b = self.ObjectiveValue(), self.BestObjectiveBound()
                    print(f"        [{plant}] {self.WallTime():>7.1f}s  "
                          f"obj {o:>8,.0f}  bound {b:>8,.0f}  "
                          f"gap {100 * (o - b) / max(abs(o), 1.0):>5.1f}%",
                          flush=True)
            _cb = _Tr()
        st = slv.Solve(mdl, _cb) if _cb else slv.Solve(mdl)
        if (st in (cp_model.OPTIMAL, cp_model.FEASIBLE)
                and slv.WallTime() >= budget * 0.98):
            print(f"  !! {plant}: WALL-CLOCK LIMIT BOUND at {slv.WallTime():.0f}s "
                  f"-- this partition is NOT reproducible. RAISE the budget "
                  f"argument (2nd CLI arg). Do NOT lower "
                  f"PLANNER_PARTITION_DET_TIME: that buys a stable "
                  f"answer by truncating the search, which is the "
                  f"defect this warning exists to report.")
        if st != cp_model.OPTIMAL:
            all_optimal = False
        if st not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            print(f"  {plant}: {slv.StatusName(st)} -- no partition")
            continue
        nrim = sum(1 for mm in machines for k in rims if slv.Value(rvar[(mm, k)]))
        nsplit = sum(1 for g, _ in gts
                     if sum(slv.Value(x[(g, mm)]) for mm in elig[g]) > 1)
        print(f"  {plant}: {slv.StatusName(st)}  {len(gts)} GTs · "
              f"{len(machines)} machines · rims-on-machines {nrim} "
              f"(min possible {len(machines)}) · load "
              f"{min(slv.Value(l) for _, l in _spread)/3600:.0f}-"
              f"{max(slv.Value(h) for h, _ in _spread)/3600:.0f} h of {CAPH:.0f}")
        # ALWAYS PRINT THE BOUND. "FEASIBLE" alone says only that optimality was
        # not PROVEN -- it does not say the answer is bad, and the two cases need
        # opposite responses. obj == bound with a timeout is a proof that ran out
        # of clock; a wide gap is a search that is genuinely lost.
        _obj, _bnd = slv.ObjectiveValue(), slv.BestObjectiveBound()
        _gap = 100.0 * (_obj - _bnd) / max(abs(_obj), 1.0)
        print(f"        multi-machine GTs {nsplit}/{len(gts)} "
              f"(split cost x{_sc:g}) · objective {_obj:,.0f} vs bound "
              f"{_bnd:,.0f} = gap {_gap:.1f}% · {slv.WallTime():.0f}s")
        for g, q in gts:
            qi = int(round(q))
            chosen = [mm for mm in elig[g] if slv.Value(x[(g, mm)])]
            # UNITS BACK TO TYRES, AND THE ROUNDING PAID BACK EXACTLY.
            # `ug` was a ceiling, so units * GRAN can exceed the real demand by up
            # to GRAN-1 tyres. Emitting that would hand L7 a partition claiming
            # more tyres than L4 asked for. Scale the parts down to the true total
            # and give the remainder to the largest, so `sum(qty) == qi` per GT.
            raw = [(mm, slv.Value(units[(g, mm)]) * GRAN) for mm in chosen]
            raw = [(mm, v) for mm, v in raw if v > 0]
            if not raw:
                continue
            tot = sum(v for _, v in raw)
            scaled = [(mm, int(v * qi // tot)) for mm, v in raw]
            rem = qi - sum(v for _, v in scaled)
            if rem and scaled:
                _i = max(range(len(scaled)), key=lambda i: scaled[i][1])
                scaled[_i] = (scaled[_i][0], scaled[_i][1] + rem)
            for mm, qq in scaled:
                if qq <= 0:
                    continue
                out_rows.append({
                    "plant": plant, "gt_code": g, "rim": rim.get(g, "?"),
                    "machine": mm,
                    "hours": qq * cad.get(mm, CAD_DEFAULT[plant]) / 3600.0,
                    "qty": float(qq), "tier": 1,
                    "is_split": len(scaled) > 1, "month": month})

    if out_rows:
        pl.DataFrame(out_rows).write_parquet(DER / "gt_machine_partition.parquet")
        print(f"\n  -> {DER / 'gt_machine_partition.parquet'} "
              f"({len(out_rows)} rows, stamped {month})")
        # STAMP THE INPUTS, NOT JUST THE MONTH.
        #
        # `main.py _partition_ok()` reused any partition whose `month` column
        # matched, so a re-ingested demand -- the plant sending revised volumes
        # for the SAME month, which is routine -- kept a partition sized against
        # the old volumes. The stamp matched, the gate passed, nothing said a
        # word. Same defect shape as the stale-month partition that cost July
        # 0.58 pt, one level down.
        #
        # The partition is a function of these four files and these three knobs.
        # Record all seven; `_partition_ok` refuses to reuse when any has moved,
        # which forces exactly the rebuild the month-only stamp was missing.
        prov = {"month": month, "rows": len(out_rows),
                "split_cost": _SPLIT_COST_USED, "imb_cap_h": IMB_CAP_H,
                "det_time": DET_BUDGET, "optimal": all_optimal,
                "inputs": {}}
        for _k, _f in (("net_requirement", D / f"net_requirement_{month}.parquet"),
                       ("cap_machine", D / f"cap_machine_{month}.parquet"),
                       ("gt_size", DER / "gt_size.parquet"),
                       ("allowed_machine_matrix",
                        DER / "allowed_machine_matrix.parquet"),
                       # B16 groups are now an INPUT to the model, so a re-split
                       # must force a rebuild like any other input change.
                       ("cap_ttl_groups",
                        D / f"cap_ttl_groups_{month}.parquet")):
            if _f.exists():
                prov["inputs"][_k] = hashlib.sha1(_f.read_bytes()).hexdigest()[:12]
        (DER / "gt_machine_partition.provenance.json").write_text(
            json.dumps(prov, indent=2, sort_keys=True), encoding="utf-8")
        print(f"     provenance: {prov['inputs']}")


if __name__ == "__main__":
    main()
