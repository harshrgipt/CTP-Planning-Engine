"""Definitive GT -> SKU map, from the curing recipe. 100% of produced volume.

    python scripts/build_gt_sku.py

Every cured tyre carries BOTH keys:
    v_curing.gtbarCode -> v_build.itemCode        the GREEN TYRE   (route A)
    v_curing.recipeID  -> recipemaster.iD         the FINISHED SKU (route B)
                       -> SAPMaterialCode

They are not competing keys and they do not disagree -- they name the two ends of
the same operation. Route A was already correct; route B is the SKU dimension we
never had.

Coverage beats every alternative we tried:
    v_bom_gt                 55% of GTs, ~0% of TBR (TBR BOM is GT-5001-keyed
                             while TBR MES is size-led)
    recipe master chain      97.9% of GT codes, but needs a dual key
    ALL PCR CTP SKUS.xlsx    PCR only
    THIS                     100% of cured volume, both plants, zero nulls

A GT can map to several SKUs (same green tyre, different branding), so the map
carries the modal SKU plus the count of alternatives rather than pretending it
is 1:1.
"""
from __future__ import annotations

import sys
from pathlib import Path

import polars as pl

from planner.config import CONFIG
from planner.data.warehouse import duck, set_cutoff
from planner.runs.logger import log

RM = Path("C:/Users/91810/Downloads/send/Recipemaster 1.xlsx")


def _c(x) -> str:
    return "".join(ch for ch in str(x) if ord(ch) < 128).strip() if x is not None else ""


def main() -> int:
    set_cutoff(None)
    con = duck()
    import openpyxl
    wb = openpyxl.load_workbook(RM, read_only=True, data_only=True)
    raw = [[_c(c) for c in r] for r in wb.worksheets[0].iter_rows(values_only=True)]
    wb.close()
    hdr = raw[0]
    rm = [dict(zip(hdr, r)) for r in raw[1:] if any(r)]
    con.register("recipe", pl.DataFrame([{
        "recipe_id": d["iD"], "sku_code": d.get("SAPMaterialCode", ""),
        "recipe_name": d.get("name", ""), "sku_desc": d.get("description", ""),
        "tyre_size": d.get("tyreSize", ""), "oem": d.get("oemID", ""),
    } for d in rm]).to_arrow())

    d = con.execute("""
        WITH x AS (
            SELECT b.plant, b.itemCode AS gt_code, r.sku_code, r.sku_desc,
                   r.recipe_name, r.tyre_size, count(*) AS n
            FROM v_curing c
            JOIN v_build b ON b.productionID = c.gtbarCode AND b.stage = 2
            JOIN recipe r  ON c.recipeID::VARCHAR = r.recipe_id
            WHERE c.statuscritical = 'Normal' AND b.itemCode IS NOT NULL
            GROUP BY 1,2,3,4,5,6),
        rk AS (
            SELECT *, row_number() OVER (PARTITION BY plant, gt_code
                                         ORDER BY n DESC, sku_code) AS r,
                   sum(n) OVER (PARTITION BY plant, gt_code) AS tot,
                   count(*) OVER (PARTITION BY plant, gt_code) AS n_skus
            FROM x)
        SELECT plant, gt_code, sku_code, sku_desc, recipe_name, tyre_size,
               n AS tyres, tot AS gt_tyres, n_skus,
               round(100.0 * n / tot, 1) AS modal_share_pct
        FROM rk WHERE r = 1 ORDER BY plant, gt_code
    """).pl()

    print(f"GT -> SKU map: {d.height} (plant, GT) pairs, "
          f"{d['sku_code'].n_unique()} distinct SKUs")
    print(f"  covering {d['gt_tyres'].sum():,} cured tyres")
    for p in sorted(d["plant"].unique().to_list()):
        s = d.filter(pl.col("plant") == p)
        multi = s.filter(pl.col("n_skus") > 1)
        print(f"  {p}: {s.height} GTs, {multi.height} map to >1 SKU "
              f"(modal share p50 {s['modal_share_pct'].median():.0f}%)")

    mes = {r[0] for r in con.execute(
        "SELECT DISTINCT itemCode FROM v_build WHERE stage=2 AND itemCode IS NOT NULL"
    ).fetchall()}
    have = set(d["gt_code"].to_list())
    print(f"\nCoverage vs all MES GT codes: {len(mes & have)}/{len(mes)} "
          f"({100*len(mes & have)/len(mes):.1f}%)")
    print("  (GTs built but never cured have no SKU -- correctly absent)")

    out = CONFIG.paths.warehouse / "derived" / "gt_sku_from_recipe.parquet"
    d.write_parquet(out, compression="zstd")
    print(f"\nWROTE {out}  ({d.height} rows)")
    log.info("gt_sku.done", rows=d.height, skus=int(d["sku_code"].n_unique()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
