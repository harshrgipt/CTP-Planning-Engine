"""INDEPENDENT check: does the build schedule respect the plant's allowable list?

    python scripts/check_allowable.py <run> [--month YYYY-MM]

Reads ONLY the run's build_schedule.parquet and INPUT/derived/allowed_machine_matrix
.parquet. It does not import a planner layer, so it cannot inherit the engine's
own view of eligibility -- which is exactly how 19.9 % of July PCR volume sat on
unsanctioned machines while every gate passed.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from planner import paths                                          # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    a = ap.parse_args()
    bs = pl.read_parquet(paths.RUNS / a.run / "build_schedule.parquet").filter(
        pl.col("machine") != "OPENING_STOCK")
    am = pl.read_parquet(paths.input_derived("allowed_machine_matrix.parquet")
                         ).select(["plant", "gt_code", "machine"]).unique()
    ruled = am.select(["plant", "gt_code"]).unique().with_columns(
        pl.lit(True).alias("_r"))
    bad = (bs.join(ruled, on=["plant", "gt_code"], how="left")
             .join(am.with_columns(pl.lit(True).alias("_ok")),
                   on=["plant", "gt_code", "machine"], how="left")
             .filter(pl.col("_r").is_not_null() & pl.col("_ok").is_null()))
    print(f"\n  ALLOWABLE CHECK  runs/{a.run}")
    for p, g in bs.group_by("plant"):
        b = bad.filter(pl.col("plant") == p[0])
        tot = float(g["qty"].sum())
        print(f"    {p[0]}: {tot:>10,.0f} tyres · violations "
              f"{float(b['qty'].sum()):>9,.0f} "
              f"({100 * float(b['qty'].sum()) / max(tot, 1):.2f}%) "
              f"over {b['gt_code'].n_unique()} GTs")
    n = bad.height
    if n:
        print(bad.group_by(["plant", "gt_code", "machine"])
                 .agg(pl.col("qty").sum()).sort("qty", descending=True).head(10))
    print(f"    -> {'FAIL' if n else 'PASS'}  ({n} offending slices)\n")
    sys.exit(1 if n else 0)


if __name__ == "__main__":
    main()
