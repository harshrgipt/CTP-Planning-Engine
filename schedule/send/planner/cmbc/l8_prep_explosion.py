"""L8 -- PREP / MIXING BACK-EXPLOSION.

    python -m planner.cmbc.l8_prep_explosion --month 2026-07

R16 · the layer the old flow had no equivalent for.

WHY THIS LAYER EXISTS
  L7 releases building against the cure plan. But a build slice cannot start
  until its COMPONENTS exist -- tread, ply, belt, bead, inner liner -- and each
  of those has its own shelf life, several of them SHORTER than the green tyre's.
  From `Ageing spec-20.01.2024 rev12`:

      silica tread        max 24 h        <- tighter than the GT's 72 h
      cut plies (steel)   max 24 h
      fillered bead       max 24 h
      calendered belt     max 96 h
      sidewall            max 72 h

  So a component made too early is scrap just as surely as a GT cured too late.
  Planning building without exploding it into prep leaves the tightest clock in
  the plant unmodelled.

THE PREP WINDOW IS TWO-SIDED
      make(component) in [t_build - max_age, t_build - min_age]
  Too early and it expires; too late and the builder waits. Both bounds come
  from the ageing spec, per component, and neither is a preference.

STOP AT LEVEL 1 -- THE GT'S DIRECT CHILDREN ARE THE SEMI-FINISHED COMPONENTS
      GT 5001 -> PRE ASSEMBLY · NYLON-1 · NYLON2&3 · GUM STRIP · STEEL CHIPPER …
  which are exactly the construction components the TBR allowable matrix names,
  and exactly the things the ageing spec gives shelf lives for.

  A first version walked the graph to leaves. That was wrong: the BOM continues
  below the semi-finished level all the way to raw material -- SMALL CHEMICAL,
  Carbon black, Process oil, MASTER COMPOUND -- so descending multiplied
  quantities through every intermediate and produced 32.3 M metres of slitted
  fabric against 1.2 M of tread, and 58 component lines per build slice where
  PCR has 8. Raw materials have their own ageing rules but they are a mixing
  problem, not a prep-scheduling one.

  GT codes still need normalising: the BOM carries `GT 5001` while MES carries
  `GT 5079 - 255/70R22.5 JUX E`. Joining raw found 0 of 46 TBR GTs and looked
  like missing data; it was the same key-namespace mismatch that hit the mould
  master at L1 and L5.

BOM COVERAGE IS REPORTED, NEVER ASSUMED
  An unexploded GT is prep demand that exists on the floor but not in the plan,
  so uncovered GTs are printed with their volume rather than silently skipped.
"""
from __future__ import annotations

import argparse
import re
from datetime import timedelta
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parent.parent.parent
D = ROOT / "warehouse" / "derived"
INP = ROOT.parent.parent / "INPUT" / "derived"
SHIFT_H = 8

# BOM child_desc -> keywords that identify it in `Ageing spec-20.01.2024 rev12`.
# Limits are READ FROM THE SPEC (INPUT/derived/semi_finished_ageing.parquet),
# never hardcoded here, so a spec revision flows through without a code change.
# Where the spec gives several variants for one component the TIGHTEST max is
# taken -- planning to a looser limit than some variant carries licences scrap.
SPEC_KEYS = {
    "Tread":                 ["tread"],
    "SideWall":              ["sidewall"],
    "SHOULDER PAD":          ["sidewall"],
    "Ply":                   ["bodyply", "cut fabric", "slitted fabric"],
    "BODYPLY":               ["bodyply"],
    "Steel Belt":            ["belts", "steel belt"],
    "BELT-1": ["belts"], "BELT-2": ["belts"],
    "BELT-3": ["belts"], "BELT-4": ["belts"],
    "BELT EDGE FILLER":      ["belt edge filler", "edge gum"],
    "Cap Strip":             ["edge gum"],
    "GUM STRIP":             ["edge gum"],
    "Bead Apex":             ["apex"],
    "APEXED BEAD":           ["apex"],
    "Inner Liner":           ["inner liner"],
    "PRE ASSEMBLY":          ["inner liner"],
    "NYLON-1":               ["calendered textile", "textile fabric"],
    "NYLON2&3":              ["calendered textile", "textile fabric"],
    "STEEL CHIPPER LEFT":    ["belts", "stc"],
    "STEEL CHIPPER RIGHT":   ["belts", "stc"],
    "SLITTED MATERIAL":      ["slitted"],
    "Rubberized Ply":        ["bodyply"],
    "PRE CUT ROLL MATERIAL": ["cut fabric"],
    "CALANDARED ROLL":       ["calendered"],
}


def load_age_map(spec_path):
    """Resolve each BOM component to (min_h, max_h) from the ageing spec."""
    import polars as _pl
    if not spec_path.exists():
        return {}, []
    sp = _pl.read_parquet(spec_path)
    rows = [(str(r["component"]).lower(), r["min_h"], r["max_h"])
            for r in sp.iter_rows(named=True)]
    out, unmapped = {}, []
    for desc, keys in SPEC_KEYS.items():
        hits = [(mn, mx) for comp, mn, mx in rows
                if any(k in comp for k in keys) and mx is not None]
        if not hits:
            unmapped.append(desc)
            continue
        mx = min(h[1] for h in hits)                      # tightest max
        mn = max((h[0] for h in hits if h[0] is not None), default=0.0)
        out[desc] = (desc.lower(), float(mn), float(mx))
    return out, unmapped


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", default="2026-07")
    ap.add_argument("--run", default=None)
    a = ap.parse_args()
    run = ROOT / "runs" / (a.run or f"cmbc_{a.month}")
    from planner.data.warehouse import duck, set_cutoff
    set_cutoff(None)

    AGE_MAP, unmapped = load_age_map(INP / "semi_finished_ageing.parquet")
    if not AGE_MAP:
        raise SystemExit("ageing spec not found -- run scripts/build_gaps.py")
    bs = pl.read_parquet(run / "build_schedule.parquet").filter(
        pl.col("machine") != "OPENING_STOCK")
    bom = duck().execute(
        "SELECT plant, parent, child, child_qty, child_unit, child_desc "
        "FROM v_bom").pl()

    def norm(x: str | None) -> str:
        """Collapse `GT 5079 - 255/70R22.5 JUX E` and `GT 5001` to `GT5001`."""
        if not x:
            return ""
        t = str(x).upper().strip()
        m = re.match(r"^GT\s*(\d{3,5})\b", t)
        if m:
            return f"GT{m.group(1)}"
        return re.sub(r"[^A-Z0-9./]", "", t)

    # adjacency, keyed on the normalised parent
    adj: dict[str, list] = {}
    for r in bom.iter_rows(named=True):
        adj.setdefault(norm(r["parent"]), []).append(
            (r["child"], float(r["child_qty"] or 0), r["child_unit"],
             r["child_desc"], r["plant"]))

    def leaves(gt: str) -> list:
        """The GT's DIRECT children -- the semi-finished level. No walk."""
        return [(child, q, unit, desc, pl_)
                for (child, q, unit, desc, pl_) in adj.get(norm(gt), [])
                if desc in AGE_MAP]

    print("=" * 92)
    print(f"L8  PREP / MIXING BACK-EXPLOSION  --  {a.month}   (R16)")
    print("=" * 92)

    gts = set(bs["gt_code"].unique().to_list())
    exploded: dict[str, list] = {g: leaves(g) for g in gts}
    covered = {g for g, v in exploded.items() if v}
    miss = gts - covered
    missing_qty = float(bs.filter(pl.col("gt_code").is_in(list(miss)))["qty"].sum())
    if unmapped:
        print(f"  components with no ageing-spec match: {unmapped}")
    print(f"  BOM coverage: {len(covered)}/{len(gts)} GTs "
          f"({100*len(covered)/max(len(gts),1):.0f}%)   "
          f"UNEXPLODED {missing_qty:,.0f} tyres across {len(miss)} GTs")
    print("  (an unexploded GT is prep demand that exists on the floor but not "
          "in this plan)\n")

    # ---- explode -------------------------------------------------------
    rows = []
    for r in bs.iter_rows(named=True):
        for child, q, unit, desc, _pl in exploded.get(r["gt_code"], []):
            rows.append({"plant": r["plant"], "gt_code": r["gt_code"],
                         "machine": r["machine"], "start_ts": r["start_ts"],
                         "child": child, "child_unit": unit,
                         "child_desc": desc, "need": float(r["qty"]) * q})
    if not rows:
        raise SystemExit("no GT exploded -- check BOM key normalisation")
    ex = pl.DataFrame(rows)
    ex = ex.with_columns(
        pl.col("child_desc").replace_strict(
            {k: v[0] for k, v in AGE_MAP.items()}, default="other").alias("comp"),
        pl.col("child_desc").replace_strict(
            {k: v[1] for k, v in AGE_MAP.items()}, default=0.0).alias("min_h"),
        pl.col("child_desc").replace_strict(
            {k: v[2] for k, v in AGE_MAP.items()}, default=72.0).alias("max_h"))
    # the prep window: make it late enough not to expire, early enough to be there
    ex = ex.with_columns(
        (pl.col("start_ts") - pl.duration(hours=pl.col("max_h"))).alias("make_from"),
        (pl.col("start_ts") - pl.duration(hours=pl.col("min_h"))).alias("make_by"))

    print("  COMPONENT REQUIREMENT")
    print(f"  {'component':<24}{'min h':>7}{'max h':>7}{'lines':>8}"
          f"{'quantity':>14}{'unit':>6}{'window h':>10}")
    for r in (ex.group_by(["comp", "min_h", "max_h", "child_unit"])
              .agg(pl.len().alias("n"), pl.col("need").sum().alias("q"))
              .sort("q", descending=True).iter_rows(named=True)):
        print(f"  {r['comp'][:24]:<24}{r['min_h']:>7.0f}{r['max_h']:>7.0f}"
              f"{r['n']:>8,}{r['q']:>14,.0f}{str(r['child_unit'])[:5]:>6}"
              f"{r['max_h']-r['min_h']:>10.0f}")

    # ---- per shift ------------------------------------------------------
    t0 = bs["start_ts"].min()
    ex = ex.with_columns(
        (((pl.col("make_by") - pl.lit(t0)).dt.total_seconds() / 3600
          // SHIFT_H)).cast(pl.Int64).alias("shift"))
    per = (ex.group_by(["plant", "shift", "comp"])
           .agg(pl.col("need").sum().alias("q"))
           .sort(["plant", "shift", "comp"]))
    per.write_parquet(run / "prep_requirement.parquet")

    print(f"\n  PER-SHIFT PREP LOAD ({SHIFT_H} h shifts)")
    print(f"  {'plant':<6}{'component':<24}{'shifts':>8}{'q/shift p50':>14}"
          f"{'peak':>12}{'peak/p50':>10}")
    for r in (per.group_by(["plant", "comp"])
              .agg(pl.len().alias("sh"), pl.col("q").median().alias("p50"),
                   pl.col("q").max().alias("mx"))
              .sort(["plant", "p50"], descending=[False, True]).iter_rows(named=True)):
        print(f"  {r['plant']:<6}{r['comp'][:24]:<24}{r['sh']:>8}"
              f"{r['p50']:>14,.0f}{r['mx']:>12,.0f}"
              f"{r['mx']/max(r['p50'],1e-9):>9.1f}x")

    # ---- the binding clock ----------------------------------------------
    print("\n  TIGHTEST SHELF LIVES -- these bind prep, not the GT's 72 h")
    tight = (ex.group_by(["comp", "max_h"]).agg(pl.col("need").sum().alias("q"))
             .sort("max_h").head(5))
    for r in tight.iter_rows(named=True):
        print(f"    {r['comp'][:28]:<28} max {r['max_h']:>5.0f} h   "
              f"{r['q']:>14,.0f} required")
    print("    -> a component made before its window is scrap, exactly as a GT")
    print("       cured after 72 h is scrap. Both bounds are hard.")

    # ---- prep capacity, where we have it --------------------------------
    pc = INP / "prep_changeover.parquet"
    if pc.exists():
        p = pl.read_parquet(pc)
        print(f"\n  PREP CHANGEOVER MASTER: {p.height} operations across "
              f"{p['machine'].n_unique()} machines, {p['section'].n_unique()} sections")
        print("    (capacity check needs prep routing + rates -- GAP-9 gave us")
        print("     changeover times only, so load is not yet computable)")
    print(f"\n  -> {run.name}/prep_requirement.parquet")


if __name__ == "__main__":
    main()
