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
import json
import sys
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from planner import paths                                        # noqa: E402
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
    }
    missing = [k for k, p in required.items() if not p.exists()]
    for k in missing:
        add("ERROR", "input.missing", f"{k} -> {required[k]}", rule="L1")
    if missing:
        return pl.DataFrame(FINDINGS)
    add("INFO", "input.present", f"{len(required)} required inputs found")

    # ---- 2. L0 parameters -------------------------------------------------
    try:
        P = json.loads(paths.latest_params().read_text(encoding="utf-8"))
        for t in ("build_cadence", "cure_cycle"):
            f = paths.params(P["tables"][t])
            if not f.exists():
                add("ERROR", "params.table", f"{t} -> {f} referenced but absent")
        add("INFO", "params.ok", f"as_of={P.get('as_of')} tau/yield/availability loaded")
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
    cm = pl.read_parquet(paths.wh_derived(f"cap_machine_{month}.parquet"))
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
            add("WARN", f"cycletime.{name}_missing",
                f"{miss.height} demanded GTs have no plant {name} cycle time "
                f"({miss['qty'].sum():,} tyres) -> mined fallback used",
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
    part = pl.read_parquet(paths.input_derived("gt_machine_partition.parquet"))
    if "month" not in part.columns:
        add("ERROR", "partition.unstamped",
            "gt_machine_partition.parquet carries no month column")
    else:
        months = part["month"].unique().to_list()
        if months != [month]:
            add("ERROR", "partition.stale",
                f"partition was built for {months}, not {month} -- rebuild with "
                f"`python scripts/build_gt_machine_partition.py {month}`. L7 will "
                f"refuse to plan.", rule="L7")
        else:
            add("INFO", "partition.fresh",
                f"{part.height} GT-machine rows stamped {month}")

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
