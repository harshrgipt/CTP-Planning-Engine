"""DIAGNOSTIC -- measure build RUN fragmentation in an emitted schedule.

    python scripts/diag_runs.py runs/july_cmbc_v3

A build RUN is a maximal consecutive same-GT block on one machine, ordered by
start_ts. It is the object the plant sets up for and the object R9/B12's floor
is about; the engine currently emits only slices, so the run is measured here
rather than read from a column.

Read-only. Writes nothing. Prints the numbers the defect register claims so
they can be confirmed or refuted before any code changes.
"""
from __future__ import annotations

import sys
from pathlib import Path

import polars as pl

FLOOR = {"PCR": 150, "TBR": 70}
RUN_BAND_H = {"PCR": (6.0, 10.0), "TBR": (4.0, 7.0)}
CO_PER_MDAY = {"PCR": 2.46, "TBR": 3.51}


def runs_of(b: pl.DataFrame) -> pl.DataFrame:
    """Collapse slices into maximal consecutive same-GT blocks per machine.

    OPENING_STOCK is not a machine and must be excluded, or it reads as a run
    boundary and this count disagrees with L7's own -- two routes to one
    quantity, which is how the 449-tyre phantom survived a full release cycle.
    """
    b = b.filter(pl.col("machine") != "OPENING_STOCK").sort(
        ["plant", "machine", "start_ts"])
    # A new run starts when plant/machine changes, or the GT differs from the
    # previous slice on that same machine.
    b = b.with_columns(
        (
            (pl.col("gt_code") != pl.col("gt_code").shift(1).over(["plant", "machine"]))
            .fill_null(True)
        ).alias("_new")
    )
    b = b.with_columns(pl.col("_new").cum_sum().over(["plant", "machine"]).alias("run_id"))
    return (
        b.group_by(["plant", "machine", "run_id"])
        .agg(
            pl.col("gt_code").first(),
            pl.col("qty").sum().alias("run_qty"),
            pl.col("start_ts").min().alias("t0"),
            pl.col("end_ts").max().alias("t1"),
            pl.len().alias("n_slices"),
        )
        .with_columns(
            ((pl.col("t1") - pl.col("t0")).dt.total_seconds() / 3600.0).alias("run_h")
        )
        .sort(["plant", "machine", "t0"])
    )


def main() -> None:
    run = Path(sys.argv[1] if len(sys.argv) > 1 else "runs/july_cmbc_v3")
    b = pl.read_parquet(run / "build_schedule.parquet")
    r = runs_of(b)

    print(f"# {run}   slices={b.height:,}   runs={r.height:,}")
    for plant in ("PCR", "TBR"):
        rp = r.filter(pl.col("plant") == plant)
        bp = b.filter(pl.col("plant") == plant)
        if not rp.height:
            continue
        lo, hi = RUN_BAND_H[plant]
        floor = FLOOR[plant]
        below = rp.filter(pl.col("run_qty") < floor).height
        in_band = rp.filter((pl.col("run_h") >= lo) & (pl.col("run_h") <= hi)).height
        # machine-days actually used, for the changeover rate
        mdays = (
            bp.with_columns(pl.col("start_ts").dt.date().alias("d"))
            .select(["machine", "d"]).unique().height
        )
        # a changeover is a run boundary within a machine, i.e. runs - machines
        n_mach = bp.select("machine").unique().height
        chg = rp.height - n_mach
        per_gt = (
            bp.select(["gt_code", "machine"]).unique()
            .group_by("gt_code").len().rename({"len": "n_mach"})
        )
        print(f"\n== {plant} ==")
        print(f"  slices           {bp.height:,}   tyres {bp['qty'].sum():,.0f}")
        print(f"  RUNS             {rp.height:,}")
        print(f"  run qty          p50 {rp['run_qty'].median():.0f}  "
              f"min {rp['run_qty'].min():.0f}  max {rp['run_qty'].max():.0f}")
        print(f"  below floor {floor:<4}  {below:,} ({100*below/rp.height:.1f}%)")
        print(f"  run hours        p50 {rp['run_h'].median():.2f}  "
              f"in {lo}-{hi}h band {in_band:,} ({100*in_band/rp.height:.1f}%)")
        print(f"  machines/GT      p50 {per_gt['n_mach'].median():.0f}  "
              f"max {per_gt['n_mach'].max()}  "
              f"GTs on exactly 1 {per_gt.filter(pl.col('n_mach') == 1).height}"
              f" of {per_gt.height}")
        print(f"  changeovers      {chg:,} over {mdays:,} machine-days = "
              f"{chg/max(mdays,1):.2f}/machine-day  (plant {CO_PER_MDAY[plant]})")
        print(f"  slices per run   p50 {rp['n_slices'].median():.0f}")


if __name__ == "__main__":
    main()
