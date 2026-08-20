"""PREFLIGHT -- the input gate.  Every check the plan depends on, no MES needed.

    python -m planner.cmbc.l1_preflight --month 2026-08

WHY THIS REPLACED L1 IN THE SHIPPED PIPELINE
  `l1_validate.py` does the same job but reaches into `v_curing`/`v_build` for
  its mould cross-check, so it raises `CatalogException: Table with name
  v_curing does not exist` on any checkout without the 4.4 GB raw MES drop --
  i.e. on every clone, and on the frontend. Its findings were also read by
  nothing downstream, so a crash there blocked a pipeline that did not need it.

  This module checks the same contracts against the FROZEN masters that the
  scheduler itself reads. If a check here passes, the layer that consumes that
  master will find what it needs. The original L1 is at
  `planner/cmbc/_retired/l1_validate.py`: its three outputs had exactly one
  consumer between them (L9 read `cost_table.parquet`), and L9 is retired too.

SEVERITY
  ERROR  the plan would be wrong or would crash      -> exit 1, do not plan
  WARN   the plan is valid but a master is thin      -> proceed, reported
  INFO   context worth printing                      -> proceed

  ERROR is reserved for things that actually stop a layer. A GT with no rim is
  a WARN, not an ERROR: L7 falls back to dynamic assignment and still plans it
  -- it just costs setup hours. That distinction is the whole point; treating
  every gap as fatal is how a gate becomes something people pass with a flag.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sys
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from planner import paths                                        # noqa: E402

ROOT = paths.ROOT
from planner.cmbc import _stamp, allowable                                # noqa: E402
from planner.cmbc.l2_ttl import B16_FLAGS as _b16_flags            # noqa: E402
from planner.config import CONFIG, GT_SHELF_LIFE_H               # noqa: E402

FINDINGS: list[dict] = []


def add(sev: str, check: str, detail: str, n: int = 0, rule: str = "") -> None:
    FINDINGS.append({"severity": sev, "check": check, "detail": detail,
                     "n": n, "rule": rule})


def _read(p: Path) -> pl.DataFrame | None:
    try:
        return pl.read_parquet(p)
    except Exception:
        return None


# --------------------------------------------------------------------------
def preflight(month: str) -> pl.DataFrame:
    FINDINGS.clear()

    # ---- 1. required inputs exist ----------------------------------------
    required = {
        "demand": paths.demand(month),
        "opening_gt": paths.opening_gt(month),
        "params (L0)": paths.WH_PARAMS,
        "cap_machine (L2)": paths.wh_derived(f"cap_machine_{month}.parquet"),
        "cap_press (L2)": paths.wh_derived(f"cap_press_{month}.parquet"),
        "cap_mould (L2)": paths.wh_derived(f"cap_mould_{month}.parquet"),
        "cap_ttl_groups (L2)": paths.wh_derived(f"cap_ttl_groups_{month}.parquet"),
        "cap_changeover (L2)": paths.wh_derived("cap_changeover.parquet"),
        "l3_cavities (L3)": paths.wh_derived("l3_cavities.parquet"),
        "press_mould_change": paths.wh_derived("press_mould_change.parquet"),
        "plant_ct_build": paths.wh_derived("plant_ct_build.parquet"),
        "plant_ct_cure_gt": paths.wh_derived("plant_ct_cure_gt.parquet"),
        "gt_size": paths.input_derived("gt_size.parquet"),
        "tt_tl": paths.input_derived("tt_tl.parquet"),
        "partition": paths.input_derived("gt_machine_partition.parquet"),
        "press_list": paths.press_list(month),
        # ADDED 2026-08-17. Both were read by the pipeline and absent from this
        # list, and BOTH DEGRADE SILENTLY rather than failing -- the class of
        # defect that let the partition sit switched off for months.
        #
        #   allowed_machine_matrix  is the plant's own hard statement of which GT
        #     may run on which machine, and it is doing real work: it cuts
        #     cap_machine from 652 rows to 353. `allowable.restrict()` responds to
        #     a missing file by printing one `!!` line and returning the frame
        #     UNRESTRICTED, so the run would plan tyres onto roughly twice the
        #     machines the plant permits and still pass every gate. A plan the
        #     floor cannot run is worse than no plan.
        #
        #   pcr_inch_eligibility  is read by check 7 BELOW, guarded by
        #     `if inch is not None`. Without the file that check -- one of this
        #     module's own 16 ERROR paths -- does not fail, it VANISHES. A guard
        #     that disappears when its input does is the exact "a passing check is
        #     not a correct check" shape in the ledger.
        "allowed_machine_matrix": paths.input_derived(
            "allowed_machine_matrix.parquet"),
        "pcr_inch_eligibility": paths.input_derived(
            "pcr_inch_eligibility.parquet"),
        # Both are read with a BARE `pl.read_parquet` -- no `.exists()` guard --
        # so their absence is a stack trace, not a silent downgrade. That is the
        # safer failure of the two, but it lands at step 04 (L5) or inside the
        # partition build rather than here, which defeats the point of an input
        # gate. `allowed_press_matrix` is also the CURING-side twin of the machine
        # matrix above: the plant's own statement of which GT may run on which
        # press, on the side of the plant where the plan is actually born.
        "allowed_press_matrix": paths.wh_derived("allowed_press_matrix.parquet"),
        "cycle_time_building": paths.wh_derived("cycle_time_building.parquet"),
    }
    # FLAG-CONDITIONAL INPUTS. These are read ONLY when their STRICT flag is on
    # (both default to "0", and the restrict functions return before touching the
    # file), so they are not required in general -- but with the flag on and the
    # file gone, the restriction silently becomes a no-op and the arm measures
    # nothing. Required exactly when they are used.
    for _flag, _fn, _lbl in (
            ("PLANNER_STRICT_RIMLOCK", "machine_rim_lock.parquet", "rimlock"),
            ("PLANNER_STRICT_RIMSET", "machine_gt_share.parquet", "rimset")):
        if os.environ.get(_flag, "0") != "0":
            required[f"{_lbl} ({_flag}=1)"] = paths.input_derived(_fn)
    missing = [k for k, p in required.items() if not p.exists()]
    for k in missing:
        add("ERROR", "input.missing", f"{k} -> {required[k]}", rule="L1")
    if missing:
        return pl.DataFrame(FINDINGS)
    add("INFO", "input.present", f"{len(required)} required inputs found")

    # ---- 1b. optional inputs that degrade quietly -------------------------
    # L4b uses these to group GTs into sister families and construction clusters
    # and simply skips the grouping when a file is absent (`if f.exists():`).
    # That is a legitimate degradation, not a fault -- but it must not be
    # invisible, because a feasibility gate that silently stopped grouping is a
    # different gate from the one that was measured.
    for _fn, _who in (("gt_sister_group.parquet", "L4b sister families"),
                      ("sku_con_cluster.parquet", "L4b construction clusters"),
                      ("gt_home_machine.parquet", "L7 home-machine preference")):
        if not paths.input_derived(_fn).exists():
            add("WARN", "input.optional_absent",
                f"{_fn} absent -- {_who} not applied (the layer still runs, "
                f"its decision is coarser)", rule="L4b")
    # Month-scoped masters that L4 / L5 read behind an `.exists()` test and simply
    # skip. `l3_ceiling` is the one that matters most: it is the PLANT's own
    # capacity ceiling, read by both L4 and L5, and planning without it means
    # planning against no ceiling at all. None of these are errors -- the layers
    # are written to run without them -- but running without them silently is how
    # a month gets planned to a different contract than the one that was measured.
    for _fn, _who in ((f"l3_ceiling_{month}.parquet", "L4/L5 plant capacity ceiling"),
                      (f"running_moulds_{month}.parquet", "L5 mounted-mould state"),
                      (f"machine_warm_{month}.parquet", "L5 warm-machine state")):
        if not paths.wh_derived(_fn).exists() and not paths.input_derived(_fn).exists():
            add("WARN", "input.optional_absent",
                f"{_fn} absent -- {_who} not applied", rule="L4/L5")

    # ---- 2. L0 parameters -------------------------------------------------
    try:
        P = json.loads(paths.latest_params().read_text(encoding="utf-8"))
        for t in ("build_cadence", "cure_cycle"):
            f = paths.params(P["tables"][t])
            if not f.exists():
                add("ERROR", "params.table", f"{t} -> {f} referenced but absent")
        add("INFO", "params.ok", f"as_of={P.get('as_of')} tau/yield/availability loaded")
        # NO MONTH GATE ON PARAMS. `latest_params()` takes the lexically last
        # file, so a July plan silently uses parameters mined as-of August. They
        # are 8-month aggregates and drift slowly, so this is a WARN not an
        # ERROR -- but "the yields in this plan were mined for another month"
        # should not be discoverable only by opening the JSON.
        _as = str(P.get("as_of") or "")
        if _as[:7] and _as[:7] != month:
            add("WARN", "params.month",
                f"{paths.latest_params().name} mined as_of {_as}, planning "
                f"{month} -- cure_yield and tau* come from another month's "
                f"window. 8-month aggregates, so usually benign; reported "
                f"because nothing else reports it.", rule="R15")
    except Exception as e:                                        # noqa: BLE001
        add("ERROR", "params.unreadable", str(e))
        return pl.DataFrame(FINDINGS)

    # ---- 3. demand integrity (R1) ----------------------------------------
    dem = pl.read_parquet(paths.demand(month))
    if dem.height == 0:
        add("ERROR", "demand.empty", f"no rows for {month}")
        return pl.DataFrame(FINDINGS)
    bad = dem.filter(pl.col("qty").is_null() | (pl.col("qty") <= 0))
    if bad.height:
        add("ERROR", "demand.qty", "non-positive or null qty", bad.height, "R1")
    nn = dem.filter(pl.col("gt_code").is_null() | pl.col("plant").is_null())
    if nn.height:
        add("ERROR", "demand.key", "null plant/gt_code", nn.height, "R1")
    off = dem.filter(pl.col("month") != month)
    if off.height:
        add("ERROR", "demand.month", f"rows not in {month}", off.height, "R1")
    for p, g in dem.group_by("plant"):
        add("INFO", "demand.volume",
            f"{p[0]}: {g['qty'].sum():,} tyres over {g.height} GT rows")

    # ---- 4. B12 minimum demand floor -------------------------------------
    floor = CONFIG.thresholds.min_demand_units
    per_gt = dem.group_by(["plant", "gt_code"]).agg(pl.col("qty").sum())
    for p, g in per_gt.group_by("plant"):
        lim = floor.get(p[0], 0)
        thin = g.filter(pl.col("qty") < lim)
        if thin.height:
            add("WARN", "demand.b12_residual",
                f"{p[0]}: {thin.height} GTs / {thin['qty'].sum():,} tyres below "
                f"min_demand_units={lim} -> residual policy, not planned",
                thin.height, "B12")

    # ---- 5. machine eligibility (R2 -- the allowable-machine check) -------
    # Check the ENFORCED eligibility, not the mined one. Before the allowable
    # list became hard this reported "110/110 GTs have >=1 machine" while 19.9 %
    # of PCR volume was landing on machines the plant forbids -- a gate that
    # validates against the engine's own widened view cannot catch that.
    cm_raw = pl.read_parquet(paths.wh_derived(f"cap_machine_{month}.parquet"))
    cm = allowable.restrict(cm_raw, label="cap_machine", quiet=True)
    if allowable.enabled():
        am = allowable.matrix()
        ruled = 0 if am is None else (
            am.select(["plant", "gt_code"]).unique()
            .join(per_gt.select(["plant", "gt_code"]), on=["plant", "gt_code"])
            .height)
        add("INFO", "allowable.enforced",
            f"plant allowable list is HARD: cap_machine {cm_raw.height} -> "
            f"{cm.height} rows; {ruled}/{per_gt.height} demanded GTs are ruled on")
    else:
        add("WARN", "allowable.soft",
            "PLANNER_STRICT_ALLOWABLE=0 -- the plant's allowable machine list is "
            "NOT enforced; INCH-eligible machines can be used at penalty 5000")
    elig = cm.group_by(["plant", "gt_code"]).agg(pl.len().alias("n_mach"))
    j = per_gt.join(elig, on=["plant", "gt_code"], how="left").with_columns(
        pl.col("n_mach").fill_null(0))
    none_m = j.filter(pl.col("n_mach") == 0)
    if none_m.height:
        add("WARN", "eligibility.no_machine",
            f"{none_m.height} demanded GTs have NO allowed building machine "
            f"({none_m['qty'].sum():,} tyres) -> unplannable, reported not dropped",
            none_m.height, "R2")
    # Structural failure, as opposed to per-GT gaps: if a whole plant has no
    # eligible machine anywhere, the capability master did not load and nothing
    # downstream can plan. THAT is worth blocking on.
    for p, g in j.group_by("plant"):
        if g.filter(pl.col("n_mach") > 0).height == 0:
            add("ERROR", "eligibility.plant_dead",
                f"{p[0]}: not one demanded GT has an eligible machine -- "
                f"cap_machine_{month}.parquet is empty or keyed wrong", rule="R2")
    add("INFO", "eligibility.machine",
        f"{j.filter(pl.col('n_mach') > 0).height}/{j.height} GTs have >=1 machine; "
        f"median {j['n_mach'].median():.0f}")

    # ---- 6. PCR inch eligibility (R6/R7) ---------------------------------
    inch = _read(paths.input_derived("pcr_inch_eligibility.parquet"))
    size = pl.read_parquet(paths.input_derived("gt_size.parquet"))
    if inch is not None:
        # gt_size.rim is text ("R12", "R22.5"); pcr_inch_eligibility.rim is an
        # int inch. Parse rather than compare -- polars refuses str vs numeric,
        # and a silent cast would drop R22.5.
        rim = (size.filter(pl.col("plant") == "PCR")
               .select(["gt_code",
                        pl.col("rim").str.strip_prefix("R").cast(pl.Float64,
                                                                 strict=False)])
               .unique(subset=["gt_code"]))
        pcr = (per_gt.filter(pl.col("plant") == "PCR")
               .join(rim, on="gt_code", how="left"))
        known = pcr.filter(pl.col("rim").is_not_null())
        # A rim is servable if SOME machine's window covers it. The eligibility
        # table already enumerates one row per (machine, covered rim), so the
        # covered set is exact -- a global min/max would wrongly pass a rim that
        # falls in a gap between two machines' windows.
        covered = set(inch["rim"].cast(pl.Float64).unique().to_list())
        lo, hi = inch["inch_lo"].min(), inch["inch_hi"].max()
        out = known.filter(~pl.col("rim").is_in(list(covered)))
        if out.height:
            add("ERROR", "inch.out_of_range",
                f"{out.height} PCR GTs whose rim is covered by no machine "
                f"(windows span [{lo}, {hi}]): "
                f"{sorted(set(out['rim'].to_list()))}", out.height, "R6/R7")
        unk = pcr.filter(pl.col("rim").is_null())
        if unk.height:
            add("WARN", "inch.rim_unknown",
                f"{unk.height} PCR GTs ({unk['qty'].sum():,} tyres) have no rim in "
                f"gt_size -> cannot be rim-locked or partitioned; L7 falls back to "
                f"dynamic assignment and setup hours rise", unk.height, "R6/R7")
        add("INFO", "inch.ok",
            f"{known.height}/{pcr.height} PCR GTs rim-resolved, window [{lo}, {hi}]")

    # ---- 7. TT/TL split (B16, TBR) ---------------------------------------
    # JOIN ON `sku`, NOT `gt_code`. `tt_tl.gt_code` is the TBR BOM short code
    # ("GT 5001"); demand carries the MES itemCode ("10.00 R 20 JUH5"). The two
    # namespaces have ZERO string overlap (README section 6), so a gt_code join
    # returns 0 matches and this check would report "0/37 tagged" on a month
    # where B16 is in fact fully resolved. L7 line 809 joins on sku -- match it.
    tt = pl.read_parquet(paths.input_derived("tt_tl.parquet"))
    tmap = (tt.filter((pl.col("sku") != "") & pl.col("tt_tl").is_not_null())
            .select(["sku", "tt_tl"]).unique(subset=["sku"]))
    tbr = (dem.filter(pl.col("plant") == "TBR")
           .join(tmap, on="sku", how="left"))
    untag = tbr.filter(pl.col("tt_tl").is_null())
    if untag.height:
        add("WARN", "b16.untagged",
            f"{untag.height} TBR demand rows ({untag['qty'].sum():,} tyres) have no "
            f"TT/TL tag for their SKU -> excluded from the tube-type group gate",
            untag.height, "B16")
    grp = pl.read_parquet(paths.wh_derived(f"cap_ttl_groups_{month}.parquet"))
    add("INFO", "b16.groups",
        f"{grp.height} machine-group rows; TBR tagged "
        f"{tbr.filter(pl.col('tt_tl').is_not_null()).height}/{tbr.height}")

    # ---- 8. press + mould (R3, R14) --------------------------------------
    cp = pl.read_parquet(paths.wh_derived(f"cap_press_{month}.parquet"))
    pj = per_gt.join(cp.group_by(["plant", "gt_code"]).agg(pl.len().alias("n_press")),
                     on=["plant", "gt_code"], how="left").with_columns(
        pl.col("n_press").fill_null(0))
    nop = pj.filter(pl.col("n_press") == 0)
    if nop.height:
        # WARN, NOT ERROR. Per-GT unplannable demand is a documented, expected
        # property of the order book (README section 9.2: ~1.0 % of August never
        # reaches the plan) -- the month still plans, that volume is just
        # flagged. Failing the gate here would refuse a month that plans fine
        # today, which is the always-failing-guard mode EXPERT_AUDIT records.
        add("WARN", "capability.no_press",
            f"{nop.height} demanded GTs have NO eligible press "
            f"({nop['qty'].sum():,} tyres) -> unplannable, reported not dropped",
            nop.height, "R3")
    mo = pl.read_parquet(paths.wh_derived(f"cap_mould_{month}.parquet"))
    mj = per_gt.join(mo.select(["plant", "gt_code", "moulds"]),
                     on=["plant", "gt_code"], how="left")
    nom = mj.filter(pl.col("moulds").is_null() | (pl.col("moulds") <= 0))
    if nom.height:
        add("WARN", "capability.no_mould",
            f"{nom.height} demanded GTs have no mould count in cap_mould "
            f"({nom['qty'].sum():,} tyres)", nom.height, "R14")
    for p, g in pj.group_by("plant"):
        if g.filter(pl.col("n_press") > 0).height == 0:
            add("ERROR", "capability.plant_dead",
                f"{p[0]}: not one demanded GT has an eligible press -- "
                f"cap_press_{month}.parquet is empty or keyed wrong", rule="R3")
    add("INFO", "capability.press",
        f"{pj.filter(pl.col('n_press') > 0).height}/{pj.height} GTs have >=1 press")

    # ---- 9. cycle-time coverage (B8) -------------------------------------
    cb = pl.read_parquet(paths.wh_derived("plant_ct_build.parquet"))
    cc = pl.read_parquet(paths.wh_derived("plant_ct_cure_gt.parquet"))
    for name, tbl, rule in (("build", cb, "B8"), ("cure", cc, "C1")):
        have = tbl.select(["plant", "gt_code"]).unique()
        miss = per_gt.join(have.with_columns(pl.lit(True).alias("_h")),
                           on=["plant", "gt_code"], how="left").filter(
            pl.col("_h").is_null())
        if miss.height:
            # NAME THEM. This used to report only a count -- "2 demanded GTs have
            # no plant build cycle time (10,511 tyres)" -- which nobody can act
            # on, because chasing the plant for a number requires knowing WHICH
            # tyre. Checked against INPUT/cycletime/ on 2026-08-17, the four July
            # cases were all genuine plant gaps, not ingestion faults, and one is
            # listed on a sheet the plant itself named "Missing curing cycle
            # time". They are still worth naming every run: the fallback is a
            # MACHINE-level mined cadence (PCR p50 63 s) standing in for a
            # GT-level plant norm whose real spread is 40-84 s, so a big tyre on
            # the fallback can be 20-30 % wrong -- and it is silent in the plan.
            _top = miss.sort("qty", descending=True)
            _names = "; ".join(
                f"{r['plant']} {r['gt_code']} ({r['qty']:,.0f})"
                for r in _top.head(6).iter_rows(named=True))
            if _top.height > 6:
                _names += f"; +{_top.height - 6} more"
            add("WARN", f"cycletime.{name}_missing",
                f"{miss.height} demanded GTs have no plant {name} cycle time "
                f"({miss['qty'].sum():,} tyres, "
                f"{100 * miss['qty'].sum() / max(per_gt['qty'].sum(), 1):.1f}% of "
                f"month) -> mined fallback used: {_names}",
                miss.height, rule)

    # ---- 10. opening GT (R1, R5) -----------------------------------------
    og = _read(paths.opening_gt(month))
    if og is None:
        add("ERROR", "opening_gt.unreadable", str(paths.opening_gt(month)))
    else:
        stale = og.filter(pl.col("age_h") > GT_SHELF_LIFE_H)
        if stale.height:
            add("WARN", "opening_gt.expired",
                f"{stale.height} opening tyres older than the {GT_SHELF_LIFE_H:.0f} h "
                f"shelf life -> dropped by L5/L7", stale.height, "R5")
        add("INFO", "opening_gt.ok",
            f"{og.height:,} tyres, max age {og['age_h'].max():.1f} h, "
            f"file={paths.opening_gt(month).name}")

    # ---- 11. partition freshness (the L7 gate, checked early) -------------
    # Mirror L7's own activation rule. With PLANNER_PARTITION_PLANTS empty the
    # partition file is never opened, so its month stamp is irrelevant and
    # blocking on it would refuse a run that is perfectly well defined.
    # Default must MIRROR L7's, or preflight passes a run L7 then refuses.
    part_plants = set(os.environ.get("PLANNER_PARTITION_PLANTS", "PCR")
                      .replace(" ", "").split(","))
    if part_plants == {""}:
        add("INFO", "partition.off",
            "PLANNER_PARTITION_PLANTS is empty -- no plant is partitioned, the "
            "partition file is not read, L7 assigns machines dynamically")
    else:
        part = pl.read_parquet(paths.input_derived("gt_machine_partition.parquet"))
        if "month" not in part.columns:
            add("ERROR", "partition.unstamped",
                "gt_machine_partition.parquet carries no month column")
        else:
            months = part["month"].unique().to_list()
            if months != [month]:
                add("ERROR", "partition.stale",
                    f"partition was built for {months}, not {month} -- rebuild "
                    f"with `python scripts/cpsat_partition.py {month}` (needs no "
                    f"raw MES; PLANNER_PARTITION_BUILDER=greedy for the mined "
                    f"builder, which does). L7 will refuse to plan. (Or run "
                    f"unpartitioned with PLANNER_PARTITION_PLANTS= )", rule="L7")
            else:
                add("INFO", "partition.fresh",
                    f"{part.height} GT-machine rows stamped {month}, "
                    f"partitioned plants: {sorted(part_plants)}")

    # ---- 8b. SHARED-MASTER PROVENANCE (L4 / L4.5) -------------------------
    # `scripts/run_arm.py` runs L5->L11 only, so net_requirement and l45_lots are
    # NOT re-derived per arm -- every arm inherits whatever the last full
    # pipeline run left in warehouse/derived/. A single `main.py plan` with an
    # L4.5-affecting flag silently re-bases every subsequent arm, and nothing
    # detected it: check_arm_fresh.py does not look at warehouse/ at all.
    # `n_lots` is decided in L4.5 and drives campaign count, concurrency,
    # campaign length and build run size -- so the single most consequential
    # decision in the engine has never been A/B'd cleanly.
    for _t in (paths.wh_derived(f"net_requirement_{month}.parquet"),
               paths.wh_derived(f"l45_lots_{month}.parquet")):
        _why = _stamp.check(_t)
        if _why:
            add("ERROR", "master.stale_flags", _why, rule="L4/L4.5")
        else:
            add("INFO", "master.stamped",
                f"{_t.name} provenance matches the current flags")
    # cap_ttl_groups is the THIRD shared master to need this, for the same reason:
    # `run_arm.py` did not rebuild it either, so arms inherited the B16 split from
    # whatever `main.py plan` ran last. It decides which of the nine TBR building
    # machines may build which tyres -- an inherited one re-bases the entire TBR
    # side of an arm. Its flags live in `extra` so they stay independent of the
    # L4/L4.5 fingerprint above.
    _ttl = paths.wh_derived(f"cap_ttl_groups_{month}.parquet")
    _why = _stamp.check(_ttl, extra=_b16_flags(),
                        remedy="Re-run planner.cmbc.l2_ttl")
    if _why:
        add("ERROR", "master.stale_flags", _why, rule="B16")
    else:
        add("INFO", "master.stamped",
            f"{_ttl.name} provenance matches the current B16 flags")

    # ---- 8c. INPUT-MASTER CONTENT DIGEST -----------------------------------
    # THE GAP THE OTHER STAMPS CANNOT COVER. `_stamp` fingerprints the ENV FLAGS
    # that GENERATED a master, so it protects net_requirement, l45_lots, carry_in
    # and the partition. It cannot protect a HAND-CURATED INPUT: nothing about the
    # environment changes when someone edits `allowed_machine_matrix.parquet`, and
    # that file decides which machine every GT may run on.
    #
    # Measured cost of not having this, 2026-08-17: the matrix was widened
    # 1,318 -> 1,373 rows midway through a July A/B sequence. Every gate passed,
    # every run succeeded, and the resulting three-arm table reported a 0.7 pt
    # gain that did not exist -- the arms had been planned against different
    # inputs. It took a determinism test and a file-mtime check to find, and the
    # run directories themselves held no record of which matrix they had used.
    #
    # This is EVIDENCE, NOT A GATE. There is no blessed hash to compare against,
    # so it cannot refuse a run. What it does is (a) record the digest of every
    # input master in preflight_<month>.parquet, so any past run can be asked
    # what it was built on, and (b) WARN the moment a digest moves, which is
    # exactly when a human can still remember why.
    _seen_f = paths.wh_derived("masters_digest.json")
    # MONTH-SCOPED KEYS FOR MONTH-SCOPED FILES.
    #
    # The first version of this guard keyed everything by bare name in one file
    # with no month in it, so `demand` and `opening_gt` -- which are SUPPOSED to
    # differ per month -- compared July's digest against August's. Measured:
    # running Jul, then Aug, then Jul emitted "demand changed: rows 1724 -> 116"
    # and "opening_gt changed: rows 6117 -> 6398" on the Aug run and the mirror
    # image on the next Jul run, forever, with nothing having changed.
    #
    # That is worse than no guard. A warning that fires on normal operation is a
    # warning people learn to scroll past, and the real signal -- a master edited
    # WITHIN a month, which is what cost 0.7 pt of phantom gain on 2026-08-17 --
    # arrives in the same shape as the noise.
    #
    # `gt_machine_partition` is month-scoped too: one file serves all months and
    # carries a month stamp, so a global key would fire on every month change.
    # The other four are genuinely month-independent and compare globally.
    _tracked = {
        "allowed_machine_matrix": paths.input_derived(
            "allowed_machine_matrix.parquet"),
        "gt_size": paths.input_derived("gt_size.parquet"),
        "tt_tl": paths.input_derived("tt_tl.parquet"),
        "pcr_inch_eligibility": paths.input_derived("pcr_inch_eligibility.parquet"),
        f"gt_machine_partition@{month}": paths.input_derived(
            "gt_machine_partition.parquet"),
        f"demand@{month}": paths.demand(month),
        f"opening_gt@{month}": paths.opening_gt(month),
        # ADDED 2026-08-18. `params_*.json` carries `cure_yield`, which multiplies
        # into every GT's cure_requirement and gross_build. It sat outside every
        # gate: `_stamp` hashes env flags not inputs, and this digest did not list
        # it -- so swapping the params file re-based net_requirement, l45_lots,
        # every cure campaign and every arm's fulfilment denominator with nothing
        # moving. The partition provenance has the same shape of hole for
        # `cycle_time_building` and `cap_changeover`; those are the builder's, not
        # preflight's, and are noted in cpsat_partition.py.
        "params": paths.latest_params(),
    }
    _now: dict = {}
    for _k, _p in _tracked.items():
        if not _p.exists():
            continue
        _b = _p.read_bytes()
        try:
            _n = pl.read_parquet(_p).height
        except Exception:                                          # noqa: BLE001
            _n = -1
        _now[_k] = {"sha1": hashlib.sha1(_b).hexdigest()[:12], "rows": _n}
    try:
        _prev = json.loads(_seen_f.read_text(encoding="utf-8")) if _seen_f.exists() else {}
    except Exception:                                              # noqa: BLE001
        _prev = {}
    _moved = [(k, _prev[k], v) for k, v in _now.items()
              if k in _prev and _prev[k].get("sha1") != v["sha1"]]
    for _k, _o, _v in _moved:
        add("WARN", "master.content_changed",
            f"{_k} changed since the last plan: sha1 {_o.get('sha1')} -> "
            f"{_v['sha1']}, rows {_o.get('rows')} -> {_v['rows']}. Any arm "
            f"measured before this run is NOT comparable with one measured "
            f"after it.", rule="L1")
    add("INFO", "master.digest",
        " ".join(f"{k}={v['sha1']}/{v['rows']}" for k, v in sorted(_now.items())))
    try:
        # MERGE, DO NOT REPLACE. Writing `_now` alone dropped every other month's
        # recorded digest, so a July run erased August's baseline and the next
        # August run had nothing to compare against -- it would record silently
        # and the guard would be dead for that month, exactly when it mattered.
        _seen_f.write_text(json.dumps({**_prev, **_now}, indent=2, sort_keys=True),
                           encoding="utf-8")
    except Exception:                                              # noqa: BLE001
        pass

    # ---- 9. CARRY-IN month stamp (the rolling boundary contract) ------------
    # THE THIRD MASTER IN THIS CODEBASE TO NEED A STAMP GATE, and the first two
    # each cost real fulfilment before they got one (gt_machine_partition: July
    # 0.58 pt while every other gate passed). A carry-in file describes the press
    # state at ONE instant. Feeding August a file stamped for the July boundary
    # is correct; feeding it June's silently frees 59 presses that are busy and
    # re-books 27 mounted moulds as cold -- and nothing downstream can tell.
    # `carry_from` must equal this month's t0, exactly.
    _ci = os.environ.get("PLANNER_CARRY_IN", "")
    _cif = (Path(_ci) if _ci
            else ROOT / "masters" / "carry_in" / f"carry_in_{month}.parquet")
    _t0 = dt.datetime(int(month[:4]), int(month[5:7]), 1, 7, 0)
    if not _cif.exists():
        add("INFO", "carry_in.absent",
            f"no carry-in for {month} ({_cif.name}) -- presses start cold at t0. "
            f"Physically this means the previous month's plan and this one both "
            f"claim the same presses across the boundary; verify with the union "
            f"concurrency check before shipping a consecutive pair.")
    else:
        _c = pl.read_parquet(_cif)
        if "carry_from" not in _c.columns:
            add("ERROR", "carry_in.unstamped",
                f"{_cif.name} carries no carry_from column", rule="L5")
        else:
            _cf = sorted({str(x) for x in _c["carry_from"].unique().to_list()})
            if len(_cf) != 1 or _c["carry_from"][0] != _t0:
                add("ERROR", "carry_in.stale",
                    f"{_cif.name} is stamped carry_from={_cf}, not {_t0} -- it "
                    f"describes a different boundary. Re-emit it from the "
                    f"PREVIOUS month's run (runs/<prev>/carry_out.parquet).",
                    rule="L5")
            else:
                _busy = (int(_c.filter(pl.col("busy_at_t0")).height)
                         if "busy_at_t0" in _c.columns else _c.height)
                add("INFO", "carry_in.fresh",
                    f"{_c.height} press states stamped {_t0} "
                    f"({_busy} busy at t0, {_c.height - _busy} idle with a "
                    f"mounted mould)")

    return pl.DataFrame(FINDINGS)


# --------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="MES-free input gate")
    ap.add_argument("--month", default="2026-08")
    ap.add_argument("--strict", action="store_true",
                    help="treat WARN as blocking too")
    a = ap.parse_args()

    rep = preflight(a.month)
    out = paths.wh_derived(f"preflight_{a.month}.parquet")
    rep.write_parquet(out)

    order = {"ERROR": 0, "WARN": 1, "INFO": 2}
    print(f"\n  PREFLIGHT  {a.month}\n  {'-' * 76}")
    for sev in ("ERROR", "WARN", "INFO"):
        for r in rep.filter(pl.col("severity") == sev).iter_rows(named=True):
            tag = f"[{r['rule']}]" if r["rule"] else ""
            print(f"  {sev:<6} {r['check']:<26} {r['detail']} {tag}")
    n_err = rep.filter(pl.col("severity") == "ERROR").height
    n_warn = rep.filter(pl.col("severity") == "WARN").height
    print(f"  {'-' * 76}\n  {n_err} ERROR · {n_warn} WARN  ->  {out.name}")
    _ = order

    if n_err or (a.strict and n_warn):
        print("  REFUSING TO PLAN\n")
        sys.exit(1)
    print("  OK to plan\n")


if __name__ == "__main__":
    main()
