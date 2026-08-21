"""R3 EFFECTIVE MOULD CONCURRENCY -- ONE derivation, every reader.

WHAT THIS DOES / WHY IT EXISTS -- a measured defect, found 2026-08-21
  R3 is "concurrent presses running a GT <= its active mould count". The cap is
  read from `cap_mould_<M>.parquet`, whose `max_concurrent_presses` equals
  `moulds` on **100 % of its 110 rows** (verified on 2026-08). That is only
  correct if a press holds ONE mould.

  THE PLANT RULING IS THAT IT HOLDS TWO -- LH + RH, one cycle, two tyres, both
  plants. So `moulds` is a count of mould HALVES and the number of presses a GT
  can occupy at once is about `moulds / 2`.

  MES proof, independent of the ruling: press `wcID=185`, 10 July, 170 curing
  rows -- 1 distinct `MouldCodeLH`, 1 distinct `MouldCodeRH`, and LH != RH on all
  170. Plant-wide moulds-per-press p50 = 1.71. And `cap_mould`'s own
  `observed_max` (distinct presses the plant really ran a GT on in a day, over 8
  months) has p50 EXACTLY moulds/2 on both plants: PCR moulds 4 / observed 2,
  TBR 6 / 3.

  What the uncorrected cap let the August plan do, measured on `runs/MC_base`
  (the shipped SHIP2_aug configuration, reproduced fresh):

      plant   GTs over the plant's own observed_max   tyres on them
      TBR                    9 of 37                  52,325  (52.3 %)
      PCR                    2 of 73                  12,435

  `10.00 R 20 JUH5` is seated on 17 presses at once. The plant has never run it
  above 16 in 8 months, and it has 26 moulds -- 13 press-pairs.

WHY IT IS A MODULE AND NOT FOUR `min()` CALLS
  The R3 cap is read in FOUR places: `l45_lotsize` (the R5 campaign-length
  ceiling `moulds x rate x 72 h`), `l5_cure_master` (placement, the split/repair
  pass, and its own self-check) and `l11_validate_plan` (the invariant that
  GRADES it). PARTITION §1g and do-not #13 record two live instances of the same
  limit written twice with two different values, one of which made an invariant
  grade itself against the wrong constant for a whole session. So the divisor is
  applied exactly once, here, and l11 grades the plan against the same number l5
  planned to.

SHIPS OFF. `PLANNER_R3_DIV` defaults to 1.0, which returns `moulds` unchanged --
byte-identical to the engine before this module existed: verified on 2026-08-21,
`MC_null` vs `MC_base` equal on all ten arm parquets (cure_campaigns,
build_schedule, gt_events, cure_campaigns_reconciled, build_starved,
cure_by_shift, build_by_shift, mould_changes, carry_out, l11_invariants) and the
shared `l45_lots_2026-08.parquet` unchanged.

===============================================================================
SHIPS OFF. MEASURED 2026-08-21, AUGUST 2026, BOTH PLANTS. IT COSTS 8 POINTS.
===============================================================================
Arms fresh via `scripts/run_arm.py`, all gated FRESH by `check_arm_fresh.py`.
Partition `INPUT/derived/gt_machine_partition.parquet` stamped 2026-08, sha1
809beda91344. Baseline `MC_base` = the shipped SHIP2_aug configuration
(`PLANNER_L7_CLOSING_BUFFER=1`), reproduced to the tyre. The August plant-day
closure was pinned with `PLANNER_HOLIDAYS=2026-08-15` on every arm, because
`masters/holidays_2026-08.json` had been removed from the tree and an unpinned
calendar would have moved the baseline underneath the experiment.

  PCR   demand 429,146
  arm                        BUILT    dBUILT   in-month   ful%   R5      L11
  MC_base    div 1.0       409,511        +0    397,326  92.59  63.3   32/48
  MC_a15     div 1.5       381,726   -27,785    368,693  85.91  63.7   31/48
  MC_a20     div 2.0       377,450   -32,061    363,280  84.65  60.3   32/48
  MC_aobs    div 2.0 +obs  376,652   -32,859    363,141  84.62  71.7   32/48
  MC_aobs1   obs only      390,237   -19,274    377,995  88.08  65.9   29/48

  TBR   demand 99,019
  MC_base    div 1.0        98,003        +0     96,932  97.89  71.7   32/48
  MC_a15     div 1.5        93,847    -4,156     91,819  92.73  68.8   31/48
  MC_a20     div 2.0        94,640    -3,363     92,076  92.99  71.1   32/48
  MC_aobs    div 2.0 +obs   95,116    -2,887     92,044  92.96  69.4   32/48
  MC_aobs1   obs only       95,828    -2,175     92,571  93.49  61.9   29/48

Negative on both plants at every setting -- BUILT and in-month move together, so
this is DESTROYED capacity, not relocated output. That is the expected and
correct sign for a constraint that removes press-seats the plant does not have.

WHICH GTs BIND, div 2.0 (cured-tyre delta vs MC_base; 35 of 55 PCR and 24 of 34
TBR placed GTs end AT their cap):

  PCR    GT 2476 SUP MM     moulds 10 -> cap 5   -6,528     PCR total -25,182
         GT  T1457 STAR     moulds  5 -> cap 2   -5,883
         GT 2258 RAN HPE    moulds  4 -> cap 2   -3,864
         GT 1925 XPC1       moulds 13 -> cap 6   -2,846
         GT 1673 NEO        moulds  2 -> cap 1   -1,900
         GT 1773 NEO        moulds  4 -> cap 2   -1,174
  TBR    GT 5076 295/90R20 JDE XF  11 -> cap 5   -2,617     TBR total  -3,524
         GT 5114 315/80R22.5 JDC XD 2 -> cap 1     -339

Six PCR GTs carry -22,195 of the -25,182. This is a concentrated loss, not a
diffuse one.

-------------------------------------------------------------------------------
⚠ THE TRAP: `observed_max` IS NOT A CONCURRENCY MEASUREMENT
-------------------------------------------------------------------------------
`_offline/l2_capability.py` builds it as

    d AS (SELECT plant, gt, date, count(DISTINCT press) AS n ... GROUP BY 1,2,3)
    SELECT plant, gt, max(n) AS observed_max FROM d GROUP BY 1,2

-- DISTINCT PRESSES TOUCHED IN ONE PLANT-DAY, maximised over 8 months. Our peak
concurrency is an interval sweep, i.e. presses running AT ONE INSTANT. They are
different quantities, and a GT whose mould pairs are moved once mid-day visits
two presses without ever running on two. So the headline "the plan exceeds the
plant's observed_max on Aug TBR 9 GTs / 52,325 tyres (52.3 %) and PCR 2 /
12,435" -- reproduced exactly on MC_base -- compares OUR simultaneity against
the PLANT's daily press-visit count. Fifth instance of the denominator class
after PARTITION §1e, §4d, §4p.1 and §4q.7.

On our own plan the two definitions agree (daily-distinct / simultaneous p50
1.000, mean 1.003, both plants) -- but only because our campaigns are long
(PCR p50 213 h), so a press that touches a GT on a day runs it all day. The
plant's campaigns are far shorter, so for the PLANT the ratio must be higher and
the gap unmeasurable from any committed artefact. It needs `v_curing`, which
this checkout does not have.

`PLANNER_R3_OBS` therefore exists ONLY to price that variant on request. It is
the mined-statistic-as-a-cap defect by construction, AND the statistic is
mis-defined. Do not default it.

-------------------------------------------------------------------------------
⚠ AND THE DIVISOR CONTRADICTS THE PLANT'S OWN HISTORY ON MOST OF THE VOLUME
-------------------------------------------------------------------------------
`floor(moulds / 2) < observed_max` on **26 of 73 demanded PCR GTs carrying
267,980 of 430,423 tyres (62 %)** and **14 of 37 TBR GTs carrying 52,557 of
100,850 (52 %)**. `GT 2476 SUP MM` has 10 active moulds and the plant ran it on
8 presses in a day; two moulds per press makes that 5.

That is NOT the `max_horizontal(moulds, observed_max)` reconciliation leaking
in -- rebuilding the raw count from `curing_item_mould_mapping 2.csv` x
`mould_inv_ctp_17072026.csv` (ACTIVE) shows `moulds` was raised on only **2 of
73 PCR GTs (18,309 tyres: GT 1482 UHL 2->6, GT 1856 ROYL 2->3) and 0 of 37 TBR
GTs**. The column is essentially the raw active-mould count, and the raw count
still contradicts the divisor.

Two readings, and ONLY THE PLANT CAN CHOOSE:
  (a) `moulds` counts mould HALVES and moulds move between presses within a
      plant-day, so `observed_max` legitimately exceeds `moulds / 2`. Divisor 2
      is right and the loss above is the real price.
  (b) the inventory already lists a mould ASSEMBLY once, so `moulds` is a
      press-equivalent count and the divisor is 1.0 -- i.e. today's behaviour is
      already correct and this whole module should stay off.
The naming evidence points at (a): `mould_inv_ctp` lists `...HMI01` and
`...HMI02` as separate `Equnr` rows and the plant labels a press load as the
PAIR `HM01#HM02` (see plant_ct.py). It is not conclusive, and 8 points of
fulfilment turn on it. ESCALATE to plant-authority; do not pick one here.

-------------------------------------------------------------------------------
INTERACTION WITH THE PRESS-AVAILABILITY HAIRCUT (plant_ct.PRESS_AVAIL)
-------------------------------------------------------------------------------
Measured directly rather than assumed, the way PARTITION §4y.2 does it. dBUILT
against MC_base; `b93` = avail PCR 0.930 / TBR 0.900.

  arm                       A alone   B alone   additive   measured  interaction
  MC_ab   div2.0 + b93 PCR  -32,061    -3,488    -35,549    -35,613     -64
                       TBR   -3,363    -1,950     -5,313     -4,898    +415
  MC_abo  obs    + b93 PCR  -19,274    -3,488    -22,762    -13,001  +9,761
                       TBR   -2,175    -1,950     -4,125     -3,528    +597

The DIVISOR form is additive to within 0.02 % of BUILT on PCR and 0.42 % on TBR
-- two independent constraints, no coupling, exactly like §4y.2's cap/floor pair.
The `observed_max` form is NOT: combining it with the haircut costs 9,761 FEWER
PCR tyres than the two alone predict (+2.4 % of BUILT). A slower press makes
L4.5's `max_lot = concurrency x rate x 72 h` smaller, so the same volume is cut
into more and shorter campaigns, which a per-GT press-count clamp then obstructs
less. Do not read `MC_abo`'s milder loss as evidence the clamp is cheap -- it is
cheap only in company.
"""
from __future__ import annotations

import math
import os

from planner.config import CONFIG

# Divisor applied to the mould count to get concurrent PRESSES.
#   1.0  -> moulds            (SHIPS -- today's behaviour, exactly)
#   2.0  -> moulds / 2        (the plant ruling: LH + RH per press)
#   1.5  -> a midpoint, because the plant-wide realised moulds-per-press is 1.71
#           and not every press is loaded as a pair on every GT.
# The DEFAULT VALUE of the divisor when someone asks for "the plant ruling" is
# `CONFIG.thresholds.moulds_per_press`; this env var is the sweep handle.
R3_DIV = float(os.environ.get("PLANNER_R3_DIV", "1.0"))

# Additionally clamp to `cap_mould.observed_max` -- the most presses the plant
# itself ever ran this GT on in one day across 8 months.
#
# ⚠ THIS IS A MINED STATISTIC USED AS A CAP, which is this project's signature
# defect (PARTITION §1: tau* and min_lot as floors cost 13.4 pt between them).
# It is offered ONLY as a sweep arm, never as a default, and it is wrong the
# moment a GT's demand rises above what that history window contained. A GT the
# plant never ran (observed_max == 0) is NOT clamped -- absence of history is not
# evidence of a limit.
R3_OBS = os.environ.get("PLANNER_R3_OBS", "0") == "1"


def effective(moulds: int, observed_max: int | None = None) -> int:
    """Concurrent presses this GT may occupy. Never below 1.

    `moulds` is the mould-HALF count from `cap_mould_<M>.parquet`.
    `observed_max` is that file's own column; pass it or None.
    """
    m = max(int(moulds or 0), 1)
    cap = m if R3_DIV <= 1.0 else max(1, int(math.floor(m / R3_DIV)))
    if R3_OBS:
        o = int(observed_max or 0)
        if o > 0:                       # 0 == never observed, not "limit 0"
            cap = max(1, min(cap, o))
    return cap


def table(rows) -> dict[tuple[str, str], int]:
    """`cap_mould` rows -> {(plant, gt_code): effective cap}. The ONE loader."""
    return {(r["plant"], r["gt_code"]):
            effective(r["moulds"], r.get("observed_max"))
            for r in rows}


def banner() -> str:
    if R3_DIV <= 1.0 and not R3_OBS:
        return ("  R3 concurrency: moulds (ships; 1 mould per press assumed) "
                f"[moulds_per_press ruling = {CONFIG.thresholds.moulds_per_press}]")
    return (f"  R3 concurrency: floor(moulds / {R3_DIV:g})"
            + ("  clamped to plant observed_max" if R3_OBS else ""))
