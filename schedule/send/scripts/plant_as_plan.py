"""Reconstruct the PLANT'S OWN executed month from MES into our artifact schema,
so it can be scored by our own L11 invariants.

    python scripts/plant_as_plan.py 2026-07 [--run plant_2026-07]

WHY THIS EXISTS -- it is the existence proof, turned into a test.

On Dec-2025..Jul-2026 our demand IS what the plant cured. So on those months a
feasible schedule provably EXISTS: the plant ran it, on the same machines, the
same presses, the same moulds, in the same month. Any point we miss there is not
a capacity question -- it is our engine or our own constraints.

That cuts both ways, and this script tests the other side: **does the plant's own
executed schedule pass OUR invariants?** Every gate the plant itself fails is a
gate we mined wrong -- a distribution turned into a wall. That bug class has cost
this project 13.4 points twice already (PARTITION_AND_CHANGEOVER §1).

RECONSTRUCTION, per tyre, using the documented barcode join (MEMORY §3):
    v_build.productionID = v_curing.gtbarCode        (99.6 % hit rate)
    build machine  = v_build.machineCode
    build ts       = v_build.event_ts
    press          = v_curing.wcID  (cast VARCHAR)
    cure ts        = v_curing.event_ts   -- press CLOSE; `cycleStart` is press
                     OPEN, i.e. the cycle END. The source is named backwards.
Only `statuscritical = 'Normal'` cures count, matching make_demand.py.

A build RUN is a maximal consecutive same-GT block on one machine, split at any
gap > 1 h -- the same definition the scorecard and arm_report use, so the two
sides are measured identically. A cure CAMPAIGN is the same on a press.
"""
from __future__ import annotations

import argparse
import calendar
from datetime import datetime
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(ROOT))
from planner.data.warehouse import duck, set_cutoff  # noqa: E402


def blocks(df: pl.DataFrame, key: str, ts: str, gap_h: float = 1.0) -> pl.DataFrame:
    """Maximal consecutive same-GT blocks on one resource, split at >gap_h."""
    d = df.sort([key, ts])
    d = d.with_columns([
        (pl.col("gt_code") != pl.col("gt_code").shift(1).over(key)).fill_null(True)
        .alias("_newgt"),
        ((pl.col(ts) - pl.col(ts).shift(1).over(key)).dt.total_seconds() / 3600.0
         > gap_h).fill_null(True).alias("_gap"),
    ])
    return d.with_columns(
        (pl.col("_newgt") | pl.col("_gap")).cum_sum().over(key).alias("_blk"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("month")
    ap.add_argument("--run", default=None)
    a = ap.parse_args()
    y, m = int(a.month[:4]), int(a.month[5:7])
    nd = calendar.monthrange(y, m)[1]
    t0, t1 = datetime(y, m, 1), datetime(y + (m == 12), (m % 12) + 1, 1)
    run = ROOT / "runs" / (a.run or f"plant_{a.month}")
    run.mkdir(parents=True, exist_ok=True)
    set_cutoff(None)

    q = f"""
    SELECT b.plant                       AS plant,
           b.itemCode                    AS gt_code,
           b.machineCode                 AS machine,
           CAST(c.wcID AS VARCHAR)       AS press,
           b.event_ts                    AS built_ts,
           c.event_ts                    AS cure_ts
    FROM v_build b
    JOIN v_curing c ON b.productionID = c.gtbarCode
    WHERE b.stage = 2
      AND c.statuscritical = 'Normal'
      AND c.date >= DATE '{t0:%Y-%m-%d}' AND c.date < DATE '{t1:%Y-%m-%d}'
      AND b.itemCode IS NOT NULL AND b.machineCode IS NOT NULL
    """
    t = duck().execute(q).pl()
    print(f"  {a.month}: {t.height:,} plant tyres reconstructed "
          f"(PCR {t.filter(pl.col('plant')=='PCR').height:,} / "
          f"TBR {t.filter(pl.col('plant')=='TBR').height:,})")
    t = t.with_columns(
        ((pl.col("cure_ts") - pl.col("built_ts")).dt.total_seconds() / 3600.0)
        .alias("wait_h"))

    # ---- build_schedule: one row per RUN --------------------------------
    b = blocks(t, "machine", "built_ts")
    bs = (b.group_by(["plant", "machine", "_blk"]).agg(
            pl.col("gt_code").first(),
            pl.col("press").first(),
            pl.col("built_ts").min().alias("start_ts"),
            pl.col("built_ts").max().alias("end_ts"),
            pl.len().cast(pl.Int64).alias("qty"),   # NEVER u32: negate() fails,
            pl.col("cure_ts").max().alias("cure_ts"),  # and u32 underflow has bitten before
            pl.col("wait_h").max().alias("wait_h"))
          .with_columns((pl.col("machine") + "#" +
                         pl.col("_blk").cast(pl.Utf8)).alias("run_id"))
          .drop("_blk")
          .select(["plant", "gt_code", "machine", "press", "start_ts", "end_ts",
                   "qty", "cure_ts", "wait_h", "run_id"]))
    bs.write_parquet(run / "build_schedule.parquet")

    # ---- cure_campaigns: one row per press campaign ---------------------
    c = blocks(t, "press", "cure_ts")
    cc = (c.group_by(["plant", "press", "_blk"]).agg(
            pl.col("gt_code").first(),
            pl.col("cure_ts").min().alias("start_ts"),
            pl.col("cure_ts").max().alias("end_ts"),
            pl.len().cast(pl.Int64).alias("qty"))
          .with_columns(
              ((pl.col("end_ts") - pl.col("start_ts")).dt.total_seconds() / 3600.0)
              .alias("hours"),
              pl.col("gt_code").alias("mould_set"))
          .drop("_blk")
          .select(["plant", "gt_code", "mould_set", "press", "start_ts",
                   "end_ts", "qty", "hours"]))
    cc.write_parquet(run / "cure_campaigns.parquet")
    cc.with_columns(pl.col("qty").alias("qty_fed"),
                    pl.col("qty").alias("fed_qty"),
                    pl.lit(0).alias("qty_unfed"),
                    pl.lit(0).alias("_scarcity")
                    ).write_parquet(run / "cure_campaigns_reconciled.parquet")

    # ---- gt_events: +1 at build, -1 at cure, per tyre --------------------
    ev = pl.concat([
        t.select(["plant", "gt_code", pl.col("built_ts").alias("ts")])
         .with_columns(pl.lit(1.0).alias("d")),
        t.select(["plant", "gt_code", pl.col("cure_ts").alias("ts")])
         .with_columns(pl.lit(-1.0).alias("d")),
    ]).sort("ts")
    ev.write_parquet(run / "gt_events.parquet")
    pl.DataFrame(schema={"plant": pl.Utf8, "gt_code": pl.Utf8, "press": pl.Utf8,
                         "qty": pl.Float64, "reason": pl.Utf8}
                 ).write_parquet(run / "build_starved.parquet")
    pl.DataFrame(schema={"plant": pl.Utf8, "gt_code": pl.Utf8, "mould_set": pl.Utf8,
                         "qty": pl.Float64, "seq": pl.Int64, "reason": pl.Utf8}
                 ).write_parquet(run / "cure_unplaced.parquet")

    for p in ("PCR", "TBR"):
        bp, cp = bs.filter(pl.col("plant") == p), cc.filter(pl.col("plant") == p)
        if not bp.height:
            continue
        print(f"    {p}: {bp.height:,} build runs (lot p50 "
              f"{bp['qty'].median():.0f}), {cp.height:,} cure campaigns, "
              f"{bp['machine'].n_unique()} machines, {cp['press'].n_unique()} presses")
    print(f"  -> {run}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
