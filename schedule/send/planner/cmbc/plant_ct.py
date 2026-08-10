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
LOAD_UNLOAD_MIN = {"PCR": 2.9, "TBR": 8.3}       # measured, see docstring

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
        self.avail = avail or {"PCR": 1.0, "TBR": 1.0}
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
        c = self.cure_min(plant, gt)
        return None if c is None else c + LOAD_UNLOAD_MIN.get(plant, 0.0)

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
