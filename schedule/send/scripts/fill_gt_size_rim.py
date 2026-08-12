"""Fill the missing rims in `gt_size.parquet` from Recipemaster's own rim column.

    python -m scripts.fill_gt_size_rim --month 2026-08            # report only
    python -m scripts.fill_gt_size_rim --month 2026-08 --write     # apply

WHY
  `gt_size.parquet` has no rim for 35 of August's 73 PCR GTs and 41 of July's.
  Those GTs cannot be rim-locked, cannot be partitioned, scatter across machines,
  and -- until the L11 fix -- contaminated the same-size metric in both
  directions. PARTITION/README call rim coverage the highest-value master-data
  fix available.

  It does not need the plant: `aging_limits.parquet`, derived from
  `Recipemaster 1.xlsx`, carries a rim for EVERY one of them. The GT's rim is
  taken only when all of its demanded SKUs agree, so nothing is guessed -- a GT
  whose SKUs disagree is left unset and reported.

THIS IS A SCHEDULING CHANGE, NOT JUST A REPORTING ONE
  `gt_size` is consumed by L7 (rim-aware machine choice, the rim lock, cluster
  sequencing) and by the partition builder. Adding 35 rims WILL move placement
  decisions. So it is applied behind `--write`, the previous file is kept as
  `gt_size.pre_rimfill.parquet`, and the effect must be measured as an arm --
  never assumed to be an improvement because the master got better.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from planner import paths                                          # noqa: E402


def build(month: str) -> tuple[pl.DataFrame, pl.DataFrame]:
    sz = pl.read_parquet(paths.input_derived("gt_size.parquet"))
    have = {(r["plant"], r["gt_code"]) for r in sz.iter_rows(named=True)
            if r.get("rim")}
    dem = pl.read_parquet(paths.demand(month))
    al = (pl.read_parquet(paths.input_derived("aging_limits.parquet"))
          .select(["sku", "rim"]).drop_nulls().unique(subset=["sku"]))
    sku_rim = {r["sku"]: r["rim"] for r in al.iter_rows(named=True)}

    add, ambiguous = [], []
    for (p, g), grp in dem.group_by(["plant", "gt_code"]):
        if (p, g) in have:
            continue
        rims = {sku_rim[s] for s in grp["sku"].to_list() if s in sku_rim}
        if len(rims) == 1:
            add.append({"plant": p, "gt_code": g, "sku": None,
                        "rim": rims.pop()})
        elif len(rims) > 1:
            ambiguous.append({"plant": p, "gt_code": g,
                              "rims": ",".join(sorted(rims)),
                              "qty": float(grp["qty"].sum())})
    return pl.DataFrame(add, schema=sz.schema), pl.DataFrame(ambiguous)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", required=True)
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()

    add, amb = build(a.month)
    sz = pl.read_parquet(paths.input_derived("gt_size.parquet"))
    dem = pl.read_parquet(paths.demand(a.month))

    print(f"\n  GT_SIZE RIM FILL  {a.month}")
    print(f"  {'-' * 66}")
    print(f"  gt_size rows before      {sz.height}")
    for p, g in add.group_by("plant"):
        print(f"    {p[0]}: +{g.height} GTs  rims {sorted(set(g['rim'].to_list()))}")
    if amb.height:
        print(f"  AMBIGUOUS (left unset)   {amb.height} GTs")
        print(amb)
    for p in ("PCR", "TBR"):
        d = dem.filter(pl.col("plant") == p)
        known = {r["gt_code"] for r in sz.iter_rows(named=True)
                 if r["plant"] == p and r.get("rim")}
        known |= set(add.filter(pl.col("plant") == p)["gt_code"].to_list())
        n = d["gt_code"].n_unique()
        cov = len([g for g in d["gt_code"].unique().to_list() if g in known])
        print(f"    {p} demanded-GT rim coverage after fill: {cov}/{n} "
              f"= {100 * cov / max(n, 1):.1f}%")

    if not a.write:
        print("  (report only -- pass --write to apply)\n")
        return
    src = paths.input_derived("gt_size.parquet")
    bak = src.parent / "gt_size.pre_rimfill.parquet"
    if not bak.exists():
        shutil.copy2(src, bak)
        print(f"  backup -> {bak.name}")
    pl.concat([sz, add]).write_parquet(src)
    print(f"  -> {src}  ({sz.height + add.height} rows)")
    print("  NOW RE-MEASURE: gt_size feeds L7 placement, so fulfilment, "
          "same-size AND every hard cap must be re-checked.\n")


if __name__ == "__main__":
    main()
