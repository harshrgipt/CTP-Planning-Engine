"""Occupancy spread / tyres-per-machine across A/B arms.  Read-only.

    python scripts/_diag_occ_arms.py <month> <arm> [<arm> ...]
"""
from __future__ import annotations

import calendar
import sys
from pathlib import Path

import numpy as np
import polars as pl
from planner import paths

ROOT = Path(__file__).resolve().parent.parent
INP = paths.INPUT_DERIVED
LOCK = pl.read_parquet(INP / "machine_rim_lock.parquet")
CAD = pl.read_parquet(INP / "cycle_time_building.parquet")


def occ(run: Path, plant: str, H: float):
    b = (pl.read_parquet(run / "build_schedule.parquet")
         .filter((pl.col("plant") == plant)
                 & (pl.col("machine") != "OPENING_STOCK")))
    g = (b.group_by("machine")
         .agg(((pl.col("end_ts") - pl.col("start_ts"))
               .dt.total_seconds().sum() / 3600.0).alias("h"),
              pl.col("qty").sum().alias("q")))
    M = (LOCK.filter(pl.col("plant") == plant).select("machine", "locked_rim")
         .join(CAD.select("machine", "s_per_tyre"), on="machine", how="left")
         .join(g, on="machine", how="left").fill_null(0.0)
         .with_columns((pl.col("h") / H * 100).alias("occ")))
    return M


def main() -> int:
    month = sys.argv[1]
    y, m = (int(x) for x in month.split("-"))
    H = calendar.monthrange(y, m)[1] * 24.0
    arms = sys.argv[2:]
    for plant in ("PCR", "TBR"):
        print("=" * (30 + 14 * len(arms)))
        print(f"{plant}  {month}   machine occupancy across arms")
        print("=" * (30 + 14 * len(arms)))
        tab = {a: occ(ROOT / "runs" / a, plant, H) for a in arms}
        base = tab[arms[0]]
        print(f"{'machine':<18}{'rim':<7}{'cad':>5}"
              + "".join(f"{a[-12:]:>14}" for a in arms))
        for r in base.sort("occ", descending=True).iter_rows(named=True):
            cells = "".join(
                f"{float(tab[a].filter(pl.col('machine') == r['machine'])['occ'][0]):>14.1f}"
                for a in arms)
            print(f"{r['machine'].replace('Stage2',''):<18}{r['locked_rim']:<7}"
                  f"{r['s_per_tyre']:>5.0f}{cells}")
        for lbl, fn in (("min", np.min), ("max", np.max),
                        ("spread", lambda v: v.max() - v.min()),
                        ("CV", lambda v: v.std() / v.mean()),
                        ("corr(cad,occ)", None)):
            cells = ""
            for a in arms:
                v = tab[a]["occ"].to_numpy()
                if fn is None:
                    c = np.corrcoef(tab[a]["s_per_tyre"].to_numpy(), v)[0, 1]
                    cells += f"{c:>14.3f}"
                else:
                    cells += f"{fn(v):>14.3f}" if lbl == "CV" else f"{fn(v):>14.2f}"
            print(f"{lbl:<30}{cells}")
        print(f"{'tyres min/max':<30}"
              + "".join(f"{tab[a]['q'].min():>7,.0f}/{tab[a]['q'].max():<6,.0f}"
                        for a in arms))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
