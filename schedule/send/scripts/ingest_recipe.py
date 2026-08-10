"""Ingest the RECIPE MASTER + LOOKUP -- the GT <-> SKU bridge, and real aging limits.

    python scripts/ingest_recipe.py

THE CHAIN (as specified by planning):

    v_curing.RecipeId  ->  recipemaster.iD
    recipemaster.SAPMaterialCode  ->  the SAP code

    If SAPMaterialCode is a GT CODE (a BUILDING recipe, processID 4/7):
        take that recipe's iD
        -> recipelookup.tbmRecipeID  ->  curingRecipeID
        -> recipemaster.iD           ->  SAPMaterialCode = the SKU code

So building recipes carry the GT, curing recipes carry the finished SKU, and
`recipelookup` is the bridge between them. This is the mapping the BOM could not
give us -- it resolved only 55% of GT codes and ~0% of TBR, because TBR's BOM is
keyed on "GT 5001" while TBR MES itemCode is size-led ("10.00 R 20 JDC3").

TWO THINGS BEYOND THE MAPPING, both of which we have been guessing at:

  Minaging / MaxAging   PER-RECIPE shelf life. The engine assumes a FLAT 72h for
                        every GT. If these vary, our single hard constraint is
                        wrong in both directions -- too loose on some GTs (real
                        scrap we do not flag) and too tight on others (capacity
                        we refuse to use).

  curingCapacity        the plant's own stated press capacity per recipe, to
                        check against the 156/48 tyres-per-press-day we derived.
"""
from __future__ import annotations

import sys
from pathlib import Path

import polars as pl

from planner.config import CONFIG
from planner.data.warehouse import duck, set_cutoff
from planner.runs.logger import log

RM = Path("C:/Users/91810/Downloads/send/Recipemaster 1.xlsx")
RL = Path("C:/Users/91810/Downloads/send/recipelookup 1.xlsx")
BUILD_PIDS = {"4", "7"}      # building recipes -> SAPMaterialCode is a GT code
CURE_PIDS = {"5", "8", "27"}  # curing recipes  -> SAPMaterialCode is an SKU


def _clean(x) -> str:
    return "".join(ch for ch in str(x) if ord(ch) < 128).strip() if x is not None else ""


def _sheet(p: Path) -> list[dict]:
    import openpyxl
    wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
    rows = list(wb.worksheets[0].iter_rows(values_only=True))
    wb.close()
    hdr = [_clean(x) for x in rows[0]]
    return [dict(zip(hdr, [_clean(x) for x in r])) for r in rows[1:] if any(r)]


def main() -> int:
    set_cutoff(None)
    rm = _sheet(RM)
    rl = _sheet(RL)
    log.info("recipe.loaded", master=len(rm), lookup=len(rl))

    by_id = {d["iD"]: d for d in rm}
    print(f"recipe master {len(rm)} rows, lookup {len(rl)} rows")

    # ---- the chain ------------------------------------------------------
    # building recipe id -> curing recipe id
    tbm2cure = {d["tbmRecipeID"]: d["curingRecipeID"] for d in rl
                if d.get("tbmRecipeID") and d.get("curingRecipeID")}
    rows = []
    for d in rm:
        if d.get("processID") not in BUILD_PIDS:
            continue
        gt = d.get("SAPMaterialCode", "")
        if not gt:
            continue
        cid = tbm2cure.get(d["iD"])
        cure = by_id.get(cid) if cid else None
        rows.append({
            "build_recipe_id": d["iD"], "gt_code": gt,
            "gt_name": d.get("name", ""), "gt_desc": d.get("description", ""),
            "tyre_size": d.get("tyreSize", ""),
            "curing_recipe_id": cid or "",
            "sku_code": (cure or {}).get("SAPMaterialCode", ""),
            "sku_desc": (cure or {}).get("description", ""),
            "min_aging_h": (cure or {}).get("Minaging", ""),
            "max_aging_h": (cure or {}).get("MaxAging", ""),
            "aging_interlock": (cure or {}).get("AgingInterlock", ""),
            "curing_capacity": (cure or {}).get("curingCapacity", ""),
        })
    m = pl.DataFrame(rows)
    linked = m.filter(pl.col("sku_code") != "")
    print(f"\nGT -> SKU CHAIN: {m.height} building recipes, "
          f"{linked.height} resolve to an SKU ({100*linked.height/max(m.height,1):.1f}%)")
    print(linked.select(["gt_code", "curing_recipe_id", "sku_code"]).head(6))

    # ---- does it cover the GT codes the MES actually uses? ---------------
    mes = {r[0] for r in duck().execute(
        "SELECT DISTINCT itemCode FROM v_build WHERE stage=2 AND itemCode IS NOT NULL"
    ).fetchall()}
    have = set(m["gt_code"].to_list())
    # MES itemCode may be the recipe NAME rather than the SAP code
    have_name = set(m["gt_name"].to_list())
    print(f"\nCOVERAGE vs MES ({len(mes)} distinct GT codes):")
    print(f"  by SAPMaterialCode : {len(mes & have)}  ({100*len(mes & have)/len(mes):.1f}%)")
    print(f"  by recipe name     : {len(mes & have_name)} "
          f"({100*len(mes & have_name)/len(mes):.1f}%)")
    print(f"  by either          : {len(mes & (have | have_name))} "
          f"({100*len(mes & (have|have_name))/len(mes):.1f}%)")
    missing = sorted(mes - (have | have_name))[:8]
    if missing:
        print(f"  unmatched sample   : {missing}")

    # ---- REAL AGING LIMITS ----------------------------------------------
    print("\n" + "=" * 74)
    print("AGING LIMITS -- we assume a FLAT 72h for every GT. Do they vary?")
    print("=" * 74)
    ag = pl.DataFrame([{"min_aging_h": d.get("Minaging", ""),
                        "max_aging_h": d.get("MaxAging", ""),
                        "interlock": d.get("AgingInterlock", ""),
                        "cap": d.get("curingCapacity", ""),
                        "pid": d.get("processID", "")}
                       for d in rm if d.get("processID") in CURE_PIDS])
    for c in ("min_aging_h", "max_aging_h", "cap"):
        v = [float(x) for x in ag[c].to_list() if x not in ("", "NULL", None)
             and str(x).replace(".", "", 1).replace("-", "", 1).isdigit()]
        if v:
            v.sort()
            print(f"  {c:<14} n={len(v):4d}  min={v[0]:.0f}  p50={v[len(v)//2]:.0f}  "
                  f"max={v[-1]:.0f}  distinct={len(set(v))}")
        else:
            print(f"  {c:<14} EMPTY / non-numeric")
    il = ag["interlock"].value_counts().sort("count", descending=True)
    print(f"  AgingInterlock values: {il.head(4).to_dicts()}")

    out = CONFIG.paths.warehouse / "derived"
    out.mkdir(parents=True, exist_ok=True)
    m.sort(["gt_code"]).write_parquet(out / "recipe_gt_sku.parquet", compression="zstd")
    print(f"\nWROTE {out/'recipe_gt_sku.parquet'}  ({m.height} rows)")
    log.info("recipe.done", rows=m.height, linked=linked.height)
    return 0


if __name__ == "__main__":
    sys.exit(main())
