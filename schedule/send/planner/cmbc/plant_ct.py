"""PLANT CYCLE TIMES -- the single lookup every layer uses.

The plant supplied authoritative cycle-time workbooks (`cycletime/`). They
REPLACE the mined cadence, and they arrive at a finer grain than what they
replace, so this module exists to keep exactly one implementation of the
granularity change instead of five copies drifting apart (PARTITION §1g).

    build   plant x GT x MACHINE MAKE   (CONTI / BJ on PCR; one make on TBR)
    cure    plant x GT                  (rolled up from plant x SKU)

Ingest is `scripts/ingest_plant_cycle_times.py`; it writes
`warehouse/derived/plant_ct_build.parquet` and `plant_ct_cure_gt.parquet`.
Set `PLANNER_PLANT_CT=0` to fall back to the mined tables for an A/B.

-------------------------------------------------------------------------------
THE PRESS RUNS TWO TYRES PER CYCLE. A CURE TIME IS PER CYCLE, NOT PER TYRE.
-------------------------------------------------------------------------------
Measured on July 2026 `v_curing`, clustering a press's tyre events into loads at
a 120 s gap: **92.5 % of PCR loads and 92.3 % of TBR loads are exactly 2 tyres**
(mean load 1.96 on both). `MouldCountLH`/`MouldCountRH` and the `HM01#HM02`
mould-pair labels say the same. So

    tyres per press-hour = 2 x 60 / press_cycle_min

and anything that forgets the 2 halves press capacity.

-------------------------------------------------------------------------------
THE LOAD/UNLOAD ADDER IS MEASURED, NOT ASSUMED
-------------------------------------------------------------------------------
Per GT, July 2026, observed inter-load gap minus the plant's stated cure time,
over 46 PCR GTs spanning 10.0-20.0 min and 49 TBR GTs spanning 44-57 min:

    overhead min   p10    p25    p50    p75   mean
    PCR           2.28   2.52   2.85   3.38   2.93
    TBR           6.50   7.00   8.08   9.70   8.30

A near-constant offset across a 2x range of cure times: the plant file is
CONFIRMED by the MES at per-GT grain, and the residual is press open/load/close.

-------------------------------------------------------------------------------
AVAILABILITY **MUST** BE APPLIED HERE, AND MUST NOT HAVE BEEN BEFORE
-------------------------------------------------------------------------------
`l3_ceiling.py` warns "DO NOT APPLY AVAILABILITY ON TOP OF A DERIVED RATE" --
correctly, because `cavities = observed tyres-per-day / theoretical cycles` is a
rate mined FROM OUTPUT and already contains every stoppage. The plant cure time
is the opposite: a NAMEPLATE. Haircutting it is therefore not double-counting,
it is the first count.

    plant            nameplate/press-day   x avail   observed July mean   p50
    PCR  0.8897           169.1             150.4          150.5         158
    TBR  0.8282            48.3              40.0           40.7          44

Both land within 1.5 % of what the plant actually did. Without the haircut we
would plan 12 % (PCR) / 20 % (TBR) above anything the plant has ever
demonstrated.

Net effect on aggregate press capacity vs the model it replaces
(`cav_p x 3600 / cyc_p`): PCR 152.8 -> 150.4/press-day (-1.6 %), TBR 42.9 ->
40.0 (-6.8 %). **The value of the change is not the aggregate -- it is the
dispersion.** The old model charged every PCR GT the same 6.37 tyres/press-h;
the new one runs 8.0 for an 11.4-min GT and 5.2 for a 17.5-min GT, a 1.5x
spread that simply did not exist before.
"""
from __future__ import annotations

import os
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parent.parent.parent
D = ROOT / "warehouse" / "derived"

CAVITIES = 2.0                                   # measured, both plants
# SENSITIVITY ONLY, ships at 0.0. Measured 2026-08-14 at +2.5 min:
#   Jul PCR 95.9 -> 86.3 %   Aug PCR 91.1 -> 81.2 %   (BUILT -20k / -38k)
#   Jul TBR 95.8 -> 93.0 %   Aug TBR 89.1 -> 87.1 %
# PCR loses ~10 pt because its cycle is ~9.5 min (+2.5 = +26 %); TBR ~36 min
# (+7 %). Cure cycle time is the most sensitive input in the model.
CURE_CT_ADD_MIN = float(os.environ.get("PLANNER_CURE_CT_ADD_MIN", "0"))
# Load/unload minutes added to the cure time to give the PRESS CYCLE.
# The mined values are PCR 2.9 / TBR 8.3. PLANNER_LOAD_UNLOAD_MIN overrides BOTH
# plants with a single figure -- the plant stated 2.5 min on 2026-08-14, which is
# a small cut on PCR and a large one on TBR (8.3 -> 2.5). It is an override of a
# MEASURED value by plant instruction, so state it whenever the numbers are used.
# PLANT INSTRUCTION 2026-08-19: both plants ship at the plant's stated 2.5 min.
# The MINED values were PCR 2.9 / TBR 8.3, derived as observed inter-load gap
# minus the plant's stated cure time. On TBR that mined figure is 8.3 min against
# a ~36 min cycle -- ~23 % of press capacity priced out as handling -- and the
# plant stated 2.5 on 2026-08-14. This replaces a measured value with a plant
# ruling, deliberately; `PLANNER_LOAD_UNLOAD_MIN` still overrides both plants,
# and the mined pair is recorded here so the substitution is never invisible.
# PLANT INSTRUCTION 2026-08-19, SECOND REVISION: PCR 2.5, TBR **5.75**.
# The single 2.5 figure was applied to both plants; the plant has since given a
# separate TBR number. TBR now sits between the mined 8.3 and the PCR 2.5, which
# is what a longer TBR handling cycle would predict. The three values for TBR,
# so the substitution stays visible: mined 8.3 · first ruling 2.5 · now 5.75.
#
# `PLANNER_LOAD_UNLOAD_MIN` still overrides BOTH plants with one figure, so it
# can no longer express the shipped default -- use `PLANNER_LOAD_UNLOAD_PCR` /
# `PLANNER_LOAD_UNLOAD_TBR` to move one plant alone.
_LU = os.environ.get("PLANNER_LOAD_UNLOAD_MIN", "")
LOAD_UNLOAD_MIN = ({"PCR": float(_LU), "TBR": float(_LU)} if _LU
                   else {"PCR": float(os.environ.get("PLANNER_LOAD_UNLOAD_PCR", "2.5")),
                         "TBR": float(os.environ.get("PLANNER_LOAD_UNLOAD_TBR", "5.75"))})

# -------------------------------------------------------------------------
# PRESS AVAILABILITY -- ONE RESOLUTION, TWO READERS. Ships 1.0 / 1.0.
# -------------------------------------------------------------------------
# THE PER-SKU CURE TIME IS ALREADY HERE. `plant_ct_cure_gt.parquet` is built by
# scripts/ingest_plant_cycle_times.py from the plant's OWN workbooks
# (`INPUT/cycletime/PCR Curing cycle time 2.xlsx` sheet 'PCR CYCLE TIME';
# `TBR CURING CYCLE TIME.xlsx` sheet 'Bladder list'), bridged to GT codes through
# scripts/gt_namespace.py, and it reproduces the workbooks exactly:
#   PCR  min 10.0  p25 12.5  p50 13.1  p75 15.0  max 22.0  min/cycle  (230 GTs)
#   TBR  min 42.0  p25 49.0  p50 52.0  p75 54.0  max 60.0            (131 GTs)
# `press_rate` below is therefore ALREADY 2 tyres / (per-GT cure + load/unload),
# per GT, with the plant's own dispersion. `cycle_time_curing.parquet` -- the
# press-keyed 172-row table with `slots = 4` and `eff_ct_min` p50 35.8 -- is read
# ONLY by diagnostics, exporters and the RETIRED `_retired/l1_validate.py`. It
# reaches no live cure rate. Do not "fix" it expecting the plan to move.
#
# WHAT IS ACTUALLY MISSING IS THE HAIRCUT. Measured on August 2026, volume-
# weighted over `net_requirement.cure_requirement` (harmonic, because the
# quantity conserved is press-HOURS):
#
#            nameplate t/press-h   plan realised   plant realised p50
#   PCR            7.218               6.989            6.50   (156/press-day)
#   TBR            2.103               2.033            1.83   ( 44/press-day)
#
# so the plan runs 7.5 % (PCR) / 11.1 % (TBR) above what the plant demonstrably
# achieves, and the difference is availability -- breakdowns, no-load, changeover
# the model does not carry. The docstring above says availability MUST be applied
# here and MUST NOT have been applied before it; it then shipped OFF on
# 2026-08-19 by plant instruction, leaving a nameplate plan.
#
# THE FIGURE IS A SWEEP HANDLE, NOT A MINED CONSTANT. Wiring one quantile in as
# a hard number is this project's signature defect (PARTITION §1 -- tau* and
# min_lot cost 13.4 pt between them), so nothing is defaulted: 1.0 ships, and the
# arms below name the factor they used and where it came from.
#
# `PLANNER_PRESS_AVAIL` still overrides BOTH plants with one figure, so it can no
# longer express a per-plant setting -- use `PLANNER_PRESS_AVAIL_PCR` /
# `PLANNER_PRESS_AVAIL_TBR`. This dict is the ONLY place the value is resolved;
# `l5_cure_master` and `l45_lotsize` both read it, because the two layers sizing
# and seating against different press rates is the duplicated-constant defect
# PARTITION §1g records (and §8: "add a cap in config.py or nowhere" -- this is
# not a cap, it is a rate input, and its single home is this module).
# =========================================================================
# SHIPS 1.0 / 1.0 (OFF). MEASURED 2026-08-21, AUGUST 2026, BOTH PLANTS.
# =========================================================================
# Arms fresh via scripts/run_arm.py, gated FRESH by check_arm_fresh.py.
# Baseline `MC_base` = the shipped SHIP2_aug configuration, reproduced to the
# tyre. Partition stamped 2026-08 sha1 809beda91344; the plant-day closure pinned
# with PLANNER_HOLIDAYS=2026-08-15 on every arm.
#
#   PCR   demand 429,146                      cure   max    days over
#   arm      avail      BUILT    dBUILT   ful%  rate   day    13,854   L11
#   MC_base  1.0000   409,511        +0  92.59  6.989 14,465    16    32/48
#   MC_b96   0.9600   402,815    -6,696  91.07  6.707 14,423    17    33/48
#   MC_b93   0.9300   406,023    -3,488  90.83  6.505 14,202    10    33/48
#   MC_bmt   0.8897   403,891    -5,620  88.19  6.240 13,558     0    32/48
#
#   TBR   demand 99,019                       cure   max    days over
#   MC_base  1.0000    98,003        +0  97.89  2.033  3,477     0    32/48
#   MC_b96   0.9600    96,935    -1,068  96.99  1.952  3,440     0    33/48
#   MC_b93   0.9000    96,053    -1,950  95.62  1.832  3,430     0    33/48
#   MC_bmt   0.8282    95,987    -2,016  94.02  1.688  3,270     0    32/48
#
# WHERE THE FACTORS COME FROM, so the substitution is never invisible:
#   0.930 / 0.900  chosen so the PLAN's realised rate lands on the plant's own
#                  realised per-press-day p50 (PCR 156/day = 6.50 t/press-h,
#                  TBR 44/day = 1.83). It does: 6.505 and 1.832.
#   0.8897 / 0.8282  the mined MTBF/MTTR haircut from warehouse/params (PCR
#                  mtbf 106.8 h / mttr 13.2 h over 4,267 down events) that this
#                  engine shipped until the plant switched it off on 2026-08-19.
#   0.96           a light arm, to show the curve is not a step.
# NONE of them is defaulted. A quantile wired in as a constant is PARTITION §1
# (tau*, min_lot, 13.4 pt); the arm names the number and the plant chooses.
#
# THE TEST THAT MATTERED, AND ITS ANSWER
#   "Does the plan's best day fall inside the plant's demonstrated range?"
#   TBR: ALREADY YES on every arm -- max 3,477 against a 3,599 record, 0 days
#        over. The TBR rate being 11 % high never showed up as an impossible day
#        because TBR is not press-bound. Do not sell a TBR haircut as fixing a
#        daily-rate problem TBR does not have.
#   PCR: NO, on every arm except MC_bmt. Base runs a FLAT 14,465/day plateau on
#        10 days -- that is 86 presses x 24 h x 6.989 -- and exceeds the plant's
#        8-month record on 16 of the 30 open days. Calibrating the RATE exactly
#        onto the plant's p50 (MC_b93) still leaves 10 days over, because the
#        plant does not also keep every press seated 24 h. Only 0.8897 clears it.
#
# ⚠ AND THE ARM THAT CLEARS IT BREACHES G8. MC_bmt's PCR GT-inventory daily-mean
#   max is **5,129 against the 4,800 rail** (base 4,561); the time-weighted mean
#   rises 3,998 -> 4,167 and the carry-out tail 12,620 -> 26,816. Slower presses
#   drain the buffer more slowly, so the cure-rate haircut is paid for in green
#   tyres standing. The only arm that makes the daily curve physically credible
#   makes the inventory rail illegal. That trade is in one sentence on purpose.
#
# ⚠ IT ALSO MAKES THE R3 PROBLEM WORSE, NOT BETTER. GTs whose peak concurrency
#   exceeds the plant's observed_max: PCR 2 -> 5 (12,435 -> 97,618 tyres),
#   TBR 9 -> 10 at MC_b93. A slower press needs more presses at once for the same
#   demand. The two fixes pull in opposite directions on that metric even though
#   their volume effects are additive (see the interaction table in r3_cap.py).
_PA = os.environ.get("PLANNER_PRESS_AVAIL", "")
PRESS_AVAIL = ({"PCR": float(_PA), "TBR": float(_PA)} if _PA
               else {"PCR": float(os.environ.get("PLANNER_PRESS_AVAIL_PCR", "1.0")),
                     "TBR": float(os.environ.get("PLANNER_PRESS_AVAIL_TBR", "1.0"))})

# PCR machine make -- READ FROM THE ENGINE'S OWN CAPABILITY LAYER, not restated.
# `cap_changeover.parquet` (written by l2_capability.py) already carries
# `machine_type`: BJ for TBMPCR1..5, CONTI for TBMPCR6..11, SAV/MESNAC for TBR.
# Restating it here as a literal is exactly the duplicated-constant defect that
# PARTITION §1g records twice, so the literals below are only the fallback for a
# checkout where L2 has not run, and they must agree with the file.
#
# Four independent sources agree on the boundary:
#   1. cap_changeover.parquet `machine_type`      (l2_capability.py:430)
#   2. Master_Building_ChangeoverTime_pcr.csv     28/60 vs 22/42, split 5 | 6
#   3. PROJECT_STATE.md:176, CMBC_BUILD_LOG.md:145-146  name BJ and CONTI
#   4. cap_changeover `inch_lo/inch_hi`           BJ 12-20/12-16, CONTI 13-18
# and the 34NN -> TBMPCR<NN>Stage2 identity underneath is measured at 98-100 %.
# TBR's nine machines are SAV-1..3 + MESNAC-1..6 but share one 10/24 changeover
# and one CT per GT in the plant's file, so they collapse to a single make here.
_FALLBACK_MAKE: dict[str, str] = (
    {f"TBMPCR{i}Stage2": "BJ" for i in range(1, 6)}
    | {f"TBMPCR{i}Stage2": "CONTI" for i in range(6, 12)}
    | {f"TBMTBR{i}Stage2": "TBR" for i in range(1, 10)})


def _load_make() -> dict[str, str]:
    f = D / "cap_changeover.parquet"
    if not f.exists():
        return dict(_FALLBACK_MAKE)
    try:
        df = pl.read_parquet(f).select(["plant", "machine", "machine_type"])
    except Exception:                                    # noqa: BLE001
        return dict(_FALLBACK_MAKE)
    out = {}
    for r in df.iter_rows(named=True):
        # TBR's SAV/MESNAC distinction is real for changeover and absent from the
        # cycle-time file, so both fold to one CT namespace.
        out[r["machine"]] = ("TBR" if r["plant"] == "TBR"
                             else str(r["machine_type"]))
    return out or dict(_FALLBACK_MAKE)


MAKE_OF: dict[str, str] = _load_make()


def enabled() -> bool:
    return os.environ.get("PLANNER_PLANT_CT", "1") != "0"


class PlantCT:
    """Build cadence (s/tyre) and press rate (tyres/press-h), per GT.

    Every accessor returns None when the plant file has nothing for that GT, so
    the caller keeps its own fallback and the coverage gap stays visible instead
    of being papered over with a plant median inside this class.
    """

    def __init__(self, avail: dict[str, float] | None = None) -> None:
        self.ok = False
        # Default to the module-level resolution, NOT to a literal 1.0. `get()`
        # is a process-wide cache and its first caller wins, so a layer that
        # calls `plant_ct.get()` with no argument (l4b, and l7 for build cadence)
        # must not silently get a different press rate from the layer that
        # called it with one.
        self.avail = avail or dict(PRESS_AVAIL)
        self._b: dict[tuple[str, str, str], float] = {}
        self._b_gt: dict[tuple[str, str], float] = {}   # make-agnostic mean
        self._c: dict[tuple[str, str], float] = {}
        fb, fc = D / "plant_ct_build.parquet", D / "plant_ct_cure_gt.parquet"
        if not (enabled() and fb.exists() and fc.exists()):
            return
        for r in pl.read_parquet(fb).iter_rows(named=True):
            self._b[(r["plant"], r["gt_code"], r["make"])] = float(r["ct_s"])
        agg: dict[tuple[str, str], list[float]] = {}
        for (p, g, _mk), v in self._b.items():
            agg.setdefault((p, g), []).append(v)
        self._b_gt = {k: sum(v) / len(v) for k, v in agg.items()}
        for r in pl.read_parquet(fc).iter_rows(named=True):
            self._c[(r["plant"], r["gt_code"])] = float(r["cure_min"])
        self.ok = bool(self._b) and bool(self._c)

    # ---- build -----------------------------------------------------------
    def build_ct_s(self, plant: str, gt: str, machine: str | None) -> float | None:
        """Seconds per tyre for this GT on this machine's make."""
        if not self.ok:
            return None
        if machine is not None:
            mk = MAKE_OF.get(machine)
            if mk is not None:
                v = self._b.get((plant, gt, mk))
                if v is not None:
                    return v
        return self._b_gt.get((plant, gt))

    # ---- cure ------------------------------------------------------------
    def cure_min(self, plant: str, gt: str) -> float | None:
        return self._c.get((plant, gt)) if self.ok else None

    def press_cycle_min(self, plant: str, gt: str) -> float | None:
        """Cure minutes + load/unload, plus an optional SENSITIVITY offset.

        PLANNER_CURE_CT_ADD_MIN adds a flat number of minutes to EVERY press
        cycle. It exists to answer "what if the mined cure times are optimistic
        by N minutes?" -- it makes curing SLOWER, so it can only reduce output.
        It is a stress test, never a planning setting: 0.0 is the shipped value
        and anything else must be stated when the numbers are quoted.
        """
        c = self.cure_min(plant, gt)
        if c is None:
            return None
        return c + LOAD_UNLOAD_MIN.get(plant, 0.0) + CURE_CT_ADD_MIN

    def press_rate(self, plant: str, gt: str) -> float | None:
        """Tyres per press-HOUR, nameplate x availability. See docstring."""
        m = self.press_cycle_min(plant, gt)
        if m is None or m <= 0:
            return None
        return CAVITIES * 60.0 / m * self.avail.get(plant, 1.0)

    # ---- reporting -------------------------------------------------------
    def summary(self) -> str:
        if not self.ok:
            return "plant CT: OFF (PLANNER_PLANT_CT=0 or files absent)"
        out = ["plant CT: ON  (build GTxmake, cure GT; cavities 2, "
               f"load/unload PCR {LOAD_UNLOAD_MIN['PCR']} TBR "
               f"{LOAD_UNLOAD_MIN['TBR']} min)"]
        for p in ("PCR", "TBR"):
            bs = [v for (q, _g, _m), v in self._b.items() if q == p]
            cs = [v for (q, _g), v in self._c.items() if q == p]
            if bs and cs:
                bs, cs = sorted(bs), sorted(cs)
                out.append(
                    f"    {p}  build {len(bs)} rows  {bs[0]:.0f}-{bs[-1]:.0f} s "
                    f"(p50 {bs[len(bs)//2]:.0f})   "
                    f"cure {len(cs)} GTs  {cs[0]:.0f}-{cs[-1]:.0f} min "
                    f"(p50 {cs[len(cs)//2]:.0f})  avail {self.avail.get(p, 1.0):.4f}")
        return "\n".join(out)


_CACHE: PlantCT | None = None


def get(avail: dict[str, float] | None = None) -> PlantCT:
    global _CACHE
    if _CACHE is None:
        _CACHE = PlantCT(avail)
    return _CACHE
