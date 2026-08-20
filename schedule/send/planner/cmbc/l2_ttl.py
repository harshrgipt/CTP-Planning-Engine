"""L2-TTL -- THE B16 TT/TL MACHINE PARTITION, RECOMPUTED EVERY PLAN.

    python -m planner.cmbc.l2_ttl --month 2026-07

Writes warehouse/derived/cap_ttl_groups_<month>.parquet
      warehouse/derived/b16_machine_reach_<month>.parquet

WHY THIS EXISTS AS A LIVE LAYER
  The identical search used to live inside `_offline/l2_capability.py`, which
  mines `v_build` and therefore needs the ~4.4 GB raw MES drop. That made the
  TT/TL split a FROZEN artefact: rebuilt only when someone ran `main.py rebuild`
  with the MES present, and otherwise inherited from whatever month last touched
  it. Two consequences, both measured:

    * On 2026-08-14 the August file was hand-edited to TL={TBMTBR4,TBMTBR6} to
      test a hypothesis. That edit was worth +5,394 TBR BUILT and was NOT
      reproducible from any input -- it existed only as bytes on disk. A plan is
      not allowed to depend on a number no input can regenerate.
    * The partition is a function of THAT MONTH'S demand mix (n_tt is sized from
      the TT share of volume). Carrying August's split into September is the
      same class of defect as the stale gt_machine_partition, which cost July
      0.58 pt of fulfilment while every gate passed.

  The search itself needs only three things, ALL of them committed artefacts:
      cap_machine_<month>.parquet     eligibility (mined once, committed)
      masters/demand/demand_<month>   the month's order book
      INPUT/derived/tt_tl.parquet     SKU -> TT/TL tag
  None of them is the MES. So it runs in ~1 s as pipeline step 00a_l2_ttl_b16,
  every plan,
  and the split is always a function of the month being planned.

THE SEARCH (CHANGED 2026-08-18 -- this docstring described the old engine until
then, which is the failure mode README section 9.4 exists to name)
  9 TBR building machines. ALL 510 non-trivial partitions are enumerated across
  every TT count, not C(9, n_tt) for one volume-derived count -- `n_tt` is now an
  OUTPUT of the search. B16 forbids spilling across the boundary, so a partition
  has THREE gates, and the capacity one outranks both coverage ones:

    load()       group demand vs the group's OWN machine-hours (BUSINESS_RULES
                 section 1a step 7). A group over PLANNER_B16_LOAD_CAP cannot
                 build its month, whatever its coverage looks like. This gate did
                 not exist in code until 2026-08-18; without it the other two
                 rank INFEASIBLE splits as best (August maxmin chose a split at
                 TL 190 % load).

    uncovered()  GTs with no eligible machine in their own group.  PRIMARY --
                 such a GT cannot be built at all.
    stranded()   machines with no eligible in-group demand.  SECONDARY -- a dead
                 machine only wastes capacity, but one of nine is 11 % of TBR
                 building. This side went unchecked for most of the project;
                 Aug 2026 stranded TBMTBR8, May 2026 stranded TBMTBR7 while the
                 plant ran it at 89.6 % for 11,587 tyres.

  Never trade an uncovered GT for a live machine: the key orders GT-side first.
  Ties break on make coherence then lowest machine number, so it is deterministic.

PLANNER_B16_CRITERION  gt | machine | coverage | maxmin   (default MAXMIN --
                       see the comment on the key; `coverage` was the default
                       until 2026-08-14 and measured -400 TBR BUILT on July)
PLANNER_B16_NTT        PIN the TT machine count instead of searching it. The
                       volume-derived round(9 x TT share) is still the seed and
                       the fallback, but it no longer bounds the search: under
                       the load gate all 510 partitions are ranked and the gate
                       reproduces the volume-derived answer on both months by
                       itself (Jul n_tt=6, Aug n_tt=7). Verified independently
                       2026-08-18.
PLANNER_B16_LOAD_CAP   the capacity gate threshold, default 0.95. It changes
                       which splits are feasible, so it is in B16_FLAGS().
PLANNER_B16_FREEZE=1   keep the file on disk, recompute nothing (escape hatch
                       for reproducing an old run; prints what it would have
                       chosen so the divergence is never silent)
"""
from __future__ import annotations

import argparse
import calendar as _cal
import os
from itertools import combinations

import polars as pl

from planner import paths
from planner.cmbc import _stamp

ROOT = paths.ROOT
D = ROOT / "warehouse" / "derived"
INP = paths.INPUT_DERIVED

# B16 step 7: a group over this load cannot build its month. 0.95 leaves the same
# 5 % headroom the rule text implies; it is a LIMIT, not a mined value.
def LOAD_CAP() -> float:
    """Read at CALL time: an override applied by `main.py` to os.environ
    after import must be seen, or the stamp and the search disagree."""
    return float(os.environ.get("PLANNER_B16_LOAD_CAP", "0.95"))


def B16_FLAGS() -> dict:
    """The flags that change what this layer emits. Read at call time, not import
    time, so an override applied by `main.py` to `os.environ` is seen."""
    return {k: os.environ.get(k, "") for k in
            ("PLANNER_B16_CRITERION", "PLANNER_B16_NTT", "PLANNER_B16_FREEZE",
             # ADDED 2026-08-18. It sets the capacity gate's threshold, so it
             # changes which splits are feasible and therefore which one is
             # chosen -- the same omission PLANNER_L45_CONC_FLOOR was in
             # `_stamp.WATCHED`, one flag over, found the same way.
             "PLANNER_B16_LOAD_CAP")}

TBR_MAKE = {1: "SAV", 2: "SAV", 3: "SAV",
            4: "MESNAC", 5: "MESNAC", 6: "MESNAC",
            7: "MESNAC", 8: "MESNAC", 9: "MESNAC"}


def solve(month: str) -> tuple[pl.DataFrame, pl.DataFrame, dict]:
    """Return (groups, machine_reach, report). Pure -- writes nothing."""
    cm = pl.read_parquet(D / f"cap_machine_{month}.parquet").filter(
        pl.col("plant") == "TBR")
    dem = pl.read_parquet(ROOT / "masters" / "demand" / f"demand_{month}.parquet")
    tt = pl.read_parquet(INP / "tt_tl.parquet").filter(pl.col("sku") != "")
    tmap = tt.select(["sku", "tt_tl"]).unique(subset=["sku"])
    tbr = dem.filter(pl.col("plant") == "TBR").join(tmap, on="sku", how="left")

    q = tbr.group_by("tt_tl").agg(pl.col("qty").sum().alias("q"))
    tot = float(q["q"].sum())
    ttq = float(q.filter(pl.col("tt_tl") == "TT")["q"].sum() or 0)
    n_tt = int(round(9 * ttq / tot)) if tot else 0
    # PLANNER_B16_NTT overrides the volume-derived machine count. It exists
    # because deriving n_tt from the TT VOLUME SHARE and then searching only
    # C(9, n_tt) partitions is this project's #1 defect shape -- a mined
    # statistic used as a hard constraint. It discards 84 % of the partition
    # space on July (84 of 512) and 93 % on August (36 of 512), on the assumption
    # that machine count should track volume share. It should not necessarily:
    # the machines are not interchangeable, and one that can build many TT GTs is
    # worth more to the TT group than one that can build few. The right n_tt is
    # an OUTCOME of the search, not an input to it.
    _ov = os.environ.get("PLANNER_B16_NTT", "")
    if _ov != "":
        n_tt = int(_ov)

    elig_by_gt: dict[str, set[str]] = {}
    for r in cm.iter_rows(named=True):
        elig_by_gt.setdefault(r["gt_code"], set()).add(r["machine"])
    tag_by_gt: dict[str, str] = {}
    for r in tbr.iter_rows(named=True):
        if r["tt_tl"]:
            tag_by_gt[r["gt_code"]] = r["tt_tl"]
    qty_by_gt: dict[str, float] = {
        r["gt_code"]: float(r["q"]) for r in
        tbr.group_by("gt_code").agg(pl.col("qty").sum().alias("q")).iter_rows(named=True)}

    def uncovered(tt_set: set[int]) -> tuple[int, float]:
        ttm = {f"TBMTBR{n}Stage2" for n in tt_set}
        tlm = {f"TBMTBR{n}Stage2" for n in TBR_MAKE if n not in tt_set}
        n, v = 0, 0.0
        for g, tag in tag_by_gt.items():
            if not (elig_by_gt.get(g, set()) & (ttm if tag == "TT" else tlm)):
                n += 1
                v += qty_by_gt.get(g, 0.0)
        return n, v

    # ---- THE CAPACITY GATE (B16 step 7) --------------------------------------
    # BUSINESS_RULES.md section 1a step 7 specifies this and the code never had
    # it: "if either group exceeds ~95 % load -> INFEASIBLE and re-split". The
    # layer shipped with THREE COVERAGE GATES AND ZERO CAPACITY GATES, so its
    # ranking could not see the one quantity that decides whether a closed group
    # can build its month at all.
    #
    # What that blindness cost, measured 2026-07/08: `n_tt` was computed from the
    # TT volume share and never searched, which looked like this project's #1
    # defect shape (a mined statistic used as a hard constraint) -- and a sweep
    # over n_tt appeared to find strictly better partitions on both months by the
    # layer's own maxmin tie-break. They are not better; they are INFEASIBLE, and
    # nothing in the ranking could say so:
    #
    #     July  n_tt=6 (default)  TT  83.0 %  TL  84.3 %   feasible
    #           n_tt=7 "better"   TT  71.7 %  TL 126.6 %   impossible
    #     Aug   n_tt=7 (default)  TT  81.9 %  TL  92.9 %   feasible
    #           n_tt=8 "better"   TT  71.9 %  TL 190.2 %   impossible
    #
    # With this gate the search reproduces the volume-derived answer on both
    # months by itself -- so n_tt was right by construction, as a capacity-balance
    # proxy wearing a volume disguise, for a reason nobody had written down.
    #
    # HOURS, NOT TYRES. TBR build rates span 189-219 s/tyre (16 %), so a group of
    # two slow machines is not two fast ones. Capacity is summed per machine from
    # its own cadence; `qty` alone would agree on these two months and eventually
    # will not.
    HZ_H = float(_cal.monthrange(int(month[:4]), int(month[5:7]))[1] * 24)
    _ctb = pl.read_parquet(D / "cycle_time_building.parquet").filter(
        pl.col("plant") == "TBR")
    sec = {r["machine"]: float(r["s_per_tyre"]) for r in _ctb.iter_rows(named=True)}
    dem_of = {"TT": ttq, "TL": tot - ttq}

    def load(tt_set: set[int]) -> tuple[float, dict]:
        """(worst group load as a fraction, per-group loads). >1 is impossible."""
        out = {}
        for tag in ("TT", "TL"):
            ms = [n for n in TBR_MAKE
                  if (n in tt_set) == (tag == "TT")]
            cap = sum(HZ_H * 3600.0 / sec.get(f"TBMTBR{n}Stage2", 204.0) for n in ms)
            out[tag] = (dem_of[tag] / cap) if cap > 0 else float("inf")
        return max(out.values()), out

    def stranded(tt_set: set[int]) -> tuple[int, float, dict]:
        grp_of = {n: ("TT" if n in tt_set else "TL") for n in TBR_MAKE}
        reach = {n: sum(qty_by_gt.get(g, 0.0) for g, tag in tag_by_gt.items()
                        if tag == grp_of[n]
                        and f"TBMTBR{n}Stage2" in elig_by_gt.get(g, set()))
                 for n in TBR_MAKE}
        dead = sum(1 for n in TBR_MAKE if reach[n] <= 0.0)
        deficit = 0.0
        for tag in ("TT", "TL"):
            ms = [n for n in TBR_MAKE if grp_of[n] == tag]
            if not ms:
                continue
            share = sum(qty_by_gt.get(g, 0.0)
                        for g, t in tag_by_gt.items() if t == tag) / len(ms)
            deficit += sum(max(0.0, share - reach[n]) for n in ms)
        return dead, deficit, reach

    crit = os.environ.get("PLANNER_B16_CRITERION", "maxmin")
    # n_tt IS NOW SEARCHED, UNDER THE LOAD GATE.
    # The volume-derived value stays as the seed and as the fallback when nothing
    # is load-feasible, but the search ranges over every count. That answers the
    # original objection (a mined statistic must not bound the search) WITHOUT
    # letting maxmin choose the count -- maxmin's `reach` is defined over the
    # in-group demand pool, so it rises mechanically whenever a machine moves to
    # the busier group, which is exactly why the un-gated sweep walked toward
    # TL 190 % on August. The load gate outranks it in the key below.
    _rng = ([n_tt] if os.environ.get("PLANNER_B16_NTT", "") != ""
            else range(1, len(TBR_MAKE)))
    best = None
    n_tied = 0
    _n_searched = _n_feas = 0
    for _n in _rng:
     for combo in combinations(sorted(TBR_MAKE), _n):
        cnt, vol_bad = uncovered(set(combo))
        dead, deficit, rc = stranded(set(combo))
        worst_load, _ = load(set(combo))
        over = 1 if worst_load > LOAD_CAP() else 0
        _n_searched += 1
        _n_feas += 0 if over else 1
        makes = len({TBR_MAKE[n] for n in combo})
        if cnt == 0 and dead == 0 and deficit == 0 and not over:
            n_tied += 1
        if crit == "gt":
            key = (cnt, vol_bad, over, worst_load, makes, combo)
        elif crit == "machine":
            key = (cnt, vol_bad, over, dead, worst_load, makes, combo)
        elif crit == "coverage":
            key = (cnt, vol_bad, over, dead, deficit, worst_load, makes, combo)
        else:
            # MAXMIN -- the tiebreak that is about TYRES.
            #
            # Measured 2026-08-14 on July. Both gates are FEASIBILITY tests: they
            # answer "can this partition be built at all", not "how much". Three
            # of 84 partitions passed both with zero deficit, and `coverage` then
            # broke that 3-way tie on make coherence and lowest machine number --
            # neither of which has anything to do with throughput. It chose the
            # partition whose WEAKEST machine reaches 19,237 tyres over the one
            # reaching 21,869, and measured -400 TBR BUILT.
            #
            # A TT/TL group is a closed system: B16 forbids spilling, so the
            # group's output is bounded by its least-employable machine. Maximise
            # that. `deficit` does not capture it -- it is a SUM of fair-share
            # gaps and hits zero the moment every machine clears its own average,
            # which all three tied partitions did.
            key = (cnt, vol_bad, over, dead, deficit, -min(rc.values()),
                   makes, combo)
        if best is None or key < best[0]:
            best = (key, combo)

    tt_m = sorted(best[1])
    n_tt = len(tt_m)          # the count is now an OUTPUT of the search
    bad, bad_vol = uncovered(set(tt_m))
    n_dead, deficit, reach = stranded(set(tt_m))
    grp_of = {n: ("TT" if n in tt_m else "TL") for n in TBR_MAKE}

    groups = pl.DataFrame(
        [{"plant": "TBR", "machine": f"TBMTBR{n}Stage2", "make": TBR_MAKE[n],
          "group": grp_of[n]} for n in TBR_MAKE])
    mrows = []
    for n in TBR_MAKE:
        mach = f"TBMTBR{n}Stage2"
        el = sum(1 for g in elig_by_gt if mach in elig_by_gt[g])
        ing = sum(1 for g, t in tag_by_gt.items()
                  if t == grp_of[n] and mach in elig_by_gt.get(g, set()))
        mrows.append({"plant": "TBR", "machine": mach, "group": grp_of[n],
                      "eligible_gts": el, "in_group_gts": ing,
                      "reach_tyres": float(reach[n]),
                      "stranded": bool(reach[n] <= 0), "month": month})
    rep = dict(n_tt=n_tt, tt=tt_m, tl=[n for n in TBR_MAKE if n not in tt_m],
               tt_share=100 * ttq / tot if tot else 0.0, crit=crit,
               n_searched=_n_searched, load_cap=LOAD_CAP(),
               load=load(set(tt_m))[1], n_feasible=_n_feas,
               uncovered_gts=bad, uncovered_vol=bad_vol, n_dead=n_dead,
               deficit=deficit, n_tied=n_tied,
               min_reach=min(reach.values()) if reach else 0.0,
               untagged=int(tbr.filter(
                   pl.col("tt_tl").is_null())["qty"].sum() or 0))
    return groups, pl.DataFrame(mrows), rep


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", default="2026-07")
    a = ap.parse_args()
    out = D / f"cap_ttl_groups_{a.month}.parquet"

    print("=" * 78)
    print(f"L2-TTL  B16 TT/TL MACHINE PARTITION  --  {a.month}")
    print("=" * 78)

    groups, reach, r = solve(a.month)

    prev = None
    if out.exists():
        _p = pl.read_parquet(out)
        prev = {x["machine"]: x["group"] for x in _p.iter_rows(named=True)}

    if os.environ.get("PLANNER_B16_FREEZE", "0") == "1" and prev:
        print("  FROZEN (PLANNER_B16_FREEZE=1) -- file on disk kept.")
        print(f"    on disk : TT {[m for m, g in prev.items() if g == 'TT']}")
        print(f"    computed: TT {[f'TBMTBR{n}Stage2' for n in r['tt']]}")
        return

    print(f"  TT is {r['tt_share']:.1f}% of TBR demand -> {r['n_tt']} TT / "
          f"{9 - r['n_tt']} TL   (searched {r['n_searched']} partitions "
          f"across all counts, {r['n_feasible']} load-feasible at "
          f"<={100*r['load_cap']:.0f}%, criterion {r['crit']})")
    print(f"    group load: TT {100*r['load']['TT']:.1f}%  "
          f"TL {100*r['load']['TL']:.1f}%   (B16 step 7 gate)")
    print(f"    {r['n_tied']} partition(s) pass both gates with zero deficit -> "
          f"tie broken on weakest-machine reach = {r['min_reach']:,.0f} tyres")
    print(f"    TT: {[f'TBM{n}' for n in r['tt']]}   "
          f"makes {sorted({TBR_MAKE[n] for n in r['tt']})}")
    print(f"    TL: {[f'TBM{n}' for n in r['tl']]}   "
          f"makes {sorted({TBR_MAKE[n] for n in r['tl']})}")
    if r["untagged"]:
        print(f"    note: {r['untagged']:,} tyres have no TT/TL tag on their SKU "
              f"-> excluded from the gate")

    if r["uncovered_gts"]:
        print(f"    gate GT-side : {r['uncovered_gts']} GTs "
              f"({r['uncovered_vol']:,.0f} tyres) have NO eligible machine in "
              f"their group  ***FAIL***")
    else:
        print("    gate GT-side : every tagged GT has an eligible in-group "
              "machine  PASS")

    for x in reach.iter_rows(named=True):
        flag = "  <<< STRANDED (0 in-group)" if x["stranded"] else ""
        print(f"      {x['machine']:<16}{x['group']:<3} eligible "
              f"{x['eligible_gts']:>3} | in-group {x['in_group_gts']:>3} | "
              f"reach {x['reach_tyres']:>9,.0f}{flag}")
    if r["n_dead"]:
        print(f"    !! {r['n_dead']} MACHINE(S) STRANDED -- ~{100*r['n_dead']/9:.0f}% "
              f"of TBR building cannot be used. The search already MINIMISES "
              f"this, so no partition of these 9 avoids it: fix is upstream "
              f"(widen eligibility or re-tag). Report to plant.")
    else:
        print("    gate machine-side: every machine has in-group demand  PASS "
              f"(fair-share deficit {r['deficit']:,.0f})")

    if prev:
        now = {x["machine"]: x["group"] for x in groups.iter_rows(named=True)}
        moved = sorted(m for m in now if prev.get(m) != now[m])
        if moved:
            print(f"    CHANGED vs the file on disk: "
                  + ", ".join(f"{m} {prev.get(m)}->{now[m]}" for m in moved))
        else:
            print("    unchanged vs the file on disk")

    groups.write_parquet(out)
    reach.write_parquet(D / f"b16_machine_reach_{a.month}.parquet")
    # STAMP IT. `cap_ttl_groups` is a shared master that `scripts/run_arm.py`
    # does NOT rebuild -- its STAGES list starts at L4 -- so every arm inherits
    # whatever the last `main.py plan` left on disk, under whatever B16 flags that
    # run used. That is the same defect that made `n_lots` un-A/B-able before L4
    # and L4.5 were stamped, and this file decides which nine TBR machines may
    # build which tyres, so an inherited one silently re-bases the whole arm.
    # `_stamp.WATCHED` is the L4/L4.5 flag set; these flags go in `extra` so the
    # two fingerprints stay independent.
    _stamp.write(out, extra=B16_FLAGS())
    print(f"\n  -> {out.name} · b16_machine_reach_{a.month}.parquet"
          f"  (stamped {_stamp.env_fingerprint(B16_FLAGS())['sha1']})")


if __name__ == "__main__":
    main()
