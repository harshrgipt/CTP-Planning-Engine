"""How many of the occupancy points are RIM-STRUCTURAL and how many ALLOCATION.

    python scripts/_diag_occ_split.py <run-dir> <YYYY-MM> [PCR]

Counterfactual: hold every rim's REALISED hours fixed (so fulfilment, the lock
and the spill are all untouched) and redistribute them equally inside each rim
group.  Whatever spread survives THAT is what the lock forces; the rest is what
an allocation change could in principle reach.
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
DER = ROOT / "warehouse" / "derived"
pl.Config.set_tbl_rows(60)
pl.Config.set_tbl_width_chars(210)


def main(run: Path, month: str, plant: str) -> None:
    y, m = (int(x) for x in month.split("-"))
    H = calendar.monthrange(y, m)[1] * 24.0
    lock = pl.read_parquet(INP / "machine_rim_lock.parquet") \
        .filter(pl.col("plant") == plant)
    cad = pl.read_parquet(INP / "cycle_time_building.parquet")
    gsz = (pl.read_parquet(INP / "gt_size.parquet")
           .filter(pl.col("plant") == plant).unique(["gt_code"])
           .select("gt_code", "rim"))
    bs = (pl.read_parquet(run / "build_schedule.parquet")
          .filter((pl.col("plant") == plant)
                  & (pl.col("machine") != "OPENING_STOCK")))
    st = (pl.read_parquet(run / "build_starved.parquet")
          .filter(pl.col("plant") == plant))

    real = (bs.group_by("machine")
            .agg(((pl.col("end_ts") - pl.col("start_ts"))
                  .dt.total_seconds().sum() / 3600.0).alias("r_h"),
                 pl.col("qty").sum().alias("r_q")))
    M = (lock.select("machine", "locked_rim", "tier")
         .join(cad.select("machine", "s_per_tyre"), on="machine", how="left")
         .join(real, on="machine", how="left").fill_null(0.0)
         .with_columns((pl.col("r_h") / H * 100).alias("occ")))

    # equal-occupancy counterfactual inside each rim group
    M = M.with_columns(
        (pl.col("r_h").mean().over("locked_rim") / H * 100).alias("bal_occ"))
    occ = M["occ"].to_numpy()
    bal = M["bal_occ"].to_numpy()
    print("=" * 104)
    print(f"{plant} {month}   occupancy: realised vs equal-inside-rim "
          f"counterfactual  ({H:.0f} h)")
    print("=" * 104)
    print(M.select("machine", "locked_rim", "s_per_tyre", "r_h", "occ",
                   "bal_occ",
                   (pl.col("bal_occ") - pl.col("occ")).round(2).alias("move_pt"),
                   ((pl.col("bal_occ") - pl.col("occ")) * H / 100)
                   .round(1).alias("move_h"))
          .sort("occ", descending=True))
    print(f"\n  realised spread      {occ.max()-occ.min():6.2f} pt "
          f"(min {occ.min():.2f} max {occ.max():.2f})  CV {occ.std()/occ.mean():.4f}")
    print(f"  after perfect within-rim rebalance "
          f"{bal.max()-bal.min():6.2f} pt "
          f"(min {bal.min():.2f} max {bal.max():.2f})  "
          f"CV {bal.std()/bal.mean():.4f}")
    print(f"  ==> ADDRESSABLE by allocation   "
          f"{(occ.max()-occ.min())-(bal.max()-bal.min()):6.2f} pt")
    print(f"  ==> RIM-STRUCTURAL (lock-forced) {bal.max()-bal.min():6.2f} pt")
    mv = np.abs(bal - occ).sum() / 2 * H / 100
    print(f"  hours a perfect rebalance would move: {mv:,.1f} h of "
          f"{M['r_h'].sum():,.1f} h  ({mv/M['r_h'].sum()*100:.1f} %)")
    n1 = M.group_by("locked_rim").len().filter(pl.col("len") == 1).height
    print(f"  rim groups with ONE machine: {n1} of "
          f"{M['locked_rim'].n_unique()}  -> "
          f"{M.join(M.group_by('locked_rim').len(), on='locked_rim').filter(pl.col('len')==1).height}"
          f" of {M.height} machines have ZERO allocation freedom")

    # ---- starvation by rim, against idle hours on that rim ------------------
    sv = (st.join(gsz, on="gt_code", how="left")
          .group_by("rim").agg(pl.col("qty").sum().alias("starved")))
    idle = (M.group_by("locked_rim")
            .agg(((H - pl.col("r_h")).sum()).alias("idle_h"),
                 pl.len().alias("n_mach"),
                 pl.col("s_per_tyre").min().alias("fastest_s")))
    z = (idle.join(sv, left_on="locked_rim", right_on="rim", how="left")
         .fill_null(0.0)
         .with_columns((pl.col("starved") * pl.col("fastest_s") / 3600.0)
                       .round(1).alias("starved_h_at_fastest")))
    print("\n-- STARVATION vs IDLE HOURS ON THE SAME RIM")
    print(z.sort("starved", descending=True))
    print(f"   total starved {st['qty'].sum():,.0f} tyres; "
          f"total PCR idle {(H*M.height - M['r_h'].sum()):,.0f} h")

    # per-machine idle, and starvation of GTs whose rim is that machine's
    print("\n-- per-machine idle hours")
    print(M.select("machine", "locked_rim", "s_per_tyre",
                   (H - pl.col("r_h")).round(1).alias("idle_h"))
          .sort("idle_h", descending=True))


if __name__ == "__main__":
    main(Path(sys.argv[1]), sys.argv[2],
         sys.argv[3] if len(sys.argv) > 3 else "PCR")
