"""THE PLANT'S ALLOWABLE MACHINE LIST, ENFORCED AS A HARD CONSTRAINT.

    from planner.cmbc import allowable
    cm = allowable.restrict(cm)          # drop every non-allowable (GT, machine)

WHY THIS EXISTS -- a measured defect, found 2026-08-11
  `INPUT/derived/allowed_machine_matrix.parquet` is the plant's own statement of
  which building machine may run which GT. It was a frozen input that **nothing
  on the live path read**. PCR eligibility came instead from `cap_machine_<M>`,
  which L2 mines from 8 months of MES history and widens with an `INCH` basis:
  any machine whose rim window covers the GT is listed, at penalty 5000.

  A 5000 penalty is a PRICE, not a constraint. The greedy release pays it when
  no preferred machine has a slot. Measured on July 2026 PCR:

      build volume on GTs the matrix covers        382,573 tyres
      built on a machine NOT in the matrix          76,254 tyres  = 19.9 %
      across                                            21 GTs

  Example: GT 1503 NEO MSIL (SKU 1325215813079TUNE3). The matrix allows
  TBMPCR5/10/3/4; `cap_machine` listed ELEVEN machines, and 3,870 tyres landed
  on TBMPCR7, which the plant does not sanction for that GT.

  This is the same bug class as PARTITION section 1 in reverse -- there a mined
  median became a hard constraint; here a hard constraint stayed a soft price.

SEMANTICS -- the matrix is authoritative WHERE IT SPEAKS
  * (plant, gt) present in the matrix -> keep ONLY the listed machines.
  * (plant, gt) absent                -> keep the mined eligibility untouched.

  A GT the plant has not ruled on must not become unplannable. Verified for both
  shipped months: the matrix covers 48/48 and 73/73 PCR GTs and 56/56 and 37/37
  TBR GTs, so the fallback is currently unused -- but a new month will hit it.

COST -- state it, do not hide it
  PCR goes from ~11 eligible machines per GT to a median of 2. That is the point,
  and it necessarily costs fulfilment: volume that used to spill onto an
  INCH-eligible machine now has to wait for an allowable one or go unbuilt.
  Report the drop next to the gain, PCR and TBR separately.

  `PLANNER_STRICT_ALLOWABLE=0` restores the old soft-penalty behaviour for a
  deliberate A/B. It ships at 1.
"""
from __future__ import annotations

import os

import polars as pl

from planner import paths

_MATRIX = "allowed_machine_matrix.parquet"


def enabled() -> bool:
    return os.environ.get("PLANNER_STRICT_ALLOWABLE", "1") != "0"


def matrix() -> pl.DataFrame | None:
    f = paths.input_derived(_MATRIX)
    if not f.exists():
        return None
    return pl.read_parquet(f).select(["plant", "gt_code", "machine"]).unique()


def restrict(df: pl.DataFrame, *, label: str = "cap_machine",
             quiet: bool = False) -> pl.DataFrame:
    """Drop rows whose (plant, gt_code, machine) the plant does not allow.

    `df` must carry those three columns. Rows for a GT absent from the matrix
    pass through unchanged -- see the module docstring.
    """
    if not enabled():
        if not quiet:
            print(f"  [allowable] STRICT OFF -- {label} left at "
                  f"{df.height} rows (PLANNER_STRICT_ALLOWABLE=0)")
        return df
    am = matrix()
    if am is None or am.height == 0:
        print(f"  [allowable] !! {_MATRIX} absent -- {label} NOT restricted")
        return df
    if not {"plant", "gt_code", "machine"} <= set(df.columns):
        raise ValueError(f"{label} lacks plant/gt_code/machine")

    ruled = am.select(["plant", "gt_code"]).unique().with_columns(
        pl.lit(True).alias("_ruled"))
    keep = am.with_columns(pl.lit(True).alias("_ok"))
    out = (df.join(ruled, on=["plant", "gt_code"], how="left")
             .join(keep, on=["plant", "gt_code", "machine"], how="left")
             # keep when the plant has not ruled on this GT, or has and allows it
             .filter(pl.col("_ruled").is_null() | pl.col("_ok").is_not_null())
             .drop(["_ruled", "_ok"]))
    if not quiet:
        dropped = df.height - out.height
        n_un = (df.height and
                df.join(ruled, on=["plant", "gt_code"], how="left")
                  .filter(pl.col("_ruled").is_null())["gt_code"].n_unique())
        print(f"  [allowable] {label}: {df.height} -> {out.height} rows "
              f"({dropped} dropped by the plant's allowable list"
              + (f"; {n_un} GTs not ruled on, kept as mined)" if n_un else ")"))
        for p, g in out.group_by("plant"):
            per = g.group_by("gt_code").agg(pl.len().alias("n"))
            print(f"              {p[0]}: {per.height} GTs, machines/GT "
                  f"p50={per['n'].median():.0f} min={per['n'].min()} "
                  f"max={per['n'].max()}")
    return out


# PRESS PLATEN RIM WINDOW -- DELIBERATELY ABSENT.
# `press_platen_master.rim_lo/rim_hi` is not a usable constraint and no code here
# reads it. It disagrees with the plant's own `press_class_pcr` (45 in recorded
# 14-20 where the plant states 12-16; the 46 in class has no plant entry and was
# defaulted), and every (GT, press) pair it rejected is explicitly permitted by
# `allowed_press_matrix` -- 57 of 61 on a `direct` basis. Press eligibility is
# therefore taken from allowed_press_matrix alone, which already measures 0
# violations on both shipped months. Do not re-add a platen filter without a
# corrected master from the plant.


# ==========================================================================
# INCH / RIM LOCK  -- machines run their own rim
# ==========================================================================
# `machine_rim_lock.parquet` tags each machine with the rim it ran in 8 months
# of MES and a tier: hard (single rim, ~100 % purity), primary (dominant rim),
# flex (the plant's designated mixer -- PCR TBMPCR2, 66.4 % purity, 4 rims).
#
# L7 consumed it only as an ORDERING preference (`lock_of` supplies candidate
# machines rim-first) and `PLANNER_HARD_LOCK` only disables an off-lock spill
# pass. Measured on the allowable-compliant runs, off-lock volume was:
#
#     Jul PCR  hard 13.7 %   primary 26.3 %   flex 55.9 %   total 22.2 %
#     Aug PCR  hard  0.5 %   primary 12.6 %   flex 75.5 %   total 11.0 %
#     Aug TBR  hard  0.0 %   primary  100 %   flex  100 %   total 31.0 %
#
# A `hard`-tier machine carrying 13.7 % foreign-rim volume means the lock was
# not a constraint. This makes it one for hard and primary tiers; the flex
# machine keeps its whole eligible set, because mixing rims is its documented
# purpose and PARTITION section 12 sizes a deliberate spill through it.
#
# SHIPS AT 0 -- OFF -- AND THE REASON IS ARITHMETIC, NOT PREFERENCE.
# L4b (max-flow feasibility) prices it exactly, August 2026 PCR:
#
#     rimlock hard  need 7,128 h · max-flow 6,630 h -> SHORT 499 h (7.0 %)
#     rimlock off   need 7,131 h · max-flow 7,068 h -> SHORT  63 h (0.9 %)
#
# The lock is responsible for 436 of the 499 infeasible hours -- 87 % of it. With
# it hard, 8 PCR GTs get ZERO eligible hours and 30 GTs are single-machine, so
# the month cannot be built no matter how well it is sequenced; the measured cost
# was 14 points of PCR fulfilment.
#
# AND IT IS NOT THE PLANT'S RULE. `machine_rim_lock` is MINED from 8 months of
# MES -- it records which rim a machine HAPPENED to run, at purities as low as
# 66.4 % (TBMPCR2) and 89.3 % (TBMPCR5), i.e. the plant itself mixes rims on
# those machines. Wiring a mined statistic in as a hard constraint is the exact
# defect PARTITION section 1 records twice, at a combined cost of 13.4 points.
#
# PHYSICAL INCH CAPABILITY IS A DIFFERENT THING AND IS ALREADY HARD: it lives in
# `pcr_inch_eligibility` and reaches the scheduler through cap_machine's INCH
# basis, intersected with the plant's allowable matrix. Do not confuse the two --
# "this machine CAN hold this rim" is physics; "this machine USUALLY runs this
# rim" is habit.
def restrict_rimlock(df: pl.DataFrame, *, label: str = "cap_machine",
                     quiet: bool = False) -> pl.DataFrame:
    """Keep (gt, machine) only where the machine's locked rim matches the GT's.

    Untouched: GTs with no rim, machines with no lock row, and flex-tier
    machines. `PLANNER_STRICT_RIMLOCK=0` restores ordering-only behaviour.
    """
    if os.environ.get("PLANNER_STRICT_RIMLOCK", "0") == "0":
        if not quiet:
            print(f"  [rimlock] STRICT OFF -- {label} left at {df.height} rows")
        return df
    f = paths.input_derived("machine_rim_lock.parquet")
    if not f.exists():
        return df
    lk = pl.read_parquet(f).select(["plant", "machine", "locked_rim", "tier"])
    sz = (pl.read_parquet(paths.input_derived("gt_size.parquet"))
          .select(["plant", "gt_code", "rim"]).unique(subset=["plant", "gt_code"]))
    out = (df.join(sz, on=["plant", "gt_code"], how="left")
             .join(lk, on=["plant", "machine"], how="left")
             .filter(pl.col("rim").is_null()
                     | pl.col("locked_rim").is_null()
                     | (pl.col("tier") == "flex")
                     | (pl.col("rim") == pl.col("locked_rim")))
             .drop(["rim", "locked_rim", "tier"]))
    if not quiet:
        print(f"  [rimlock] {label}: {df.height} -> {out.height} rows "
              f"({df.height - out.height} dropped by the machine rim lock)")
    return out
