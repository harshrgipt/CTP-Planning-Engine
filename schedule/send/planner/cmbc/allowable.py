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

THE MATRIX CONTRADICTS THE PLANT'S OWN MES. MEASURED 2026-08-13.
  For July's six biggest PCR shortfalls, MES shows the plant built that GT on
  5-11 machines while this matrix permits 1-2:

      GT 2258 RAN HPE     plant used  8 machines · matrix allows 1 (M2)
      GT 1513 XPC1 MSIL   plant used 11 machines · matrix allows 2
      GT 1402 XPC TATA    plant used  5 machines · matrix allows 1 (M4)
      GT 2568 HT2         plant used  8 machines · matrix allows 1 (M9)

  Across every short GT: 99 % of July PCR's shortfall (12,154 of 12,295 tyres)
  and 94 % of August's (15,401 of 16,301) sits on GTs where MES shows MORE
  machines than the matrix allows -- MES p50 11 against matrix p50 2.

  The plant reaches 100 % in July because it is not operating under this
  constraint. Cost of enforcing it, measured on BUILT, both months, both plants:

      Jul PCR  +4,216 BUILT (96.2 -> 97.1 %)     Jul TBR    +81 (95.7 -> 95.8 %)
      Aug PCR  +1,787 BUILT (91.4 -> 91.7 %)     Aug TBR +4,703 (89.4 -> 94.0 %)

  ~10,800 tyres over the two months, 0 L11 invariants lost, POSITIVE ON ALL
  FOUR PLANT-MONTHS -- it passes the two-month gate outright. August TBR's
  89.4 -> 94.0 is the v9 regression, restored.

  IT STILL SHIPS HARD, BY INSTRUCTION, AND THAT IS THE RIGHT DEFAULT UNTIL THE
  PLANT RULES. Two readings and only the plant can choose:
    (a) the file is a PREFERENCE / home-machine list and the floor floats work
        across machines when it needs to -- then this should be soft ordering;
    (b) the file is authoritative and current, in which case the plant's own
        July production VIOLATED it, and our plan is more compliant than what
        the plant actually ran.
  Do not flip this on the measurement alone. It is a plant rule, not a knob.
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


# ==========================================================================
# RIM SET -- the JK-plant formulation of the inch lock
# ==========================================================================
# `machine_rim_lock` gives each machine ONE rim. Enforced hard it cost 14 pt of
# PCR fulfilment and caused 87 % of August's infeasible hours, because 8 GTs got
# zero eligible machines (see the note above).
#
# The JK B2C scheduler (`bc (1).md` section 4.5) uses a different shape and runs
# it HARD in production: a per-machine SET of the rims that machine actually ran,
# admitted at >= 2 % of its volume and capped at 3. On their plant that yields 27
# single-rim (FIXED) and 12 multi-rim (FLEXIBLE) machines, and it nets positive
# (June +7.7k / July +25.3k, August -6k). Their rule for the boundary is worth
# copying verbatim: "historically-evidenced +3 jumps are legal; an inch never run
# is not" -- evidence, not a band.
#
# Derived here from `machine_gt_share.parquet` (12-month share per GT x machine)
# crossed with `gt_size`. On our plant it gives PCR 6 FIXED / 5 FLEXIBLE and
# TBR 7 FIXED / 2 FLEXIBLE.
#
# This is strictly WIDER than `machine_rim_lock` -- a machine keeps every rim it
# genuinely ran, not just its modal one -- so it is a different experiment, not a
# retry of the one that failed. PLANNER_STRICT_RIMSET=1 to enable.
_RIMSET_MIN_SHARE = float(os.environ.get("PLANNER_RIMSET_MIN_SHARE", "0.02"))
_RIMSET_MAX = int(os.environ.get("PLANNER_RIMSET_MAX", "3"))


def rim_sets() -> dict[tuple, set]:
    """(plant, machine) -> the set of rims that machine genuinely ran."""
    f = paths.input_derived("machine_gt_share.parquet")
    if not f.exists():
        return {}
    sh = pl.read_parquet(f)
    sz = (pl.read_parquet(paths.input_derived("gt_size.parquet"))
          .select(["plant", "gt_code", "rim"]).unique(subset=["plant", "gt_code"]))
    j = sh.join(sz, on=["plant", "gt_code"], how="left").drop_nulls("rim")
    m = (j.group_by(["plant", "machine", "rim"])
           .agg(pl.col("share_pct").sum().alias("w"))
           .with_columns((pl.col("w") / pl.col("w").sum()
                          .over(["plant", "machine"])).alias("frac"))
           .filter(pl.col("frac") >= _RIMSET_MIN_SHARE)
           .sort(["plant", "machine", "frac"], descending=[False, False, True]))
    out: dict[tuple, set] = {}
    for r in m.iter_rows(named=True):
        k = (r["plant"], r["machine"])
        s = out.setdefault(k, set())
        if len(s) < _RIMSET_MAX:
            s.add(r["rim"])
    return out


def restrict_rimset(df: pl.DataFrame, *, label: str = "cap_machine",
                    quiet: bool = False) -> pl.DataFrame:
    """Keep (gt, machine) only where the GT's rim is in the machine's rim SET.

    Untouched: GTs with no rim, and machines with no share history.
    """
    if os.environ.get("PLANNER_STRICT_RIMSET", "0") == "0":
        return df
    rs = rim_sets()
    if not rs:
        return df
    sz = (pl.read_parquet(paths.input_derived("gt_size.parquet"))
          .select(["plant", "gt_code", "rim"]).unique(subset=["plant", "gt_code"]))
    j = df.join(sz, on=["plant", "gt_code"], how="left")
    keep = [
        (r["rim"] is None) or ((r["plant"], r["machine"]) not in rs)
        or (r["rim"] in rs[(r["plant"], r["machine"])])
        for r in j.iter_rows(named=True)
    ]
    out = j.filter(pl.Series(keep)).drop("rim")
    if not quiet:
        print(f"  [rimset] {label}: {df.height} -> {out.height} rows "
              f"({df.height - out.height} dropped by the machine rim SET; "
              f"{sum(1 for v in rs.values() if len(v) == 1)} FIXED / "
              f"{sum(1 for v in rs.values() if len(v) > 1)} FLEXIBLE machines)")
    return out
