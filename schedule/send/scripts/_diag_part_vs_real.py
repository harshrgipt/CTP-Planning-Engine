"""Partition BOOK vs L7 DELIVERY, per machine and per (GT, machine).

    python scripts/_diag_part_vs_real.py <run-dir> <YYYY-MM> [PCR]

The partition is the horizon assignment; L7 places against it and may spill.
This asks the only question that matters for an occupancy RCA: WHERE was the
work booked, where did it end up, and what is the delivery ratio per machine.
"""
from __future__ import annotations

import calendar
import sys
from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parent.parent
INP = ROOT.parent.parent / "INPUT" / "derived"

pl.Config.set_tbl_rows(80)
pl.Config.set_tbl_width_chars(230)


def main(run: Path, month: str, plant: str) -> None:
    y, m = (int(x) for x in month.split("-"))
    H = calendar.monthrange(y, m)[1] * 24.0

    part = pl.read_parquet(INP / "gt_machine_partition.parquet")
    if "month" in part.columns:
        pm = part["month"][0]
        if pm != month:
            raise SystemExit(f"partition is {pm}, need {month} -- rebuild it")
    part = part.filter(pl.col("plant") == plant)
    lock = pl.read_parquet(INP / "machine_rim_lock.parquet") \
        .filter(pl.col("plant") == plant)
    cad = pl.read_parquet(INP / "cycle_time_building.parquet")
    bs = (pl.read_parquet(run / "build_schedule.parquet")
          .filter((pl.col("plant") == plant)
                  & (pl.col("machine") != "OPENING_STOCK")))

    real = (bs.group_by(["machine", "gt_code"])
            .agg(pl.col("qty").sum().alias("r_q"),
                 ((pl.col("end_ts") - pl.col("start_ts"))
                  .dt.total_seconds().sum() / 3600.0).alias("r_h")))
    book = part.group_by(["machine", "gt_code"]).agg(
        pl.col("hours").sum().alias("b_h"), pl.col("qty").sum().alias("b_q"))

    # ---------- per machine ------------------------------------------------
    mb = book.group_by("machine").agg(pl.col("b_h").sum(), pl.col("b_q").sum())
    mr = real.group_by("machine").agg(pl.col("r_h").sum(), pl.col("r_q").sum())
    mm = (lock.select("machine", "locked_rim", "tier", "purity")
          .join(cad.select("machine", "s_per_tyre"), on="machine", how="left")
          .join(mb, on="machine", how="left")
          .join(mr, on="machine", how="left").fill_null(0.0)
          .with_columns(
              (pl.col("b_h") / H * 100).round(1).alias("book_pct"),
              (pl.col("r_h") / H * 100).round(1).alias("real_pct"),
              (pl.col("r_h") / pl.col("b_h").clip(1e-9)).round(3).alias("deliver"),
              (pl.col("r_h") - pl.col("b_h")).round(1).alias("dh")))
    print("=" * 118)
    print(f"{plant} {month}: PARTITION BOOK vs L7 DELIVERY   "
          f"({H:.0f} h/machine)")
    print("=" * 118)
    print(mm.select("machine", "locked_rim", "tier", "s_per_tyre", "purity",
                    "b_h", "book_pct", "r_h", "real_pct", "dh", "deliver")
          .sort("book_pct", descending=True))

    bk = mm["book_pct"].to_numpy()
    rl = mm["real_pct"].to_numpy()
    cd = mm["s_per_tyre"].to_numpy()
    dv = mm["deliver"].to_numpy()
    print(f"\n  book_pct   min {bk.min():5.1f}  max {bk.max():5.1f}  "
          f"spread {bk.max()-bk.min():5.1f}  CV {bk.std()/bk.mean():.4f}")
    print(f"  real_pct   min {rl.min():5.1f}  max {rl.max():5.1f}  "
          f"spread {rl.max()-rl.min():5.1f}  CV {rl.std()/rl.mean():.4f}")
    print(f"\n  corr(cadence, BOOK   occ) = {np.corrcoef(cd, bk)[0,1]: .3f}")
    print(f"  corr(cadence, REAL   occ) = {np.corrcoef(cd, rl)[0,1]: .3f}")
    print(f"  corr(BOOK occ, DELIVERY ) = {np.corrcoef(bk, dv)[0,1]: .3f}"
          "   <- over-booked machines under-deliver")
    print(f"  corr(BOOK occ, REAL occ ) = {np.corrcoef(bk, rl)[0,1]: .3f}")

    # ---------- spill: work that left its booked machine --------------------
    j = (book.join(real, on=["machine", "gt_code"], how="full", coalesce=True)
         .fill_null(0.0))
    on_pin = j.filter((pl.col("b_h") > 0) & (pl.col("r_h") > 0))
    off_pin = j.filter((pl.col("b_h") == 0) & (pl.col("r_h") > 0))
    unbuilt = j.filter((pl.col("b_h") > 0) & (pl.col("r_h") == 0))
    print(f"\n-- FLOW  booked {book['b_h'].sum():,.0f} h · "
          f"realised {real['r_h'].sum():,.0f} h")
    print(f"   on-pin  pairs {on_pin.height:3d}   {on_pin['r_h'].sum():8,.1f} h "
          f"({on_pin['r_h'].sum()/max(real['r_h'].sum(),1)*100:5.1f} % of realised)")
    print(f"   OFF-PIN pairs {off_pin.height:3d}   {off_pin['r_h'].sum():8,.1f} h "
          f"({off_pin['r_h'].sum()/max(real['r_h'].sum(),1)*100:5.1f} % of realised)")
    print(f"   booked-but-never-built pairs {unbuilt.height:3d}   "
          f"{unbuilt['b_h'].sum():8,.1f} h")
    print("\n   OFF-PIN detail (work L7 put on a machine the partition did not book)")
    print(off_pin.sort("r_h", descending=True).head(20)
          .join(lock.select("machine", "locked_rim"), on="machine", how="left"))
    print("\n   BOOKED BUT NOT BUILT (largest)")
    print(unbuilt.sort("b_h", descending=True).head(12)
          .join(lock.select("machine", "locked_rim"), on="machine", how="left"))


if __name__ == "__main__":
    main(Path(sys.argv[1]), sys.argv[2],
         sys.argv[3] if len(sys.argv) > 3 else "PCR")
