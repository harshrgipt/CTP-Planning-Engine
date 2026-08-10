"""Derive a per-month demand file from the MES production history.

    python -m scripts.make_demand [out_dir]

Writes one file per month into `masters/demand/`:

    demand_<YYYY-MM>.csv        plant, gt_code, sku, qty, month, due_date
    demand_<YYYY-MM>.parquet    same, for the planner
    demand_summary.csv          one row per (month, plant)

DEMAND IS WHAT WAS **CURED**, not what was built. A finished tyre is the unit
the customer orders; green tyres are an intermediate the plant chooses in
service of it. Using build counts instead would bake our own WIP policy into the
demand signal -- the plant built ~4% more green tyres than it cured in July, and
that surplus is a scheduling artefact, not customer demand.

Quality filter mirrors the rest of the engine: `statuscritical = 'Normal'` on
the curing side, so scrap and rework re-cures are excluded.

The join is the one documented in MEMORY s3: `v_build.productionID =
v_curing.gtbarCode` (per-tyre barcode, 99.6% hit rate). Never join on
`gt_code IS NOT NULL` -- that is a cartesian product and was a real bug.

DAILY ROWS, day 1 .. 30/31. Each row is what that GT actually shipped on that
calendar day, so the file carries the real demand PROFILE, not just a monthly
total. The planner still re-derives its own build targets from the press
campaign plan, but the daily figures are what any due-date or fulfilment check
has to be scored against.
"""
from __future__ import annotations

import calendar
import sys
from datetime import date
from pathlib import Path

import polars as pl

from planner.config import CONFIG
from planner.data.warehouse import duck, set_cutoff
from planner.runs.logger import log


def build_demand() -> pl.DataFrame:
    """One row per (day, plant, gt_code) with the cured quantity."""
    con = duck()
    df = con.execute("""
        SELECT date_trunc('month', c.event_ts)::DATE AS month,
               CAST(c.event_ts AS DATE)              AS due_date,
               b.plant,
               b.itemCode                            AS gt_code,
               count(*)::BIGINT                      AS qty
        FROM v_build b
        JOIN v_curing c ON b.productionID = c.gtbarCode
        WHERE b.stage = 2
          AND c.statuscritical = 'Normal'
          AND b.itemCode IS NOT NULL
        GROUP BY 1,2,3,4
        HAVING count(*) > 0
    """).pl()
    # Finished-SKU label where the BOM resolves it. TBR will not resolve: the
    # BOM keys TBR on "GT 5001"-style codes while TBR MES itemCode is size-led
    # ("10.00 R 20 JDC3") -- zero overlap (MEMORY s3, join trap 2).
    # SKU from the CURING RECIPE, not the BOM. Every cured tyre carries its
    # curing recipeID, and that recipe's SAPMaterialCode IS the finished SKU --
    # 100% coverage of produced volume, both plants. The BOM route resolved 55%
    # of GTs and ~0% of TBR, because TBR's BOM is keyed "GT 5001" while TBR MES
    # itemCode is size-led.
    try:
        from planner.config import CONFIG as _C
        _p = _C.paths.warehouse / "derived" / "gt_sku_from_recipe.parquet"
        bom = (pl.read_parquet(_p).select(["plant", "gt_code", "sku_code"])
               .rename({"sku_code": "sku"}))
        df = df.join(bom, on=["plant", "gt_code"], how="left")
    except Exception as e:  # noqa: BLE001
        log.warning("demand.bom_join_failed", err=str(e))
        df = df.with_columns(pl.lit(None, dtype=pl.Utf8).alias("sku"))
    return (df.with_columns(pl.col("sku").fill_null(""))
              .sort(["month", "plant", "due_date", "qty"],
                    descending=[False, False, False, True]))


def main(out_dir: Path) -> int:
    set_cutoff(None)
    out_dir.mkdir(parents=True, exist_ok=True)
    df = build_demand()
    if df.height == 0:
        log.error("demand.empty")
        return 1

    summary = []
    for mo in df["month"].unique().sort().to_list():
        sub = df.filter(pl.col("month") == mo)
        y, m = mo.year, mo.month
        last = date(y, m, calendar.monthrange(y, m)[1])
        tag = f"{y}-{m:02d}"
        out = sub.select([
            "plant", "gt_code", "sku",
            pl.col("qty").cast(pl.Float64),
            pl.lit(tag).alias("month"),
            "due_date",
            pl.col("due_date").dt.day().alias("day"),
        ]).sort(["plant", "day", "gt_code"])
        out.write_csv(out_dir / f"demand_{tag}.csv")
        out.write_parquet(out_dir / f"demand_{tag}.parquet", compression="zstd")
        for plant in sorted(sub["plant"].unique().to_list()):
            s = sub.filter(pl.col("plant") == plant)
            daily = s.group_by("due_date").agg(pl.col("qty").sum().alias("q"))
            summary.append({
                "month": tag, "plant": plant,
                "days": s["due_date"].n_unique(), "month_days": last.day,
                "gts": s["gt_code"].n_unique(),
                "qty": int(s["qty"].sum()),
                "qty_per_day_p50": int(daily["q"].median()),
                "qty_per_day_min": int(daily["q"].min()),
                "qty_per_day_max": int(daily["q"].max()),
            })
        print(f"  demand_{tag}.csv  {sub.height:>6} rows  "
              f"{sub['gt_code'].n_unique():>4} GTs  "
              f"days {sub['due_date'].dt.day().min():>2}-{sub['due_date'].dt.day().max():<2}  "
              f"{int(sub['qty'].sum()):>9,} tyres")

    sm = pl.DataFrame(summary)
    sm.write_csv(out_dir / "demand_summary.csv")
    print("\n" + str(sm))
    log.info("demand.written", months=df["month"].n_unique(), dir=str(out_dir))
    return 0


if __name__ == "__main__":
    d = Path(sys.argv[1]) if len(sys.argv) > 1 else CONFIG.paths.masters / "demand"
    sys.exit(main(d))
