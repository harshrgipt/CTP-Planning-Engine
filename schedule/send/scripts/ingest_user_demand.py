"""L0.5 -- USER DEMAND INTAKE.  One SKU list in, PCR and TBR demand out.

    python -m scripts.ingest_user_demand --csv "../../INPUT/raw/Book6(Sheet5) (1).csv" \
                                         --month 2026-08

WHAT THE USER SUPPLIES
  A flat SKU list with no plant column:

      SKUCode, SKU Description, Requirement, Order Type, Market, Delivery date
      1325231015109QRMT0, 31x10.50R15 LT_ RANGER M/T..., 2500, MTS, Replacement,

  206 rows / 514,391 tyres in the shipped file. The plant split is OURS to
  derive -- the user should not have to know which works builds which SKU.

HOW THE PLANT IS DECIDED, in priority order
  1. `gt_sku_master` (SKU -> GT -> plant) via the curing-recipe chain. This is
     the documented bridge: Recipemaster.SAPMaterialCode -> v_curing.recipeID ->
     v_build.itemCode, at 100 % of cured volume on both plants.
  2. `aging_limits` / `tt_tl` rim, then the PCR/TBR rim split. PCR is R12-R19
     passenger; TBR is R20/R22.5 truck. Used only when the chain is silent.
  3. Unresolved -> written to UNRESOLVED_<month>.csv and NOT planned. Never
     guessed onto a plant, because a PCR SKU planned as TBR is silently
     unbuildable and shows up as a fulfilment loss with no named cause.

  The file is read as latin-1: it is not valid UTF-8 (the SKU descriptions carry
  a stray byte) and polars refuses it with `invalid utf-8 sequence`.

OUTPUT
  masters/demand/demand_<month>.parquet  -- the schema every later layer expects:
      plant, gt_code, sku, qty, month, due_date, day
  Multiple SKUs mapping to one GT are kept as separate rows; L4 aggregates.
"""
from __future__ import annotations

import argparse
import calendar
import sys
from datetime import date
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from planner import paths                                          # noqa: E402

# PCR is passenger/light-truck radial, TBR truck & bus. The split is by rim.
TBR_RIMS = {"R20", "R22.5", "R24", "R24.5", "20", "22.5"}


def _plant_from_rim(rim: str | None) -> str | None:
    if not rim:
        return None
    return "TBR" if str(rim).strip() in TBR_RIMS else "PCR"


def run(csv: Path, month: str, *, write: bool = True) -> pl.DataFrame:
    raw = pl.read_csv(csv, infer_schema_length=0, encoding="latin-1")
    need = {"SKUCode", "Requirement"}
    if not need <= set(raw.columns):
        raise SystemExit(f"{csv.name} lacks {need - set(raw.columns)}")

    d = (raw.select([
            pl.col("SKUCode").str.strip_chars().alias("sku"),
            pl.col("Requirement").cast(pl.Float64, strict=False).alias("qty"),
            pl.col("Order Type").alias("order_type")
            if "Order Type" in raw.columns else pl.lit(None).alias("order_type"),
            pl.col("Market").alias("market")
            if "Market" in raw.columns else pl.lit(None).alias("market")])
          .filter(pl.col("sku").is_not_null() & (pl.col("qty") > 0)))
    n_in, q_in = d.height, d["qty"].sum()

    # ---- 1. the recipe chain -------------------------------------------
    gsm = (pl.read_parquet(paths.wh_derived("gt_sku_master.parquet"))
           .select(["plant", "sku_code", "gt_code"])
           .rename({"sku_code": "sku"})
           .unique(subset=["sku"]))
    d = d.join(gsm, on="sku", how="left")
    n_chain = d.filter(pl.col("plant").is_not_null()).height

    # ---- 2. rim fallback, plant only (GT stays null -> unplannable) -----
    al = (pl.read_parquet(paths.input_derived("aging_limits.parquet"))
          .select(["sku", "rim"]).unique(subset=["sku"]))
    d = (d.join(al, on="sku", how="left")
          .with_columns(pl.when(pl.col("plant").is_not_null())
                        .then(pl.col("plant"))
                        .otherwise(pl.col("rim").map_elements(
                            _plant_from_rim, return_dtype=pl.Utf8))
                        .alias("plant")))
    n_rim = d.filter(pl.col("plant").is_not_null()).height - n_chain

    # ---- 3. what we cannot place ---------------------------------------
    bad = d.filter(pl.col("plant").is_null() | pl.col("gt_code").is_null())
    ok = d.filter(pl.col("plant").is_not_null() & pl.col("gt_code").is_not_null())

    y, m = int(month[:4]), int(month[5:7])
    days = calendar.monthrange(y, m)[1]
    out = ok.select([
        "plant", "gt_code", "sku",
        pl.col("qty"),
        pl.lit(month).alias("month"),
        pl.lit(date(y, m, days)).alias("due_date"),
        pl.lit(days).alias("day"),
    ])

    print(f"\n  USER DEMAND INTAKE  {csv.name}  ->  {month}")
    print(f"  {'-' * 68}")
    print(f"  input                 {n_in:>5} SKU rows · {q_in:>10,.0f} tyres")
    print(f"  resolved via recipe   {n_chain:>5} rows")
    print(f"  plant via rim only    {n_rim:>5} rows (no GT -> not plannable)")
    for p, g in out.group_by("plant"):
        print(f"    {p[0]:<4} {g.height:>5} rows · {g['qty'].sum():>10,.0f} tyres "
              f"· {g['gt_code'].n_unique()} GTs")
    print(f"  UNRESOLVED            {bad.height:>5} rows · {bad['qty'].sum():>10,.0f} "
          f"tyres ({100*bad['qty'].sum()/max(q_in,1):.1f}%)")
    print(f"  planned total               {out['qty'].sum():>10,.0f} tyres")

    if write:
        f = paths.demand(month)
        f.parent.mkdir(parents=True, exist_ok=True)
        out.write_parquet(f)
        print(f"  -> {f}")
        if bad.height:
            u = paths.MASTERS / "demand" / f"UNRESOLVED_{month}.csv"
            bad.select(["sku", "qty", "order_type", "market", "rim"]).write_csv(u)
            print(f"  -> {u.name}  ({bad.height} rows the plant must map)")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="User SKU demand -> PCR/TBR demand")
    ap.add_argument("--csv", required=True)
    ap.add_argument("--month", required=True)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    run(Path(a.csv), a.month, write=not a.dry_run)


if __name__ == "__main__":
    main()
