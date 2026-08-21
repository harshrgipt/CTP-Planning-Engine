"""L5 -- CURE CAMPAIGN MASTER PLAN.  The plan is born here.

    python -m planner.cmbc.l5_cure_master --month 2026-07

Places every L4.5 cure lot as a campaign:  mould-set x press x [t_start, t_end].
Plant step 4 · R4, R3, R9.

CURING IS PLANNED FIRST AND ALONE.
  Nothing about building enters this layer. The plant sets its rhythm on the
  presses and building chases it (Phase 0: cure campaigns 58.5 h / 210.7 h fed by
  build campaigns of 7.6 h / 5.4 h, r=0.92/0.94 same-day). L6 then asks whether
  building CAN feed what L5 decided; if not, L5 reshapes. Building never gets to
  reshape curing by being scheduled first -- that inversion is the defect the
  whole architecture exists to fix.

CONSTRAINTS ENFORCED HERE
  * one GT per press at a time
  * concurrent presses for a GT <= its ACTIVE MOULD COUNT   (R3)
    A press with no mould in it cures nothing. `runs/july_v4` violates this in
    4 places -- GT 2167 RAN HPE on 6 presses against 2 moulds -- because the old
    engine had no mould model at all.
  * a mould change (PCR 6.0 h / TBR 6.02 h) whenever a press switches GT
  * press eligibility from L2
  * campaign length reported against the L0 band, not forced into it

PLANT HOLIDAYS (rule G3) -- CAMPAIGNS PAUSE, THEY DO NOT MOVE
  A closed plant-day (07:00 -> 07:00) is a hole in the press calendar. `free[press]`
  here is a single "available from" instant, not an interval list, so the hole
  cannot be booked the way L7 books it on a machine; it is expressed as
  ARITHMETIC. `holiday.add_work` spends a campaign's press-hours THROUGH the
  calendar, so a campaign meeting a closure keeps its press-hours and its
  wall-clock end moves +24 h. `holiday.next_free` keeps a seat, and the mould
  change before it, out of the shut window.
  This is the right model HERE and the wrong one on the build side: a cure
  campaign is 8-11 days, so pushing it wholly clear of a closure is a deletion,
  not a schedule. L7 blocks instead. See planner/cmbc/holiday.py for the
  measurement (August 2026, both plants) and PARTITION_AND_CHANGEOVER.md 4aa.
  Absent calendar => every call is the identity => byte-identical.

GREEDY AND DETERMINISTIC, BY CONSTRAINT
  No MILP, no CP-SAT (project rule). Lots are placed in a total order and each
  takes the eligible press that frees earliest; ties break on press id. Same
  inputs give a byte-identical plan, which is what makes A/B testing meaningful.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from datetime import datetime, timedelta
from pathlib import Path

import polars as pl

from planner.cmbc import allowable, holiday, plant_ct, r3_cap
from planner.config import CONFIG, GT_SHELF_LIFE_H
from planner import paths

ROOT = Path(__file__).resolve().parent.parent.parent

# ---- OPENING GT SOURCE OVERRIDE ---------------------------------------
# The next month's opening stock is THIS month's carry-forward. L7 emits it as
# `masters/opening_gt/carryforward_gt_<next>.parquet` under its own name so a
# planner output can never overwrite the MES-derived `opening_gt_<month>`
# master. Point a run at it with PLANNER_OPENING_GT -- a bare filename resolves
# inside masters/opening_gt, an absolute path is taken as given.
def _opening_gt_path(root, month):
    """Thin shim over `paths.opening_gt`. There were SIX character-identical
    re-implementations of this resolver (L4, L5, L7, plus inline reads in
    export_btp_format and export_shift_schedule, plus paths.py itself), against
    CLAUDE.md's "all input lookups go through planner/paths.py". They agreed;
    the doctrine is that more than one site is the defect, because the day they
    stop agreeing nothing detects it. `root` is accepted and ignored so the call
    sites did not have to change."""
    from planner import paths as _paths
    return _paths.opening_gt(month)

SRC_INP = paths.INPUT_DERIVED
D = ROOT / "warehouse" / "derived"
PARAMS = ROOT / "warehouse" / "params"


def _load_delay(month: str) -> dict[tuple, float]:
    """L6's reseat request: (plant, gt_code) -> hours to delay the cure seat.

    Written by `planner/cmbc/l56_loop.py`. Absent = empty = no-op, so a plain
    `python -m planner.cmbc.l5_cure_master` is byte-identical to before.
    """
    f = D / f"l56_delay_{month}.parquet"
    if not f.exists():
        return {}
    d = pl.read_parquet(f)
    return {(r["plant"], r["gt_code"]): float(r["delay_h"])
            for r in d.iter_rows(named=True) if r["delay_h"]}


_DELAY: dict[tuple, float] = {}


def _load_split(month: str) -> dict[tuple, int]:
    """L6's reseat request, split form: (plant, gt_code) -> how many pieces to
    divide each of that GT's campaigns into.

    WHY SPLIT AND NOT DELAY
      Delaying an unfed campaign was measured on 2026-08: campaigns-unfed fell
      111 -> 87, but in-month fed volume fell 458,863 -> 442,315. Every rightward
      move pushes output past the reporting boundary, so delay trades the metric
      it is trying to fix. Splitting does not move the seat -- it makes each
      piece need a SHORTER contiguous build window, which is what the gaps in a
      fragmented machine calendar can actually hold.

      Two independent headroom tests support it. 95-100 % of unfed volume sits on
      GTs below their R3 mould-concurrency cap, and ZERO unfed GTs are
      build-rate limited -- for every one, its allowable machines can collectively
      out-produce the cure draw. The volume is blocked by timing alone.

    THE FLOOR IS NOT NEGOTIABLE
      A split piece must stay at or above the cure-lot floor L4.5 actually
      applied (max of the derived min_cure_lot and B12's min_lot_units). Splitting
      below it would buy fulfilment by breaking B12 -- exactly the kind of
      cap-violating "improvement" this project's ledger warns about. The factor
      is therefore clamped per GT, and a GT whose campaigns are already at the
      floor cannot be split at all.
    """
    f = D / f"l56_split_{month}.parquet"
    if not f.exists():
        return {}
    d = pl.read_parquet(f)
    return {(r["plant"], r["gt_code"]): int(r["factor"])
            for r in d.iter_rows(named=True) if int(r["factor"]) > 1}


_SPLIT: dict[tuple, int] = {}

# Treat the horizon as a rolling WINDOW rather than a closed box: a campaign that
# starts inside it may finish outside it, and the tail carries into next month.
# Set "0" to restore the hard wall.
CARRY_OUT = os.environ.get("PLANNER_CARRY_OUT", "1") != "0"

# ---- HORIZON POLICY -- set by the plant, 2026-08-09 ----------------------
# "Only demand which is filled within the month is considered fulfilled. After
#  that, discard -- that's unfulfilment."
#
# THE MONTH IS A CLOSED BOX, NOT A WINDOW. Nothing may be delivered outside it
# and nothing outside it may be counted. Three modes:
#   "truncate" (B, DEFAULT) a campaign may start and is CUT at the horizon; the
#              in-month part is delivered and the tail is unfulfilled. The press
#              is released at the horizon, so nothing is committed past month end.
#   "strict"   (A) a campaign is placed only if it FINISHES inside the horizon.
#              Anything that cannot finish is never started.
#   "window"   the previous carry-out behaviour. A/B ONLY -- it emits rows dated
#              into the next month and commits presses the next month does not
#              know about (24 campaigns / 805 press-h measured on July).
#
# BOTH truncate AND strict fully satisfy the rule: zero out-of-month rows, zero
# campaigns ending past the horizon, nothing counted outside the month. They do
# NOT converge -- an earlier note here guessed they would and was wrong.
# Measured, all four arms fresh (MEMORY §11c):
#   mode       Jul PCR  Jul TBR  Aug PCR  Aug TBR   out-of-month rows
#   window      94.6 %   94.2 %   90.6 %   92.6 %   28 / 14  (non-compliant)
#   truncate    94.5 %   94.5 %   90.1 %   92.7 %   0 / 0    <- SHIPPED
#   strict      93.5 %   93.7 %   89.4 %   92.6 %   0 / 0
#
# `truncate` is shipped because it costs LESS for identical compliance: strict
# gives up a further 1.0 pt (Jul PCR), 0.8 pt (Jul TBR) and 0.7 pt (Aug PCR) by
# refusing work that could have been delivered inside the month. Delivering the
# in-month portion IS "demand filled within the month"; only the cut tail is
# unfulfilment. Strict also loses 2 invariants on July.
#
# ---- SUPERSEDED BY "extend", PLANT RULING 2026-08-10 ---------------------
#   "extend"  (DEFAULT) PLAN on month + tail, REPORT only the month.
#
# The reporting rule is UNCHANGED -- a tyre counts for this month only if it is
# CURED inside the month (`qty_fed_in_month` in L7 already clips exactly that).
# What changes is the PLANNING horizon: a cure campaign may start inside the
# month and finish in the tail, so the presses are not tapered at hour 744 and
# building is not starved of a downstream pull in the last day.
#
# The defect this fixes: under `truncate` a campaign is CUT at hour 744 and the
# press released, so nothing pulls building in the final ~25 h. PCR built 5,712
# tyres on day 30 and ZERO on day 31 while the plant runs flat to the last hour.
# The month-end GT balance therefore collapsed to ~0 and the hand-off to next
# month was fictitious.
#
# WHAT THE TAIL IS FOR, and why 72 h. It is NOT extra demand and NOT extra
# fulfilment -- every tyre cured after hour 744 is excluded from this month's
# numerator. It exists so that GREEN TYRES BUILT IN THE MONTH have a real
# consumer just past the boundary, which is what makes them next month's opening
# stock instead of scrap. A tyre built at hour 744 can be legally held for
# exactly GT_SHELF_LIFE_H = 72 h (R5) and no longer, so a cure seat further out
# than 72 h can never be fed by in-month building -- it would need next month's
# build, which is not ours to plan. 72 h is therefore the largest tail that does
# any work, and the smallest that does all of it.
HORIZON_MODE = os.environ.get("PLANNER_HORIZON_MODE", "extend")

# Hours of PLANNING horizon past the plant month end. Only consulted in
# "extend"; the reporting boundary is always the month.
HORIZON_TAIL_H = float(os.environ.get("PLANNER_HORIZON_TAIL_H", "72"))

# Let a GT with enough opening stock start curing at t0 instead of paying the
# tau* + build_band floor.
#
# ON. It was off for a long time on a measurement that no longer applies.
#
# The old note read "MEASURED NET NEGATIVE, 98.9% -> 98.7%". That was the
# 98.9%-era engine -- before tau_min release, the machine partition, the derived
# slice rule and the rail margin. RE-MEASURED on the current engine it is worth
# +2.3 points:
#     floor basis  early stock   first cure   PCR daymax   fulfilment
#     star            OFF          11.86 h       4,679        91.3 %
#     star            ON            0.00 h       4,611        93.6 %   <- shipped
# Presses were idling for 11.86 h on PCR and 10.18 h on TBR while 2,770 PCR and
# 1,264 TBR tyres of opening stock -- 40 % and 54 % of what we hold -- sat unused.
#
# The bound is the GAP, not the campaign: stock need only cover (tau* + band) x
# draw rate, ~72 tyres on PCR and ~31 on TBR, against ~120/GT on hand. Requiring
# it to cover the campaign QUANTITY is the version that never fires.
#
# NB lowering the floor itself does NOT work -- "min" and "slice" bases let L5
# place more (491,539 vs 488,860) but fulfilment FALLS to 90.1-90.3 %, because
# L7 cannot feed campaigns that start before any tyre exists. Stock is what makes
# an early start real; a smaller number does not.
EARLY_STOCK = os.environ.get("PLANNER_EARLY_STOCK", "1") != "0"
# Warm-start floor for the campaign that OPENS a press -- see earliest_cure.
WARM_OPEN = os.environ.get("PLANNER_WARM_OPEN", "0") != "0"   # measured -0.1 pt
# Mould life: one change every N cycles of WEAR. 3,000 cycles x 2 cavities
# = 6,000 tyres per change, costed exactly like a GT-switch change.
MOULD_LIFE_CYCLES = float(os.environ.get("PLANNER_MOULD_LIFE_CYCLES", "0"))
# Hours at the start of the month during which an L5<->L6 delay hint is NOT
# applied. 0 = off (the hint delays every campaign of the GT, day-one included).
PROTECT_FIRST_H = float(os.environ.get("PLANNER_L56_PROTECT_FIRST_H", "0"))
# Charge a mould change against PRESS availability, in parallel with waiting
# for green tyres, instead of serialising the two. 0 restores the old form.
CHG_PARALLEL = os.environ.get("PLANNER_L5_CHG_PARALLEL", "0") != "0"
MOULD_LIFE_TYRES = MOULD_LIFE_CYCLES * 2.0

# Basis for the day-1 cure floor. See earliest_cure(). "star" is the legacy
# 11.86 h wall built from two MEDIANS; "min" and "slice" use the physical
# tau_min. NB the EARLY_STOCK note above was measured on the 98.9 %-era engine,
# long before tau_min release, the partition and the derived slice rule -- its
# baseline no longer exists, so re-measure before trusting it.
# SHIPS "slice" BY PLANT INSTRUCTION, 2026-08-20. tau* is REMOVED from the
# release: a press may cure at t0 + tau_min (16 min) instead of
# t0 + tau* + first-slice build (259 min + slice) on PCR.
#
# THE PLANT'S MODEL, and it is right about the physics: building starts, the
# first tyres exist ~16-20 min later, and the press can take them. The B12 build
# RUN floor then keeps building on that GT for at least min_lot (PCR 150 /
# TBR 70) before any changeover, so the press is fed continuously rather than
# for one slice. Verified on the shipped plan: 0 of 792 PCR and 0 of 514 TBR
# build runs fall below the floor, run size p50 318 / 120.
#
# WHAT IT BUYS -- day 1, measured both months:
#     Jul PCR  cure 8,782 -> 13,519   press util 59 -> 89 %   t0 presses 25 -> 52
#     Aug PCR  cure 7,902 -> 12,961   press util 55 -> 88 %   t0 presses 20 -> 47
#     Jul TBR  2,444 -> 3,139         Aug TBR 2,317 -> 3,214
#
# WHAT IT COSTS, AND THE COST IS REAL -- see §4ad:
#     BUILT  Jul PCR -3,885 · TBR -1,787 · Aug PCR -7,088 · TBR -1,501
#     starvation Aug PCR 18,311 -> 26,183
#     in-month  95.07 -> 94.14 · 97.87 -> 95.60 · 92.18 -> 90.71 · 97.89 -> 96.42
#
# The day-by-day decomposition (§4ad): d1 +5,060 cure, d2-7 exactly 0 (presses
# are at 100 % either way), d8-31 -3,473. The d1 gain is the standing floor
# surplus being consumed, not tyres created. The month loses because 47 presses
# then want 47 different GTs from 11 machines, with 32.6 % of PCR volume locked
# to 1-2 of them.
#
# SHIPPED ON INSTRUCTION WITH THE COST STATED. `PLANNER_L5_FLOOR_BASIS=star`
# restores the wall and recovers the BUILT. The real fix is `allowed_machine_matrix`
# widening -- a PLANT RULING, since MES shows the plant itself runs 5-11 machines
# per GT where the matrix permits 1-2. `feed` is a bounded variant, still OFF.
FLOOR_BASIS = os.environ.get("PLANNER_L5_FLOOR_BASIS", "star")
# Prefer the press with the fewest ELIGIBLE GTs when two are otherwise equal.
# See the block beside its use: the plant's four fastest PCR presses are also
# its narrowest, and greedy never picks them.
SCARCE_PRESS = os.environ.get("PLANNER_L5_SCARCE_PRESS", "0") == "1"
# Hours of start-time slack a NARROW press may claim over a wider one. 0 keeps
# scarcity as a pure tiebreak (measured: fires 8 h of 480, useless).
SCARCE_SLACK_H = float(os.environ.get("PLANNER_L5_SCARCE_SLACK_H", "0"))
# Distinct presses a GT may visit, as a multiple of the plant's observed_max.
# 0 = off. See the block beside its use for the 189-vs-136 measurement.
MAXPG = float(os.environ.get("PLANNER_L5_MAX_PRESS_PER_GT", "0"))
# ---- MONTH-END COMPLETABILITY (PLANNER_L5_MONTHEND_FIT) ---------------------
#
# WHAT THIS DOES / WHY IT EXISTS -- an experiment against a stated defect, run
# 2026-08-21. In the last N plant-days, prefer a (press, start) that lets a
# campaign FINISH before `month_end` over one that spills past it.
#   off (default) | prefer | require | split          window: ..._WIN_D (days)
#   ..._STRICT=1  makes `require` REFUSE a campaign that cannot fit rather than
#                 fall back to the ungoverned seat (DAY_CAP soft/strict doctrine:
#                 a shape preference that becomes lost demand must be PRICED).
#
# THE PREMISE IT WAS BUILT ON IS FALSE, AND THAT IS THE MAIN RESULT.
#   The brief was "a campaign ending after month_end contributes ZERO to
#   in-month fulfilment". It does not. `l7_pull_release.py` (search
#   `frac_in_month`) PRORATES a crossing campaign:
#       frac = 1.0                                  if end_ts <= month_end
#            = 0.0                                  if start_ts >= month_end
#            = (month_end - start_ts) / duration     otherwise
#   Measured on the shipped August plan: of 57 crossing PCR campaigns
#   (40,997 tyres) only TWO have frac == 0, and those START after the boundary.
#   The other 55 already deliver their in-month share. L5's own gate line has
#   said so all along -- "campaigns crossing month end : 73 -- only the in-month
#   fraction is counted". The 12,620-tyre PCR "tail" is the OUT-of-month
#   remainder of prorated campaigns, not a set of zeroed ones.
#
# `prefer` AND `require`(soft) ARE STRUCTURAL NO-OPS -- proven, not argued.
#   `dur` is qty/rate with rate keyed on (plant, gt), NOT on press;
#   MOULD_LIFE_CYCLES defaults to 0 and no holiday calendar is configured, so
#   `en == st + dur` exactly and identically on every candidate press.
#   Completability is therefore a MONOTONE function of `st` -- the key the
#   greedy already leads on. If the earliest press cannot finish in-month, no
#   press can; if it can, it was already chosen. Ranking `_fits` above `st`
#   re-orders nothing. This is DO-NOT #35 one level up: a preference that is a
#   monotone function of the key you already sort on is not a preference.
#
# SHIPS OFF. MEASURED 2026-08-21, AUGUST 2026 ONLY (the partition on disk is
# stamped 2026-08 and must not be rebuilt; NO JULY ARM EXISTS). Fresh arms via
# scripts/run_arm.py from ME_base, all gated FRESH by check_arm_fresh.py,
# PLANNER_L7_CLOSING_BUFFER=1.
#
#   arm         PCR BUILT  dBUILT   in-month   ful%    tail  starved   L11
#   ME_base       409,511      +0    397,326  92.59  12,620   12,477  32/48
#   pref  N=7     409,511      +0    397,326  92.59  12,620   12,477  32/48
#   split N=7     409,511      +0    397,241  92.57  12,567   12,615  32/48
#   split N=5     408,795    -716    397,091  92.53  12,556   12,776  32/48
#   require N=7   399,907  -9,604    391,819  91.30   8,783   10,917  32/48
#
#   arm         TBR BUILT  dBUILT   in-month   ful%    tail  starved
#   ME_base        98,003      +0     96,932  97.89   1,508    1,654
#   pref  N=7      98,003      +0     96,932  97.89   1,508    1,654
#   split N=7      97,990     -13     96,961  97.92   1,466    1,667
#   split N=5      97,990     -13     96,962  97.92   1,465    1,667
#   require N=7    96,529  -1,474     95,577  96.52   1,389    1,775
#
#   `pref` at N = 5, 7 AND 10 is BYTE-IDENTICAL to ME_base on all ELEVEN run
#   artefacts (cure_campaigns, build_schedule, gt_events, reconciled,
#   build_starved, build_by_shift, cure_by_shift, mould_changes,
#   l11_invariants, carry_forward_gt, carry_out). The mechanism fires -- it
#   detects 63/68/73 spilling campaigns -- and changes nothing.
#
# `split` IS ARITHMETICALLY NEUTRAL BY CONSTRUCTION. Cutting at the boundary
# turns one prorated row into a frac==1.0 row plus a frac==0.0 row whose
# quantities are that same proration, so in-month cannot move except by integer
# rounding at the cut: the L5-side prorated total moves 6 tyres on PCR and 2 on
# TBR. What it DOES change is L7's problem -- 17 extra campaign rows are 17
# extra independent build releases -- and that is why PCR starvation rises 138
# and in-month falls 85 while TBR gains 29. MIXED SIGN ACROSS PLANTS = fails
# the gate. This is the THIRD cure-campaign split measured on this engine
# (MAX_CAMPAIGN_H -43,104; l56_loop -18,129) and the first that is merely
# neutral rather than catastrophic -- because it is the only one that does NOT
# move the press seats. It still does not pay.
#
# THE TRAP, STATED PLAINLY. `require` CUTS THE PCR TAIL 12,620 -> 8,783 and
# that is its worst arm: BUILT -9,604 and in-month -1.28 pt. A smaller tail is
# not a win -- tail tyres are next month's opening stock, and the only way to
# shrink the tail without producing less is to move work in-month, which
# requires an EARLIER seat, which the greedy already takes. Read the tail column
# alone and `require` is the best arm in the table. It destroys 9,604 tyres.
# Second trap: `split N=7` shows BUILT-in-month +765 PCR while total BUILT is
# UNCHANGED at 409,511 -- 765 tyres of build merely crossed the reporting
# boundary. That is the PLANNER_L7_TAIL_BUILD_PULL reading of section 4ak.1;
# quote clipped and unclipped BUILT together or it reads as a free win.
#
# NOT PLANNER_L5_MAX_CAMPAIGN_H, and it did not collapse into it. That flag is
# a GLOBAL span cap applied to every campaign before the sort (-43,104 PCR BUILT
# at 240 h, unfed x6.7). This is scoped to the boundary, touches only the 12-17
# campaigns that cross it, and leaves the press timeline bit-identical: union
# press-hours 109,675.79 in both base and split arms.
#
# See PARTITION_AND_CHANGEOVER.md section 4aw.
MONTHEND_FIT = os.environ.get("PLANNER_L5_MONTHEND_FIT", "off")
MONTHEND_WIN_D = float(os.environ.get("PLANNER_L5_MONTHEND_WIN_D", "7"))
MONTHEND_STRICT = os.environ.get("PLANNER_L5_MONTHEND_STRICT", "0") == "1"
# Fraction of an ABSORBER GT's lots deferred behind the rest of the book, so
# smaller GTs finish earlier and their presses free up sooner. See the block
# beside its use for the GT 1513 plant-vs-us ramp that motivates it.
BACKLOAD = float(os.environ.get("PLANNER_L5_BACKLOAD", "0"))
# Sort cure lots by their ORDER-BOOK due date before scarcity. See the block
# beside the sort: the plant's cumulative curve IS its due-date curve.
EDD = os.environ.get("PLANNER_L5_EDD", "0") == "1"
# Honour the `_warm` set (GTs whose machine/press is mid-run at t0) as a release
# at tau_min. The set was UNREACHABLE until 2026-08-14 because the "star" branch
# returned first; making it reachable is the correctness fix, honouring it is a
# measured -1,319 PCR. Default preserves shipped behaviour. See earliest_cure.
WARM_RELEASE = os.environ.get("PLANNER_L5_WARM_RELEASE", "0") == "1"
# Let the campaigns that ALREADY HOLD ENOUGH GREEN TYRES to start at t0 claim
# the opening press seats before the biggest stockless campaign takes them.
# Bounded to the seats the opening stock actually pays for, so `-qty` is
# untouched from the first unstocked job onwards. See the block beside the sort.
STOCK_FIRST = os.environ.get("PLANNER_L5_STOCK_FIRST", "0") == "1"
# Make the L5 SEAT QUEUE aware that green tyres are already on the floor and are
# PERISHING. Distinct from STOCK_FIRST above: that one promotes by how many t0
# seats the stock can PAY FOR and orders the head by -qty; this one promotes the
# fewest presses that can DRINK the stock inside its own remaining shelf life and
# orders the head by that shelf life, soonest-to-expire first. See the block
# beside the sort for the measurement.
#   "0"  off (ships)            "1"      head ordered by remaining shelf life
#   "alpha"  head ordered by gt_code     "qty"    head ordered by -qty (§4z's key)
# `alpha` and `qty` are the NULL CONTROL required by DO-NOT #49: they promote the
# IDENTICAL set of campaigns and differ only in how that set is ordered among
# itself, which cannot carry the mechanism. If they move BUILT as much as "1"
# does, the effect is the greedy re-settling and not the shelf-life preference.
STOCK_URGENT_MODE = os.environ.get("PLANNER_L5_STOCK_URGENT", "0").strip().lower()
STOCK_URGENT = STOCK_URGENT_MODE in ("1", "on", "alpha", "qty")
# Promote only GTs holding more than this many usable tyres. 0 = every GT that
# holds any. A probe knob for the narrow variant -- never ship a mined value in
# it (PARTITION §1).
STOCK_URGENT_MINQ = float(os.environ.get("PLANNER_L5_STOCK_URGENT_MINQ", "0")
                          or 0.0)

# ---- FULL AVAILABILITY AT t0 (stated planning assumption, B-ASSUME-1) -----
# PLANT RULING, 2026-08-09: "assume everything is available for building from
# the very start -- we don't have to wait for anything." Materials, components,
# compounds, machines and moulds are all staged at hour 0; there is no ramp-up,
# no warm-up and no staging delay. This is an ASSUMPTION handed down by the
# domain authority, NOT a measured plant behaviour -- see BUSINESS_RULES.md.
#
# What it does NOT license: it does not create inventory that does not exist,
# and it does not touch R5 (72 h shelf life), the WIP rail, the B12 lot floor,
# TT/TL or the rim locks. A cure at t0 still needs a tyre that physically
# exists at t0; that is cure-before-build, not an availability assumption.
#
# MEASURED, AND DEFAULTED OFF -- the ruling is ALREADY SATISFIED at default.
# Materials/components never gate building at all (L8 explodes them downstream
# of L7; L6 is a load report). Machines and presses are free from hour 0
# (`busy = {}` in l7, `free.get(pr, t0)` here). All in-shelf-life opening stock
# is offered to the plan. Building starts at t0+0.00 h. There is no ramp to
# remove, so this flag does not implement the ruling -- it implements a DIFFERENT
# seating policy that the ruling does not require, and it measures WORSE.
#
# What it changes: the stock exemption is a BINARY test (`stock >= gap_tyres`),
# so a GT holding 73 of the 76 tyres needed to bridge the gap gets ZERO credit
# and waits the full 11.86 h. Partial credit makes the wait continuous:
# `wait = (gap_tyres - stock)/rate`. 6 PCR GTs and 3 TBR GTs are in that band.
#
# July, both arms fresh (jul_off vs jul_ramp):
#     day-1/2 unfed  PCR 3,869 -> 3,595   TBR 1,770 -> 1,697   (better)
#     TOTAL   unfed  PCR 7,582 -> 8,332   TBR 2,794 -> 3,253   (WORSE)
#     fulfilment     PCR 96.95 -> 96.83   TBR 95.56 -> 95.14
#     `cold` bails   161 -> 177  (MORE runs need to start before t0, not fewer)
#     and it flips TBR mean GT inventory below the G8 band.
# Pulling a cure seat earlier pulls its BUILD deadline earlier too, past t0 --
# so it moves starvation off day 1 into the rest of the month rather than
# removing it. Same mechanism §4i measured for FLOOR_BASIS min/slice.
FULL_AVAIL_T0 = os.environ.get("PLANNER_FULL_AVAILABILITY_T0", "0") != "0"
# Sub-switch so the two halves of the ruling can be measured ONE AT A TIME
# (§29 / DO-NOT: never change two variables at once). This is the L5 half --
# the start-of-horizon ramp. The L7 half is PLANNER_FULL_AVAIL_LADDER.
FULL_AVAIL_RAMP = os.environ.get(
    "PLANNER_FULL_AVAIL_RAMP", "1" if FULL_AVAIL_T0 else "0") != "0"

# ---- LEVEL-LOADED PRESS-CONCURRENCY BUDGET (takt cap) --------------------
# Shipped L5 places every campaign AS EARLY AS POSSIBLE. Measured on its own
# output (runs/aug_v3, runs/jul_v3), clipping every campaign into the days it is
# actually spent:
#     TBR Aug   press occupancy  98.2 % on days 1-20,  34.7 % on days 21-31,
#               2.5 % on day 31.  Work content is 44,456 press-h against 58,776
#               available -- the month only needs 75.6 % -- and the greedy spends
#               all of it in the first two thirds.
#     TBR Jul   98.3 % / 81.6 %, work content 92.3 %.
#     PCR       98.0 % / 91.2 % (Jul), 98.1 % / 93.8 % (Aug), work content
#               95.6 % / 96.6 % -- almost no slack to level.
# Building must feed that draw. TBR build occupancy sits at 80-87 % for days
# 1-20 and near zero after, and 5,810 TBR August tyres starve inside the busy
# stretch while 24 % of the month's press capacity idles behind them.
#
# DELAY ALONE CANNOT FIX IT and that is the whole design constraint: a TBR
# campaign is 248 h at p50 (10.3 days), so there is no room to "spread starts".
# The lever is CONCURRENCY -- run fewer presses for longer, not all of them for
# two thirds of the month. 44,456 press-h / 744 h = 59.8 presses, so TBR August
# wants ~60 presses seated at all times instead of 79 seated for 21 days.
#
# So the governor is a budget on CONCURRENTLY SEATED PRESSES per partition:
#     N_k = clip( ceil(ALPHA * W_k / U),  1,  |presses in k| )
# with W_k the partition's total cure press-hours and U the usable span. ALPHA
# is the front-loading allowance over the level rate (1.0 = perfectly flat).
# Concurrency rather than a tyres/h ceiling because the press rate is a plant
# constant here, so concurrency x press_rate IS the build draw; the integer form
# is the physical decision ("how many presses may be seated at once"), needs no
# cadence master, and is O(1) to test.
#
# PARTITIONS -- a campaign is charged to every partition it belongs to:
#   (plant, "ALL")   always
#   TBR (plant, TT/TL group)   the B16 dedication
#   PCR (plant, rim)           the rim lock
#
# THE HORIZON GUARD -- this is the defect that killed the previous prototype.
# The governor is CONSULTED ONLY WHEN THE UNGOVERNED PLACEMENT ALREADY FITS IN
# THE MONTH, and it may only move the campaign to another window that also fits.
# It can therefore never turn a deliverable campaign into an overrun one, and it
# never touches a campaign that was going to be split/refused anyway. Under the
# closed-box horizon a campaign pushed past month end is LOST VOLUME, not
# carry-out (MEMORY §12), so the guard is a correctness requirement, not a
# tuning choice. If no in-month governed window exists the campaign is placed
# ungoverned: a shape preference must never become lost demand.
# ALPHA = 1.0 is an INTERIOR MAXIMUM on both months, not a tuned constant: TBR
# July 95.76 / 96.59 / 94.73 and August 98.00 / 98.55 / 97.23 at alpha
# 0.95 / 1.00 / 1.10. It is the takt rate itself.
# TBR ONLY. On PCR the same governor measured -0.28 pt July / +0.18 pt August --
# MIXED SIGN across months, so it is rejected (a knob validated on one month is
# not validated). PCR has only 3.4 %/4.4 % press slack; there is nothing to level.
GOV = os.environ.get("PLANNER_L5_TAKT", "flat")         # flat (default) | off
ALPHA = float(os.environ.get("PLANNER_L5_ALPHA", "1.0"))
# SHIPPED ON PCR 2026-08-20 (was "TBR"). Two-month, two-plant gate, fresh arms,
# per-month partition -- Jul PCR BUILT +4,693 / in-month +0.16, Aug PCR +4,985 /
# +0.27, TBR byte-identical on both months. Weighted build changeover 79.5 FAIL
# -> 64.3 PASS on July. See PARTITION_AND_CHANGEOVER.md 4am; this overturns
# 4l.1's July -0.28 pt rejection, which was measured on the pre-PINNED_FIRST,
# pre-CLOSING_BUFFER, pre-4ab, pre-cavities=2 baseline.
GOV_PLANTS = {x for x in os.environ.get(
    "PLANNER_L5_TAKT_PLANTS", "PCR,TBR").split(",") if x}

# ---- THE GOVERNOR ON PCR -- RE-MEASURED 2026-08-20, AND IT WON --------------
#
# WHAT THIS DOES / WHY IT EXISTS -- a measured defect in the tail, found
# 2026-08-20 while investigating "the plan collapses at month end".
#
#   Shipped August PCR runs its presses at 100 % occupancy on days 2-10 and
#   39 % on day 31; building follows it down to 43 %. 12,686 tyres starve as
#   `no feasible release` while 513 idle building-machine hours sit in d27-31.
#   Those idle hours are NOT reachable by the starved volume: R5 bounds a build
#   to [t_cure - 72 h, t_cure - tau_min], so month-wide idle hours say nothing
#   about whether a run 20 days earlier can be placed (see l7_pull_release.py,
#   "THE LEGAL BAND IS NOT [t0, ideal]"). The only way to reach tail machine
#   hours is to move CURE SEATS into the tail, which is this governor's job.
#
#   The PCR budget is dominated by the RIM sub-partitions, not by `ALL`:
#   ALL 81 of 86 presses, but R13 24 of 51, R14 9 of 43, R17 10 of 46. So what
#   the governor actually prevents is one RIM claiming 51 presses in week one
#   when the machines allowed on that rim can never feed them.
#
# SHIPS OFF (GOV_PLANTS still defaults to "TBR"). MEASURED 2026-08, PCR+TBR,
# fresh arms via scripts/run_arm.py, gated by scripts/check_arm_fresh.py:
#
#   arm                       BUILT    dBUILT   in-month   ful%  starved   L11
#   TF_base  PCR            409,967        +0    401,222  94.03   12,686  31/48
#   TAKT_PLANTS=PCR,TBR     414,301    +4,334    399,511  93.63    5,129  32/48
#   TF_base  TBR             98,003        +0     96,932  98.17    1,654
#   TAKT_PLANTS=PCR,TBR      98,003        +0     96,932  98.17    1,654   <- byte-identical
#
#   PCR build d27-31 as % of interior median: 89/79/57/46/45 -> 101/98/99/97/80.
#   PCR WEIGHTED build changeover 86.3 -> 73.1 min/machine-day: the ONLY L11
#   invariant that flips, and it flips to PASS against the 74.0 cap.
#   PCR same-size share 53.7 -> 65.4 %; last-day GT 1,787 -> 3,931.
#
# THE TRAP IN READING IT, AND IT IS THE POINT OF THE EXERCISE:
#   BUILT by window   d1-2 -416 · interior d3-26 -15,732 · tail d27-31 +18,998.
#   THE TAIL FILLS PARTLY BY BORROWING FROM THE INTERIOR. Only 4,334 of the
#   18,998 tyres that appear in the tail are new output; the other 77 % is
#   interior work relocated. Total build machine-hours used moves 6,772 ->
#   6,820 of 8,184 -- the gain is real but it is 48 machine-hours wide, not
#   19,000. This is the mirror image of §4ad / memory `day1-gain-is-borrowed`.
#   Say it in the headline, never in a footnote.
#   IN-MONTH FALLS WHILE BUILT RISES: in-month cure 401,222 -> 399,511
#   (-0.40 pt) because 6,751 more tyres cure in September; total real output
#   (in-month + tail) is 410,775 -> 415,815, +5,040.
#   R5 max 61.4 -> 70.3 h against the hard 72 h -- 1.7 h of margin left. A
#   flatter cure curve holds green tyres longer (same cost §4y found).
#   Carry-out debt 14.3 h (8,586 tyres) -> 27.1 h (16,285), still PASS <= 72 h.
#
# NOT GATED ON JULY. The July partition could not be built in this session, and
# July is exactly where this flag previously measured -0.28 pt. Do not ship it
# to defaults until a fresh July arm agrees.
#
# ---- PER-PLANT ALPHA AND PER-PLANT SUB-PARTITION ----------------------------
# `ALPHA` and `PLANNER_L5_TAKT_PART` are PLANT-WIDE, and the two plants do not
# want the same value. Measured on the same August arms:
#
#   arm                                  PCR BUILT   TBR BUILT   PCR in-month
#   ALPHA=1.00 (TF_taktboth)              +4,334          +0        -0.40 pt
#   ALPHA=1.05                            +4,071        -791        +0.38 pt
#   ALPHA=1.10                            +2,412        -989        +0.06 pt
#   ALPHA=1.20                            +2,932      -2,818        +0.36 pt
#   ALPHA=0.95                            +1,554         -42        -1.72 pt
#   TAKT_PART=0 (both plants)             +4,985        -897        +0.27 pt
#
# Every non-1.0 alpha and every subpart change is MIXED SIGN across plants,
# which fails the gate -- because one knob is being asked to serve two plants
# with different press slack (PCR 3.4-4.4 %, TBR 24 %). These two flags let the
# knob be set per plant so the mixed sign can be tested away rather than
# accepted. UNSET, both fall back to the global value, so the shipped plan is
# byte-identical -- verified as a control arm (TF_ctl vs TF_base and TF_tk_ctl
# vs TF_taktboth), all six artefacts unchanged in both.
#
# WITH THE PER-PLANT FLAG, TBR IS BYTE-IDENTICAL IN EVERY ARM BELOW, so the
# mixed sign is gone and the whole response is PCR's (2026-08, fresh arms):
#
#   alpha_PCR   sub-part ON: dBUILT  d in-month      sub-part OFF: dBUILT  d in-m
#     0.95           +1,554        -1.72 pt              (0.98)  +3,545  -0.44 pt
#     1.00           +4,334        -0.40 pt                      +4,985  +0.27 pt
#     1.01           +4,403        +0.15 pt                      +5,169  +0.58 pt
#     1.02           +3,618        +0.03 pt                      +2,284  +0.29 pt
#     1.03           +3,827        +0.13 pt
#     1.04           +3,337        +0.18 pt
#     1.05           +4,071        +0.38 pt                        -617  -0.21 pt
#     1.07           +3,981        +0.44 pt
#     1.10           +2,412        +0.06 pt
#     1.20           +2,932        +0.36 pt
#
# DO NOT TUNE ALPHA ON THIS TABLE. The response is NOT monotone and not even
# smooth: with the sub-partition off, 1.01 -> +5,169, 1.02 -> +2,284 and
# 1.05 -> -617. A 0.04 change of the knob swings BUILT by 5,800 tyres. That is
# greedy-placement jitter, not an optimum, and picking the argmax of one month
# is precisely the mined-constant defect class (§1, "a median is not a floor").
# ALPHA = 1.0 is the only value with a reason: it IS the takt rate, and it is
# TBR's measured interior maximum on two months. Ship 1.0 or ship nothing.
#
# WHAT IS ROBUST IS THE DIRECTION. 17 of the 18 arms with the governor on PCR
# raise BUILT, and both alpha-1.0 variants survive two perturbed baselines:
#
#   baseline                              sub-part OFF   sub-part ON
#   shipped                                    +4,985        +4,334
#   PLANNER_L7_CLOSING_BUFFER=0                +5,348        +5,490
#   PLANNER_LOT_INTERVAL_H=8                   +7,745        +5,820
#
# so the gain is not an artefact of the closing buffer (it is LARGER without
# it -- the buffer was spending idle tail hours the governor then uses better).
ALPHA_P = {p: float(os.environ.get(f"PLANNER_L5_ALPHA_{p}", "") or ALPHA)
           for p in ("PCR", "TBR")}
# SHIPPED AS TBR-ONLY 2026-08-20 (was "PCR,TBR"). The sub-partition governor
# helps TBR and costs PCR: with it on PCR, Jul BUILT +4,095 vs +4,693 and Aug
# +4,334 vs +4,985, and August R5 max runs to 70.3 h against the hard 72 --
# 1.7 h of margin, versus 61.9 h without. PCR's binding scarcity is the RIM
# sub-partition, not the plant press total, so a second concurrency governor
# stacked on top over-constrains it. TBR keeps it: measured -3,968 BUILT off.
TAKT_PART_PLANTS = {x for x in os.environ.get(
    "PLANNER_L5_TAKT_PART_PLANTS", "TBR").split(",") if x}

# ---- GT/RIM FEED CEILING (PLANNER_L5_FEED_CEIL) --------------------------
# WHAT THIS DOES / WHY IT EXISTS -- built 2026-08-21 to test a diagnosis, not
# to ship a belief.
#
#   The diagnosis under test: L5 seats more concurrent presses on a rim than
#   the BUILDING machines eligible for that rim can feed in the window, the
#   surplus is nominally scheduled, and it starves later in L7. August PCR
#   starvation is 12,477, of which 55 % `release_before_t0` and 40 %
#   `r5_shelf_life` -- and PARTITION §4au already proved `r5_shelf_life` is
#   the WRONG NAME for what it records: the machines are 90-98 % occupied
#   inside the R5 band while running 70-91 % month-wide, so what is missing is
#   a CONTIGUOUS hole, not shelf life. Both labels are building-machine
#   contention.
#
#   A DYNAMIC per-(rim, window) ceiling, every term derived from the month's
#   own data at run time -- no mined constant anywhere (§1):
#
#       cure drawn on rim r inside any W-hour window
#           <=  SLACK x ( eligible building output for r in W
#                         + R5-usable opening GT stock for r )
#
#   `eligible` is the partition + home + rim-lock set L7 ACTUALLY enforces via
#   `_locked`, never `cap_machine` -- that is ~3x wider and is not what binds.
#   A machine serving two rims is apportioned by its own booked share, never
#   counted whole to each (DO-NOT #44: check the resource PER HOLDER).
#
# HOW IT DIFFERS FROM THE SHIPPED TAKT GOVERNOR (§4am), which it must not
# merely re-implement:
#   takt  = press CONCURRENCY per partition, derived from CURE press-hours vs
#           span. On PCR it runs on the plant aggregate ALL only
#           (TAKT_PART_PLANTS defaults to TBR), so it carries NO rim term.
#   this  = a tyres/hour ceiling per RIM, derived from BUILD-side capability.
#   The two constrain different resources on different keys.
#   NOT REDUNDANT, and that is measured, not argued: every arm below ran with
#   the shipped takt governor ON, and the L5 log shows PCR budgeting exactly one
#   partition (`PCR ALL`, 81 of 86 presses). The ceiling still moved seats that
#   takt had already placed. It binds where takt does not. It still ships OFF.
#
# =====================================================================
# SHIPS OFF. MEASURED 2026-08-21, AUGUST ONLY (the partition on disk is stamped
# 2026-08 and must not be rebuilt), PCR+TBR. Fresh arms via scripts/run_arm.py,
# all gated FRESH by scripts/check_arm_fresh.py, baseline
# PLANNER_L7_CLOSING_BUFFER=1 PLANNER_HOLIDAYS=2026-08-15.
# TBR is BYTE-IDENTICAL in every arm (FEED_PLANTS defaults to PCR).
#
#   PCR, demand 429,146          BUILT   dBUILT  in-month   ful%   starved   L11
#   FC_base                    409,511       +0   397,326  92.59    12,477  32/48
#   W=72 slack=1.00            407,568   -1,943   393,048  91.59    11,265  33/48
#   W=24 slack=1.00            410,892   +1,381   397,473  92.62    10,614  32/48
#   W=72 slack=1.25            410,314     +803   398,135  92.77    11,580  32/48
#   W=72 slack=1.50            409,511       +0 (inert -- ceiling never binds)
#
# THE GATE IT PASSES: the ceiling is NOT vacuous. Recomputed on the realised
# plan it binds in 37.5 % of 72 h windows, and R16 -- the rim carrying the most
# starvation (4,363) -- binds in 43 of them (DO-NOT #30, verify a gate binds
# before building the exact version of it).
#
# THE GATE IT FAILS, AND IT IS THE WHOLE RESULT:
#   THE MEASURED EFFECT IS NOT ATTRIBUTABLE TO THE MECHANISM. It is greedy-
#   placement jitter, and the null control proves it. At FIXED slack 1.25,
#   varying only the window width -- a parameter with no physical meaning at
#   1 h resolution, against a 72 h shelf life:
#
#     W (h)        68     70     71     72     73     76
#     dBUILT     +826   +841   +271   +803    +60    -54
#     dstarved   -825   -854   -220   -897    -25    +30
#
#   mean +458, sd 414, range -54..+841. A ONE-HOUR change swings BUILT by 743
#   tyres and flips the sign of the starvation delta. The best single arm
#   (+803) sits inside one sd of the mean of physically equivalent settings.
#   The slack knob is no better: 1.18 -> +53, 1.20 -> +1,457, 1.22 -> -33,
#   1.25 -> +803, 1.28 -> inert. NOT MONOTONE, and moving FEWER seats (3 at
#   slack 1.22) is worse than moving more (8 at 1.20).
#
#   The direct action is tiny and the cascade is everything: at slack 1.25 the
#   ceiling moved ONE seat, and 33 campaign starts moved in response. The
#   +803 is the greedy re-settling, not 897 tyres of prevented starvation.
#
# WHAT IT COSTS WHERE IT "WINS" -- a ceiling refuses seats, so say what it
# refuses. In-month cure, base -> arm, decomposed per GT:
#     W=72 slack=1.25   gained 1,153 on  6 GTs   REFUSED   344 on  3 GTs  net +809
#     W=24 slack=1.00   gained 4,817 on 17 GTs   REFUSED 4,670 on 26 GTs  net +147
#   The W=24 arm churns 9,487 tyres of cure across 43 GTs to net 147. That is a
#   coin toss with a large variance, which is exactly what the null control says
#   it is.
#
# ROBUSTNESS (dBUILT PCR) -- survives all three baselines, and it does not
# matter, because the null control above shows the SAME arm's neighbours do not:
#     baseline                  W=24 s1.00   W=72 s1.25
#     shipped                       +1,381         +803
#     PLANNER_L7_CLOSING_BUFFER=0   +2,146         +897
#     PLANNER_LOT_INTERVAL_H=8        +950         +421
#   W=24 turns NEGATIVE on in-month on the LOT_INTERVAL_H=8 baseline (-1,019,
#   -0.23 pt) while BUILT rises -- BUILT and in-month at different tiers, which
#   fails the scoring rule on its own.
#
# WHY THE VOLUMETRIC PREMISE IS WRONG, which is the transferable lesson:
#   Month-wide, every PCR rim except R12 has feed headroom (R13 135,871 cure
#   against 158,472 feedable; R16 37,017 against 49,253). The shortfall is not
#   volume, it is CONTIGUITY -- §4au measured the eligible machines at 90-98 %
#   occupancy INSIDE the R5 band while running 70-91 % month-wide, with a
#   largest free gap of 0.47-1.83 h against a 2-4 h run. A tyres/hour ceiling
#   cannot see a hole-shape problem, so it refuses seats that were feasible and
#   leaves the fragmented ones exactly where they were. R13 is the proof: it
#   starves 2,265 tyres and the ceiling NEVER binds on it (worst excess -223).
#   Same family as DO-NOT #19 -- do not size a capacity exception off a NOMINAL
#   load table -- one layer up.
#
# The verifier's 2 pre-existing hard violations are unchanged in CLASS on both
# arms (changeover-not-reserved 12/1269 -> 11/1268; machine-day over 24 h 2 of
# 597 -> 2 of 599, worst 25.17 -> 25.75 h). No new violation class (§4am).
#
# AUGUST ONLY -- NO JULY ARM EXISTS. Even had the response been clean, a knob
# validated on one month is not validated (§4am, DAY_CAP trap 2).
# =====================================================================
FEED_CEIL_DOC = "measured 2026-08-21, jitter-dominated, ships off"
FEED_CEIL = os.environ.get("PLANNER_L5_FEED_CEIL", "0") == "1"
FEED_W_H = float(os.environ.get("PLANNER_L5_FEED_W_H", "72"))
FEED_SLACK = float(os.environ.get("PLANNER_L5_FEED_SLACK", "1.0"))
# PCR only by default: TBR has a median of 3 eligible machines per GT against
# PCR's 11 and is already at 97.9 % in-month with 1,654 starved, so there is
# almost nothing for a feed ceiling to refuse (DO-NOT #15 -- never assume a
# rule tuned on PCR transfers to TBR).
FEED_PLANTS = {x for x in os.environ.get(
    "PLANNER_L5_FEED_PLANTS", "PCR").split(",") if x}

# ---- DAILY CURE-RATE GOVERNOR (PLANNER_L5_DAY_CAP) -----------------------
# WHAT THIS DOES -- an exploratory probe, built 2026-08-19, SHIPS OFF.
#   A ceiling on TYRES CURED PER PLANT-DAY. The engine has never had one: the
#   takt governor above is a budget on concurrently SEATED PRESSES, it is
#   TBR-only by default (PLANNER_L5_TAKT_PLANTS="TBR"), and re-measured on the
#   current baseline it is INERT -- PCR is byte-identical at every takt setting.
#   So "level the plant at N tyres/day" had no implementation at all.
#
#   The cap value lives in `CONFIG.thresholds.cure_day_cap` -- caps go in
#   config.py or nowhere (PARTITION §8). 0 per plant = that plant is ungoverned.
#
# MECHANISM. A campaign delivers `rate` tyres/press-hour uniformly over its
#   span, so it charges each plant-day it touches `rate x hours-in-that-day`.
#   Before a seat is taken, the governor walks the days the campaign would
#   cover; if any in-month day would exceed the cap it jumps the start to the
#   first hour AFTER that day and retries. The ledger is charged at
#   `_takt_commit` -- the one place EVERY seat passes through (governed,
#   ungoverned, split-to-fit and backfill) -- for the same reason the takt
#   profile is charged there: a governor whose ledger misses a commit site
#   stops describing the plan.
#
# TWO ENFORCEMENT MODES, because they answer different questions.
#   soft (default when armed): if no in-cap in-month window exists the campaign
#        is placed UNGOVERNED. A shape preference must never become lost demand
#        -- the same horizon guard the takt governor carries. The leaked volume
#        is counted and printed; it is the honest measure of how far the cap is
#        from constructible.
#   strict (PLANNER_L5_DAY_CAP_STRICT=1): the campaign is DROPPED instead. This
#        is the literal "cap the day, drop the excess" instruction. It can only
#        lose tyres relative to soft; it exists to price that instruction.
#
# THE CAP IS ONLY ENFORCED ON IN-MONTH DAYS. Days in the HORIZON_TAIL_H planning
#   tail are charged to the ledger (so the printed profile describes the whole
#   plan) but never gated: the tail is not part of a "flat month", and the
#   governor is already fenced to campaigns that finish inside month_end, so it
#   cannot push volume out of the reported month to satisfy a shape preference.
# =====================================================================
# SHIPS OFF. MEASURED 2026-08-19, BOTH MONTHS, BOTH PLANTS.
# Plant instruction under test: "cure a flat 13,000/day PCR and 3,200/day TBR,
# flat from day 1, no month-end tail, and bank the surplus as next month's
# opening GT". Arms fresh via scripts/run_arm.py, gated by check_arm_fresh.py.
# July partition rebuilt for the month (cpsat_partition 2026-07, PCR OPTIMAL
# obj 1,044 in 773 s, sha1 8a47e109e27d); August ran on the stored 8bcb10c113bf.
# PLANNER_CARRY_IN was OFF on both (masters/carry_in/ does not exist).
#
#   arm         = base | cap (13,000/3,200) | floor (FLOOR_BASIS=lot)
#                 | cap+floor.  schedCV/day1 are the SCHEDULED daily cure curve
#                 from the governor's own ledger; L11 is the whole-run count.
#
#   JULY PCR   demand 397,288                     AUGUST PCR  demand 426,688
#   arm          BUILT   dBUILT in-month  ful%   arm          BUILT   dBUILT in-month  ful%
#   base       387,098       +0  381,854  96.1   base       407,125       +0  386,447  90.6
#   cap        388,519   +1,421  380,490  95.8   cap        403,689   -3,436  380,662  89.2
#   floor      383,686   -3,412  380,528  95.8   floor      404,220   -2,905  384,945  90.2
#   cap+floor  385,069   -2,029  378,765  95.3   cap+floor  401,506   -5,619  380,424  89.2
#
#   JULY TBR   demand  97,436                    AUGUST TBR  demand  98,743
#   base        95,312       +0   93,107  95.6   base        97,211       +0   94,188  95.4
#   cap         95,312       +0   93,107  95.6   cap         97,211       +0   94,188  95.4
#   floor       95,458     +146   93,643  96.1   floor       96,931     -280   94,299  95.5
#   cap+floor   95,458     +146   93,643  96.1   cap+floor   96,931     -280   94,299  95.5
#
#   shape           Jul PCR schedCV / day1        Aug PCR schedCV / day1     L11
#   base                 0.0891 /  7,813               0.0956 /  7,251     32|28 /48
#   cap                  0.0830 /  7,748               0.0832 /  7,102     32|29 /48
#   floor                0.0666 / 10,871               0.0623 / 10,705     33|30 /48
#   cap+floor            0.0531 / 10,717               0.0460 / 10,295     31|29 /48
#
# WHAT THIS ESTABLISHES, and the traps in reading it.
#
# 1. THE SHAPE IS CONSTRUCTIBLE; THE OUTPUT IS NOT. cap+floor roughly HALVES the
#    scheduled daily CV on both months (PCR 0.0891 -> 0.0531 Jul, 0.0956 ->
#    0.0460 Aug; TBR 0.0547 -> 0.0377 Jul) and lifts day 1 by ~3,000 tyres. It
#    also costs 2,029 (Jul) and 5,619 (Aug) PCR BUILT. Flatness is real and it
#    is paid for in tyres.
#
# 2. REJECTED ON MIXED SIGN. The cap ALONE is +1,421 BUILT on July PCR and
#    -3,436 on August PCR. A knob validated on one month is not validated. Had
#    only July been run this would have shipped as a free win.
#
# 3. THE CAP DOES NOT RESCUE THE FLOOR -- the hypothesis this experiment existed
#    to test. The premise was that capping the cure rate BELOW the fleet maximum
#    leaves building slack to catch up, so the day-1 release wall could finally
#    be lowered without starving presses (every previous attempt lost: WARM_
#    RELEASE -1,319, T0_STOCK_BASIS lot -1,670, FLOOR_BASIS min/slice -2,478..
#    -6,050, CHG_PARALLEL -3,665). MEASURED, THE TWO EFFECTS ARE ADDITIVE:
#        July  cap +1,421, floor -3,412, predicted -1,991, measured -2,029  (-38)
#        Aug   cap -3,436, floor -2,905, predicted -6,341, measured -5,619 (+722)
#    An interaction of -0.01 % / +0.18 % of BUILT, with the sign flipping. There
#    is no coupling. The wall protects BUILDING'S LEAD TIME, and a ceiling on
#    the cure RATE does not shorten a lead TIME.
#
# 4. THE CAP CANNOT DELIVER "FLAT FROM DAY 1" IN PRINCIPLE. It is a ceiling. Day
#    1 sits at 7,813 (Jul) / 7,251 (Aug) because of the release floor, and no
#    ceiling raises a floor. Only FLOOR_BASIS moves day 1, and even at the
#    physical 2.35 h lot floor it reaches 10,717 / 10,295 -- still 18-21 % short
#    of the requested 13,000, the remainder being day-1 mould changes.
#
# 5. THE FLATNESS IS BOUGHT WITH THE MONTH-END TAIL THE INSTRUCTION FORBIDS.
#    Campaigns crossing month end, July PCR: base 134 (54,195 tyres) -> cap 142
#    (61,059) -> a 12,300/day arm 163 (73,876). The governor levels by pushing
#    seats right, and right eventually means out of the month: July PCR tail
#    10,339 -> 13,044 (cap) -> 20,480 (12,300/day). "Flat" and "no tail" are
#    opposed under this mechanism, not complementary.
#
# 6. THE REQUESTED NUMBERS SIT ON THE WRONG SIDE OF THE PLANT'S OWN CEILING.
#    L5 seats July PCR in 63,499 press-h of 63,984 available = 99.2 % press
#    utilisation, and the days that are already full sit at 13,228/day. 13,000
#    is 98.3 % of that, so the cap binds on only 2 seats and the "13,000 x 31 =
#    403,000" premise assumes a day-1 the plant does not have. TBR is the
#    opposite: its plateau is 3,186/day, so a 3,200 cap NEVER BINDS -- both TBR
#    columns above are byte-identical to base, which is the honest reading of
#    "TBR is inert", not evidence the cap is safe. The originally requested TBR
#    3,000/day WOULD bind and was measured separately: BUILT -8 but in-month
#    93,107 -> 90,077 and tail 3,522 -> 6,398, i.e. it relocates ~3,000 tyres
#    into next month rather than destroying them. 3,000 x 31 = 93,000 against
#    97,436 of demand -- the cap is below the requirement by construction.
#
# 7. R5 IS THE SAFETY COST. July PCR GT wait max 62.3 h (base) -> 68.4 h (cap)
#    -> 70.2 h (cap+floor) against the hard 72 h. A flatter cure curve holds
#    green tyres longer. GT inventory stayed inside the rail on every arm
#    (PCR daily-mean max 4,557-4,669 vs 4,800; TBR 1,311-1,327 vs 1,400).
#
# Ledger class: PARTITION_AND_CHANGEOVER.md §4y (this experiment), §4b (the
# daily BUILD quota, rejected on the same shape of evidence), §1 (a rate the
# plant states is not a rate the plant can hold on day 1).
# The flag and the cap dict are KEPT, defaulted off. Deleting them destroys the
# evidence and invites a blind re-run. Re-measure only if the day-1 release wall
# is ever solved on its own terms -- per-GT tau_min + time-until-building-
# reaches-this-GT -- because THAT is what binds, not the rate.
# =====================================================================
DAY_CAP_ON = os.environ.get("PLANNER_L5_DAY_CAP", "0") == "1"
DAY_CAP_STRICT = os.environ.get("PLANNER_L5_DAY_CAP_STRICT", "0") == "1"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", default="2026-07")
    ap.add_argument("--out", default=None, help="run directory name")
    a = ap.parse_args()

    holiday.load(a.month)                 # rule G3; empty = every call is identity
    global _DELAY, _SPLIT
    _DELAY = _load_delay(a.month)
    _SPLIT = _load_split(a.month)
    if _DELAY:
        print(f"  [l5<->l6] {len(_DELAY)} GTs carry a reseat delay from L6 "
              f"(max {max(_DELAY.values()):.1f} h)")
    if _SPLIT:
        print(f"  [l5<->l6] {len(_SPLIT)} GTs carry a SPLIT request from L6 "
              f"(max factor {max(_SPLIT.values())}); floor-clamped per GT")

    P = json.loads(sorted(PARAMS.glob("params_*.json"))[-1].read_text())
    lots = pl.read_parquet(D / f"l45_lots_{a.month}.parquet").filter(
        pl.col("n_lots") > 0)
    # PRESS PLATEN WINDOW IS NOT ENFORCED -- REMOVED 2026-08-11 by instruction.
    # The underlying master is unusable: press_platen_master.rim_lo/rim_hi
    # disagrees with the plant's own press_class_pcr (45 in recorded 14-20 where
    # the plant states 12-16; the 46 in class invented outright), and the plant's
    # allowed_press_matrix explicitly permits every pair the window rejected --
    # 57 of 61 as `direct`, i.e. observed in real production. Two plant masters
    # contradict each other, so press eligibility comes from allowed_press_matrix
    # (already clean, 0 violations) and NOT from platen geometry.
    press = pl.read_parquet(D / f"cap_press_{a.month}.parquet")
    # ...AND THE CODE ABOVE DID NOT DO WHAT THAT COMMENT SAYS.
    # cap_press_<M> is MINED -- it lists (gt, press) pairs observed in the MES
    # window. allowed_press_matrix is the PLANT'S OWN file and is broader: it
    # holds 92 PCR presses against cap_press's 86. PCR presses 17-22 are
    # physically present (132 tyres/day each), marked `direct` by the plant for
    # GT 1402 XPC TATA and GT 1412 XPC MM, and were invisible to the planner.
    #
    # This is the exact converse of the v9 building-machine fix. There the plant
    # matrix was NARROWER than mined capability, so it became a hard RESTRICT.
    # Here it is BROADER, so it must be a UNION -- and nobody checked that
    # direction, because the press gate was verified only for violations
    # ("0 violations, clean") and never for omissions. A gate can be clean and
    # still be costing you output.
    #
    # R3 still binds: concurrent presses <= mould count, enforced downstream.
    # Verified there is real headroom, so this is not a no-op:
    #     GT 1402  moulds 20, engine used 12    GT 1412  moulds 6, engine used 3
    #
    # SHIPS OFF. MEASURED 2026-08-13 -- AND THE HEADLINE NUMBER IS A TRAP.
    #
    #                    BUILT     in-month          tail    TOTAL REAL OUTPUT
    #   Aug off        410,652   392,239 (91.4%)   23,381    415,620 (96.8%)
    #   Aug ON         408,220   406,830 (94.8%)    6,616    413,446 (96.3%)
    #   Jul off        385,084   383,266 (96.2%)    6,372    389,638 (97.8%)
    #   Jul ON         378,838   382,070 (95.9%)    1,290    383,360 (96.2%)
    #
    # !! SAME 2026-08-13 HARNESS AS THE SWEEP BELOW -- the `in-month`, `%` and
    # !! `TOTAL REAL OUTPUT` columns here carry the SAME wrong-denominator defect,
    # !! and the BUILT absolutes the same OPENING_STOCK inclusion. See the caveat
    # !! block on the sweep further down for the mechanism and the magnitudes.
    # !! The DIFFERENCES (-2,432 Aug / -6,246 Jul BUILT) are unaffected, and the
    # !! conclusion below rests on those, not on the absolutes.
    #
    # Reading only in-month, this looks like +3.4 pt on August and the biggest
    # win of the project. It is not. The union BUILDS FEWER TYRES on both months
    # (-2,432 Aug, -6,246 Jul) and total real output FALLS on both. All of the
    # apparent August gain is the carry-out tail moving inside the horizon:
    # 23,381 -> 6,616. Cures happen earlier, not more.
    #
    # Mechanism for the loss: more eligible presses => more parallel campaigns
    # => building must feed more GTs at once => same-size share 75.2% -> 69.5%
    # (an L11 invariant flips PASS->FAIL) and weighted changeover 99.8 -> 107.2
    # min/machine-day. The extra press seats cost more in setup than they return.
    #
    # LESSON, and it generalises past this flag: IN-MONTH FULFILMENT IS
    # TAIL-SENSITIVE. Any change that pulls cures earlier inflates it without
    # producing anything. Always report BUILT and in-month+tail alongside it --
    # a change that moves in-month while BUILT falls is relocating output, not
    # creating it. This is the sixth measurement defect found in this project
    # and the first that would have flattered a KPI rather than a report.
    #
    # The underlying master defect is REAL and still stands: cap_press omits 6
    # plant-sanctioned presses. It is simply not worth using them. If August
    # capacity gets tighter, re-measure -- but grade it on BUILT, not in-month.
    #
    # FULL AUGUST SWEEP, 2026-08-13, chasing ">95 % in-month" (scripts/
    # arm_scorecard.py). NOTHING REACHES IT, and every arm that lifts in-month
    # cuts BUILT. The in-month ceiling on August PCR is ~94.8 %.
    #
    # !! CAVEAT ADDED 2026-08-19 -- THE `in-month` AND `ful%` COLUMNS BELOW ARE
    # !! WRONG. READ ONLY `dBUILT`. This sweep ran on 2026-08-13; the harness
    # !! that produced it was fixed on 2026-08-14, i.e. AFTER. See the block at
    # !! `scripts/arm_scorecard.py` ("IN-MONTH IS READ, NOT RECONSTRUCTED").
    # !!
    # !!   `in-month` was reconstructed as `L11_pct / 100 * demand`. L11 computes
    # !!     fed / GROSS_BUILD, and gross_build = demand - opening cover + yield
    # !!     uplift, so the rebase is onto the WRONG DENOMINATOR. The error is
    # !!     ~1 % and ITS SIGN FLIPS BY PLANT (opening cover dominates PCR, yield
    # !!     uplift TBR): measured Jul PCR +3,708 / TBR -538, Aug PCR +2,444 /
    # !!     TBR -749. Every absolute in-month figure quoted from this script
    # !!     before 2026-08-14 is overstated on PCR and understated on TBR.
    # !!   `ful%` is L11's own percentage verbatim, i.e. on the GROSS_BUILD
    # !!     basis -- not the demand basis this column's neighbours imply and not
    # !!     what the same column prints today (the fixed script divides the true
    # !!     `qty_fed_in_month` by `demand`). The two are not comparable.
    # !!   The `BUILT` ABSOLUTES also include the `OPENING_STOCK` pseudo-machine,
    # !!     which is not production -- same script, same date, ~3.8 k PCR /
    # !!     ~1.0 k TBR overstated (that exclusion also landed after this sweep).
    # !!
    # !! `dBUILT` SURVIVES ALL THREE. Both the opening-stock rows and the
    # !! denominator ratio are IDENTICAL across arms, so they cancel in a
    # !! difference. The sweep's CONCLUSION is therefore intact and is not
    # !! restated here on trust: every arm that lifts in-month still cuts BUILT,
    # !! and that is read from `dBUILT` alone. The table is kept, not deleted --
    # !! deleting it destroys the evidence and invites a blind re-run.
    # !! Ledger class: PARTITION_AND_CHANGEOVER.md §1 (measurement basis), sixth
    # !! instance. VERSION republishes the same bad absolutes and is caveated
    # !! there too.
    #
    #   arm                       BUILT    dBUILT   in-month   ful%    L11
    #   base                    410,652        +0    392,239   91.4   27/42
    #   HORIZON_MODE=strict     380,080   -30,572    384,944   89.7   25/42
    #   HORIZON_MODE=truncate   384,156   -26,496    388,806   90.6   26/42
    #   HORIZON_MODE=window     393,182   -17,470    389,665   90.8   27/42
    #   HORIZON_TAIL_H=48       403,762    -6,890    391,810   91.3   26/42
    #   HORIZON_TAIL_H=24       393,800   -16,852    391,381   91.2   26/42
    #   PRESS_FROM_MATRIX=1     408,220    -2,432    406,830   94.8   26/42
    #   TAIL_H=24 + PRESS       404,061    -6,591    405,543   94.5   24/42
    #   TAIL_H=0  + PRESS       399,316   -11,336    404,256   94.2   25/42
    #
    #   L5_TAKT=off             410,652        +0    392,239   91.4   27/42  <- INERT on PCR
    #   L5_ALPHA=1.2 / 0.8      410,652        +0    392,239   91.4   27/42  <- INERT
    #   L5_TAKT_PART=0          410,652        +0    392,239   91.4   27/42  <- INERT
    #   PRESS + L7_MAKEROOM=1   408,220    -2,432    406,830   94.8   26/42  <- makeroom is already on
    #   PRESS + SLIVER_PCR=0    408,564    -2,088    406,830   94.8   25/42  <- recovers 344, costs an invariant
    #
    # The takt governor is BYTE-IDENTICAL on PCR at every setting -- it is not a
    # lever on this month. The union's BUILT loss is NOT recoverable: makeroom is
    # already default-on, and anti-sliver off returns 344 of the 2,432 while
    # costing an L11 invariant.
    #
    # THE TWO CEILINGS, and they are arithmetic, not algorithmic:
    #   in-month can never exceed BUILT. So for August 2026 --
    #     PCR   BUILT 410,652 = 95.7 % of demand -> in-month ceiling 95.7 %
    #     TBR   BUILT  92,541 = 93.5 % of demand -> in-month ceiling 93.5 %
    #   TBR THEREFORE CANNOT REACH 95 % IN-MONTH BY ANY SCHEDULING CHANGE.
    #   Its build side is 96 % floor-blocked (6,100 of 6,330 unfed tyres are
    #   `would breach min_lot`), and PLANNER_STRICT_LOT_FLOOR=1 force-disables
    #   the plant-calibrated sub-floor budget that exists for exactly this case.
    #   Raising TBR is a B12 PLANT RULING, not an engineering task.
    #
    # Do not re-run this sweep expecting a different answer. August in-month is
    # bounded by the fact that ~14,400 tyres are BUILT after 1 Sep 07:00 under
    # the default 72 h extension; shortening the extension does not move them
    # inside the month, it just deletes them (BUILT falls faster than in-month
    # rises). The only honest routes above 95 % are the horizon RULING
    # (in-month + tail = 96.8 % today) and the B12 sub-floor ruling on TBR.
    if os.environ.get("PLANNER_PRESS_FROM_MATRIX", "0") != "0":
        apm = pl.read_parquet(D / "allowed_press_matrix.parquet")
        add = (apm.select("plant", "gt_code", "press",
                          pl.lit("plant_matrix").alias("basis"))
                  .join(press.select("plant", "gt_code", "press"),
                        on=["plant", "gt_code", "press"], how="anti")
                  .join(press.select("plant", "gt_code").unique(),
                        on=["plant", "gt_code"], how="semi"))
        if add.height:
            press = pl.concat([press, add.select(press.columns)], how="vertical")
            print(f"  press eligibility: +{add.height} (gt, press) pairs from "
                  f"the plant matrix that cap_press_{a.month} omitted "
                  f"({add['press'].n_unique()} distinct presses, "
                  f"{add['gt_code'].n_unique()} GTs)")
    mould = pl.read_parquet(D / f"cap_mould_{a.month}.parquet")
    cav = pl.read_parquet(D / "l3_cavities.parquet")
    pmc = pl.read_parquet(D / "press_mould_change.parquet")

    cav_p = {p: float(cav.filter(pl.col("plant") == p)["cavities"].median())
             for p in ["PCR", "TBR"]}
    cyc_p = {p: float(cav.filter(pl.col("plant") == p)["cycle_s"].median())
             for p in ["PCR", "TBR"]}
    mch_p = {p: float(pmc.filter(pl.col("plant") == p)["mould_change_min"].median())
             * 60.0 for p in ["PCR", "TBR"]}          # seconds
    # Per-press mould-change time, resolved through the wcID bridge (165 of 165
    # presses match). PCR presses span 210-430 min around a 360 median; TBR is a
    # single 361 for every press, so this is a PCR correctness fix in practice.
    #
    # THIS TABLE WAS LOADED AND NEVER USED. The reservation below charged the
    # plant MEDIAN, so every press whose real change is longer than the median
    # had its next campaign started before the mould was out: 28 August events
    # under-reserved by up to 70 min and physically over-ran a still-curing
    # press. An OVERLAP check is not a FEASIBILITY check (MEMORY §12) -- the
    # resource has to be reserved for the time it is actually consumed, and the
    # time it is actually consumed is per press.
    mch_press = {r["wc_id"]: float(r["mould_change_min"])
                 for r in pmc.iter_rows(named=True) if r.get("wc_id")}

    def mchg_s(plant: str, press: str) -> float:
        """Mould-change reservation for THIS press, seconds. Falls back to the
        plant median only when the press is missing from the master."""
        v = mch_press.get(str(press))
        return (v * 60.0) if v is not None else mch_p[plant]
    # R3 CONCURRENCY. `moulds` counts mould HALVES -- the plant ruling is 2 per
    # press (LH + RH, one cycle, two tyres). This dict feeds ALL THREE R3 sites
    # in this module (placement, the split/repair pass, the self-check) and the
    # divisor is applied exactly once, in r3_cap, which l45 and l11 also read.
    # Ships at DIV = 1.0 -- `moulds` unchanged, byte-identical.
    moulds = r3_cap.table(mould.iter_rows(named=True))
    print(r3_cap.banner())
    if r3_cap.R3_DIV > 1.0 or r3_cap.R3_OBS:
        _raw = {(r["plant"], r["gt_code"]): max(int(r["moulds"]), 1)
                for r in mould.iter_rows(named=True)}
        for _p in ("PCR", "TBR"):
            _cut = [(k[1], _raw[k], moulds[k]) for k in _raw
                    if k[0] == _p and moulds[k] < _raw[k]]
            print(f"    {_p}: {len(_cut)} of "
                  f"{sum(1 for k in _raw if k[0] == _p)} GTs capped lower "
                  f"(e.g. " + ", ".join(f"{g} {a}->{b}" for g, a, b in _cut[:3])
                  + ")")
    elig: dict[tuple, list[str]] = {}
    for r in press.iter_rows(named=True):
        elig.setdefault((r["plant"], r["gt_code"]), []).append(r["press"])
    for k in elig:
        elig[k] = sorted(set(elig[k]))

    y, m = int(a.month[:4]), int(a.month[5:7])
    t0 = datetime(y, m, 1, 7, 0)                      # plant day starts 07:00

    # A PRESS CANNOT CURE BEFORE ITS GT CAN EXIST.
    # L5 previously started all 165 presses at t0. To feed a press curing at t0,
    # building must FINISH at t0 - tau*, which is before the horizon begins -- so
    # L7 could not release those slices and 87% of its starved volume sat on day
    # one. Opening stock covers only the GTs whose first campaign falls inside
    # its remaining shelf life, which is a minority.
    #
    # The honest constraint: a GT with opening stock may cure from t0; any other
    # must wait tau* plus the time to build its first slice. That delay is what a
    # cold start actually costs, and paying it here is cheaper than discovering
    # it as unbuildable demand two layers later.
    ogf = _opening_gt_path(ROOT, a.month)
    have_stock: set[tuple] = set()
    if ogf.exists():
        _og = pl.read_parquet(ogf)
        have_stock = {(r["plant"], r["gt_code"]) for r in
                      _og.select(["plant", "gt_code"]).unique().iter_rows(named=True)}
    P0 = json.loads(sorted(PARAMS.glob("params_*.json"))[-1].read_text())
    tau_h = {p: float(P0["tau"][p]["tau_star_h"]) for p in ["PCR", "TBR"]}
    tau_min_h = {p: float(P0["tau"][p]["tau_min_h"]) for p in ["PCR", "TBR"]}
    bband = {p: float(P0["campaign_bands"][p]["build"]["hours_p50"])
             for p in ["PCR", "TBR"]}
    _cadf = pl.read_parquet(PARAMS / P0["tables"]["build_cadence"])
    plant_cad_s = {p: float(_cadf.filter(pl.col("plant") == p)["cadence_s_p50"].median())
                   for p in ["PCR", "TBR"]}

    # QUANTITY-BOUNDED opening-stock budget, in tyres, per (plant, GT).
    # The flat floor below blocks EVERY press for tau* + build_band -- 11.86 h on
    # PCR, 10.18 on TBR -- which is exactly 49%/42% of day one and reproduces the
    # measured day-1 press utilisation of 51%/58% to the point. That is 1,020 +
    # 804 press-hours blocked, while 4,820 + 1,297 usable green tyres (1,519
    # press-hours) sit on the floor at t0, already built.
    #
    # An earlier attempt exempted the whole GT and failed, because stock covers
    # only a campaign's FIRST slices and the rest still need building. This is
    # bounded BY QUANTITY: a GT may cure from t0 for as many tyres as it actually
    # holds, and pays the full floor once that budget is spent.
    PROTECT_MOULD = os.environ.get("PLANNER_L5_PROTECT_MOULD", "0") == "1"
    early_budget: dict[tuple, float] = {}
    # Per-(plant, GT) cover actually required by earliest_cure's t0 test, so the
    # debit below charges the same currency the test priced. SOLUTIONS S8.
    _gapq_of: dict[tuple, float] = {}
    # Remaining shelf life of each GT's opening stock, hours from t0. DERIVED AT
    # RUN TIME FROM THE STOCK'S OWN age_h -- never a mined constant (PARTITION
    # §1). The MEDIAN age is used because that is the wall L7 actually applies
    # (`opening_life` in l7_pull_release), and pricing a preference in a
    # different currency from the consumer that spends it is how the `gap_q`
    # debit came to over-charge by 5.4x (see `gap_q_of` below).
    _stock_life: dict[tuple, float] = {}
    if ogf.exists():
        for r in (pl.read_parquet(ogf).filter(pl.col("age_h") <= GT_SHELF_LIFE_H)
                  .group_by(["plant", "gt_code"])
                  .agg(pl.len().alias("len"),
                       pl.col("age_h").median().alias("_amed"))
                  .iter_rows(named=True)):
            early_budget[(r["plant"], r["gt_code"])] = float(r["len"])
            _stock_life[(r["plant"], r["gt_code"])] = max(
                0.0, GT_SHELF_LIFE_H - float(r["_amed"]))
    # Tyres of stock needed to bridge the floor on ONE press: the press draws
    # `rate` tyres/h and must last `tau* + band` hours until fresh supply lands.
    gap_tyres = {p: (tau_h[p] + bband[p]) * (cav_p[p] * 3600.0 / cyc_p[p])
                 for p in ("PCR", "TBR")}
    # Draw rate of ONE press, tyres/h -- the denominator that turns a shortfall
    # in TYRES into a wait in HOURS for the partial-credit branch below.
    rate_p = {p: cav_p[p] * 3600.0 / cyc_p[p] for p in ("PCR", "TBR")}
    gap_h_p = {p: tau_h[p] + bband[p] for p in ("PCR", "TBR")}

    # PLANT CURE TIMES -- per GT, replacing the plant-median press rate.
    # `cav_p x 3600/cyc_p` is one number for every GT on a plant; the plant's own
    # file gives 10.0-20.0 min on PCR and 44-57 on TBR, so a campaign's press
    # hours were previously wrong by up to +/-25 % per GT while summing to
    # roughly the right plant total. `rate_of` keeps the old value as the
    # fallback for any GT the plant file does not name, so coverage gaps degrade
    # rather than crash. See planner/cmbc/plant_ct.py for the cavity count, the
    # measured load/unload adder and why availability belongs here.
    # Press availability multiplies every cure rate. Mined value is 0.8897 on
    # PCR (mtbf 106.8 h / mttr 13.2 h over 4,267 down events), i.e. 11 % of press
    # time is assumed lost to breakdowns and is priced INTO the rate rather than
    # shown as idle hours. PLANNER_PRESS_AVAIL overrides both plants -- 1.0 means
    # "presses never break down", which is a planning assumption, not a fact.
    # PRESS AVAILABILITY IS NO LONGER APPLIED BY DEFAULT. PLANT INSTRUCTION,
    # 2026-08-19. The mined MTBF/MTTR haircut (PCR 0.8897, TBR 0.8282) was never
    # a plant ruling: L0 mined it with "press gap > 4.0 h counted as down", and
    # it reached every cure rate as a flat per-plant multiplier via
    # `plant_ct.press_rate`. It appears in NONE of the five ledger documents and
    # `ROADMAP.md` GAP-7 still reads "not yet done" while the code shipped it.
    #
    # WHAT IT COST, AND WHY THAT NUMBER IS NOT THE WHOLE STORY. Measured
    # 2026-08-19, fresh arms via run_arm.py, both months, both plants:
    #
    #                 in-month            BUILT        starved
    #   Jul PCR   95.85 -> 96.26 %      -7,859        +8,057
    #   Jul TBR   94.99 -> 99.16 %      +1,078          -723
    #   Aug PCR   89.71 -> 93.95 %      -4,891       +12,796
    #   Aug TBR   94.75 -> 97.59 %        -391          +663
    #
    # In-month rises on all four cells; BUILT FALLS on three. On PCR the gain is
    # the tail moving inside the horizon (Aug tail 33,147 -> 22,860), not tyres
    # created -- the tail-sensitivity trap this file documents elsewhere. The
    # mechanism is measured: a faster cure rate does not shorten BUILDING's lead
    # time, so presses seat earlier and run dry (Aug PCR starvation 7,486 ->
    # 20,282). TBR is the opposite case and gains honestly on July.
    #
    # It ships OFF anyway, by instruction. `PLANNER_PRESS_AVAIL` still overrides,
    # so `PLANNER_PRESS_AVAIL=0.8897` restores the old PCR behaviour for an A/B,
    # and a per-plant dict is the obvious next step if the plant wants TBR-only.
    # RESOLVED IN ONE PLACE -- `plant_ct.PRESS_AVAIL`. This site used to re-read
    # PLANNER_PRESS_AVAIL itself and L4.5 re-read it a second time; two copies of
    # a rate input is how L4.5 came to size lots against a slower press than L5
    # seats them on. `PLANNER_PRESS_AVAIL` still overrides both plants;
    # `PLANNER_PRESS_AVAIL_PCR` / `_TBR` move one plant alone. Ships 1.0 / 1.0.
    _pav = dict(plant_ct.PRESS_AVAIL)
    PCT = plant_ct.get(_pav)
    print("  " + PCT.summary())

    # EVERY PRESS HAS 2 CAVITIES AND CURES 2 TYRES PER CYCLE, BOTH PLANTS.
    # Plant ruling, 2026-08-19. `plant_ct.CAVITIES = 2.0` already honours it and
    # covers 100 % of PCR volume (73/73 GTs) and 96.1 % of TBR (31/37 GTs,
    # 95,159 of 99,019 tyres on 2026-08), so the primary path was never wrong.
    #
    # THE FALLBACK WAS. `rate_p = cav_p x 3600/cyc_p` reads `l3_cavities`, whose
    # `cavities` is NOT a physical count -- `_offline/l3_ceiling.py` derives it as
    # `observed tyres_per_day / theoretical cycles_per_day`, i.e. physical slots
    # x uptime (PCR 3.40/4 = 84.9 %, TBR 2.41/3 = 80.5 %), paired with the
    # OBSERVED DWELL `cycle_s`. It is a different parameterisation of the same
    # throughput, not a second opinion on cavity count.
    #
    # So do NOT "fix" l3_cavities to 2 -- `2 x 3600/1920` halves the rate and is
    # wrong. Fall back instead to the plant-median of the rates plant_ct itself
    # produces, which are built on 2 cavities by construction. One cavity number
    # in the engine, and it is the plant's.
    for _p in ("PCR", "TBR"):
        _vals = sorted(
            v for (pp, gg) in (PCT._c.keys() if getattr(PCT, "ok", False) else [])
            if pp == _p for v in (PCT.press_rate(pp, gg),) if v)
        if _vals:
            _old = rate_p[_p]
            rate_p[_p] = _vals[len(_vals) // 2]
            gap_tyres[_p] = gap_h_p[_p] * rate_p[_p]
            print(f"  [cavities=2] {_p} fallback rate {_old:.3f} -> "
                  f"{rate_p[_p]:.3f} t/press-h (plant_ct median over "
                  f"{len(_vals)} GTs, 2 cavities)")

    def rate_of(p: str, gt: str) -> float:
        """Tyres per press-hour for ONE press running this GT.

        2 cavities, always, both plants -- see the plant ruling above.
        """
        r = PCT.press_rate(p, gt)
        return r if r else rate_p[p]

    # ---- CURE-TIME COVERAGE, PRINTED, NEVER SILENT ----------------------
    # A GT the plant's cure-time workbook does not name falls back to
    # `rate_p[p]`, the plant MEDIAN over the GTs it does name. That fallback is
    # correct -- a crash would be worse -- but it is invisible, and an invisible
    # plant median standing in for a per-GT figure is exactly how tau* and
    # min_lot got wired in (PARTITION §1). So say which GTs get one, and how many
    # tyres ride on it, every run.
    _nocov: dict[str, list[tuple[str, float]]] = {"PCR": [], "TBR": []}
    for _r in pl.read_parquet(
            D / f"net_requirement_{a.month}.parquet").iter_rows(named=True):
        if _r.get("residual"):
            continue
        if PCT.press_rate(_r["plant"], _r["gt_code"]) is None:
            _nocov[_r["plant"]].append((_r["gt_code"],
                                        float(_r.get("cure_requirement") or 0)))
    for _p in ("PCR", "TBR"):
        _miss = sorted(_nocov[_p], key=lambda x: -x[1])
        if _miss:
            print(f"  [cure CT] {_p}: {len(_miss)} demanded GTs have NO plant "
                  f"cure time -> plant-median fallback {rate_p[_p]:.3f} "
                  f"t/press-h for {sum(q for _g, q in _miss):,.0f} tyres  "
                  + ", ".join(f"{g} ({q:,.0f})" for g, q in _miss[:4]))
        else:
            print(f"  [cure CT] {_p}: every demanded GT has a plant cure time")

    def gap_q_of(p: str, gt: str) -> float:
        """Tyres of opening stock that buy this GT ONE t0 press seat.

        ONE DEFINITION, TWO READERS. `earliest_cure` uses it to decide whether a
        campaign may cure from t0; PLANNER_L5_STOCK_FIRST uses it to decide how
        many t0 seats the stock can pay for, which is what bounds the promotion.
        They MUST price the same currency. The debit site already got this wrong
        once -- it charged the plant median `gap_tyres[p]` for cover the test had
        priced per-GT, a 5.4x over-charge that emptied the budget after ~8
        presses instead of ~42 (see `_gapq_of` below). A second copy of this
        formula is that defect waiting to be rewritten, so there is exactly one.
        """
        _b = os.environ.get("PLANNER_T0_STOCK_BASIS", "star")
        if _b == "star":
            _h = tau_h[p] + bband[p]
        else:
            _fl = float(CONFIG.thresholds.min_lot_units.get(p, 0) or 0)
            _ct = PCT.build_ct_s(p, gt, None) or plant_cad_s[p]
            _h = tau_min_h[p] + _fl * _ct / 3600.0
        return _h * rate_of(p, gt)

    # ---- BUILD-ORDER STAGGER: when can building actually REACH this GT? ----
    # Measured July day 1: building touches 9 GTs in the first 2 h, 15 by 8 h,
    # 19 by 12 h, 28 by 24 h -- because 11 machines each build ONE GT at a time
    # and the B12 floor makes every run at least 150 tyres (2.08 h on PCR).
    #
    # The flat tau* + band floor ignores this completely: it makes the GT whose
    # machine starts it at minute 0 wait 11.86 h, and releases the GT that
    # building cannot reach until hour 10 at the same moment. Both are wrong,
    # in opposite directions.
    #
    # The physical wait is: tau_min + (how many GTs are queued ahead of me on my
    # own machine) x one legal run. So GTs are ranked within each machine by
    # volume -- biggest first, which is the order L7 releases them in -- and the
    # k-th GT on a machine becomes curable at tau_min + (k+1) x lot_build_h.
    # First GT per machine -> 2.35 h, second -> 4.43 h, third -> 6.51 h ...
    #
    # This asserts NO pre-month building. It only replaces one plant-wide median
    # with each GT's own place in the build queue.
    # ---- WHICH GTs CAN BUILDING ACTUALLY START AT t0? --------------------
    # One machine builds one GT at a time, so the number of GTs that can be fed
    # from minute zero is bounded by the machine count, not by press count. Walk
    # GTs by volume, claim an allowable machine for each, stop when they run out.
    # Everything claimed gets tau_min in `earliest_cure`; the rest keep the wall.
    _feed_early: set = set()
    _early_cap: dict = {}
    if FLOOR_BASIS == "feed":
        _cmf2 = D / f"cap_machine_{a.month}.parquet"
        if _cmf2.exists():
            _cm2 = pl.read_parquet(_cmf2)
            try:
                _cm2 = allowable.restrict(_cm2, quiet=True)
                _cm2 = allowable.restrict_rimlock(_cm2, quiet=True)
            except Exception:                                    # noqa: BLE001
                pass
            _elig2: dict = {}
            for _r in _cm2.iter_rows(named=True):
                _elig2.setdefault((_r["plant"], _r["gt_code"]), []).append(_r["machine"])
            _vol2 = {(r["plant"], r["gt_code"]): float(r["qty"])
                     for r in lots.group_by(["plant", "gt_code"]).agg(
                         (pl.col("lot_qty") * pl.col("n_lots")).sum().alias("qty")
                     ).iter_rows(named=True)}
            # ---- THE CAP IS PRESSES, NOT GTs. Measured 2026-08-20. -----------
            # Claiming one machine per GT limited the GT count to 11 and the
            # PRESS count barely moved (47 -> 44), because one GT opens as many
            # presses as it has moulds. A 20-mould GT on one machine draws
            # 20 x 7.2 = 144 tyres/h against the ~60 tyres/h that machine makes,
            # so it starves by arithmetic. BUILT still fell 5,105 on August PCR.
            #
            # So bound each early GT to the presses its OWN machines can feed:
            #     presses_feedable = machines x (machine tyres/h) / (press tyres/h)
            # and hand that number to the seating loop as a hard concurrency cap
            # for t0. This is the feed balance, not a preference.
            _mrate = {q: 3600.0 / (plant_cad_s[q] or 60.0) for q in ("PCR", "TBR")}
            for _p2 in ("PCR", "TBR"):
                _taken: set = set()
                _cands = sorted((k for k in _vol2 if k[0] == _p2),
                                key=lambda k: (-_vol2[k], k[1]))
                _pr = max(rate_p[_p2], 1e-9)
                # ONE machine per GT, round-robin by volume. Claiming every
                # eligible machine for the first GT gave 4 GTs and a 81-press cap
                # -- larger than the plant. One machine each spreads the early
                # release across the GTs that will actually be seated first.
                for _k2 in _cands:
                    _mine = [m for m in sorted(_elig2.get(_k2, [])) if m not in _taken]
                    if not _mine:
                        continue
                    _taken.add(_mine[0])
                    _feed_early.add(_k2)
                    _early_cap[_k2] = max(1, int(_mrate[_p2] / _pr))
                _ng = sum(1 for k in _feed_early if k[0] == _p2)
                _np = sum(v for k, v in _early_cap.items() if k[0] == _p2)
                print(f"  [feed] {_p2}: {len(_taken)} machines -> {_ng} GTs at tau_min "
                      f"({tau_min_h[_p2]*60:.0f} min), capped at {_np} concurrent "
                      f"presses total ({_mrate[_p2]:.0f} build vs {_pr:.1f} cure tyres/h)")
    _stag: dict[tuple, float] = {}
    if FLOOR_BASIS == "stagger":
        _cmf = D / f"cap_machine_{a.month}.parquet"
        if _cmf.exists():
            _cm = pl.read_parquet(_cmf)
            try:
                from planner.cmbc import allowable as _alw
                _cm = _alw.restrict(_cm, quiet=True)
                _cm = _alw.restrict_rimlock(_cm, quiet=True)
            except Exception:
                pass
            _vol = {(r["plant"], r["gt_code"]): float(r["qty"])
                    for r in lots.group_by(["plant", "gt_code"]).agg(
                        (pl.col("lot_qty") * pl.col("n_lots")).sum().alias("qty")
                    ).iter_rows(named=True)}
            _bymc: dict[tuple, list] = {}
            _seen: set = set()
            for r in _cm.sort("penalty").iter_rows(named=True):
                k = (r["plant"], r["gt_code"])
                if k in _seen:
                    continue
                _seen.add(k)
                _bymc.setdefault((r["plant"], r["machine"]), []).append(k)
            for _mk, _gts in _bymc.items():
                _gts.sort(key=lambda k: -_vol.get(k, 0.0))
                for _i, _k in enumerate(_gts):
                    _stag[_k] = float(_i)
            print(f"  [stagger] {len(_stag)} GTs ranked in their machine's build "
                  f"queue; first per machine curable at tau_min + 1 lot")

    # ---- WARM MACHINES AT t0 --------------------------------------------
    # `machine_warm_<month>.parquet` (scripts/derive_machine_warm.py) names the
    # GT each building machine was running at 06:59, recovered from the build
    # timestamps of the opening stock: the tyres built in the final hour before
    # t0 fall on exactly 11 GTs for PCR's 11 machines and 10 for TBR's 9.
    #
    # For those GTs supply is REAL from minute one -- the machine is threaded,
    # staffed and mid-run -- so their presses need not wait tau* + build_band.
    # Every other GT keeps the full floor, which is what distinguishes this from
    # the six global floor experiments (lot / stagger / slice / min / warm-open /
    # feed-condition): those released ALL presses early, so presses started and
    # starved. Here only the ~11 GTs per plant whose supply genuinely exists at
    # t0 are released.
    #
    # No pre-month production is claimed: these tyres are already counted in the
    # opening stock.
    _warm: set = set()
    if os.environ.get("PLANNER_MACHINE_WARM", "1") != "0":
        _wmf = D / f"machine_warm_{a.month}.parquet"
        if _wmf.exists():
            for _r in pl.read_parquet(_wmf).iter_rows(named=True):
                _warm.add((_r["plant"], _r["gt_code"]))
            print(f"  [warm-mc] {len(_warm)} (plant, GT) pairs were mid-run on a "
                  f"machine at t0 -- released at tau_min")
    # ---- C3 READER: the PREVIOUS month's build carry-out -------------------
    # `machine_warm_<M>.parquet` is derived from opening-stock build timestamps
    # and does not exist for every month (2026-08 has none). The prior month's
    # own `build_carry_out.parquet` says the same thing exactly and always
    # exists once that month has been planned: the last slice on each machine
    # before the boundary is the run it is mid-way through at t0.
    # ONLY the machines still mid-run are released -- `busy_at_t0`. A machine
    # that finished before the boundary is idle and its GT is NOT warm; treating
    # those as warm is the "release ALL presses early" failure the six global
    # floor experiments already measured (they started and then starved).
    _bci = os.environ.get("PLANNER_BUILD_CARRY_IN", "").strip()
    if _bci:
        _bcf = Path(_bci) if Path(_bci).is_absolute() else ROOT / _bci
        if _bcf.exists():
            _b4 = len(_warm)
            _bd = pl.read_parquet(_bcf)
            if "busy_at_t0" in _bd.columns:
                _bd = _bd.filter(pl.col("busy_at_t0"))
            for _r in _bd.iter_rows(named=True):
                _warm.add((_r["plant"], _r["gt_code"]))
            print(f"  [warm-mc] +{len(_warm) - _b4} (plant, GT) from "
                  f"{_bcf.name} -- machines mid-run at t0")
        else:
            print(f"  [warm-mc] build carry-in {_bcf} not found -- building "
                  f"starts cold (no-op)")

    def earliest_cure(plant: str, gt: str, qty: float = 0.0,
                      hours: float = 0.0) -> datetime:
        """Earliest a press may start curing this campaign, from the pull equation.

        FLAT, NOT RAMP-SHAPED. Per-campaign shaping was tried -- delay each press
        by its OWN first-slice build time rather than the plant band -- and it
        recovered 0.6 pp of press utilisation and 804 tyres. But it converted
        those press-hours into HEAD: PCR p50 5.71 -> 6.15 h, TBR 5.73 -> 6.18,
        TBR p95 9.20 -> 24.90, inventory 4,103 -> 4,281. Starting campaigns at
        t0+5.3 h instead of t0+11.9 h compresses the window building has to fill
        them, so slices are pushed earlier and wait longer.
        Head is the metric this architecture exists to fix (516 tyres/h on PCR),
        so the flat floor wins and the shaped version is deliberately not used.

        EXCEPT where the GT already holds stock. The stock does NOT have to cover
        the whole campaign -- only the GAP until fresh supply arrives. One press
        draws `rate` tyres/h, so a floor of `tau* + band` hours needs
        `(tau* + band) x rate` tyres of cover: ~72 on PCR, ~31 on TBR. Average
        opening stock is ~120 tyres/GT, so most GTs can cover it.
        (Requiring the stock to cover the campaign QUANTITY -- 1,228 tyres at p50
        -- is the version that never fires; that was the first attempt here.)
        """
        # Per-GT, not per-plant: the cover a press needs is `hours x ITS OWN draw
        # rate`, and with plant cure times the draw rate is a property of the GT
        # (10-20 min/cycle on PCR), not of the plant. `gap_tyres[plant]` is the
        # fallback when the plant file does not name this GT.
        _gr = rate_of(plant, gt)
        # ---- HOW MUCH STOCK IS ENOUGH TO START AT t0 ----------------------
        # The old test asked the stock to cover `tau* + build_band` = 11.86 h on
        # PCR, i.e. 72 tyres per press. Both terms are MEDIANS -- tau* is the
        # plant's median coupling buffer and build_band the median campaign
        # LENGTH -- and neither describes when the FIRST tyre arrives. That is
        # the same mined-median-as-a-floor defect PARTITION section 1 records,
        # applied to the start of the horizon.
        #
        # Physically the stock only has to last until building can hand over its
        # first LEGAL run: one B12 lot (PCR 150 / TBR 70) at this GT's own build
        # cadence, then tau_min of ageing. On PCR that is
        #     0.25 h + 150 x 50 s = 2.33 h  ->  14 tyres per press,
        # against the 72 the old test demanded -- a 5.1x overstatement (TBR 3.4x).
        # A press holding 50 tyres of its GT can therefore start immediately: it
        # cures for 8.2 h while building delivers a full 150-tyre lot in 2.1 h.
        #
        # PLANNER_T0_STOCK_BASIS=star restores the old median test for A/B.
        # The formula itself lives in `gap_q_of` above -- PLANNER_L5_STOCK_FIRST
        # reads it too, and two copies of it is the `_gapq_of` defect again.
        _gap_q = gap_q_of(plant, gt)
        # REMEMBER WHAT THIS GT WAS ASKED FOR, so the debit can charge the SAME
        # currency. See the debit site: it used to subtract the plant median
        # `gap_tyres[p]` while this test required the per-GT `_gap_q`, so a GT
        # was charged 75.5 tyres per seat for cover the test priced at ~14 --
        # a 5.4x over-charge that emptied the budget after ~8 presses instead of
        # ~42. Measured 2026-08-14 on July day 1: 129.1 press-hours of head idle,
        # 100 % of it on presses whose first GT still had hundreds of tyres
        # standing (GT 1513 held 600 and two presses waited 7.2 h each).
        _gapq_of[(plant, gt)] = _gap_q
        _stk = early_budget.get((plant, gt), 0.0) if EARLY_STOCK else 0.0
        if _stk >= _gap_q:
            return t0
        # ---- PARTIAL CREDIT (PLANNER_FULL_AVAILABILITY_T0) ------------------
        # The test above is BINARY on a CONTINUOUS quantity. Measured July:
        # 6 PCR GTs and 3 TBR GTs sit in 0 < stock < gap -- and three of the PCR
        # ones hold 66/71/73 tyres against a gap of 76, i.e. 87-97 % of the
        # cover, yet waited the entire 11.86 h as though they held nothing.
        # That wall is an artificial start-of-horizon delay, which is precisely
        # what the plant ruling forbids.
        #
        # The press draws `rate` tyres/h and must last until fresh supply lands
        # at t0 + gap_h. Stock of `s` tyres covers the LAST s/rate hours of that
        # window, so the press need only wait for the uncovered head:
        #       wait = gap_h - s/rate  ==  (gap_tyres - s)/rate
        # It reduces to t0 when s >= gap_tyres (the branch above) and to the
        # full wall when s = 0, so the two old cases are preserved exactly and
        # only the interior changes. It CANNOT starve the press: by construction
        # the stock covers the whole interval between the seat and fresh supply.
        if FULL_AVAIL_RAMP and _stk > 0.0:
            return t0 + timedelta(
                hours=max(0.0, (_gap_q - _stk) / max(_gr, 1e-9)))
        # FLOOR BASIS. `tau* + build_band` is 11.86 h on PCR and blocks EVERY
        # press for half a day -- and it is the SAME formulation error as
        # PARTITION_AND_CHANGEOVER.md �1a, one layer up: tau* is the plant's
        # MEDIAN coupling buffer (47 % of plant tyres cure sooner) and build_band
        # is the MEDIAN campaign length, not a minimum. Neither is a floor.
        # The physical earliest cure is tau_min plus the time to build the first
        # slice, not the median of anything.
        #   "star"  -> tau* + band          the legacy 11.86 h wall
        #   "min"   -> tau_min + band       keep the build time, drop the median
        #   "slice" -> tau_min only         the true physical floor
        # A GT whose machine was already running it at t0 has real supply from
        # minute one; it waits only for the first tyre to age.
        #
        # THIS TEST MUST PRECEDE THE FLOOR_BASIS DISPATCH, and for most of this
        # project it did not. `FLOOR_BASIS` defaults to "star", whose branch
        # returned two lines above -- so the warm set was UNREACHABLE on every
        # shipped run. Measured 2026-08-14: that made `machine_warm_<M>` (20 GTs),
        # the C3 build carry-out (+6) and the cure carry-in all dead code, which
        # is exactly why C3 measured byte-identical twice.
        #
        # The ordering is not cosmetic. `_warm` is a statement about SUPPLY --
        # "this GT is physically in production right now" -- while FLOOR_BASIS
        # chooses a FORMULA for when supply is unknown. A fact outranks a
        # formula, so the fact is tested first. Press 210 is the proof case:
        # carried campaign of GT 2258 running t0 -> 5.67 h, and the next campaign
        # of the SAME GT on the SAME press waited until 11.86 h.
        #
        # GATED, AND THE GATE IS THE HONEST PART. Making `_warm` reachable is a
        # correctness fix -- a rule that never executes silently invalidates every
        # experiment that depends on it. But the BEHAVIOUR it unblocks measured
        # NEGATIVE on July 2026:
        #     day-1 cure       PCR 11,126 -> 11,266   TBR 2,872 -> 2,930
        #     day-1 occupancy  PCR  88.9% ->  90.0%   TBR 91.5% -> 93.4%
        #     head idle        PCR  105.4h ->  76.5h  TBR 72.7h -> 49.2h
        #     in-month         PCR  96.2% ->  95.8%   TBR 95.9% -> 95.9%
        # and the -1,319 PCR is ENTIRELY extra starvation, +1,400 of it on the
        # warm GTs themselves (GT 2258 alone: starved 1,234 -> 2,251). Releasing
        # at tau_min assumes supply exists at full rate from minute one; building
        # needs ~11.6 h more to reach that GT, so the press is seated and then
        # runs dry. The wall was never protecting the press -- it protects
        # BUILDING'S LEAD TIME, which is why every early-release variant loses
        # (FLOOR_BASIS min/slice -2,478..-6,050, CHG_PARALLEL -3,665,
        # T0_STOCK_BASIS lot -1,670).
        # The physically right release is tau_min + (time until building reaches
        # THIS GT), per-GT. Until that exists, ship the conservative wall.
        if WARM_RELEASE and (plant, gt) in _warm:
            return t0 + timedelta(hours=tau_min_h[plant])
        # ---- FEED-CHECKED EARLY RELEASE (PLANNER_L5_FLOOR_BASIS=feed) --------
        #
        # `slice` releases EVERY press at tau_min. Measured 2026-08-20: day 1
        # does exactly what the physics predicts (Aug PCR cure 7,902 -> 12,961,
        # press util 55 -> 88 %, presses at t0 20 -> 47) and the MONTH loses on
        # all four cells (BUILT -3,885 / -1,787 / -7,088 / -1,501, starvation
        # 18,311 -> 26,183). §4ad has the day-by-day decomposition.
        #
        # THE REASON IS NOT THE RELEASE TIME. It is that 47 presses want 47
        # DIFFERENT GTs at t0 and PCR has 11 building machines, with 32.6 % of
        # volume locked to 1-2 of them. A machine builds ONE GT at a time, so
        # the 12th GT released early is a press with no feeder.
        #
        # So release early only as WIDE as building can actually feed at t0:
        # walk the GTs by volume, give each one a machine it is allowed on, and
        # stop when the machines run out. Those GTs get tau_min; every other GT
        # keeps the conservative wall. This is the "tau_min + time until building
        # can reach THIS GT" rule the comment above asks for, in its cheapest
        # form -- reachable now means a free machine at t0.
        #
        # The B12 build-RUN floor already guarantees the other half of the
        # user's model: once building starts a GT it runs at least min_lot
        # (PCR 150 / TBR 70) before a changeover, so a released press is fed
        # continuously, not for one slice. Verified on the shipped plan:
        # 0 of 792 PCR and 0 of 514 TBR build runs are below floor.
        if FLOOR_BASIS == "feed":
            return t0 + timedelta(
                hours=(tau_min_h[plant] if (plant, gt) in _feed_early
                       else tau_h[plant] + bband[plant]))
        if FLOOR_BASIS == "star":
            return t0 + timedelta(hours=tau_h[plant] + bband[plant])
        if FLOOR_BASIS == "slice":
            return t0 + timedelta(hours=tau_min_h[plant])
        if FLOOR_BASIS == "stagger":
            _ct = PCT.build_ct_s(plant, gt, None) or plant_cad_s[plant]
            _fl = float(CONFIG.thresholds.min_lot_units.get(plant, 0) or 0)
            _lot_h = _fl * _ct / 3600.0
            _k = _stag.get((plant, gt), 3.0)      # unranked -> assume 4th in queue
            return t0 + timedelta(hours=tau_min_h[plant] + (_k + 1.0) * _lot_h)
        if FLOOR_BASIS == "lot":
            # THE PHYSICAL FLOOR, and the only one of the four that is derived
            # rather than mined. A press cannot cure a GT until building has
            # produced ONE LEGAL RUN of it -- the B12 floor, PCR 150 / TBR 70, at
            # this GT's own cadence -- and that run has aged tau_min.
            #
            #   PCR  0.27 h + 150 x 50 s  = 2.35 h      (shipped "star" = 11.86 h)
            #   TBR  0.27 h +  70 x 140 s = 2.99 h      (shipped "star" = 10.18 h)
            #
            # "star" adds tau* (the plant's MEDIAN coupling buffer, so 47 % of
            # plant tyres cure sooner) to build_band (the MEDIAN campaign LENGTH,
            # not the time to build the first lot). Neither is a minimum, which
            # is the mined-median-as-a-floor defect PARTITION section 1 records
            # twice. "min" and "slice" only half-fix it: "min" keeps the median
            # band, "slice" drops the build time altogether and pretends a tyre
            # exists at tau_min with nothing having been built.
            #
            # This claims NO pre-month building and NO borrowed June capacity --
            # it only stops asserting a press must wait 11.86 h when the tyre it
            # needs is legally buildable in 2.35 h.
            _ct = PCT.build_ct_s(plant, gt, None) or plant_cad_s[plant]
            _floor = float(CONFIG.thresholds.min_lot_units.get(plant, 0) or 0)
            return t0 + timedelta(
                hours=tau_min_h[plant] + _floor * _ct / 3600.0)
        return t0 + timedelta(hours=tau_min_h[plant] + bband[plant])
    ndays = (datetime(y + (m == 12), (m % 12) + 1, 1) - datetime(y, m, 1)).days
    # TWO BOUNDARIES, DELIBERATELY DISTINCT.
    #   month_end -- the REPORTING boundary. Never moves. Fulfilment, the export
    #                and the WIP rail are all measured against this.
    #   horizon   -- the PLANNING boundary. In "extend" it sits HORIZON_TAIL_H
    #                past month_end so a campaign may finish outside the month.
    month_end = t0 + timedelta(days=ndays)
    tail_h = HORIZON_TAIL_H if HORIZON_MODE == "extend" else 0.0
    horizon = month_end + timedelta(hours=tail_h)

    print("=" * 92)
    print(f"L5  CURE CAMPAIGN MASTER PLAN  --  {a.month}")
    print("=" * 92)
    print(f"  horizon {t0:%Y-%m-%d %H:%M} -> {horizon:%Y-%m-%d %H:%M} "
          f"({ndays} days)   greedy, deterministic")
    print(f"  mode={HORIZON_MODE}  report boundary {month_end:%Y-%m-%d %H:%M}"
          + (f"  + {tail_h:.0f} h planning tail (NOT reported, NOT counted)"
             if tail_h else "  (closed box)"))
    print(f"  ramp: a campaign may not cure before t0 + tau* + its OWN first-slice "
          f"build time (tau* PCR {tau_h['PCR']:.2f}h TBR {tau_h['TBR']:.2f}h)")
    # PRINTED, NOT PERSISTED -- like the partition staleness line and the
    # rim-spill report, `runs/<arm>/log_04_l5_cure_master.txt` is the only
    # evidence of which calendar an arm actually ran on.
    print(f"  {holiday.summary()}\n")

    # explode lots into individual campaigns
    jobs = []
    for r in lots.iter_rows(named=True):
        # L4.5 now emits a per-lot size list carrying the observed length shape;
        # fall back to the flat qty for rows written before that change.
        sizes = r.get("lot_sizes")
        sizes = (list(sizes) if sizes is not None and len(sizes)
                 else [float(r["lot_qty"])] * int(r["n_lots"]))
        # ORDER-BOOK DEADLINE per lot (L4.5). None when the month's demand file
        # carries no `day` column -- then EDD degenerates to today's behaviour.
        _dls = r.get("lot_deadlines")
        _dls = (list(_dls) if _dls is not None and len(_dls) else None)
        # ---- L6 SPLIT REQUEST, floor-clamped -----------------------------
        _f = _SPLIT.get((r["plant"], r["gt_code"]), 1)
        if _f > 1:
            _fl = max(float(r.get("min_lot") or 0.0),
                      float(CONFIG.thresholds.min_lot_units.get(r["plant"], 0)))
            out = []
            for q in sizes:
                # never divide a piece below the floor L4.5 applied
                k = max(1, min(_f, int(float(q) // _fl) if _fl > 0 else _f))
                out.extend([float(q) / k] * k)
            sizes = out
        for i, q in enumerate(sizes):
            jobs.append({"plant": r["plant"], "gt_code": r["gt_code"],
                         "mould_set": r["mould_set"], "qty": float(q), "seq": i,
                         "deadline": (int(_dls[i]) if _dls and i < len(_dls)
                                      else 10 ** 6)})
    # total order: biggest first (hardest to place), then GT, then seq.
    # Rim coherence was tried HERE and failed: sorting jobs by (rim, -qty)
    # produced 0 same-rim changes out of 300 and cost 25,549 tyres of PCR
    # fulfilment. Job order does not control placement -- each job takes whichever
    # eligible press frees earliest -- so grouping the QUEUE by rim never makes
    # two same-rim jobs land on the same PRESS, while displacing -qty destroyed
    # the scarcity priority that large campaigns depend on.
    # Rim coherence belongs in press SELECTION (below), not in the queue.
    _sz = pl.read_parquet(SRC_INP / "gt_size.parquet")
    _rim = {r["gt_code"]: (r["rim"] or "") for r in _sz.iter_rows(named=True)
            if r.get("gt_code")}
    # ---- DEPTH BEFORE BREADTH -------------------------------------------
    # Sorting purely by -qty interleaves GTs: the biggest lot of GT A, then the
    # biggest of GT B, and so on. At t0 all 86 PCR presses are free, so that
    # opens 30 distinct GTs on day 1 -- while building, with 11 machines and a
    # 150-tyre minimum run, can only touch 9 GTs in the first two hours. The
    # presses for the other 21 start and starve.
    #
    # Measured: only 1 of those 30 GTs was at its R3 mould cap. GT 1513 held 13
    # presses against 35 moulds, GT 2267 six against 34. Mould capacity was never
    # the limit -- the QUEUE ORDER was. On mould capacity alone 11 GTs could fill
    # the whole floor.
    #
    # So GTs are ordered by total month volume and their lots kept together: a
    # GT fills its own moulds before the next GT opens. Same campaigns, same
    # start times, same floors -- only WHICH GT a press takes changes. That is
    # why it does not compress building's window, unlike the five floor
    # experiments (lot / stagger / slice / min / warm-open) which all raised day 1
    # and lost more across the month.
    #
    # MEASURED 2026-08-13 AND REJECTED -- SHIPS OFF (PLANNER_D1_DEPTH=0).
    #     PCR 95.6 -> 83.7 pt   TBR 96.3 -> 88.8 pt   unfed 6,547 -> 27,338
    # This is the failure the note above already warned about: displacing -qty
    # from the sort key destroys the SCARCITY PRIORITY that large campaigns
    # depend on. Ordering by GT total spreads the biggest GT's lots across the
    # whole month first, and every small GT is then seated last, at the horizon,
    # where building cannot reach it. The day-1 GT count is a SYMPTOM of the
    # queue being size-ordered, not a cause -- and the size order is load-bearing.
    #
    # Set PLANNER_D1_DEPTH=1 only to reproduce this measurement.
    if os.environ.get("PLANNER_D1_DEPTH", "0") != "0":
        _gtq: dict[tuple, float] = {}
        for _j in jobs:
            _k = (_j["plant"], _j["gt_code"])
            _gtq[_k] = _gtq.get(_k, 0.0) + _j["qty"]
        jobs.sort(key=lambda j: (j["plant"], -_gtq[(j["plant"], j["gt_code"])],
                                 j["gt_code"], -j["qty"], j["seq"]))
    else:
        # ---- BACK-LOAD THE ABSORBER GTs (PLANNER_L5_BACKLOAD) ---------------
        #
        # ROOT CAUSE, measured 2026-08-14 on GT 1513 (PCR's largest, 55,663
        # tyres, 35 moulds, 44 eligible presses):
        #                     d1-22      d23-31
        #     plant          33,415      22,248   <- 2,472/day, RAMPS 60% on d23
        #     ours           38,998      15,103   <- 1,678/day, decaying
        # Same monthly total. The plant FRONT-LOADS LESS and back-loads more, and
        # its concurrency on that GT climbs to 21 while ours sits at 12-15.
        #
        # Why the plant does it: its big, many-moulded GTs are the ABSORBER. As
        # small GTs finish through the month, presses free up and the plant
        # re-tools them onto GT 1513, which has mould headroom to take them. That
        # is why the plant has no month-end idle and we have 1,085 press-hours of
        # it in the last five days.
        #
        # Why we cannot: L4.5 freezes every campaign's quantity before any press
        # is seated, so nothing can GROW to absorb a freed press. This flag is
        # the cheapest test of the mechanism -- defer a fraction of each
        # absorber's lots so the rest of the book is seated first and finishes
        # EARLIER, which should pull campaigns back inside the boundary (56
        # currently cross 1 Aug carrying 27,118 tyres).
        #
        # PREFIXES the -qty key, never displaces it: scarcity order is preserved
        # inside each class. That is the distinction that separates this from
        # PLANNER_D1_DEPTH, which displaced -qty itself and lost 11.9 pt.
        if BACKLOAD > 0.0:
            _mould_n: dict = {}
            try:
                for _r in pl.read_parquet(
                        D / f"cap_mould_{a.month}.parquet").iter_rows(named=True):
                    _mould_n[(_r["plant"], _r["gt_code"])] = int(
                        _r.get("moulds") or 0)
            except Exception:                                   # noqa: BLE001
                pass
            _kmin_c: dict = {}
            _hz_h = (horizon - t0).total_seconds() / 3600.0
            _seen_n: dict = {}
            _tot_c: dict = {}
            for _j in jobs:
                _k = (_j["plant"], _j["gt_code"])
                _seen_n[_k] = _seen_n.get(_k, 0) + 1
                _tot_c[_k] = _tot_c.get(_k, 0.0) + float(_j["qty"])
            _idx_n: dict = {}
            for _j in jobs:
                _k = (_j["plant"], _j["gt_code"])
                if _k not in _kmin_c:
                    _r1 = rate_of(_k[0], _k[1])
                    _kmin_c[_k] = max(1, math.ceil(
                        _tot_c.get(_k, 0.0) / max(_r1 * _hz_h, 1e-9)))
                _mo = _mould_n.get(_k, 0)
                _idx_n[_k] = _idx_n.get(_k, 0) + 1
                # ABSORBER = has mould headroom to ramp (moulds >= 2 x the
                # minimum presses it needs). Only those are deferred.
                _absorb = _mo >= 2 * _kmin_c[_k] and _seen_n[_k] > 1
                _tail = _idx_n[_k] > _seen_n[_k] * (1.0 - BACKLOAD)
                _j["_late"] = 1 if (_absorb and _tail) else 0
        else:
            for _j in jobs:
                _j["_late"] = 0
        # ---- EDD: EARLIEST DUE DATE (PLANNER_L5_EDD) -----------------------
        #
        # The plant is not running a clever S-curve -- it builds to its ORDER
        # BOOK, and we discarded that order book at L4. Verified exactly:
        #     GT 1513 plant day-21 cum 57.2%  |  57.2% DUE by day 21
        #     GT 1503                  59.0%  |  59.0% DUE
        #     GT 2476                  69.6%  |  69.6% DUE
        # With no deadlines in the plan, L5 sorted by -qty (scarcity) and placed
        # at earliest-feasible, which yields a FLAT concurrency curve by
        # construction -- and a flat curve that sits ~2% under the required
        # average spills the remainder past the boundary. That is the whole
        # "plant finishes in-month, we exceed" symptom.
        #
        # EDD is the textbook answer -- AND IT WAS MEASURED AND IT LOST.
        # runs/EDD_off vs runs/EDD_on, 2026-07, identical denominators:
        #   PCR fulfilment 96.2 -> 92.4 % (-3.8 pt), BUILT 385,257 -> 381,156
        #   TBR fulfilment 96.7 -> 93.7 % (-3.0 pt)
        #   PCR carry-out tail 7,416 -> 19,728;  L11 31/48 -> 27/48
        # It does fix the symptom (day-0 seat share 59.3 -> 11.0 % PCR) and the
        # symptom was itself a measurement artefact: realised cure delivery is
        # already level (day-1 share 2.04 % PCR / 2.39 % TBR, daily CV 0.097).
        # A press seat is a 365 h OCCUPANCY, not a delivery; deferring it to a
        # due date idles the press and pushes the tail past the boundary.
        # This is a hypothesis that was tested and failed, not a conclusion and it is what the plant does implicitly.
        # It is NOT PLANNER_D1_DEPTH (-11.9 pt): that sorted on GT TOTAL, a
        # mined aggregate. This sorts on a DEADLINE that came from the plant.
        # -qty is kept as the secondary key, so scarcity still breaks ties
        # inside a due date -- prefixed, never displaced.
        #
        # DEGENERATES SAFELY: a month whose demand file has no `day` column
        # (2026-08 is an unphased order book -- all 528,165 tyres on day 31)
        # gives every lot deadline 10**6, so the sort reduces exactly to today's.
        if EDD:
            jobs.sort(key=lambda j: (j["plant"], j["deadline"], j["_late"],
                                     -j["qty"], j["gt_code"], j["seq"]))
        else:
            jobs.sort(key=lambda j: (j["plant"], j["_late"], -j["qty"],
                                     j["gt_code"], j["seq"]))

    # Use the floor L4.5 ACTUALLY applied (max of the derived min_cure_lot and
    # the B12 fixed floor), not B12's alone. Reading only config.min_lot_units
    # let the backfill create 15 PCR campaigns between 150 and 216 -- below the
    # derived economic floor that L4.5 had already enforced.
    _lf = pl.read_parquet(D / f"l45_lots_{a.month}.parquet").filter(
        pl.col("n_lots") > 0)
    min_lot = {p: float(_lf.filter(pl.col("plant") == p)["min_lot"].max() or 0)
               for p in ["PCR", "TBR"]}
    for _p, _v in CONFIG.thresholds.min_lot_units.items():
        min_lot[_p] = max(min_lot.get(_p, 0.0), float(_v))

    # ---- CAMPAIGN LENGTH CAP (PLANNER_L5_MAX_CAMPAIGN_H) -----------------
    # THE ONE UNBOUNDED PARAMETER IN L5. Nothing here limits how long a single
    # cure campaign may hold a press; SPAN_MULT in L7 defaults to 99, which
    # disables the only other span limit. Measured on 2026-08 PCR:
    #
    #                 campaigns  qty p50  hours p50  GTs  MOULD CHANGES
    #   days 0-10          90     2,536      369 h    31        3
    #   days 11-20         56     1,415      236 h    27       32
    #   days 21-30        118       706      103 h    46       82
    #
    # A 369 h campaign holds a press for 15 days (max observed 656 h = 27 days).
    # Big GTs sort first on -qty, take the presses for the first half of the
    # month, and every low-volume GT is deferred: the BOTTOM 20 GTs BY VOLUME
    # ALL START ON DAY 26. Each needs its own mould change, so 82 of the month's
    # 117 changes -- 493 press-hours -- land in the last third, exactly where the
    # horizon is closing. That is what makes the six dip days: 8.2 changes/day
    # and 164 idle press-h, against 1.4 and -2 on the nine good days.
    #
    # Splitting here, BEFORE the sort, caps how long any one campaign holds a
    # press while leaving the -qty ordering completely untouched -- big GTs keep
    # their scarcity priority, they just cannot hold a press for 27 days. That
    # distinction matters: displacing -qty from this sort was measured at
    # -11.9 pt (PLANNER_D1_DEPTH below) and must not be repeated.
    #
    # Headroom exists. We run 0.044 mould changes per press-day against the
    # plant's 0.06 -- 27 % FEWER. We have been optimising total changeover COUNT
    # and paying for it in CLUSTERING; the plant changes moulds more often but
    # spreads them, so no press-day loses 84 hours at once.
    #
    # SHIPS OFF (0 = no cap). MEASURED 2026-08-13 AND STRONGLY NEGATIVE.
    #
    #   arm        PCR BUILT   dBUILT   in-month   ful%   unfed    L11
    #   base         410,652       +0    392,239   91.4    6,403   27/42
    #   cap 240 h    367,548  -43,104    352,758   82.2   43,270   24/42
    #   cap 168 h    387,291  -23,361    372,499   86.8   22,588   25/42
    #   cap 120 h    364,672  -45,980    351,900   82.0   42,270   24/42
    #   (TBR -626 to -1,029 across the same arms)
    #
    # UNFED EXPLODES 6.7x. That is the whole story: splitting one campaign into
    # N pieces creates N INDEPENDENT BUILD RELEASES, each needing its own
    # R5-legal window on an allowable machine. The long campaign is not a defect
    # -- it is what lets building feed a press in ONE continuous run. Fragment
    # it and building cannot reach any of the pieces.
    #
    # THE DIAGNOSIS WAS RIGHT AND THE FIX WAS WRONG. The clustering is real
    # (82 of 117 August PCR mould changes in the last third, 493 press-hours,
    # six dip days at 8.2 changes/day against 1.4 on good days). But it is a
    # SYMPTOM of the campaign structure, not an independent defect, and the
    # campaign structure is load-bearing for the build feed. Levelling the
    # changes costs about ten times what the changes themselves cost.
    #
    # Same failure as the retired L5<->L6 split mode (-18,129 tyres). Two
    # independent attempts to split cure campaigns have now measured heavily
    # negative for the same reason. DO NOT MAKE A THIRD without a mechanism
    # that keeps the build release contiguous across the split.
    _mch = float(os.environ.get("PLANNER_L5_MAX_CAMPAIGN_H", "0"))
    if _mch > 0:
        _split: list = []
        _n_split = 0
        for _j in jobs:
            _r = rate_of(_j["plant"], _j["gt_code"])
            _h = _j["qty"] / max(_r, 1e-9)
            _fl = min_lot.get(_j["plant"], 0.0)
            if _h <= _mch or _j["qty"] < 2 * max(_fl, 1.0):
                _split.append(_j)                     # short, or cannot split legally
                continue
            # Even pieces, each <= the cap and each >= the lot floor. Never
            # create a sub-floor piece: B12 is a cap, not a knob.
            _n = max(2, int(-(-_h // _mch)))
            while _n > 1 and _j["qty"] / _n < _fl:
                _n -= 1
            if _n < 2:
                _split.append(_j)
                continue
            _q, _rem = divmod(int(_j["qty"]), _n)
            for _i in range(_n):
                _split.append({**_j, "qty": float(_q + (_rem if _i == 0 else 0)),
                               "seq": _j["seq"] * 100 + _i})
            _n_split += 1
        if _n_split:
            print(f"  [camp-cap] {_mch:.0f} h: split {_n_split} campaigns -> "
                  f"{len(_split)} jobs (was {len(jobs)})")
            jobs = _split
            jobs.sort(key=lambda j: (j["plant"], -j["qty"],
                                     j["gt_code"], j["seq"]))

    # ---- CONCURRENCY FLOOR (PLANNER_L5_CONC_FLOOR) -----------------------
    # A FLOOR, NOT A CAP -- the opposite intervention to
    # PLANNER_L5_MAX_PRESS_PER_GT above. That one limits how many DISTINCT
    # presses a GT may visit; this one raises the number of campaigns a GT
    # offers so it CAN be seated on several presses at once. It only ever
    # touches a GT whose job count is BELOW its target, so a well-parallelised
    # GT is untouched by construction -- which is the whole point of trying it
    # after PLANNER_L5_MAX_CAMPAIGN_H (-43,104 BUILT) fragmented everything.
    #
    #   k_tgt = min( basis, moulds (R3), |eligible presses|, need // min_lot )
    #   basis: "plant"  -> cap_mould.observed_max, the plant's own realised
    #                      distinct-presses-per-plant-day for that GT
    #          "mould"  -> the R3 cap itself
    #          "elig"   -> the eligible press count
    # scaled by PLANNER_L5_CONC_FLOOR (1.0 = exactly the basis).
    #
    # Splitting is by halving the LARGEST remaining job, and never below the
    # floor L4.5 applied, so B12 cannot be breached to buy concurrency.
    #
    # SHIPS OFF. MEASURED 2026-08-16 on 2026-08 (the only month whose partition
    # is stamped; no raw MES on disk to rebuild July's). Fresh arms via
    # run_arm.py from CFbase. Judged on IN-MONTH BUILD (end_ts <= month_end),
    # the boundary-safe metric, with plain BUILT beside it:
    #
    #   arm     basis        peak-conc sum   PCR BUILT / in-mo build   TBR BUILT / in-mo build
    #   CFbase  --           PCR 163 TBR 120   403,480 / 389,964         96,873 / 94,860
    #   CFp10   plant x1.0   PCR 173 TBR 131      -257 /     +10           +174 /   -460
    #   CFm05   mould x0.5   PCR 168 TBR 135      +518 /    -243           +362 /   -329
    #   CFm10   mould x1.0   PCR 188 TBR 200      +286 /    -365         -1,074 / -2,904
    #
    # The floor DOES what it says -- peak concurrency p50 goes PCR 1 -> 2,
    # TBR 2 -> 3, and 17 GTs are raised on CFp10 -- and it buys NOTHING.
    # Dose-response is monotone and negative on TBR: +9 / +12 / +67 % concurrency
    # gives -460 / -329 / -2,904 in-month build, with unfed 1,307 -> 1,549 /
    # 1,409 / 2,545. That is the PLANNER_L5_MAX_CAMPAIGN_H failure (-43,104)
    # reproduced in miniature and for the identical reason: N campaigns are N
    # INDEPENDENT BUILD RELEASES.
    #
    # PCR cannot move at all, and the reason is arithmetic. Every one of its 86
    # presses is busy on days 4-15 and 18-22 (94.0 % occupancy, 2,544 idle
    # press-h in the month of which 1,824 is the day-1/2 ramp). Sum over GTs of
    # (press-hours / span) IS the number of presses running, so a concurrency
    # floor on GT A is exactly press-hours taken from GT B. Per-GT attribution
    # on CFp10 proves it: the 11 PCR GTs whose concurrency rose lost 1,260
    # in-month build while the other 44 gained 1,270. Zero-sum, net -10.
    #
    # It also spends the whole R5 margin: max GT wait PCR 64.7 -> 71.8 / 72.0 /
    # 70.8 h and TBR 70.8 -> 71.7 / 72.0 / 70.5 h against a hard 72. L11 PASS
    # 28/48 -> 26 / 25 / 27.
    #
    # DO NOT retry without a mechanism that keeps the build release CONTIGUOUS
    # across the split -- the same condition MAX_CAMPAIGN_H's note already set.
    # Raising the queue to gang-seat a GT's pieces is NOT that mechanism: it is
    # PLANNER_D1_DEPTH, measured at PCR 95.6 -> 83.7 pt.
    _cfl = float(os.environ.get("PLANNER_L5_CONC_FLOOR", "0"))
    if _cfl > 0:
        _cbasis = os.environ.get("PLANNER_L5_CONC_BASIS", "plant")
        _obs = {(r["plant"], r["gt_code"]): int(r.get("observed_max") or 0)
                for r in mould.iter_rows(named=True)}
        _by_gt: dict[tuple, list] = {}
        for _j in jobs:
            _by_gt.setdefault((_j["plant"], _j["gt_code"]), []).append(_j)
        _out, _touched, _added = [], 0, 0
        for _k, _js in _by_gt.items():
            _p, _g = _k
            _fl = max(min_lot.get(_p, 0.0), 1.0)
            _tot = sum(_x["qty"] for _x in _js)
            _b = (_obs.get(_k, 0) if _cbasis == "plant"
                  else moulds.get(_k, 1) if _cbasis == "mould"
                  else len(elig.get(_k, [])))
            _tgt = min(int(math.floor(_b * _cfl)), moulds.get(_k, 1),
                       len(elig.get(_k, [])) or 1, int(_tot // _fl))
            if _tgt > len(_js):
                _touched += 1
                _n0 = len(_js)
                while len(_js) < _tgt:
                    _js.sort(key=lambda x: -x["qty"])
                    _big = _js[0]
                    if _big["qty"] / 2.0 < _fl:
                        break                    # cannot halve without B12 breach
                    _h = float(int(_big["qty"] // 2))
                    _js[0] = {**_big, "qty": _big["qty"] - _h}
                    _js.append({**_big, "qty": _h,
                                "seq": _big["seq"] * 100 + len(_js)})
                _added += len(_js) - _n0
            _out.extend(_js)
        if _added:
            print(f"  [conc-floor] basis={_cbasis} x{_cfl}: raised {_touched} GTs, "
                  f"+{_added} campaigns ({len(jobs)} -> {len(_out)})")
            jobs = _out
            jobs.sort(key=lambda j: (j["plant"], -j["qty"],
                                     j["gt_code"], j["seq"]))

    # ---- OPENING-STOCK FIRST (PLANNER_L5_STOCK_FIRST) --------------------
    # WHAT THIS DOES / WHY IT EXISTS -- a measured defect, found 2026-08-19
    #
    #   `earliest_cure` already grants a t0 seat to a GT holding >= gap_q of
    #   opening stock. What it CANNOT do is decide which campaign reaches a
    #   press first -- that is this sort, and this sort is pure `-qty`. So a
    #   large STOCKLESS campaign (floor 11.86 h on PCR) outranks a smaller
    #   STOCKED one, takes the press, and the stocked campaign queues behind it
    #   or lands on a press needing a mould change. The green tyres that could
    #   have been curing at 07:00 stand on the floor ageing towards 72 h.
    #
    #   MEASURED ON runs/sc_pf8 (July), before writing a line of this:
    #     t0 seats the opening stock PAYS FOR   PCR 57   TBR 64
    #     campaigns actually seated at t0       PCR 26   TBR 34
    #     UNCLAIMED PAID SEATS                  PCR 31   TBR 30
    #   and of the 60 PCR presses whose first campaign starts late (median
    #   14.1 h), 38 hold >= gap_q of their own first GT. TBR: 24 of 45.
    #   Day-1 press time on PCR is 1,216 h curing / 0 h mould change / 848 h
    #   IDLE -- changeover is NOT the cause, the seating order is.
    #
    # BOUNDED BY WHAT THE STOCK PAYS FOR, WHICH IS THE WHOLE POINT.
    #   `-qty` is load-bearing: PLANNER_D1_DEPTH displaced it globally and lost
    #   11.9 pt, PLANNER_L5_EDD prefixed it with a deadline and lost 3.8 pt.
    #   So this promotes ONLY the campaigns that can actually claim an opening
    #   seat -- min(stock // gap_q, moulds (R3), eligible presses) per GT -- and
    #   everything from the first unpaid job onwards keeps the LPT order exactly.
    #   Order INSIDE the promoted head is still `-qty`, so scarcity still breaks
    #   ties among the stocked campaigns. On July that promotes 54 of 276 PCR
    #   jobs and 54 of 195 TBR -- fewer than the 57/64 the stock alone would
    #   pay for, because R3 and eligibility bind first on several GTs.
    #
    # SHIPS OFF. MEASURED 2026-08-19 ON JULY, BOTH PLANTS, AND IT LOSES.
    #   Fresh arms via scripts/arm_scorecard.py, one partition (8a47e109e27d,
    #   stamped 2026-07), identical env but this flag. `check_arm_fresh` clean.
    #
    #   arm        day1    day2   d2-on   BUILT   dBUILT  in-month  ful%  L11
    #   PCR base  7,813  13,228  12,692 390,600       +0   385,272  97.0  31/48
    #   PCR sf1   9,076  13,043  12,662 386,263   -4,337   381,841  96.1  31/48
    #   TBR base  2,278   3,186   3,111  96,555       +0    94,332  96.8  31/48
    #   TBR sf1   2,479   3,007   3,086  95,956     -599    93,543  96.0  31/48
    #
    # THE FLAG DOES EXACTLY WHAT IT WAS BUILT TO DO. t0 seats PCR 26 -> 32 and
    #   TBR 34 -> 50; presses whose first campaign starts late PCR 60 -> 54,
    #   TBR 45 -> 29; opening stock left to expire unused PCR 468 -> 24 and
    #   TBR 231 -> 46, i.e. 699 wasted tyres become 70. Day-1 cure rises
    #   +1,263 PCR / +201 TBR. Every stated objective moves the right way.
    #
    # AND DAY-1 CURE RISES WHILE BUILT FALLS ON BOTH PLANTS. THAT IS
    #   RELOCATION, NOT CREATION -- and it is worse than relocation, because
    #   the tyres do not reappear later either: day 2 falls 13,228 -> 13,043
    #   (PCR) and 3,186 -> 3,007 (TBR), day-2-onward mean falls on both, and
    #   the PCR daily mean moves +11 on a 12,535 base while total output drops
    #   4,337. The day-1 number was bought from days 2-31 and from the month.
    #
    # THE CLAIM I WROTE BEFORE MEASURING WAS WRONG, AND THIS IS THE LESSON.
    #   I argued this was "not another early-release experiment" because
    #   `earliest_cure` is untouched and every promoted campaign is already
    #   legal. PER CAMPAIGN that is true. IN AGGREGATE it is false: the t0
    #   window is a SHARED BUILD-FEED resource, and seating more campaigns in
    #   it oversubscribes building's day-1 output no matter how each seat was
    #   individually justified. Starvation PCR 3,309 -> 7,400, of which
    #   release_before_t0 3,309 -> 6,889 plus a cause the base does not have at
    #   all, r5_shelf_life 511. ATTRIBUTED: 100 % of PCR starvation in the arm
    #   (7,400 of 7,400) sits on the promoted GTs themselves, against 3,015 of
    #   3,309 in the base. A promoted campaign burns its gap_q of stock in the
    #   first hours and then needs fresh green that cannot be released before
    #   t0. Same signature as PLANNER_L5_WARM_RELEASE (-1,319 PCR, "+1,400 of
    #   it on the warm GTs themselves"). Fifth member of that family, after
    #   FLOOR_BASIS min/slice (-2,478..-6,050), CHG_PARALLEL (-3,665),
    #   WARM_RELEASE (-1,319) and T0_STOCK_BASIS lot (-1,670).
    #
    # IT ALSO SPENDS THE SAFETY MARGINS. R5 max PCR 70.6 -> 71.9 h against a
    #   hard 72. G8 daily-mean max, ON BOTH BASES BECAUSE THEY DISAGREE:
    #     L7's       PCR 4,753 -> 4,799 (rail 4,800, one tyre of margin)
    #                TBR 1,398 -> 1,407  OVER the 1,400 rail
    #     arm_kpi's  PCR 4,838 -> 4,851 (already over on this basis in BASE)
    #                TBR 1,399 -> 1,399  unchanged, under the rail
    #   So whether TBR breaches G8 is BASIS-DEPENDENT and neither basis is
    #   quotable alone. L11's own gate resolves it against the flag:
    #   `TBR last-day GT inventory (G8)` goes PASS -> FAIL. L11 pass count is
    #   31/48 in both arms but two invariants FLIP -- `TBR last-day GT
    #   inventory (G8)` PASS -> FAIL and `PCR same-day build/cure correlation`
    #   FAIL -> PASS -- so an unchanged pass COUNT is not an unchanged plan.
    #
    # AUGUST WAS NOT RUN. The ship gate is non-negative on BUILT for both
    #   plants on both months; July fails it on both plants, so August could
    #   only confirm a decision already made. Re-measure BOTH months if the
    #   day-1 build feed ever changes -- that, not this queue, is the binding
    #   constraint. The 699 tyres of expiring opening stock ARE real waste and
    #   this flag does collect them; it just costs 4,936 tyres to collect 629.
    #
    # Ledger: PARTITION_AND_CHANGEOVER.md §4z.
    if STOCK_FIRST and EARLY_STOCK:
        _seat: dict[tuple, int] = {}
        for _k, _q in early_budget.items():
            _g = gap_q_of(_k[0], _k[1])
            if _g <= 0.0 or _q < _g:
                continue
            # R3 and eligibility bound it too: a GT cannot open more presses at
            # t0 than it has moulds for, however much stock it is holding.
            _seat[_k] = min(int(_q // _g), moulds.get(_k, 1),
                            len(elig.get(_k, ())))
        _head: list = []
        _rest: list = []
        for _j in jobs:
            _k = (_j["plant"], _j["gt_code"])
            if _seat.get(_k, 0) > 0:
                _seat[_k] -= 1
                _head.append(_j)
            else:
                _rest.append(_j)
        if _head:
            # Plant stays the outermost group -- the placement loop is one pass
            # over both plants and every later report reads it in plant order.
            jobs = [_j for _p in ("PCR", "TBR") for _grp in (_head, _rest)
                    for _j in _grp if _j["plant"] == _p]
            _np = {_p: sum(1 for _j in _head if _j["plant"] == _p)
                   for _p in ("PCR", "TBR")}
            print(f"  [stock-first] {len(_head)} campaigns promoted to the head "
                  f"of the queue (PCR {_np['PCR']} / TBR {_np['TBR']}) -- each "
                  f"holds >= gap_q of opening stock; -qty order kept elsewhere")

    # ---- L5 IS GT-INVENTORY-AWARE (PLANNER_L5_STOCK_URGENT) --------------
    #
    # WHAT THIS DOES / WHY IT EXISTS -- a measured defect, found 2026-08-21
    #   Opening GT stock is loaded, partly consumed, and the remainder silently
    #   expires. August: PCR holds 5,132 usable tyres at t0 and consumes 3,453;
    #   TBR holds 1,266 and consumes 794. 1,679 + 472 tyres of already-built,
    #   in-shelf-life green die on the floor.
    #
    #   The mechanism is the SEAT QUEUE, not the quantity. L4 already nets stock
    #   out of `cure_requirement` via `from_stock`, so the quantity is right;
    #   only the TIMING is wrong. This sort is `(plant, _late, -qty, gt_code,
    #   seq)` and `_late` is constant 0 unless BACKLOAD is set, so the effective
    #   order is pure descending quantity. NOTHING IN THE KEY KNOWS STOCK
    #   EXISTS. A small GT sorts late, its first campaign lands on day 13-30,
    #   and tyres that were legal on arrival (August age p50 6-15 h, max 55 h,
    #   ZERO rows over 72 h) are 300-700 h old by the time a press wants them.
    #
    # THE PROMOTION IS BOUNDED BY WHAT THE STOCK CAN PHYSICALLY BE DRUNK BY.
    #   n = ceil( stock / (remaining shelf life x that GT's own press rate) )
    #   -- the FEWEST presses that can consume this stock before it expires --
    #   capped by R3 moulds and eligible presses. Every term is this month's
    #   own: the life comes from the stock's `age_h`, the rate from the plant's
    #   cure-time workbook. No mined constant (PARTITION §1).
    #
    #   That bound is what separates it from PLANNER_L5_STOCK_FIRST (§4z), which
    #   promoted `stock // gap_q` campaigns -- gap_q is ~86 tyres on PCR, so a
    #   GT holding 661 bought up to 5 seats there and buys 2 here. §4z lost
    #   BUILT 4,337 PCR / 599 TBR on July because a promoted campaign burned its
    #   gap_q of cover in HOURS and then needed green that could not be released
    #   before t0 (DO-NOT #37: the t0 window is a shared build-feed resource).
    #   Sizing the promotion on the stock's own DRAW TIME instead of on seat
    #   COUNT is the direct answer to that failure mode -- and it is a
    #   re-measurement of a rejected family, so it is defaulted OFF and gated on
    #   BUILT, not on how much stock it collects.
    #
    # ORDERING inside the head is by remaining shelf life ASCENDING -- soonest
    # to expire first -- then the shipped key unchanged. `-qty` is PREFIXED for
    # the promoted jobs only and is untouched for every other job, which is the
    # distinction that separates this from PLANNER_D1_DEPTH (-11.9 pt) and
    # PLANNER_L5_EDD (-3.8 pt), both of which displaced `-qty` globally.
    #
    # =====================================================================
    # SHIPS OFF. MEASURED 2026-08-21, AUGUST 2026, BOTH PLANTS.
    # THE MECHANISM DELIVERS ITS OBJECTIVE COMPLETELY AND THE NUMBER IS NOISE.
    # =====================================================================
    # Arms fresh via `scripts/run_arm.py`, all nine gated FRESH by
    # `check_arm_fresh.py`. Partition stamped 2026-08, sha1 809beda91344.
    # `PLANNER_HOLIDAYS=2026-08-15` pinned on every arm (the August holiday
    # master is not in the tree; an unpinned arm silently drops the closure).
    # Baseline = the shipped SHIP2_aug configuration (`CLOSING_BUFFER=1`),
    # reproduced byte-identically on all seven plan parquets.
    #
    #   PCR, demand 429,146
    #   arm                     BUILT   dBUILT  in-month   ful%  stkEXP  addr   starved   L11
    #   SP_base   off         409,511       +0   397,326  92.59   1,679  1,023   12,477  32/48
    #   SP_su_1   life-order  410,089     +578   399,655  93.13     656      0   10,755  32/50
    #   SP_su_alpha gt-order  404,787   -4,724   395,596  92.18     743     87   15,040  32/50
    #   SP_su_qty  -qty-order 407,325   -2,186   397,623  92.65     743     87   13,589  32/50
    #   SP_q8     minq=8      408,243   -1,268   398,568  92.87     664      8   12,035  31/50
    #
    #   TBR, demand 99,019
    #   SP_base   off          98,003       +0    96,932  97.89     472    232    1,654  32/48
    #   SP_su_1   life-order   97,265     -738    97,415  98.38     240      0    2,276  32/50
    #   SP_su_alpha gt-order   97,368     -635    97,361  98.33     240      0    2,080  32/50
    #   SP_su_qty  -qty-order  96,813   -1,190    96,766  97.72     240      0    2,412  32/50
    #   SP_q3/q8  minq>=3      96,992   -1,011    96,835  97.79     240      0    2,362  32/50
    #
    # WHAT IT ESTABLISHES
    #   1. THE DEFECT IS REAL AND THE FIX FIXES IT. Addressable expired stock
    #      goes 1,023 -> 0 PCR and 232 -> 0 TBR; consumption 3,453 -> 4,476 and
    #      794 -> 1,026. Both new L11 invariants go FAIL -> PASS. 24 PCR
    #      campaigns on 19 GTs and 20 TBR on 20 GTs are promoted.
    #   2. AND THE VOLUME IS NOISE. `alpha` and `qty` promote the IDENTICAL set
    #      of campaigns and differ only in the order of that set among itself --
    #      they collect the same stock (expired 743 vs 656 PCR; 240 on TBR in
    #      all three) -- yet PCR BUILT spans -4,724..+578, a range of 5,302 with
    #      sd 2,059 over the five settings. The +578 sits INSIDE one sd of the
    #      mean (-1,074) of settings that are equivalent on the objective.
    #   3. THE SHARPEST NULL: `MINQ=8` drops ONE GT holding EIGHT tyres of stock
    #      -- 0.16 % of the PCR opening floor and 2.6 % of the 311.5-tyre B12
    #      cure floor -- and PCR BUILT moves +578 -> -1,268, a 1,846-tyre swing
    #      that flips the sign, with L11 32 -> 31. Dropping the 3-tyre TBR GT
    #      (MINQ=3) moves TBR in-month +483 -> -97. Textbook DO-NOT #49: the
    #      effect is the greedy re-settling, not the preference.
    #   4. TBR BUILT IS NEGATIVE IN EVERY ARM (-635..-1,190). Mixed sign across
    #      plants fails the ship gate on its own (DO-NOT #14), and it is the
    #      SIXTH loss in the early-release family after §4z's list.
    #   5. THE +2,329 PCR IN-MONTH IS NOT +2,329 TYRES. It decomposes as +578
    #      BUILT + 1,023 extra opening stock consumed + 1,081 of carry-out tail
    #      pulled inside the boundary (12,620 -> 11,539). Reading in-month alone
    #      this is +0.54 pt and looks like one of the best August arms of the
    #      project. It is not -- 74 % of it is relocation and stock draw.
    #
    # THE TRAP IN READING THIS TABLE: `stkEXP`/`addr` improve in EVERY arm,
    # including the two that destroy 2,186 and 4,724 tyres. Opening-stock
    # recovery and BUILT are decoupled here -- do not use the first as evidence
    # for the second. §4z is the same shape one lever along.
    #
    # WHERE THE COST LANDS. Weighted setup 458.6 -> 496.1 h PCR (+8.2 %) and
    # 94.2 -> 96.3 h TBR; PCR same-size 70.5 -> 70.2 %; TBR lot p50 122 -> 111.
    # The promoted campaigns are small, so promoting them fragments the press
    # calendar and buys 19 extra mould mounts. GT inventory stays inside the
    # rail (PCR daily-mean max 4,522 -> 4,533 of 4,800; TBR 1,313 -> 1,323 of
    # 1,400), sub-floor run share stays 0.0 % on both plants, R5 max PCR
    # 63.3 -> 67.4 h and TBR 71.7 -> 66.7 h, and the independent verifier
    # reports the SAME TWO pre-existing hard violations with no new class
    # (transitions 12/1269 -> 11/1365, machine-days over 24 h 2 -> 1).
    #
    # NOT BUILT, AND THE REASON IS MEASURED, NOT ASSUMED -- the "seat an early
    # SLICE sized to the stock and leave the remainder" design. Two independent
    # refutations on August, taken before writing it (DO-NOT #30):
    #   * B12. The August cure-lot floor is PCR 311.5 / TBR 85.7. Ten of the
    #     eleven addressable GTs hold LESS than that (PCR 167/91/87/9/8, TBR
    #     63/61/39/35/34), so a stock-sized slice is a sub-floor campaign by
    #     construction. Only `GT  T1457 STAR` (661) clears it.
    #   * GEOMETRY. On the realised plan, the largest contiguous free window on
    #     ANY eligible press inside the stock's own remaining life is 7.4 h on
    #     PCR against a 57.1 h need, and ZERO of the eligible presses for those
    #     four GTs are free at all in the first 33-64 h (August PCR presses run
    #     94.5 % occupied). TBR has room -- 50.7 h windows -- but only 171 tyres
    #     of reach, a fifth of the noise floor. A repair pass can only take an
    #     occupied window by EVICTION, which is §4bc and is not this change.
    # The reordering above is therefore the only legal shape of this lever: it
    # does not need a free window, it takes the seat by displacement.
    if STOCK_URGENT and EARLY_STOCK:
        _need: dict[tuple, int] = {}
        for _k, _q in early_budget.items():
            _life = _stock_life.get(_k, 0.0)
            if _q <= STOCK_URGENT_MINQ or _life <= 0.0:
                continue
            _r = rate_of(_k[0], _k[1])
            _need[_k] = min(max(1, math.ceil(_q / max(_life * _r, 1e-9))),
                            moulds.get(_k, 1), len(elig.get(_k, ())) or 1)
        _head, _rest = [], []
        for _j in jobs:
            _k = (_j["plant"], _j["gt_code"])
            if _need.get(_k, 0) > 0:
                _need[_k] -= 1
                _head.append(_j)
            else:
                _rest.append(_j)
        if _head:
            if STOCK_URGENT_MODE == "alpha":
                _head.sort(key=lambda j: (j["plant"], j["gt_code"], j["seq"]))
            elif STOCK_URGENT_MODE == "qty":
                _head.sort(key=lambda j: (j["plant"], -j["qty"],
                                          j["gt_code"], j["seq"]))
            else:
                _head.sort(key=lambda j: (
                    j["plant"],
                    round(_stock_life.get((j["plant"], j["gt_code"]), 0.0), 3),
                    -j["qty"], j["gt_code"], j["seq"]))
            # Plant stays the outermost group -- the placement loop is one pass
            # over both plants and every later report reads it in plant order.
            jobs = [_j for _p in ("PCR", "TBR") for _grp in (_head, _rest)
                    for _j in _grp if _j["plant"] == _p]
            for _p in ("PCR", "TBR"):
                _hp = [_j for _j in _head if _j["plant"] == _p]
                if not _hp:
                    continue
                _gts = sorted({(_stock_life.get((_p, _j["gt_code"]), 0.0),
                                _j["gt_code"]) for _j in _hp})
                print(f"  [stock-urgent:{STOCK_URGENT_MODE}] {_p}: {len(_hp)} campaigns on "
                      f"{len(_gts)} GTs promoted, soonest-to-expire first  "
                      + ", ".join(f"{g} ({lh:.0f}h)" for lh, g in _gts[:5])
                      + (" ..." if len(_gts) > 5 else ""))

    gt_total: dict[tuple, float] = {}
    for _j in jobs:
        k = (_j["plant"], _j["gt_code"])
        gt_total[k] = gt_total.get(k, 0.0) + _j["qty"]

    free: dict[str, datetime] = {}                    # press -> next free time
    last_gt: dict[str, str] = {}                      # press -> GT last run
    press_tyres: dict[str, float] = {}                # press -> cumulative
                                                      # tyres, for mould wear

    # ---- WARM START: WHAT EACH PRESS WAS ALREADY CURING -------------------
    # Every press used to start blank, so L5 assigned GTs to presses afresh at
    # t0 and the opening stock -- which sits on the GTs the plant was ACTUALLY
    # running -- landed on the wrong presses. Measured July: 4,820 PCR tyres of
    # stock spread over 25 of 48 demanded GTs, 50 of 86 presses waiting 11.86 h,
    # and 280 press-hours of stock value unrealised.
    #
    # Seeding `last_gt` from the end-of-previous-month mould snapshot makes a
    # press that continues its own GT pay NO mould change, so it frees earliest
    # and wins the seat -- which is exactly how the plant's day 1 works, and why
    # its stock always matches its presses.
    #
    # This is a STATE, not production: no tyre is built before t0. It only stops
    # us pretending every press was empty and unconfigured at 07:00.
    # SHIPS OFF -- MONTH-DEPENDENT, WHICH MEANS IT IS NOISE, NOT SIGNAL.
    #     July PCR 95.3 -> 95.9 (+0.6)      August PCR 90.8 -> 90.3 (-0.5)
    # It was adopted on a July-only A/B and never tested on August. A flag that
    # helps one month and hurts the next by a similar margin is not an
    # improvement, it is a fit to one month's data. Both months resolve ~43 % of
    # presses, so the difference is not a data-quality artefact.
    #
    # RULE THIS ESTABLISHED: a change ships only if it is NON-NEGATIVE ON BOTH
    # MONTHS. Two flags failed that test on 2026-08-13 (this one and
    # T0_STOCK_BASIS=lot) and both had been adopted on July alone.
    #
    # NOT a data-quality artefact: August resolves MORE presses than July
    # (PCR 66/77 = 86 % vs 67/85 = 79 %). The effect itself is month-dependent.
    #
    # ===== TURNED BACK ON 2026-08-13. IT IS NOT AN OPTIMISATION, IT IS A =====
    # ===== CORRECTNESS FIX, AND THE BASELINE IT WAS GATED AGAINST WAS    =====
    # ===== PHYSICALLY IMPOSSIBLE.                                        =====
    #
    # With this OFF, last_gt starts EMPTY, so `last_gt.get(pr) not in (None, gt)`
    # is false for every press's first campaign: the plan books ZERO mould
    # changes on day 1 and seats presses on whatever GT it likes for free.
    # Measured against the plant's own running-mould snapshot:
    #
    #                          Jul PCR        Aug PCR
    #   first campaign == mounted mould    4/61 (7%)     7/66 (11%)
    #   presses starting AT t0 on a
    #   FOREIGN mould, no 6 h charge         19            26     <-- impossible
    #
    # So the OFF baseline hands 19-26 presses a free mould change. Judging
    # continuity against that is judging it against a plan that cannot be run.
    # With it ON, illegal t0 starts go to 0 on both months and the 6 h shows up
    # in the schedule (start hours gain 6.0 and 17.86 = 11.86 + 6).
    #
    #                    BUILT    in-month     day 1     unfed    L11
    #   Jul PCR off    385,084   96.2 %      10,576     8,542    27/42
    #   Jul PCR ON    +2,136     96.8 %      12,183     6,378    27/42
    #   Aug PCR off    410,652   91.4 %      10,540     6,403    27/42
    #   Aug PCR ON     -1,175    90.9 %      12,014     5,590    26/42
    #
    # July improves outright. August loses 1,175 BUILT and 0.5 pt -- that is the
    # PRICE OF REMOVING A FICTION, not a regression: part of the old August
    # number was 26 presses curing on moulds that were not fitted.
    # Day 1 rises ~1,500 on both months and unfed falls on both.
    #
    # IT DOES NOT REDUCE CHANGEOVERS -- the opposite. Mould changes go
    # Jul 117 -> 117 (701 -> 713 press-h), Aug 117 -> 127 (696 -> 773 press-h),
    # because changes we previously ignored are now booked. Continuity roughly
    # triples (7 -> 26 % PCR) and the count still rises. Do not sell this as a
    # changeover saving.
    #
    # TBR IS UNAFFECTED ON BOTH MONTHS -- byte-identical -- because
    # running_moulds resolves 0 of 75 TBR presses to a current GT. The TBR
    # recipe->GT bridge is the missing input; until it lands, TBR day 1 keeps
    # the same fiction PCR just lost.
    if os.environ.get("PLANNER_WARM_PRESS", "1") != "0":
        # WHICH FILE DESCRIBES THE MOULDS MOUNTED AT t0? Not, as the name
        # suggests, running_moulds_<this month>. Measured 2026-08-14:
        #     running_moulds_2026-07.parquet   last_ts  29-31 MAY
        #     running_moulds_2026-08.parquet   last_ts  28-30 JUNE
        # The file is stamped with the month it was BUILT FOR, but it holds the
        # mould state from the end of the month BEFORE the one it should
        # describe -- so planning July seeds presses with May's moulds, two
        # months stale, and August with June's. Every month is off by one.
        # PLANNER_RUNNING_MOULDS points at the right file explicitly; the
        # default is left alone so no existing run silently changes.
        _wov = os.environ.get("PLANNER_RUNNING_MOULDS", "").strip()
        _wf = (Path(_wov) if _wov and Path(_wov).is_absolute()
               else (SRC_INP / _wov) if _wov
               else SRC_INP / f"running_moulds_{a.month}.parquet")
        if _wf.exists():
            _w = pl.read_parquet(_wf)
            if "current_gt" in _w.columns:
                _w = _w.filter(pl.col("is_current")
                               & pl.col("current_gt").is_not_null())
                _n = 0
                for _r in _w.iter_rows(named=True):
                    last_gt[str(_r["press"])] = _r["current_gt"]
                    _n += 1
                print(f"  [warm] {_n} presses seeded from {_wf.name} with the GT they "
                      f"were already running at month start")

    # ================= LEVEL-LOADED PRESS-CONCURRENCY BUDGET ==============
    # Partition tag per (plant, gt). ALL always; plus the TT/TL dedication on
    # TBR and the rim lock on PCR when PLANNER_L5_TAKT_PART=1.
    SUBPART = os.environ.get("PLANNER_L5_TAKT_PART", "1") != "0"
    _gt_grp: dict[tuple, str] = {}
    if SUBPART:
        try:
            _tt = pl.read_parquet(SRC_INP / "tt_tl.parquet")
            _dm = pl.read_parquet(ROOT / "masters" / "demand" /
                                  f"demand_{a.month}.parquet")
            _tm = (_tt.filter(pl.col("sku") != "").select(["sku", "tt_tl"])
                   .unique(subset=["sku"]))
            for r in _dm.join(_tm, on="sku", how="left").iter_rows(named=True):
                if r["plant"] == "TBR" and r.get("tt_tl"):
                    _gt_grp[("TBR", r["gt_code"])] = str(r["tt_tl"])
        except Exception as e:                        # noqa: BLE001
            print(f"  takt: TT/TL tags unavailable ({e}) -- ALL partition only")

    def _pkeys(pl_: str, gt: str) -> list:
        ks = [(pl_, "ALL")]
        # PLANNER_L5_TAKT_PART_PLANTS lets one plant keep its sub-partition
        # while the other runs on `ALL` alone. Default is both plants, i.e.
        # exactly what SUBPART did on its own.
        if not SUBPART or pl_ not in TAKT_PART_PLANTS:
            return ks
        if pl_ == "TBR" and _gt_grp.get((pl_, gt)):
            ks.append((pl_, _gt_grp[(pl_, gt)]))
        elif pl_ == "PCR" and _rim.get(gt):
            ks.append((pl_, _rim[gt]))
        return ks

    NH = ndays * 24
    _work: dict[tuple, float] = {}                    # partition -> press-hours
    _pset: dict[tuple, set] = {}                      # partition -> eligible presses
    for _j in jobs:
        _r = rate_of(_j["plant"], _j["gt_code"])
        for _k in _pkeys(_j["plant"], _j["gt_code"]):
            _work[_k] = _work.get(_k, 0.0) + _j["qty"] / max(_r, 1e-9)
            _pset.setdefault(_k, set()).update(
                elig.get((_j["plant"], _j["gt_code"]), []))
    # Usable span: the ramp floor blocks every press at the head of the month,
    # so the level rate must be computed over what is actually reachable.
    _floor_h = {p: (tau_h[p] + bband[p]) if FLOOR_BASIS == "star"
                else (tau_min_h[p] + bband[p]) for p in ("PCR", "TBR")}
    _budget: dict[tuple, int] = {}
    for _k, _w in _work.items():
        _u = max(NH - _floor_h[_k[0]], 1.0)
        _n = int(math.ceil(ALPHA_P[_k[0]] * _w / _u - 1e-9))
        _budget[_k] = max(1, min(len(_pset.get(_k, ())) or 1, _n))
    _cnt: dict[tuple, list] = {k: [0] * NH for k in _budget}
    _takt_moved = [0]
    _takt_nowin = [0]
    if GOV != "off":
        print(f"  TAKT  level-loaded press-concurrency budget "
              f"(mode={GOV} alpha={'/'.join(f'{p}={ALPHA_P[p]:g}' for p in ('PCR', 'TBR'))}"
              f" plants={','.join(sorted(GOV_PLANTS))}"
              f" subpart={int(SUBPART)}:{','.join(sorted(TAKT_PART_PLANTS))})")
        print(f"    {'partition':<14}{'work press-h':>14}{'level n':>10}"
              f"{'budget N':>10}{'presses':>9}")
        for _k in sorted(_budget):
            _u = max(NH - _floor_h[_k[0]], 1.0)
            print(f"    {_k[0] + ' ' + _k[1]:<14}{_work[_k]:>14,.0f}"
                  f"{_work[_k] / _u:>10.1f}{_budget[_k]:>10d}"
                  f"{len(_pset.get(_k, ())):>9d}")
        print()

    def _takt_free(pl_: str, gt: str, st: datetime, dur: timedelta):
        """Earliest start >= st whose WHOLE span keeps every partition under its
        concurrency budget AND still finishes in the month.  None => no such
        window; the caller then places ungoverned (never lose volume)."""
        if GOV == "off" or pl_ not in GOV_PLANTS:
            return st
        ks = [k for k in _pkeys(pl_, gt) if k in _budget]
        if not ks:
            return st
        need = max(1, int(dur.total_seconds() // 3600) + 1)
        if need > NH:
            return st
        h = max(0, int((st - t0).total_seconds() // 3600))
        while h + need <= NH:
            bad = -1
            for x in range(h, h + need):
                if any(_cnt[k][x] >= _budget[k] for k in ks):
                    bad = x
                    break
            if bad < 0:
                return max(st, t0 + timedelta(hours=h))
            h = bad + 1                               # jump past the blockage
        return None

    # ================= GT/RIM FEED CEILING ================================
    # See the PLANNER_L5_FEED_CEIL block at the top of this module. Every term
    # here is derived from THIS month's masters at run time; nothing is mined
    # into a constant (§1 -- a median is not a floor).
    #
    # ELIGIBILITY IS L7'S, NOT `cap_machine`'s. `_locked` in l7_pull_release.py
    # resolves the partition first, then the GT's home machines, then the rim
    # lock. Reproduced here in that order. Using `cap_machine` instead would
    # widen the set ~3x and the ceiling would never bind.
    _feed_rate: dict[tuple, float] = {}       # (plant, rim) -> tyres/hour
    _feed_stock: dict[tuple, float] = {}      # (plant, rim) -> R5-usable stock
    _feed_q: dict[tuple, list] = {}           # (plant, rim) -> hourly cure draw
    _feed_pre: dict[tuple, list] = {}         # prefix sums of the above
    _feed_moved = [0]
    _feed_nowin = [0]
    if FEED_CEIL:
        _part_of: dict[tuple, list] = {}
        _pf_f = SRC_INP / "gt_machine_partition.parquet"
        if _pf_f.exists():
            for _r in pl.read_parquet(_pf_f).iter_rows(named=True):
                _part_of.setdefault((_r["plant"], _r["gt_code"]), []).append(
                    _r["machine"])
        _home_of: dict[tuple, list] = {}
        _hf_f = SRC_INP / "gt_home_machine.parquet"
        if _hf_f.exists():
            for _r in (pl.read_parquet(_hf_f).sort(["plant", "gt_code", "rank"])
                       .iter_rows(named=True)):
                _home_of.setdefault((_r["plant"], _r["gt_code"]), []).append(
                    _r["machine"])
        _lock_f: dict[tuple, list] = {}
        _lf_f = SRC_INP / "machine_rim_lock.parquet"
        if _lf_f.exists():
            _tr = {"hard": 0, "primary": 1, "flex": 2}
            _tmp: dict[tuple, list] = {}
            for _r in pl.read_parquet(_lf_f).iter_rows(named=True):
                _tmp.setdefault((_r["plant"], str(_r["locked_rim"])), []).append(
                    (_tr.get(str(_r["tier"]).lower(), 3), int(_r["rank"]),
                     _r["machine"]))
            for _k, _v in _tmp.items():
                _lock_f[_k] = [m for _a, _b, m in sorted(_v)]

        def _elig_mach(p_: str, gt_: str) -> list:
            """L7 `_locked`, minus the budgeted rim-spill flex machine."""
            _rm = _rim.get(gt_, "")
            _pm = _part_of.get((p_, gt_))
            if _pm:
                return _pm + [m for m in _lock_f.get((p_, _rm), [])
                              if m not in _pm]
            _hm = _home_of.get((p_, gt_), [])
            return _hm + [m for m in _lock_f.get((p_, _rm), []) if m not in _hm]

        # Machine load per (machine, rim) from the month's OWN requirement, so a
        # machine shared by two rims is apportioned by what it actually owes
        # each -- never counted whole to both (DO-NOT #44).
        _mload: dict[str, float] = {}
        _mrload: dict[tuple, float] = {}
        _mrrate: dict[tuple, list] = {}
        for _j in jobs:
            _p, _g = _j["plant"], _j["gt_code"]
            if _p not in FEED_PLANTS:
                continue
            _rr = _rim.get(_g, "")
            if not _rr:
                continue
            _ms = _elig_mach(_p, _g)
            if not _ms:
                continue
            _ct_s = PCT.build_ct_s(_p, _g, None) or plant_cad_s[_p]
            _h = _j["qty"] * _ct_s / 3600.0            # build hours this job needs
            for _m in _ms:
                _mload[_m] = _mload.get(_m, 0.0) + _h / len(_ms)
                _mrload[(_m, _rr)] = _mrload.get((_m, _rr), 0.0) + _h / len(_ms)
                _mrrate.setdefault((_p, _rr), []).append(3600.0 / _ct_s)
        for (_m, _rr), _h in _mrload.items():
            _sh = _h / _mload[_m] if _mload.get(_m) else 0.0
            for _p in FEED_PLANTS:
                _rates = _mrrate.get((_p, _rr))
                if not _rates:
                    continue
                _avg = sum(_rates) / len(_rates)       # tyres per machine-hour
                _feed_rate[(_p, _rr)] = _feed_rate.get((_p, _rr), 0.0) + _sh * _avg
        # R5-usable opening stock per rim -- `early_budget` is already filtered
        # to age_h <= GT_SHELF_LIFE_H, so it is the usable population, not the
        # whole floor (§4p.1: "the GT holds stock" is not "stock is spare").
        for (_p, _g), _q in early_budget.items():
            _rr = _rim.get(_g, "")
            if _rr and _p in FEED_PLANTS:
                _feed_stock[(_p, _rr)] = _feed_stock.get((_p, _rr), 0.0) + _q
        for _k in _feed_rate:
            _feed_q[_k] = [0.0] * NH
            _feed_pre[_k] = [0.0] * (NH + 1)
        print(f"  FEED CEILING  per-(rim, {FEED_W_H:.0f} h window) build-feed cap "
              f"(slack={FEED_SLACK:g} plants={','.join(sorted(FEED_PLANTS))})")
        print(f"    {'rim':<10}{'feed t/h':>10}{'per window':>12}"
              f"{'R5 stock':>10}{'month cure':>12}")
        _mq: dict[tuple, float] = {}
        for _j in jobs:
            _rr = _rim.get(_j["gt_code"], "")
            if _rr and _j["plant"] in FEED_PLANTS:
                _mq[(_j["plant"], _rr)] = _mq.get((_j["plant"], _rr), 0.0) + _j["qty"]
        for _k in sorted(_feed_rate):
            print(f"    {_k[0] + ' ' + _k[1]:<10}{_feed_rate[_k]:>10.1f}"
                  f"{_feed_rate[_k] * FEED_W_H:>12,.0f}"
                  f"{_feed_stock.get(_k, 0.0):>10,.0f}{_mq.get(_k, 0.0):>12,.0f}")
        print()

    def _feed_win_ok(k: tuple, h0: int, n: int, rate_: float) -> int:
        """-1 if placing `rate_` t/h over hours [h0, h0+n) keeps EVERY W-window
        on this rim under its ceiling; otherwise the first offending window
        start.  Window sums come from a prefix array, so this is O(W + n)."""
        cap_h = _feed_rate.get(k, 0.0) * FEED_SLACK
        if cap_h <= 0:
            return -1
        w = max(1, int(FEED_W_H))
        pre, stock = _feed_pre[k], _feed_stock.get(k, 0.0)
        lo = max(0, h0 - w + 1)
        hi = min(NH - w, h0 + n - 1)
        for ws in range(lo, hi + 1):
            we = ws + w
            base = pre[we] - pre[ws]
            ov = max(0, min(h0 + n, we) - max(h0, ws))
            # The stock credit is spent, not renewed: only windows that START
            # inside the shelf life of the opening floor may count it.
            _st = stock if ws * 1.0 <= GT_SHELF_LIFE_H else 0.0
            if base + ov * rate_ > cap_h * w + _st + 1e-6:
                return ws
        return -1

    def _feed_free(pl_: str, gt: str, st: datetime, dur: timedelta,
                   rate_: float):
        """Earliest start >= st at which this campaign's own draw keeps every
        W-hour window on its rim inside the build-feed ceiling.  None => no such
        window; the caller places UNGOVERNED (a shape preference must never
        become lost demand -- the same fence takt and the day cap carry)."""
        if not FEED_CEIL or pl_ not in FEED_PLANTS:
            return st
        k = (pl_, _rim.get(gt, ""))
        if k not in _feed_rate:
            return st
        n = max(1, int(dur.total_seconds() // 3600) + 1)
        if n > NH:
            return st
        h = max(0, int((st - t0).total_seconds() // 3600))
        for _ in range(NH + 2):                        # bounded, one jump/window
            if h + n > NH:
                return None
            bad = _feed_win_ok(k, h, n, rate_)
            if bad < 0:
                return max(st, t0 + timedelta(hours=h))
            h = bad + max(1, int(FEED_W_H))            # jump past the full window
        return None

    def _feed_commit(pl_: str, gt: str, st: datetime, en: datetime,
                     rate_: float) -> None:
        """Charge the seat's hourly cure draw, at the SAME chokepoint every
        other ledger uses. A governor whose ledger misses a commit site stops
        describing the plan (§4l.3, DO-NOT #25)."""
        if not FEED_CEIL:
            return
        k = (pl_, _rim.get(gt, ""))
        if k not in _feed_q:
            return
        h0 = max(0, int((st - t0).total_seconds() // 3600))
        h1 = min(NH, int((en - t0).total_seconds() // 3600) + 1)
        q = _feed_q[k]
        for x in range(h0, h1):
            q[x] += rate_
        pre = _feed_pre[k]
        for x in range(NH):
            pre[x + 1] = pre[x] + q[x]

    # ================= DAILY CURE-RATE GOVERNOR ===========================
    # See the PLANNER_L5_DAY_CAP block at the top of this module for what this
    # is, why the engine had nothing like it, and the two enforcement modes.
    DAY_CAP: dict[str, float] = {
        p: float(CONFIG.thresholds.cure_day_cap.get(p, 0) or 0)
        for p in ("PCR", "TBR")} if DAY_CAP_ON else {"PCR": 0.0, "TBR": 0.0}
    # Ledger days span the PLANNING horizon (month + tail) so the printed
    # profile describes the whole plan; only `ndays` of them are ever GATED.
    NDAY_LEDGER = ndays + int(math.ceil(tail_h / 24.0)) + 1
    _dq: dict[str, list[float]] = {p: [0.0] * NDAY_LEDGER for p in ("PCR", "TBR")}
    _cap_moved = [0]          # seats the cap pushed to a later day
    _cap_leak = [0.0]         # tyres placed ungoverned (soft) -- the honest cost
    _cap_drop = [0.0]         # tyres refused outright (strict)
    if DAY_CAP_ON:
        print(f"  DAY CAP  daily cure ceiling "
              f"(PCR {DAY_CAP['PCR']:,.0f}/day  TBR {DAY_CAP['TBR']:,.0f}/day, "
              f"mode={'strict' if DAY_CAP_STRICT else 'soft'})")
        for _p in ("PCR", "TBR"):
            if DAY_CAP[_p] > 0:
                _need = sum(j["qty"] for j in jobs if j["plant"] == _p)
                print(f"    {_p}  work {_need:,.0f} tyres over {ndays} days = "
                      f"{_need / ndays:,.0f}/day level vs cap "
                      f"{DAY_CAP[_p]:,.0f}  "
                      f"(cap x {ndays} = {DAY_CAP[_p] * ndays:,.0f})")
        print()

    def _cap_free(pl_: str, gt: str, st: datetime, dur: timedelta,
                  rate_: float):
        """Earliest start >= st whose whole span keeps every IN-MONTH plant-day
        at or under the daily cure cap.  None => no such start; the caller then
        leaks (soft) or drops (strict).  Identical fence to `_takt_free`: it is
        only ever consulted for a campaign that already fits inside the month."""
        capq = DAY_CAP.get(pl_, 0.0)
        if capq <= 0:
            return st
        span = dur.total_seconds() / 3600.0
        h = max(0.0, (st - t0).total_seconds() / 3600.0)
        month_h = ndays * 24.0
        for _ in range(NDAY_LEDGER + 2):              # bounded: one jump per day
            if h + span > month_h:
                return None
            bad = -1
            d0, d1 = int(h // 24), int((h + span - 1e-9) // 24)
            for d in range(d0, min(d1, ndays - 1) + 1):
                lo, hi = max(h, d * 24.0), min(h + span, (d + 1) * 24.0)
                if hi > lo and _dq[pl_][d] + (hi - lo) * rate_ > capq + 1e-6:
                    bad = d
                    break
            if bad < 0:
                return max(st, t0 + timedelta(hours=h))
            h = (bad + 1) * 24.0                      # jump past the full day
        return None

    def _takt_commit(pl_: str, gt: str, st: datetime, en: datetime) -> None:
        """Charge the seat to every partition, always -- even for ungoverned and
        backfill placements, or the profile stops describing the plan."""
        h0 = max(0, int((st - t0).total_seconds() // 3600))
        h1 = min(NH, int((en - t0).total_seconds() // 3600) + 1)
        for k in _pkeys(pl_, gt):
            if k in _cnt:
                for x in range(h0, h1):
                    _cnt[k][x] += 1
        # ---- DAILY CURE-RATE LEDGER, charged at the SAME chokepoint --------
        # This is the one place every seat passes through, which is exactly the
        # property the docstring above claims for the takt profile. Adding a
        # second commit site for the day cap would reintroduce the defect that
        # docstring exists to prevent, so the two ledgers share this one.
        # ---- FEED-CEILING LEDGER, charged at the SAME chokepoint ----------
        # Every seat -- governed, ungoverned, split-to-fit and backfill --
        # passes through here, which is the whole reason the takt and day-cap
        # ledgers live at this site rather than at their own consult points.
        if FEED_CEIL:
            _feed_commit(pl_, gt, st, en, rate_of(pl_, gt))
        if DAY_CAP.get(pl_, 0.0) > 0:
            _r = rate_of(pl_, gt)
            h0f = max(0.0, (st - t0).total_seconds() / 3600.0)
            h1f = max(h0f, (en - t0).total_seconds() / 3600.0)
            d = int(h0f // 24)
            while d < NDAY_LEDGER and d * 24.0 < h1f:
                lo, hi = max(h0f, d * 24.0), min(h1f, (d + 1) * 24.0)
                if hi > lo:
                    # HOLIDAY: a paused campaign cures NOTHING on the closed day,
                    # so the day-cap ledger must be charged working hours, not
                    # clock hours. Identity with no calendar configured; the
                    # governor itself still ships OFF (PLANNER_L5_DAY_CAP).
                    _dq[pl_][d] += holiday.work_seconds(
                        pl_, t0 + timedelta(hours=lo),
                        t0 + timedelta(hours=hi)) / 3600.0 * _r
                d += 1

    # ---- CARRY-IN: the other half of the rolling horizon --------------------
    # `carry_out.parquet` has been emitted since v11 and NOTHING read it, so the
    # horizon was a window on the way out and a closed box on the way in. A press
    # finishing last month's campaign at hour 9 is NOT free at hour 0, and the GT
    # it is holding costs no mould change if the next campaign is the same GT.
    # Both are opening state, and both are what lets the plant hold plant-level
    # lot sizes AND 100 % fulfilment: its month is a window on a continuous
    # process, ours starts cold (PARTITION §4h).
    #
    # HONEST SCOPE. This models the PRESS state only -- occupancy and mounted GT.
    # It does NOT credit the carried tyres as supply; those arrive as opening GT
    # through `opening_gt`, which is a separate master and already loaded.
    #
    # NOT MEASURABLE ON JULY 2026, and this is a data fact, not an excuse: the
    # benefit accrues to the month AFTER the one that emits the file, and the
    # demand series ends at 2026-07 because demand is derived from cured MES and
    # MES ends 2026-07-31. Point `PLANNER_CARRY_IN` at a prior month's
    # `carry_out.parquet` to arm it; absent, it is a clean no-op.
    _ci = os.environ.get("PLANNER_CARRY_IN", "")
    _cif = Path(_ci) if _ci else (ROOT / "masters" / "carry_in" /
                                  f"carry_in_{a.month}.parquet")
    n_ci = 0
    n_warm = 0
    if _cif.exists():
        for _r in pl.read_parquet(_cif).iter_rows(named=True):
            _pr, _en = str(_r["press"]), _r["ends"]
            # THE MOULD IS MOUNTED WHETHER OR NOT THE PRESS IS BUSY. Setting
            # last_gt only for the still-running presses gave the idle ones a
            # free mould change (see the emitter's note). Occupancy and mounted
            # GT are two different facts and only the first one ends at t0.
            last_gt[_pr] = _r["gt_code"]
            n_warm += 1
            if _en is None or _en <= t0:
                continue                   # finished before we start; free at t0
            free[_pr] = max(free.get(_pr, t0), _en)
            n_ci += 1
            # A GT WHOSE CURE CAMPAIGN IS RUNNING AT t0 IS WARM BY DEFINITION.
            #
            # THE DEFECT, measured 2026-08-14 on press 210:
            #     CARRIED  GT 2258 RAN HPE   t0 -> 5.67 h   (press is curing it NOW)
            #     SEATED   GT 2258 RAN HPE   starts 11.86 h  <- the cold-start wall
            # Same GT, same press, same mould, zero mould change needed -- and a
            # 6.19 h hole between them. `earliest_cure` charged the full
            # tau* + build_band wall because GT 2258 holds no OPENING STOCK, and
            # opening stock is the only supply signal that test reads.
            # But a press mid-campaign is being fed right now: building is already
            # delivering that GT, or the campaign could not be running. The `_warm`
            # set encodes exactly this ("mid-run at t0 -> released at tau_min") and
            # was populated only from the BUILDING side (machine_warm,
            # build_carry_out) -- nothing told it about the CURE carry-in.
            # All 5 campaigns starting in the 11.0-12.5 h band were GTs already
            # curing at t0, so this is the whole of that band.
            _warm.add((_r["plant"], _r["gt_code"]))
        print(f"  carry-in: {n_ci} press(es) still running last month's campaign, "
              f"{n_warm - n_ci} idle with a mounted mould  ({_cif.name})")
    elif _ci:
        print(f"  carry-in: {_cif} not found -- running cold (no-op)")
    # How many GTs may each press run? Fewer = scarcer = spend it first.
    _press_n_gt: dict = {}
    # distinct presses a GT has visited, and the plant's own concurrency answer
    _gt_presses: dict = {}
    _obs_max: dict = {}
    _pcap: dict = {}
    try:
        for _r in pl.read_parquet(D / f'cap_mould_{a.month}.parquet').iter_rows(named=True):
            _obs_max[(_r['plant'], _r['gt_code'])] = int(_r.get('observed_max') or 0)
    except Exception:
        pass
    for (_pp, _gg), _prs in elig.items():
        for _pr2 in _prs:
            _press_n_gt[(_pp, str(_pr2))] = _press_n_gt.get((_pp, str(_pr2)), 0) + 1
    busy: dict[tuple, list] = {}                      # (plant,gt) -> intervals
    placed, unplaced, carry, changes = [], [], [], 0
    # Unseated demand per (plant, GT). Read by PROTECT_MOULD to tell a mounted
    # mould that is still WANTED from one that is spent.
    _pending: dict[tuple, float] = {}
    _gt_total: dict = {}
    for _j in jobs:
        _k = (_j['plant'], _j['gt_code'])
        _pending[_k] = _pending.get(_k, 0.0) + float(_j['qty'])
        _gt_total[_k] = _gt_total.get(_k, 0.0) + float(_j['qty'])

    # ---- MONTH-END COMPLETABILITY (PLANNER_L5_MONTHEND_FIT) -------------
    # Window start for the boundary preference. `_me_on` false => `_fits` below
    # is the constant 0, so the candidate key is character-for-character the
    # shipped one and the layer is byte-identical. Proven, not assumed:
    # ME_pref7/ME_pref5/ME_pref10 are byte-identical to ME_base on all 7
    # parquets (see the measurement block at the key).
    _me_on = MONTHEND_FIT in ("prefer", "require", "split")
    _me_win = month_end - timedelta(days=MONTHEND_WIN_D)
    _me_stat = {"spill": 0, "refused": 0.0, "split": 0, "split_q": 0.0}

    def _me_emit(p, gt, mset, pr, st, en, qty, rate):
        """One campaign row -- or TWO, cut at `month_end`, under
        PLANNER_L5_MONTHEND_FIT=split.

        THE PRESS COMMITMENT [st, en] IS IDENTICAL EITHER WAY. `free[pr]`,
        `busy`, `last_gt`, `press_tyres` and `_takt_commit` are all booked by
        the caller against the whole block, so nothing about WHERE the campaign
        sits changes -- only the row boundary. That is what separates this from
        PLANNER_L5_MAX_CAMPAIGN_H (-43,104 PCR BUILT), which moved the seats.
        Both pieces are the same GT on the same press and are contiguous, so no
        mould change is charged between them.
        """
        def _row(s_, e_, q_):
            return {"plant": p, "gt_code": gt, "mould_set": mset, "press": pr,
                    "start_ts": s_, "end_ts": e_, "qty": float(q_),
                    "hours": round((e_ - s_).total_seconds() / 3600, 2)}
        if (not _me_on or MONTHEND_FIT != "split" or en <= month_end
                or st >= month_end or st < _me_win):
            return [_row(st, en, qty)]
        _q1 = int(holiday.work_seconds(p, st, month_end) / 3600.0 * rate)
        _fl = min_lot.get(p, 0.0)
        # B12 IS A CAP, NOT A KNOB -- never create a sub-floor piece.
        if _q1 < _fl or (float(qty) - _q1) < _fl:
            return [_row(st, en, qty)]
        _me_stat["split"] += 1
        _me_stat["split_q"] += float(qty) - _q1
        return [_row(st, month_end, _q1),
                _row(month_end, en, float(qty) - _q1)]

    for j in jobs:
        p, gt = j["plant"], j["gt_code"]
        cand = elig.get((p, gt), [])
        if not cand:
            unplaced.append({**j, "reason": "no eligible press"})
            continue
        rate = rate_of(p, gt)                         # tyres per press-hour
        dur = timedelta(hours=j["qty"] / max(rate, 1e-9))
        # Concurrency is bounded by MOULDS ONLY (R3).
        # A volume-based press cap was tried -- limit a GT to the presses its
        # month's volume can keep busy -- and it did cut the worst spread from 26
        # presses to 18. But it also starved high-volume GTs of throughput and
        # pushed GTs below 95% of requirement from 7 to 9. Press spread is a
        # cosmetic concern; requirement coverage is not.
        cap = moulds.get((p, gt), 1)

        floor_ts = earliest_cure(p, gt, j['qty'], j['qty'] / max(rate, 1e-9))
        # ---- WARM-START FLOOR, OPENING CAMPAIGN ONLY -----------------------
        # Measured July: building runs at FULL rate on day 1 (PCR 12,946) and
        # finishes the month at 77.6 % machine occupancy with 1,830 h idle. The
        # tyres exist. What idles 50 of 86 presses for the first 11.86 h is the
        # CURE-side floor, not supply.
        #
        # tau* + build_band is the steady-state coupling buffer. It is the right
        # phasing for a campaign that follows another on the same press, and the
        # WRONG floor for the campaign that OPENS a press at t0: there is no
        # preceding campaign to be buffered against, and building only needs to
        # deliver one legal B12 run (PCR 2.35 h / TBR 2.99 h) before curing can
        # begin.
        #
        # Lowering the floor for EVERY campaign was measured and is worse
        # (-0.9 pt): it shifts the whole month earlier and compresses the window
        # building has to fill. So it is applied ONLY to the opening campaign on
        # a press -- the one whose press is still free at t0 -- leaving the
        # steady-state phasing of every later campaign untouched.
        #
        # This claims NO pre-month building: the tyres are built after t0, in the
        # month, by machines that are already idle at that hour.
        _opening = all(free.get(_pr, t0) <= t0 for _pr in cand)
        if WARM_OPEN and _opening:
            _ct = PCT.build_ct_s(p, gt, None) or plant_cad_s[p]
            _fl = float(CONFIG.thresholds.min_lot_units.get(p, 0) or 0)
            floor_ts = min(floor_ts,
                           t0 + timedelta(hours=tau_min_h[p] + _fl * _ct / 3600.0))
        # ---- L5<->L6 FEEDBACK -------------------------------------------
        # L6 hands back campaigns building could not feed and asks for them to be
        # RESEATED LATER, not failed. The hint is a per-GT delay in hours, so the
        # cure seat moves instead of the shortfall being discovered slice by slice
        # in L7. Absent file = clean no-op, so a single L5 run is unchanged.
        if _DELAY:
            _d = _DELAY.get((p, gt), 0.0)
            if _d:
                # ---- PROTECT THE OPENING WINDOW (PLANNER_L56_PROTECT_FIRST_H) --
                # The hint is keyed (plant, gt_code) and applied as a FLOOR, so a
                # single unfed LATE campaign delays EVERY campaign of that GT --
                # including a day-one campaign that has opening stock, a running
                # mould and could legally start at 07:00. Measured on July: the
                # loop dropped presses running at 07:00 from 26 to 1, and day-one
                # curing from 7,873 to 3,226, to gain +6,320 monthly BUILT.
                #
                # A campaign whose UNDELAYED floor already sits inside the opening
                # window is exempt. Campaigns later in the month still take the
                # hint, so the loop can still repair the interior.
                #
                # This is a time guard, not the real fix. The real fix is to make
                # the hint CAMPAIGN-specific -- (plant, gt, lot index, seq) --
                # so a later failure never moves an earlier success. The guard is
                # cheap and testable; the re-grain is a larger change to the
                # l56_loop contract.
                if PROTECT_FIRST_H <= 0.0 or floor_ts >= t0 + timedelta(
                        hours=PROTECT_FIRST_H):
                    floor_ts = max(floor_ts, t0 + timedelta(hours=_d))
        best = None
        # DAY CAP: the over-cap candidate pool. Stays None when the governor is
        # off, so every branch below is bit-identical to the shipped layer.
        best_ung = None
        for pr in cand:
            # ---- MOULD CHANGE IS A PRESS COST, NOT A SUPPLY COST -------------
            # (PLANNER_L5_CHG_PARALLEL, ships 1)
            #
            # The change was added AFTER the max with floor_ts, which serialises
            # two INDEPENDENT resources:
            #     free[pr]  -- when the PRESS is available   (the change eats this)
            #     floor_ts  -- when the GREEN TYRE exists    (the change eats none)
            # On day 1 free == t0 while floor_ts is 11.86 h away, so the change
            # fits entirely inside an idle press window and costs nothing. The
            # old form charged it anyway and pushed the seat to 17.86 h.
            #
            # The day-1 first-start histogram factorises exactly as
            # (0 or tau*-wall) + (0 or mould change) -- 0.0 / 6.0 / 11.86 / 17.86
            # -- which is the signature of this serialisation.
            #
            # floor_ts is UNTOUCHED, so a campaign still never starts before its
            # supply exists. That is why this is not another tau* relaxation:
            # FLOOR_BASIS=min/slice moved the supply boundary and lost 2,478-6,050
            # BUILT; this moves only the press-availability term.
            #
            # SHIPS OFF. MEASURED 2026-08-14 -- THE ARITHMETIC IS RIGHT AND THE
            # RESULT IS NEGATIVE ON 3 OF 4 PLANT-MONTHS:
            #     Jul PCR -3,665 BUILT (starved 8,927 -> 12,492), L11 28 -> 27
            #     Jul TBR   -152
            #     Aug PCR   -428        (starved  8,580 ->  8,084), L11 26 -> 27
            #     Aug TBR    +53
            # Predicted +1,105 Jul / +1,381 Aug; neither appeared. Letting the
            # change run inside the idle window pulls seats EARLIER, which lands
            # campaigns tighter against their supply boundary -- the same failure
            # that killed every FLOOR_BASIS variant. The damage is visible on
            # July (5.23 pt of headroom) and nearly invisible on August (2.55 pt),
            # which is itself evidence for grading changes on the month that has
            # room to move.
            _chg = (mchg_s(p, pr)
                    if last_gt.get(pr) not in (None, gt) else 0.0)
            # ---- DO NOT STEAL A MOUNTED MOULD SOMEONE ELSE STILL NEEDS ------
            # (PLANNER_L5_PROTECT_MOULD, default off until measured on 2 months)
            #
            # MEASURED MECHANISM, July day 1. Of 27 cold PCR presses, the 14 that
            # start on the mould already fitted begin at hour 0.0; the 13 that
            # need a different mould begin at hour 7.0 -- the 6 h change IS the
            # wait. 13 x 6 h = 78 press-h = ~473 tyres on PCR, 42 h / ~255 on TBR.
            #
            # Cheapest-insertion already charges OUR change (`_chg` above), so it
            # prefers the right press when one is free. What it does not price is
            # the change it FORCES ON SOMEONE ELSE: a high-volume GT takes press X
            # early in the -qty order, pays 6 h, and the GT whose mould is already
            # on X must then pay 6 h of its own later. That is two changes bought
            # with one decision, and only one of them is in the cost.
            #
            # So charge both when the incumbent mould still has unseated demand.
            # This is a RESOURCE-SELECTION tie-break, not a queue re-sort: the
            # -qty order is untouched, which is what separates it from
            # PLANNER_D1_DEPTH (-11.9 pt) -- that experiment displaced -qty
            # itself. Selection between equally-legal presses is free.
            #
            # SHIPS OFF. MEASURED 2026-08-14, July, and the MECHANISM WORKS while
            # the RESULT IS NEGATIVE -- the same shape as CHG_PARALLEL above:
            #     cold presses on their mounted mould  PCR 14/27 -> 19/27
            #                                          TBR 19/26 -> 20/26
            #     mould-change hours                   PCR  740h -> 660h
            #                                          TBR  535h -> 523h
            #     day-1 cure                           PCR 11,126 -> 10,764
            #                                          TBR  2,872 ->  2,779
            #     day-1 occupancy                      PCR  88.9% ->  86.5%
            #     in-month                             PCR  96.2% ->  95.4%
            #                                          TBR  95.9% ->  95.3%
            # Predicted +473 PCR / +255 TBR on day 1; neither appeared. Saving 80
            # press-hours of changeover costs more than it returns, because the
            # displaced high-volume GT lands on a press that seats it LATER, and
            # a late seat on a big campaign outweighs a whole shift of setup.
            # "Selection is free" is true of the choice; it is NOT true of the
            # deferral the choice forces. Do not retry without also re-ordering,
            # and re-ordering is PLANNER_D1_DEPTH, which lost 11.9 pt.
            if PROTECT_MOULD and _chg > 0.0:
                _inc = last_gt.get(pr)
                if _inc is not None and _pending.get((p, _inc), 0.0) > 0.0:
                    _chg += mchg_s(p, pr)
            # ---- PLANT HOLIDAY (rule G3) -- see planner/cmbc/holiday.py -----
            # A closed plant-day is a hole in the press calendar, and `free[pr]`
            # is a single "available from" instant, so the hole cannot be
            # expressed as an interval here the way it can on L7's machines. It
            # is expressed as ARITHMETIC instead: the mould change consumes
            # WORKING time (fitters do not work the closure either), the seat is
            # pushed to the first working instant, and `en` below is reached by
            # spending the campaign's press-hours through the calendar rather
            # than by adding them to the clock. A campaign that meets the
            # closure therefore PAUSES and resumes -- its productive hours are
            # unchanged and its wall-clock end moves +24 h.
            # With no calendar configured `next_free` and `add_work` are the
            # identity, so both branches below are character-for-character the
            # arithmetic that shipped. That is proven, not assumed: HOLbase2 is
            # byte-identical to HOLbase on all 14 parquets.
            if CHG_PARALLEL:
                st = max(holiday.add_work(p, holiday.next_free(
                    p, free.get(pr, t0)), _chg), floor_ts)
            else:
                st = holiday.add_work(p, holiday.next_free(
                    p, max(free.get(pr, t0), floor_ts)), _chg)
            st = holiday.next_free(p, st)
            # ---- TAKT: move the seat later, but ONLY inside the month -------
            # Consulted only if the UNGOVERNED placement already fits. A
            # campaign that was going to overrun is left exactly as the shipped
            # layer had it, so the governor cannot create horizon overflow.
            # GATED ON month_end, NOT horizon, in every mode. The takt governor
            # levels press concurrency INSIDE the month; letting it push a
            # campaign into the planning tail would move volume out of the
            # reported month to satisfy a levelling preference. Under
            # HORIZON_MODE=truncate the two boundaries coincide, so this is
            # bit-identical to the shipped behaviour.
            if st + dur <= month_end:
                _g = _takt_free(p, gt, st, dur)
                if _g is not None and _g + dur <= month_end:
                    if _g > st:
                        _takt_moved[0] += 1
                    st = _g
                elif _g is None:
                    _takt_nowin[0] += 1
            # ---- FEED CEILING: move the seat off a rim/window whose eligible
            # BUILDING machines cannot feed it. Same fence as takt, for the same
            # reason: consulted ONLY when the ungoverned placement already fits
            # inside the month, and it may only move the campaign to another
            # window that also fits, so it can never turn deliverable volume
            # into an overrun (DO-NOT #23).
            if FEED_CEIL and st + dur <= month_end:
                _f = _feed_free(p, gt, st, dur, rate)
                if _f is not None and _f + dur <= month_end:
                    if _f > st:
                        _feed_moved[0] += 1
                    st = _f
                elif _f is None:
                    _feed_nowin[0] += 1
            # ---- DAY CAP: move the seat off a day already at its ceiling ----
            # Same fence as takt, for the same reason: consulted only when the
            # ungoverned placement already fits inside the month, and it may
            # only move the campaign to another window that also fits. It can
            # therefore never turn a deliverable campaign into an overrun one.
            # A press with no in-cap window is scored into a SEPARATE candidate
            # (`best_ung`) rather than dropped, so an in-cap seat always beats an
            # over-cap one however much later it is, and the over-cap seat is
            # still available as a last resort. Collapsing the two into one
            # comparison would defeat the governor entirely: the blocked press is
            # normally the EARLIEST one, so on a plain min-start key it wins.
            _over_cap = False
            if DAY_CAP.get(p, 0.0) > 0 and st + dur <= month_end:
                _c = _cap_free(p, gt, st, dur, rate)
                if _c is not None and _c + dur <= month_end:
                    if _c > st:
                        _cap_moved[0] += 1
                    st = _c
                else:
                    _over_cap = True
            # ---- MOULD LIFE (PLANNER_MOULD_LIFE_CYCLES) ---------------------
            # A mould is changed on WEAR as well as on a GT switch: every
            # 3,000 press cycles. A press cures CAVITIES=2 tyres per cycle, so
            # that is one change per 6,000 TYRES on that press, costing the same
            # as any other mould change (mchg_s, per press).
            #
            # Charged on the press's CUMULATIVE tyres, not per campaign: a press
            # that has already cured 5,500 tyres hits its change 500 tyres into
            # the next campaign, not at 6,000 of that campaign. Counting per
            # campaign would silently reset the mould's life at every GT switch
            # and undercount the changes.
            _wear_s = 0.0
            if MOULD_LIFE_TYRES > 0:
                _c0 = press_tyres.get(pr, 0.0)
                _n_wear = (int((_c0 + j["qty"]) // MOULD_LIFE_TYRES)
                           - int(_c0 // MOULD_LIFE_TYRES))
                _wear_s = _n_wear * mchg_s(p, pr)
            # HOLIDAY: spend the campaign's press-hours THROUGH the calendar.
            # `dur` is press-HOURS (qty/rate) and stays exactly what it was; only
            # the wall-clock end moves. Identity with no calendar configured.
            en = holiday.add_work(p, st, dur.total_seconds() + _wear_s)
            # MONTH-END COMPLETABILITY: 0 = finishes inside the reported
            # month, 1 = spills past it. Ranked ABOVE `st` in the key below.
            #
            # MEASURED NO-OP -- see the PLANNER_L5_MONTHEND_FIT block at the top
            # of this file for the full table. `en == st + dur` with `dur`
            # identical on every candidate press, so `_fits` is a MONOTONE
            # function of `st` and ranking it above `st` re-orders nothing:
            # ME_pref7 is byte-identical to ME_base on all 11 artefacts at
            # N = 5, 7 and 10. Kept, gated off, because deleting a rejected
            # experiment destroys the evidence and invites a blind re-run.
            #
            # `_me_on` false => this is the constant 0 => the key is
            # character-for-character the shipped one. Verified byte-identical.
            _fits = 1 if (_me_on and st >= _me_win and en > month_end) else 0
            # R3: at most `cap` presses may run this GT concurrently
            overlap = sum(1 for (s2, e2) in busy.get((p, gt), [])
                          if s2 < en and st < e2)
            if overlap >= cap:
                continue
            # ---- DISTINCT-PRESS CAP (PLANNER_L5_MAX_PRESS_PER_GT) -----------
            #
            # MEASURED 2026-08-14 against the plant's own `observed_max`:
            #                       PCR      TBR
            #   plant peak concurrency  187      169
            #   OUR peak concurrency    136      119
            #   OUR DISTINCT presses    189      167   <- one mould mount each
            #
            # We touch as many presses as the plant runs SIMULTANEOUSLY, while
            # only ever running 73 % of that number at once. GT 1513 is the clear
            # case: 35 moulds, 44 eligible presses, plant ran 21 together; we run
            # 15 together but visit 26 different presses over 31.8 days. Eleven
            # extra mould mounts to get six fewer presses working.
            # Nothing chooses this -- concurrency is a by-product of seating
            # order, and a press that finished this GT gets handed to another GT
            # before this GT's next lot comes round, so the mould is re-mounted
            # somewhere else.
            # Cap the DISTINCT presses a GT may visit at MAXPG x its own
            # observed_max (the plant's revealed answer for that GT), so the
            # parallelism we do use is concentrated on a stable press set.
            # 0 = off (ships), 1.0 = exactly the plant's concurrency.
            # DERIVED, NOT MINED. Seeding this from `observed_max` was the same
            # mistake as tau* and min_lot: a statistic from ONE history window
            # wired in as a hard cap. It would also be wrong the moment a GT's
            # demand moves, which is every month.
            # The floor is arithmetic instead:
            #     k_min = ceil( need / (press rate x usable hours) )
            # -- the fewest presses that can physically finish this GT inside the
            # horizon, computed from THIS month's demand and THIS month's hours.
            # GT 1513: 55,663 / (6.3 x 744) = 11.9 -> 12 presses; we touched 26.
            # GT 2367:    883 / (6.3 x 744) = 0.19 ->  1 press;  we used 1, and
            # the plant's 3 was batching preference, not a requirement.
            # MAXPG is then head-room ON TOP of the physical minimum, so the rule
            # scales with demand and never encodes one month's habit.
            # FEED CAP: a GT released early at tau_min may only occupy as many
            # presses as its OWN building machines can actually feed. Applies to
            # seats that start inside the first tau*+band window -- after that the
            # normal wall would have held anyway, so the cap must not bind there
            # or it would throttle the whole month instead of the ramp.
            if _early_cap and st < t0 + timedelta(hours=tau_h[p] + bband[p]):
                _fc = _early_cap.get((p, gt))
                if _fc is not None:
                    _fs = _gt_presses.setdefault((p, gt), set())
                    if pr not in _fs and len(_fs) >= _fc:
                        continue
            if MAXPG > 0.0:
                _seen = _gt_presses.setdefault((p, gt), set())
                _lim = _pcap.get((p, gt))
                if _lim is None:
                    _r1 = rate_of(p, gt)
                    _hz_h = (horizon - t0).total_seconds() / 3600.0
                    _kmin = max(1, math.ceil(_gt_total.get((p, gt), 0.0)
                                             / max(_r1 * _hz_h, 1e-9)))
                    _lim = max(1, min(int(cap), int(math.ceil(MAXPG * _kmin))))
                    _pcap[(p, gt)] = _lim
                if pr not in _seen and len(_seen) >= _lim:
                    continue
            # Prefer, among presses free at the same moment, one whose last GT
            # shares this rim: the mould change is then a same-size change
            # (PCR 22 vs 42 min on CONTI, 28 vs 60 on BJ) instead of a
            # different-size one. Strictly a TIEBREAK -- it never delays a
            # campaign, so it cannot cost fulfilment the way queue-ordering did.
            same_rim = 0 if (last_gt.get(pr) is not None and _rim.get(last_gt.get(pr), "@") == _rim.get(gt, "#")) else 1
            # TRIED AND REVERTED: adding per-press mould-change cost as a
            # tiebreak here (st, same_rim, mcost, pr). It made things WORSE --
            # PCR changes 98 -> 110, mould-hours 1,130 -> 1,225, fulfilment
            # 98.4 -> 98.0%. A cheap-to-change press attracts every GT, so work
            # spreads over more presses and creates more changes than the
            # cheaper rate saves. Press CONTINUITY dominates press RATE.
            # ---- SCARCITY: load the press with the FEWEST alternatives first
            # (PLANNER_L5_SCARCE_PRESS, default off until measured on 2 months)
            #
            # OBSERVED FROM THE PLANT'S OWN DATA, 2026-08-14. Presses 313/311/
            # 308/192 are the FASTEST on the PCR floor -- 184-190 tyres/day
            # against a fleet median of 154 -- and the plant ran 192 for 241 days
            # and 308 for 201. We leave all four idle for the last five days of
            # July: 480 press-hours, ~3,000 tyres.
            # It is NOT eligibility and NOT moulds: GT 1995 has 14 moulds and we
            # run it on 4 presses; GT 1765 has 8 and we use 6. 40 presses are
            # eligible for the ROYL GTs and we load 11.
            # It is the tiebreak above. A press eligible for ONE GT never wins
            # `(st, same_rim, pr)` against a press eligible for six -- it is
            # never the best LOCAL choice -- so it takes whatever is left, and at
            # month end nothing is left. Greedy has no notion of resource
            # scarcity; it treats every eligible press as interchangeable.
            # A narrow press has no alternative use, so spending it early costs
            # nothing and saves a wide press for work only a wide press can take.
            # Ranked BELOW `st` and `same_rim`, so it can never delay a campaign
            # or cost a same-size changeover.
            # CONCENTRATION, NOT A TIEBREAK. Ranked below `st` the scarcity key
            # never fires -- two presses are almost never free at the same
            # instant, and the measured effect was 8 press-hours of 480.
            # The real defect is that GT 1765/1995 are spread over 40 eligible
            # presses, finish by day 26, and leave the four presses that can do
            # NOTHING ELSE with nothing to do for five days.
            # Fixing that needs a narrow press to win even when it is free
            # LATER -- so bucket the start time by SCARCE_SLACK_H and let
            # scarcity decide inside the bucket. Bounded by construction: a
            # campaign can be delayed at most SCARCE_SLACK_H, never more.
            _scar = _press_n_gt.get((p, pr), 99) if SCARCE_PRESS else 0
            # DAY CAP: over-cap candidates are ranked in their OWN pool. The two
            # pools use the identical key, so turning the cap off reproduces the
            # shipped choice exactly (`best_ung` is then never populated).
            _pool = best_ung if _over_cap else best
            if SCARCE_PRESS and SCARCE_SLACK_H > 0.0:
                _bk = int(st.timestamp() // (SCARCE_SLACK_H * 3600.0))
                _key = (_fits, _bk, _scar, same_rim, st, pr)
                _bst = (_pool[6], _pool[5], _pool[4], _pool[3], _pool[0],
                        _pool[1]) if _pool is not None else None
            else:
                _key = (_fits, st, same_rim, _scar, pr)
                _bst = (_pool[6], _pool[0], _pool[3], _pool[4], _pool[1])                     if _pool is not None else None
            if _pool is None or _key < _bst:
                _pool = (st, pr, en, same_rim, _scar,
                         int(st.timestamp() // (max(SCARCE_SLACK_H, 1e-9) * 3600.0)),
                         _fits)
                if _over_cap:
                    best_ung = _pool
                else:
                    best = _pool
        # ---- DAY CAP: no press could hold this campaign under the ceiling ----
        # soft   -> place it ungoverned and COUNT the tyres. A shape preference
        #           must never become lost demand (same doctrine as takt), and
        #           the leak is the honest measure of how far the cap is from
        #           constructible -- a large leak IS the negative result.
        # strict -> refuse it. This is the literal "cap the day, drop the
        #           excess" instruction, and it can only lose tyres vs soft.
        if best is None and best_ung is not None:
            if DAY_CAP_STRICT:
                _cap_drop[0] += float(j["qty"])
                unplaced.append({**j, "reason": "no in-cap day window"})
                continue
            _cap_leak[0] += float(j["qty"])
            best = best_ung
        if best is None:
            # every eligible press is mould-blocked -- defer behind the earliest
            # concurrent run rather than breaking R3
            iv = sorted(busy.get((p, gt), []), key=lambda t: t[1])
            if not iv:
                unplaced.append({**j, "reason": "no feasible press"})
                continue
            after = iv[max(0, len(iv) - cap)][1]
            # NOTE: this is a FALLBACK path and does not execute for either
            # shipped month -- instrumented 2026-08-13, 0 decisions. The live
            # press choice is the loop above at `for pr in cand`, which ALREADY
            # scores availability PLUS the changeover the choice forces:
            #     st = max(free[pr], floor); if mounted != gt: st += mchg_s(p, pr)
            # i.e. cheapest-insertion is already implemented. A "mould-aware
            # press selection" proposal is therefore a no-op -- do not re-add it.
            pr = min(cand, key=lambda x: (max(free.get(x, t0), after, floor_ts), x))
            st = max(free.get(pr, t0), after, floor_ts)
            if last_gt.get(pr) not in (None, gt):
                st = holiday.add_work(p, holiday.next_free(p, st),
                                      mchg_s(p, pr))
            st = holiday.next_free(p, st)
            best = (st, pr, holiday.add_work(p, st, dur.total_seconds()),
                    1, 0, 0, 0)
        st, pr, en = best[0], best[1], best[2]
        # ---- MONTH-END COMPLETABILITY: resolve the winner ------------------
        # `_fits` leads the candidate key, so if ANY eligible press could seat
        # this campaign inside the month the winner is one of them. Reaching
        # here with a spill therefore means NO press could -- which is what
        # `require` and `split` act on.
        if _me_on and st >= _me_win and en > month_end:
            _me_stat["spill"] += 1
            if MONTHEND_FIT == "require" and MONTHEND_STRICT:
                # The literal "require" reading. It can only LOSE tyres -- a
                # campaign no press can finish in-month is simply not placed --
                # and it is measured for exactly that reason. Same doctrine as
                # DAY_CAP_STRICT: a shape preference that becomes lost demand
                # must be PRICED, never hidden behind a soft fallback.
                _me_stat["refused"] += float(j["qty"])
                unplaced.append({**j, "reason": "cannot finish in month"})
                continue
            # `split` is applied at the EMISSION sites (`_me_emit`), not here:
            # a campaign can also reach `placed` through the horizon-truncation
            # branch below, and 13 of the first 17 splits did exactly that.
        if en > horizon:
            # SPLIT TO FIT -- never discard a lot whole.
            # L5 previously placed a lot all-or-nothing, so a single-lot GT whose
            # one lot overran the horizon got NOTHING: three GTs
            # (385/55R22.5JTL, GT 2568 RAN AT, GT 2666 RAN HT) disappeared from
            # the plan entirely despite being above min_demand, and seven more
            # landed below 95% of requirement for the same reason.
            # Place whatever fits and requeue the remainder; only the part that
            # genuinely cannot fit is reported unplaced.
            # CARRY-OUT -- the horizon is a WINDOW, not a closed box.
            # Measured: a PCR cure campaign is 8.0 days (TBR 10.6), so nothing can
            # START after day 23 (TBR day 20) without overrunning hour 744, and it
            # was rejected outright. Meanwhile day 30-31 press occupancy is 46-87%
            # (TBR 43/14/1%) -- 2,394 PCR + 3,887 TBR idle press-hours, 2-4x the
            # demand being discarded. The plant does not have this problem because
            # its campaigns are already running at the month boundary.
            # A campaign that STARTS inside the horizon may FINISH outside it; the
            # in-horizon portion is delivered and the tail becomes next month's
            # opening state.
            # HOLIDAY: the horizon holds fewer PRESS-HOURS than wall-clock hours.
            # Sizing the piece on the wall-clock gap would promise tyres for the
            # closed day and then not deliver them -- the piece would be booked
            # at `can` and cure `can - rate x 24`. `work_seconds` is the identity
            # with no calendar configured.
            fits_h = holiday.work_seconds(p, st, horizon) / 3600.0
            can = int(fits_h * rate)
            floor_q = min_lot.get(p, 0.0)
            if HORIZON_MODE == "strict":
                # (A) DO NOT START WHAT CANNOT FINISH. The whole lot is refused
                # here; the backfill pass below then re-places whatever fits as
                # horizon-bounded pieces, so nothing that CAN be delivered in the
                # month is lost -- only the part that cannot.
                unplaced.append({**j, "reason": "cannot finish in month"})
                continue
            if CARRY_OUT and HORIZON_MODE == "window" and st < horizon \
                    and can >= floor_q:
                if last_gt.get(pr) not in (None, gt):
                    changes += 1
                free[pr] = en                    # press stays busy into next month
                last_gt[pr] = gt
                _gt_presses.setdefault((p, gt), set()).add(pr)
                press_tyres[pr] = press_tyres.get(pr, 0.0) + float(j["qty"])
                busy.setdefault((p, gt), []).append((st, en))
                _takt_commit(p, gt, st, en)
                placed.append({"plant": p, "gt_code": gt,
                               "mould_set": j["mould_set"], "press": pr,
                               "start_ts": st, "end_ts": en, "qty": j["qty"],
                               "hours": round((en - st).total_seconds() / 3600, 2)})
                carry.append({**{k: j[k] for k in ("plant", "gt_code",
                                                   "mould_set", "seq")},
                              "press": pr, "qty": float(j["qty"] - can),
                              "carry_from": horizon, "ends": en})
                continue
            if can >= floor_q and j["qty"] - can >= 0:
                en = holiday.add_work(p, st, can / max(rate, 1e-9) * 3600.0)
                if last_gt.get(pr) not in (None, gt):
                    changes += 1
                free[pr] = en
                last_gt[pr] = gt
                press_tyres[pr] = press_tyres.get(pr, 0.0) + float(j["qty"])
                busy.setdefault((p, gt), []).append((st, en))
                _takt_commit(p, gt, st, en)
                placed.extend(_me_emit(p, gt, j["mould_set"], pr, st, en,
                                       float(can), rate))
                left = j["qty"] - can
                if left >= floor_q:
                    unplaced.append({**j, "qty": left,
                                     "reason": "remainder past horizon"})
                continue
            unplaced.append({**j, "reason": "past horizon"})
            continue
        if last_gt.get(pr) not in (None, gt):
            changes += 1
        free[pr] = en
        last_gt[pr] = gt
        _pending[(p, gt)] = max(0.0, _pending.get((p, gt), 0.0) - float(j["qty"]))
        _gt_presses.setdefault((p, gt), set()).add(pr)
        press_tyres[pr] = press_tyres.get(pr, 0.0) + float(j["qty"])
        busy.setdefault((p, gt), []).append((st, en))
        _takt_commit(p, gt, st, en)
        # This campaign spent stock budget. `floor_ts <= t0` is the full-credit
        # case; the second clause is the new PARTIAL-credit interior, where the
        # seat was pulled earlier than the no-stock wall by drawing on stock and
        # must therefore be charged for it. `- gap_tyres` already floors at 0,
        # so a GT holding less than the gap is simply emptied.
        _wall = t0 + timedelta(hours=gap_h_p[p])
        if floor_ts <= t0 or (FULL_AVAIL_RAMP and t0 < floor_ts < _wall
                              and early_budget.get((p, gt), 0.0) > 0.0):
            # CHARGE WHAT THE TEST ASKED FOR, not the plant median. Falls back
            # to gap_tyres only for a GT earliest_cure never priced.
            early_budget[(p, gt)] = max(
                0.0, early_budget.get((p, gt), 0.0)
                - _gapq_of.get((p, gt), gap_tyres[p]))
        placed.extend(_me_emit(p, gt, j["mould_set"], pr, st, en,
                               j["qty"], rate))

    if _me_on:
        print(f"  [month-end fit] mode={MONTHEND_FIT} win={MONTHEND_WIN_D:.0f}d"
              f"  spill={_me_stat['spill']}  split={_me_stat['split']}"
              f" (carried {_me_stat['split_q']:,.0f})"
              f"  refused={_me_stat['refused']:,.0f}")

    # ---- BACKFILL PASS ---------------------------------------------------
    # The main pass drops a campaign whole if it does not fit. But the capacity
    # exists -- 3,056 free PCR press-h against 984 needed -- it is just scattered
    # as tails on presses already 90-96% loaded. A 1,304-tyre campaign fits
    # nowhere; six 217-tyre pieces fit fine.
    #
    # Splitting is legitimate under R9 and correctly priced: a split costs 300 at
    # tier 3, losing the demand costs 2,000/tyre-day at tier 1. Pieces still
    # respect min_cure_lot, so this creates no sub-economic fragments.
    still, recovered = [], 0
    for j in sorted(unplaced, key=lambda x: (x["plant"], -x["qty"], x["gt_code"])):
        if j.get("reason") == "no eligible press":
            still.append(j)
            continue
        p, gt = j["plant"], j["gt_code"]
        rate = rate_of(p, gt)
        # Concurrency is bounded by MOULDS ONLY (R3).
        # A volume-based press cap was tried -- limit a GT to the presses its
        # month's volume can keep busy -- and it did cut the worst spread from 26
        # presses to 18. But it also starved high-volume GTs of throughput and
        # pushed GTs below 95% of requirement from 7 to 9. Press spread is a
        # cosmetic concern; requirement coverage is not.
        cap = moulds.get((p, gt), 1)
        remaining = float(j["qty"])
        floor = float(min_lot[p])
        # PREFER PRESSES ALREADY RUNNING THIS GT -- no mould change, no 6 h cost.
        # PCR tails average 34.9 h = 222 tyres against a 216 floor: six tyres of
        # margin. Charge a mould change and the usable tail drops to 184 tyres,
        # below the floor, so the piece is rejected. Sorting by earliest-free
        # (the first version) therefore found almost nothing -- 483 of 6,945.
        # Same-GT presses first turns a rejected tail into a usable one.
        for pr in sorted(elig.get((p, gt), []),
                         key=lambda x: (last_gt.get(x) != gt, free.get(x, t0), x)):
            if remaining < floor:
                break
            st = max(free.get(pr, t0),
                     earliest_cure(p, gt, remaining,
                                   remaining / max(rate, 1e-9)))
            if last_gt.get(pr) not in (None, gt):
                st = holiday.add_work(p, holiday.next_free(p, st),
                                      mchg_s(p, pr))
            # HOLIDAY: same correction as the split-to-fit path above -- the tail
            # this piece is being fitted into holds PRESS-hours, not clock-hours.
            st = holiday.next_free(p, st)
            gap_h = holiday.work_seconds(p, st, horizon) / 3600.0
            if gap_h <= 0:
                continue
            # whole tyres only -- the backfill was the last source of fractional
            # quantities (42 of 476 campaigns) once L4.5 and L7 were made integral
            fits = int(gap_h * rate)                  # tyres this gap can hold
            take = float(int(min(remaining, fits)))
            if take < floor:
                continue
            # keep the remainder economic: never strand less than a floor
            if remaining - take < floor:
                take = remaining
                if take > fits:
                    continue
            en = holiday.add_work(p, st, take / rate * 3600.0)
            if en > horizon:
                continue
            # ---- DAY CAP applies to the backfill too -------------------------
            # Without this the strict mode is a no-op: every campaign the main
            # pass refuses arrives here as `unplaced` and is re-seated into the
            # same over-cap days, one floor-sized piece at a time. The soft mode
            # still places (never lose volume) and counts the leak.
            if DAY_CAP.get(p, 0.0) > 0:
                _c = _cap_free(p, gt, st, en - st, rate)
                if _c is None:
                    if DAY_CAP_STRICT:
                        continue
                    _cap_leak[0] += float(take)
                else:
                    st = holiday.next_free(p, _c)
                    en = holiday.add_work(p, st, take / rate * 3600.0)
                    if en > horizon:
                        continue
            overlap = sum(1 for (s2, e2) in busy.get((p, gt), [])
                          if s2 < en and st < e2)
            if overlap >= cap:
                continue
            if last_gt.get(pr) not in (None, gt):
                changes += 1
            free[pr] = en
            last_gt[pr] = gt
            press_tyres[pr] = press_tyres.get(pr, 0.0) + float(j["qty"])
            busy.setdefault((p, gt), []).append((st, en))
            _takt_commit(p, gt, st, en)
            placed.append({"plant": p, "gt_code": gt, "mould_set": j["mould_set"],
                           "press": pr, "start_ts": st, "end_ts": en,
                           "qty": round(take, 1),
                           "hours": round((en - st).total_seconds() / 3600, 2)})
            remaining -= take
            recovered += take
        if remaining >= 1:
            still.append({**j, "qty": round(remaining, 1)})
    unplaced = still
    print(f"  backfill: recovered {recovered:,.0f} tyres by splitting into "
          f"available gaps ({len(still)} campaigns still short)\n")

    # ---- FEED CEILING: what the governor actually did ---------------------
    # `moved` is the count of seats the ceiling pushed later; `no window` is the
    # count it could not place inside the month and so let through UNGOVERNED.
    # A high `no window` means the ceiling is refusing what the month cannot
    # re-seat anywhere -- i.e. it is destroying volume, not levelling it.
    if FEED_CEIL:
        print(f"  FEED CEILING  seats moved later {_feed_moved[0]:,}"
              f"   no in-month window (placed ungoverned) {_feed_nowin[0]:,}")

    # ---- DAY CAP: what the governor actually did --------------------------
    # Printed, not persisted, like the takt line and the rim-spill report --
    # `runs/<arm>/log_04_l5_cure_master.txt` is the evidence. NB this is the
    # SCHEDULED profile (what L5 seated). The profile that matters is the FED
    # one, `cure_by_shift.parquet` after L7/L10, because a seated campaign
    # building never fed cures nothing.
    if DAY_CAP_ON:
        print(f"  DAY CAP  seats moved to a later day {_cap_moved[0]:,}"
              f"   leaked over cap {_cap_leak[0]:,.0f} tyres"
              f"   dropped {_cap_drop[0]:,.0f} tyres")
        for _p in ("PCR", "TBR"):
            if DAY_CAP[_p] <= 0:
                continue
            _prof = _dq[_p][:ndays]
            _over = sum(1 for v in _prof if v > DAY_CAP[_p] + 1.0)
            _mean = sum(_prof) / max(len(_prof), 1)
            _sd = (sum((v - _mean) ** 2 for v in _prof)
                   / max(len(_prof), 1)) ** 0.5
            print(f"    {_p}  scheduled day1 {_prof[0]:,.0f}  "
                  f"day{ndays} {_prof[-1]:,.0f}  mean {_mean:,.0f}  "
                  f"CV {_sd / max(_mean, 1e-9):.4f}  "
                  f"days over cap {_over}/{ndays}  "
                  f"tail {sum(_dq[_p][ndays:]):,.0f}")
            print("      " + " ".join(f"{v:,.0f}" for v in _prof))
        print()

    cp = pl.DataFrame(placed)
    up = pl.DataFrame(unplaced) if unplaced else pl.DataFrame(
        schema={"plant": pl.Utf8, "gt_code": pl.Utf8, "reason": pl.Utf8})
    run = ROOT / "runs" / (a.out or f"cmbc_{a.month}")
    run.mkdir(parents=True, exist_ok=True)
    cp.write_parquet(run / "cure_campaigns.parquet")
    up.write_parquet(run / "cure_unplaced.parquet")
    # ---- CARRY-OUT, DERIVED FROM THE PLACED PLAN (extend) ------------------
    # In "window" the carry list is built at the split site. In "extend" nothing
    # is split at the month boundary at all -- a campaign simply runs across it
    # -- so the carry-out has to be READ OFF the committed plan. This is the
    # press-side hand-off: `l5` next month reads it as carry-in, so the press is
    # correctly still busy and its mould already mounted.
    if HORIZON_MODE == "extend":
        # ONE ROW PER PRESS THAT RAN THIS MONTH, NOT ONE PER RUNNING CAMPAIGN.
        #
        # THE DEFECT THIS FIXES (measured 2026-08-14 on the Jul->Aug pair).
        # Emitting only `end_ts > month_end` describes the 59 PCR / 53 TBR presses
        # that are BUSY at the boundary and says nothing about the other 27 / 26.
        # Those are idle at t0 but their mould is still mounted -- their last
        # campaign ended p50 14.4 h before the boundary, and nobody walks the
        # floor at midnight to strip a mould off an idle press. Emitting nothing
        # for them leaves next month with `last_gt[press] = None`, and the
        # changeover test below (`last_gt.get(pr) not in (None, gt)`) then hands
        # each of them a FREE mould change. That is exactly the fiction
        # WARM_PRESS was written to remove, reappearing one boundary over.
        #
        # `ends <= month_end` is the signal for "free at t0, mould mounted": the
        # reader sets last_gt and skips the occupancy update. So the file is a
        # PRESS-STATE SNAPSHOT, and `qty` (post-boundary tyres) is 0 for the
        # idle ones by construction.
        _last: dict = {}
        for r in placed:
            k = (r["plant"], str(r["press"]))
            if k not in _last or r["end_ts"] > _last[k]["end_ts"]:
                _last[k] = r
        carry = [{"plant": r["plant"], "gt_code": r["gt_code"],
                  "mould_set": r["mould_set"], "seq": 0, "press": str(r["press"]),
                  # tyres of this campaign cured AFTER the boundary -- next
                  # month's output, excluded from this month's fulfilment by
                  # `frac_in_month` in L7. Zero for a press already idle at t0.
                  "qty": float(r["qty"]) * max(
                      0.0, min(1.0, (r["end_ts"] - month_end).total_seconds()
                               / max((r["end_ts"] - r["start_ts"]).total_seconds(), 1.0))),
                  "carry_from": month_end, "ends": r["end_ts"],
                  "busy_at_t0": bool(r["end_ts"] > month_end)}
                 for r in _last.values()]
    # Campaigns still running at the horizon: their tail is next month's opening
    # state, not lost demand. Written even when empty so downstream can rely on it.
    (pl.DataFrame(carry) if carry else pl.DataFrame(
        schema={"plant": pl.Utf8, "gt_code": pl.Utf8, "mould_set": pl.Utf8,
                "seq": pl.Int64, "press": pl.Utf8, "qty": pl.Float64,
                "carry_from": pl.Datetime, "ends": pl.Datetime,
                "busy_at_t0": pl.Boolean})
     ).write_parquet(run / "carry_out.parquet")
    if carry:
        _bz = sum(1 for c in carry if c["busy_at_t0"])
        print(f"  carry-out: {len(carry)} press states ({_bz} still running, "
              f"{len(carry) - _bz} idle with a mounted mould), "
              f"{sum(c['qty'] for c in carry):,.0f} tyres into next month\n")

    print(f"  {'plant':<6}{'campaigns':>11}{'tyres':>11}{'presses':>9}"
          f"{'hours p50':>11}{'band p50':>10}{'mould chg':>11}")
    for p in ["PCR", "TBR"]:
        s = cp.filter(pl.col("plant") == p)
        if not s.height:
            continue
        band = P["campaign_bands"][p]["cure"]["hours_p50"]
        print(f"  {p:<6}{s.height:>11,}{int(s['qty'].sum()):>11,}"
              f"{s['press'].n_unique():>9}{float(s['hours'].median()):>11.1f}"
              f"{band:>10.1f}{'':>11}")
    print(f"  {'TOTAL':<6}{cp.height:>11,}{int(cp['qty'].sum()):>11,}"
          f"{'':>9}{'':>11}{'':>10}{changes:>11,}")

    # ---- gates -----------------------------------------------------------
    print("\n  GATES")
    # 1. no press double-booked
    ov = 0
    for pr, grp in cp.sort(["press", "start_ts"]).group_by("press", maintain_order=True):
        e = None
        for r in grp.iter_rows(named=True):
            if e is not None and r["start_ts"] < e:
                ov += 1
            e = r["end_ts"]
    print(f"    press double-booking            : {ov}  {'PASS' if ov == 0 else 'FAIL'}")
    # 2. mould concurrency
    bad = 0
    for (p, gt), iv in busy.items():
        # Concurrency is bounded by MOULDS ONLY (R3).
        # A volume-based press cap was tried -- limit a GT to the presses its
        # month's volume can keep busy -- and it did cut the worst spread from 26
        # presses to 18. But it also starved high-volume GTs of throughput and
        # pushed GTs below 95% of requirement from 7 to 9. Press spread is a
        # cosmetic concern; requirement coverage is not.
        cap = moulds.get((p, gt), 1)
        pts = sorted([(s, 1) for s, _ in iv] + [(e, -1) for _, e in iv])
        cur = 0
        for _t, d in pts:
            cur += d
            if cur > cap:
                bad += 1
                break
    print(f"    GT concurrency > active moulds  : {bad}  "
          f"{'PASS' if bad == 0 else 'FAIL'}")
    # 3. horizon -- the PLANNING one. Nothing may run past it in any mode.
    late = cp.filter(pl.col("end_ts") > horizon).height
    print(f"    campaigns past horizon          : {late}  "
          f"{'PASS' if late == 0 else 'FAIL'}")
    # Crossing the REPORT boundary is legal and expected in "extend"; it is the
    # whole mechanism. Printed so it is never mistaken for the check above.
    xm = cp.filter(pl.col("end_ts") > month_end)
    print(f"    campaigns crossing month end    : {xm.height}"
          f"{'' if HORIZON_MODE == 'extend' else '  (should be 0 outside extend)'}"
          f"  -- {float(xm['qty'].sum()) if xm.height else 0:,.0f} campaign-tyres, "
          f"only the in-month fraction is counted")
    # 4. unplaced
    print(f"    unplaced                        : {up.height}"
          f"{'  PASS' if up.height == 0 else '  see cure_unplaced.parquet'}")
    if up.height:
        for r in (up.group_by("reason").agg(pl.len().alias("n"))
                  .sort("n", descending=True).iter_rows(named=True)):
            print(f"        {r['reason']:<34}{r['n']:>6}")

    # ---- press utilisation ----------------------------------------------
    print("\n  PRESS UTILISATION (vs L3 ceiling)")
    cf = D / f"l3_ceiling_{a.month}.parquet"
    ceil = pl.read_parquet(cf) if cf.exists() else None
    for p in ["PCR", "TBR"]:
        s = cp.filter(pl.col("plant") == p)
        if not s.height:
            continue
        used = float(s["hours"].sum())
        avail = s["press"].n_unique() * ndays * 24.0
        line = (f"    {p}: {used:>9,.0f} press-h of {avail:>9,.0f} "
                f"= {100*used/max(avail,1):>5.1f}%")
        if ceil is not None:
            mx = float(ceil.filter(pl.col("plant") == p)["max_feasible"][0]) \
                 * ndays / 7.0
            line += f"   output {int(s['qty'].sum()):,} of {mx:,.0f} = " \
                    f"{100*float(s['qty'].sum())/max(mx,1):.1f}% of ceiling"
        print(line)
    print(f"\n  -> {run}/cure_campaigns.parquet")


if __name__ == "__main__":
    main()
