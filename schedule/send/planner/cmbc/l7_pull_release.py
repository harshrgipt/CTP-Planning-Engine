"""L7 -- PULL RELEASE OF BUILDING.  The code fix.

    python -m planner.cmbc.l7_pull_release --month 2026-07

Plant step 12 · R5, R17, R2, B16.

THE INVERSION
  The old engine computes
        cure_ts = max(cure_ts, supply_ts)
  which is a FORWARD PUSH: building emits when it can, and curing waits for
  whatever arrives. Cure timing is an output of build timing.

  The plant runs the opposite. Cure campaign timing is fixed first (L5) and
  building is released BACKWARDS from it:
        release(slice) = slice.t_cure - tau* - build_duration(slice)
  Cure timing is an input; build timing is the output.

  Phase 0 measured the consequence of getting this wrong: our head was 7.4 h
  against the plant's 4.4 h, and at lambda_PCR = 516 tyres/h that 3.0 h gap is
  ~1,548 tyres of standing GT -- essentially the whole PCR inventory excess.
  It is not a tuning error. It is the structural result of letting building lead.

WHEN A MACHINE IS BUSY, BUILD EARLIER -- NEVER LATER
  A slice whose ideal release is occupied must move. Moving it LATER starves the
  press, and a press-hour lost is gone forever. Moving it EARLIER only ages the
  tyre, and it has 72 h of shelf life to spend. So the search runs backwards from
  the ideal release, and only reports starvation when no earlier slot exists.

PLANT HOLIDAYS (rule G3) -- BOOKED AS AN INTERVAL, NOT PAUSED
  A closed plant-day (07:00 -> 07:00) is written into `busy[mach]` for EVERY
  machine, with the sentinel GT `__PLANT_HOLIDAY__` that `_setup_s` prices at
  zero -- a shutdown is not a changeover. The backward walk above then does all
  the work: a run that would straddle the closure is pushed to end at 07:00,
  exactly as if another run were sitting there. This is deliberately the
  OPPOSITE model to L5, which pauses: a build run is `build_band` hours and the
  floor does not leave half of one in a machine over a shutdown, while a cure
  campaign is 8-11 days and could not be moved clear of one at all.
  Three sites do not go through the walk and had to be repaired by hand --
  `_make_room`'s insertion points, its left cascade, and its REBUILD of
  `busy[mach]` from `placed` (which erases the booking); plus the closing
  buffer's idle-gap scan. And phase 1's `t_cure` interpolation, which is the one
  that got through the first implementation -- see the block there.
  See planner/cmbc/holiday.py for the measurement and
  PARTITION_AND_CHANGEOVER.md 4aa. Absent calendar => byte-identical.

HIERARCHICAL CAMPAIGNS -- THERE ARE THREE LEVELS, NOT TWO
  cure campaign  58.5 h PCR / 210.7 h TBR   <- floor lives here (L4.5)
     build RUN    7.6 h PCR /   5.4 h TBR   <- the floor ALSO lives here
        build slice  0.75 h / 1.67 h        <- no minimum, deliberately

  A slice is a DELIVERY, not a lot -- it has no minimum size of its own, and no
  build-side slice floor is applied anywhere in this file. That doctrine is
  correct about the slice and was silent about the RUN, which did not exist as
  an object: L7 chose a machine per SLICE, so consecutive slices of one campaign
  landed on different machines and no run ever reached the floor.

  Measured on runs/july_cmbc_v3 (per-slice choice):
      PCR 4,868 runs, 91.1% below the 150 floor, p50 48 tyres, p50 1 slice/run,
      16.03 build changeovers per machine-day against the plant's 2.66,
      p50 6 machines per GT (max 12) against a calibrated HHI of 1.00.
  A PCR campaign is 1,227 tyres at p50 -- minimum 222, already above the floor --
  shattered across 4 machines. The quantity was never the problem; the machine
  assignment was.

  The RUN is now the unit of machine assignment (see the horizon assignment
  below). Slice SIZE is unchanged, so the JIT character of the pull is kept.
"""
from __future__ import annotations

import argparse
import bisect
import calendar
import heapq
import json
import os
import math
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import polars as pl

from planner.cmbc import allowable, holiday, plant_ct
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

D = ROOT / "warehouse" / "derived"
PARAMS = ROOT / "warehouse" / "params"

# Slice-size multiplier. n = cure_hours / (build_band * SLICE_MULT), so a larger
# value gives FEWER, BIGGER build runs at the cost of more GT sitting.
# We have inventory headroom to spend: the plan holds 4,147 PCR / 1,551 TBR
# against the plant's 4,772 / 1,743, and the plant's own mean head (~13 h, from
# inventory/lambda) is far above its 4.8 h median -- that right tail is what pays
# for its 86-tyre batches. Swept in scripts, not guessed.
# PER PLANT, because the headroom is per plant. Swept against the plant's own
# inventory as the budget (PCR 4,772 / TBR 1,743):
#     k    run PCR/TBR   head    inv PCR/TBR    fulfil
#     1.0     48 /  10   6.01   4,147 / 1,551   98.7%
#     2.0     96 /  19   6.94   5,174 / 1,547   98.3%  <- PCR already over
#     3.0    144 /  29   8.59   6,076 / 1,623   97.7%
#     4.0    193 /  38  10.18   7,091 / 1,918   97.2%  <- TBR over too
# PCR is already at its inventory ceiling at k=1 and buys nothing by batching.
# TBR's inventory barely moves to k=3, so it can take larger runs for free.
# LEGACY slice-count arm. 0 (default) = the DERIVED rule (see the slice block in
# main): n comes from R5 and B12, not from a multiplier. Non-zero restores
# n = cure_hours / (build_band * MULT) for A/B only.
# PER-PLANT BY MEASUREMENT. PCR 0 = the derived R5/B12 rule; TBR 3.0 = the finer
# legacy arm. The derived rule sets the slice to min_lot, which on TBR means a
# 54 % longer run (86 -> 141) needing a 54 % longer contiguous machine gap -- and
# TBR machines are only 45-84 % occupied with a MEDIAN OF 3 eligible machines per
# GT against PCR's 11. Measured per plant, same month, same demand:
#     arm                    PCR ful   TBR ful   TOTAL   TBR lot   TBR R5
#     derived both (v31)      95.74 %   85.41 %  93.68 %    141      66.6 h
#     legacy both  (v29)      95.89 %   94.08 %  95.53 %     86      71.9 h
#     PCR derived, TBR 3.0    95.74 %   96.06 %  95.81 %     87      61.0 h  <- shipped
# TBR loses 8.67 points on the derived rule and regains them here, at its best
# R5 margin of the three arms. TBR is a different plant, not a smaller PCR.
SLICE_MULT = {"PCR": float(os.environ.get("PLANNER_SLICE_MULT_PCR", "0")),
              "TBR": float(os.environ.get("PLANNER_SLICE_MULT_TBR", "3.0"))}

# Safety margin held back from the R5 shelf life when sizing a slice, in hours.
# The wait model is window + build time; placement can still push a release a
# little earlier than ideal, so leave headroom rather than sizing to the edge.
R5_SAFETY_H = float(os.environ.get("PLANNER_R5_SAFETY_H", "6.0"))
# Build tail-cure runs as early as R5 allows so they land inside the month.
# Default OFF: it moves BUILD across the boundary, it does not create output.
TAIL_PULL = os.environ.get("PLANNER_L7_TAIL_BUILD_PULL", "0") != "0"
# Rebuild the closing GT floor to the opening level so the month hands over
# what it inherited. See the block at _hzn for why. Default OFF until measured.
CLOSING_BUFFER = os.environ.get("PLANNER_L7_CLOSING_BUFFER", "0") != "0"
# ---- BUFFER_SETUP: RESERVE THE CHANGEOVER AT BOTH ENDS OF A TOPPED-UP GAP ----
# WHAT THIS DOES / WHY IT EXISTS -- a measured defect, found 2026-08-21.
#   The closing buffer fills a machine's idle gap from edge to edge. It charges
#   NO changeover to either edge, so a top-up run of a different GT can start
#   the instant the previous run ends and end the instant the next one starts.
#   The machine is then asked to change size in zero minutes, twice.
#   With the flag on, each gap is shrunk by _setup_s(prev_gt -> g) on the left
#   and _setup_s(g -> next_gt) on the right before anything is sized into it.
#
#   RESERVING THE SETUP *IS* THE 24 h MACHINE-DAY BUDGET, not a proxy for it.
#   Once every production interval and every changeover interval on a machine is
#   disjoint, their total inside any 07:00->07:00 window is <= 24 h by
#   construction. Measured on MD_base (August): production alone never exceeds
#   24 h on any machine-day -- it is exactly AT 24.00 h -- and all three PCR
#   machine-days that breach do so because setup is added on top of a day the
#   buffer already filled. Both violation classes have one cause.
#
# SHIPS OFF. MEASURED 2026-08-21, AUGUST, BOTH PLANTS. Fresh arms via
# scripts/run_arm.py, all FRESH, partition stamped 2026-08,
# PLANNER_L7_CLOSING_BUFFER=1 and PLANNER_HOLIDAYS=2026-08-15 pinned on every arm.
#
#   PCR              BUILT   dBUILT   in-month   md>24h  excess h  unresCO   L11
#   MD_base2       409,511       +0    397,326      3/343   4.22       11    31/52
#   BUFFER_SETUP=1 409,411     -100    397,326      0/343   0.00        0    31/52
#
#   TBR              BUILT   dBUILT   in-month   md>24h  excess h  unresCO   L11
#   MD_base2        98,003       +0     96,932      0/274   0.00        1    31/52
#   BUFFER_SETUP=1  98,003       +0     96,932      0/274   0.00        0    31/52
#
# scripts/verify_export.py on the exported pack, which imports no planner code:
#   base  VERDICT: plan is NOT physically executable (2 hard violations)
#           - changeover time not reserved: 12 of 1269 transitions, 6.9 h short
#           - machine-day over 24 h: 2 of 597, worst TBMPCR10Stage2 day 30
#             = 25.17 h (prod 23.40 + setup 1.77)
#   arm   VERDICT: plan is physically executable (0 hard violations)
#           all 597 machine-days fit 24 h incl. setup, max exactly 24.00 h
#
# THE PRICE IS EXACTLY 100 TYRES AND IT IS NOT A CASCADE. 5,631 of the 5,646
# build_schedule rows are IDENTICAL between the two arms; only the 15 buffer
# runs move, and the PCR buffer total goes 3,018 -> 2,918. in-month, tail,
# starvation and its cause vector, R5, same-size, weighted changeover, GT
# daily-mean max and all 52 invariants are unchanged. TBR pays nothing -- its
# four buffer gaps had the slack to absorb one 10 min reservation.
#
# THE TRAP IN READING THIS: -100 sits far inside the +-800 August PCR noise
# floor (DO-NOT #49), and a null control run beside it -- LOT_INTERVAL_H
# perturbed by 36 SECONDS in a 16 h grid -- swung PCR BUILT 0..+706, mean +235
# sd 365. So the SIGN of this arm is not measurable as a volume result. It is
# not offered as one: the result is the two HARD violations going to zero on an
# independent verifier, and 100 tyres is the receipt.
BUFFER_SETUP = os.environ.get("PLANNER_L7_BUFFER_SETUP", "0") != "0"
# ---- R5 ENFORCED ON THE FIRST TYRE OF A SLICE, NOT THE LAST -----------------
# WHAT THIS DOES / WHY IT EXISTS -- a measured defect, found 2026-08-21.
#   `_place` tests `t_cure - slice_END` against the 72 h shelf life. A slice is
#   built continuously, so its FIRST tyre comes off `slice hours` earlier and
#   waits that much longer -- and it is the first tyre that expires, not the
#   last. Measured on MD_base (August): true max 65.58 h PCR / 73.45 h TBR
#   against the 63.27 / 71.71 the test sees, with 26 TBR tyres already past
#   72 h in the shipped plan.
#   With the flag on, `_place` and `_r5_floor` both offset by (cum - qty).
#
# SHIPS OFF -- it REFUSES placements the plan currently takes, so it should cost
# volume by construction. The GRADING is fixed unconditionally in l11 and in
# l7's own gate print; only the enforcement is behind this flag.
#
# MEASURED 2026-08-21, AUGUST, BOTH PLANTS. Fresh arms, CLOSING_BUFFER=1 and
# HOLIDAYS=2026-08-15 pinned on every arm.
#
#   PCR is BYTE-IDENTICAL to base -- it never approaches the bound (65.58 h).
#
#   TBR             BUILT   dBUILT   in-month   starved   R5 1st tyre  >72 h   L11
#   MD_base2       98,003       +0     96,932     1,654     73.45 h      26   31/52
#   R5_FIRST=1     98,192     +189     97,084     1,465     70.85 h       0   33/52
#
# AND THE NULL CONTROL SAYS THE +189 IS NOTHING (DO-NOT #49). Perturbing
# PLANNER_LOT_INTERVAL_TBR -- a 6 to 30 MINUTE change in a 16 h release grid,
# no physical meaning at that resolution -- gives TBR dBUILT:
#
#   15.5 h   15.9 h   [16.0 base]   16.1 h   16.5 h
#   -207     +100          0        +166     +204        mean +66, sd 187
#
# +189 sits inside one sd of the mean of physically equivalent settings. Worse
# for the naive reading: THREE of those four null arms ALSO land under 72 h with
# zero tyres over shelf life, and MD_nt159 scores the identical 33/52 with the
# identical two invariants flipping. So on August the volume gain, the R5 number
# AND the L11 count are all reachable by accident.
#
# WHAT IS NOT REACHABLE BY ACCIDENT, AND IS THE ONLY REASON TO SHIP THIS:
# with the flag ON the bound is ENFORCED -- `_place` and `_r5_floor` both test
# the first tyre, so no placement can breach it. With it OFF the plan's
# compliance is a coin-flip of the greedy: the baseline happens to breach by
# 1.45 h on 26 tyres and four physically-identical plans happen not to.
# A rule that holds by luck is not held. Grade this on the guarantee, never on
# the +189.
R5_FIRST_TYRE = os.environ.get("PLANNER_L7_R5_FIRST_TYRE", "0") != "0"
# LEVEL-LOAD THE BUILD: release EVERY run as early as R5 allows, not just
# tail-cure runs. Targets the idle-machine days (Jul PCR day 24 = 95 idle h
# at 64 % utilisation with the SAME changeover count as an 86 % day), where
# just-in-time release leaves machines with nothing to do between cure
# clusters. Costs standing GT -- the earliness cap and the G8 WIP rail are
# what should stop it going too far. Default OFF until measured.
EARLY_RELEASE = os.environ.get("PLANNER_L7_EARLY_RELEASE", "0") != "0"

# Where inside the legal window [n_R5, n_B12] to sit. Both ends satisfy R5 AND
# B12; the choice is purely how much machine time one slice occupies.
#   0 = n_R5  -> largest legal slice  (best lot size, worst placement)
#   1 = n_B12 -> smallest legal slice (still >= floor, best placement)
SLICE_AGGR = float(os.environ.get("PLANNER_SLICE_AGGR", "1.0"))

# Machine utilisation cap for the horizon assignment. A GT is pinned to one
# machine until that machine is genuinely full, then -- and only then -- a second
# opens. CAPACITY OUTRANKS THE LOCK: ENGINE_LOG 8 booked 1,365 h of work into a
# 744 h month by honouring dedication past the point of feasibility.
MACH_UTIL_CAP = float(os.environ.get("PLANNER_MACH_UTIL_CAP", "0.95"))
# Minimum eligible machines per GT in the horizon assignment, independent of
# load. See the block at the assignment for why MACH_UTIL_CAP cannot do this.
PIN_MIN = int(os.environ.get("PLANNER_L7_PIN_MIN", "1"))

# Currency for LOAD: each machine's own cadence (default) or the plant median.
# "plant" restores the pre-2026-08-08 behaviour for A/B ONLY -- it is the bug, not
# a policy. PCR machines run 49-78 s against a 62 s median (-21 % / +26 %), TBR
# 189-219 s against ~207 s. Under "plant" the rim-load table reads R12 113.2 %,
# R13 100.7 %, R17 101.5 % ("three rims over, 153 h over-subscribed"); under
# "machine" the same demand is R12 89.5 %, R13 80.6 %, R17 103.1 % -- one rim
# over, by 22 h. Realised occupancy (61-83 % on every PCR machine) agrees with
# the second. See EXPERT_AUDIT §4b; same bug already fixed once in
# scripts/build_gt_machine_partition.py (PARTITION §3).
CAD_BASIS = os.environ.get("PLANNER_CAD_BASIS", "machine")

# Set to "0" to restore the pre-fix per-slice machine choice. Kept ONLY so the
# two arms can be A/B'd in one sitting, both run fresh -- never compare a new
# arm against an older run directory, the config hash does not cover env flags.
PIN_RUNS = os.environ.get("PLANNER_L7_PIN_RUNS", "1") != "0"

# Calibrated build-RUN band and changeover rate (ENGINE_FLOW / Phase 0, both
# 8-month validated). Reported here so the run shape is visible at the layer
# that creates it rather than only at L11.
RUN_BAND_H = {"PCR": (6.0, 10.0), "TBR": (4.0, 7.0)}
CO_PER_MDAY = CONFIG.thresholds.plant_co_per_machine_day  # config = single source

# Run size as a multiple of the B12 floor (PCR 150 / TBR 70). The floor is a
# LOWER BOUND on the time-supply lot, not the lot itself.
RUN_MULT = float(os.environ.get("PLANNER_RUN_MULT", "1.0"))

# How many replenishment intervals of CURE DEMAND one run may absorb.
# DEFAULTS OFF (large). Capping at 1.0 x T does cut inventory 6,946 -> 6,285, but
# it caps the run size with it -- Q <= r_g x span -- so it is not an independent
# lever, it is the same dial as T. Worse, it fragments: PCR runs below the B12
# floor go 4.0 % -> 35.3 %. Sweep it if you want the inventory/lot trade curve,
# but do not leave it on.
SPAN_MULT = float(os.environ.get("PLANNER_SPAN_MULT", "99"))

# B12 lot floor -- a BUDGET, not a gate. THE PLANT HAS NO HARD LOT FLOOR.
#
# Measured over the full 8 months (machine x GT x day runs in v_build stage 2),
# the plant itself runs BELOW its own floor:
#       PCR  5,691 runs · p50 415 · 13.1 % below 150   (July: 783 runs, 14.0 %)
#       TBR  6,541 runs · p50  96 · 31.0 % below  70   (July: 874 runs, 31.0 %)
# So a sub-floor run is not something a supervisor refuses to set up -- it is
# something this plant does roughly one time in seven (PCR) or one in three
# (TBR). Modelling the floor as an absolute gate is STRICTER THAN THE PLANT.
#
# It is also expensive. As a hard gate it was the single binding constraint on
# fulfilment: 30,615 tyres starved, every one of them tagged
# `would breach min_lot` -- 6.2 points, more than the WIP cap and the rim lock
# combined. Off entirely: 97.1 % fulfilment but lot p50 collapses 334 -> 241 and
# 18.7 % of runs go sub-floor, now LOOSER than the plant.
#
# So: neither. Allow sub-floor splits up to the plant's own revealed tolerance
# and refuse them after that. Budget = plant July run count x plant sub-floor
# share, i.e. exactly as many sub-floor setups as the plant actually performed.
# Spent oldest-deadline first, which is where splitting rescues the most volume.
SUBFLOOR_BUDGET = {
    "PCR": int(os.environ.get("PLANNER_SUBFLOOR_PCR", "180")),   # plant-matched 12.9 %
    "TBR": int(os.environ.get("PLANNER_SUBFLOOR_TBR", "400")),   # plant-matched 33.5 %
}
# "0" = plant-calibrated budget (default) · "1" = absolute gate · "off" = no floor
# Allow ONE halving of an atomic (single-slice) run, charged to the same B12
# budget. Split-before-starve otherwise terminates at `len(grp) == 1` and the
# budget never gets spent -- 180 available, ~9 used, 27,203 tyres starved.
# PCR ONLY, BY MEASUREMENT. On PCR it is worth +1.09 pt July / +1.04 pt August.
# On TBR the SAME change is worth -2.01 pt July / -1.05 pt August: TBR runs are
# already at the floor (p50 86 against a floor of 70), so halving an atomic
# slice there produces two fragments that BOTH fail to place -- starved groups
# 158 -> 298 while starved volume rose 4,482 -> 6,457. Negative on both months,
# so TBR is excluded (DO-NOT 15: a rule tuned on PCR does not transfer to TBR).
ATOMIC_SPLIT_PLANTS = {x for x in os.environ.get(
    "PLANNER_ATOMIC_SPLIT_PLANTS", "PCR").split(",") if x}
# How many times an atomic (single-slice) run may be halved, and the smallest
# fragment in TYRES that a halving may produce. Depth 1 + min 1 is the historical
# behaviour exactly. Raising depth is the ONLY lever that moves our sub-floor
# share toward the plant's own 12.7 % / 30.8 %: the B12 budget is not binding
# (July PCR spends ~11 of 180), the fragment geometry is.
SPLIT_DEPTH = int(os.environ.get("PLANNER_L7_SPLIT_DEPTH", "1"))
SPLIT_MIN = int(os.environ.get("PLANNER_L7_SPLIT_MIN", "1"))
# Closing-buffer gap search: "late" (default) fills the LATEST-ENDING gap first
# and abuts the run to the gap's end -- pure R5 margin on the hand-off, see the
# block beside its use. "size" restores the original largest-gap-first ordering
# for A/B only; it was measured to leave 12 % of the PCR hand-off unable to
# cover a full tau*+band.
CLOSING_GAP_ORDER = os.environ.get("PLANNER_L7_CLOSING_GAP_ORDER", "late")
# Closing-buffer TARGET: "inherit" (the historical default -- rebuild whatever
# mix the month opened with) or "cover" (size the hand-off to the next month's
# COLD presses, mounted mould first). See the block beside its use for why
# "inherit" makes every per-GT deficit sub-floor by construction.
CLOSING_MIX = os.environ.get("PLANNER_L7_CLOSING_MIX", "inherit")
# Force-disabled under STRICT_FLOOR -- see that flag. Resolved after both are
# read so the interaction is one line, not a condition at every use site.

# Committed-hours tie-break on machine choice. DEFAULT OFF -- mixed sign across
# months; see the block beside its use for the measured table.
# "0"/"off" = OFF (default, byte-identical to the pre-flag engine)
# "1"        = committed HOURS      (the arm rejected in PARTITION §4l.4/§4n.5)
# "cap"      = remaining TYRE capacity, (H - committed)/cadence -- see the block
#              beside its use. Cadence-aware, so it does not hand work to a slow
#              machine merely because that machine has idled longer.
# "share"    = hours of THIS GT's own partition book the machine still owes.
#
# MEASURED 2026-08-09, four fresh arms per mode via scripts/_diag_alloc_sweep.py
# (each arm rebuilds the month's partition first -- the partition file is a
# shared input and an arm that does not rebuild it inherits the previous arm's,
# which is DO-NOT #8 one layer out). Fulfilment points vs the shipped default:
#
#            Jul PCR   Jul TBR   Aug PCR   Aug TBR
#   hours     -0.02     +0.08     -0.46     +0.24
#   cap       +0.17     -0.22     -0.14     +0.24
#   share     +0.25      0.00     -0.69      0.00   (TBR is not partitioned)
#
# ON PCR EVERY MODE IS MIXED-SIGN and PCR stays OFF. NOTE the §4l.4/§4n.5 July
# rows (+0.42 PCR) were measured on a STALE partition -- see §4o -- and their
# TBR rows on SLIVER_TBR=1.0, which is not the shipped configuration; the table
# above is the re-measure on the production config.
#
# ON TBR `hours` is POSITIVE ON ALL FOUR MONTHS and PCR is byte-identical:
#   May 94.14 -> 94.72 (+0.58) · Jun 92.92 -> 93.58 (+0.66)
#   Jul 95.56 -> 95.64 (+0.08) · Aug 97.19 -> 97.43 (+0.24)
# 0 invariant flips on every month; rail max 1,335 of 1,400; R5 max 70.4 of 72.
# Price: TBR same-size -0.5/-0.6 pt on May/Jun, R5 +4.3 h on July, +1 h setup.
# TBR is where a load tie-break can work and PCR is not, for a structural
# reason: PARTITION_PLANTS=PCR, so on PCR the machine question is already
# answered by a capacity-feasible object and re-deciding it by load is the
# bouncing that object exists to stop (DO-NOT #4). TBR has no partition, a
# median of 3 eligible machines per GT, nine near-identical machines
# (189-219 s, 16 % spread) and only two rims, so balancing is nearly free of
# size-change cost.  Enable with
#   PLANNER_LOAD_TIEBREAK=1 PLANNER_LOAD_TIEBREAK_PLANTS=TBR
#
# What the tie-break DOES buy is the occupancy spread, and that is the finding:
#   July PCR spread 22.54 -> 11.31 (hours) / 12.19 (cap) / 16.28 (share) pt
#             CV     0.084 ->  0.041 /  0.050 /  0.060
#   Aug  PCR spread 19.64 -> 16.88 / 17.19 / 15.25 pt
# It fixes the imbalance almost completely, and the imbalance is worth ~0.2 pt.
# Only 138 h of 6,167 (July) and 126 h of 6,206 (August) are movable inside the
# rim lock at all, against 2,017 h of idle time, and total PCR starvation is
# 123 h of build work -- so the binding constraint is WHEN the hours are, not
# WHICH machine holds them. Do not re-open this without a new mechanism.
_LTB = os.environ.get("PLANNER_LOAD_TIEBREAK", "0")
LOAD_TIEBREAK = "" if _LTB in ("0", "off", "") else _LTB
# Per plant, like ATOMIC_SPLIT_PLANTS and L5_TAKT_PLANTS -- DO-NOT #15, a rule
# tuned on one plant does not transfer. PCR is mixed-sign in every mode.
LTB_PLANTS = {x for x in os.environ.get(
    "PLANNER_LOAD_TIEBREAK_PLANTS", "PCR,TBR").split(",") if x}

# ---- STRICT B12 -- ZERO RUNS BELOW THE FLOOR. Plant instruction, 2026-08-09:
# "I strictly want that NO lots below this min lot cap."
#
# DEFAULT ON, by that instruction. It is STRICTER THAN THE PLANT'S OWN
# BEHAVIOUR -- the plant runs sub-floor 12.7 % (PCR) / 30.8 % (TBR) of the time
# (PARTITION §4k) -- and it is priced in §4m. Set 0 to restore the
# plant-calibrated budget, which is the cheaper policy on fulfilment.
#
# Three gates, because one is not enough (HARD_FLOOR alone plateaued at
# 3.6 %/4.5 %):
#   1. grouping repair   -- merge a sub-floor group into its predecessor
#   2. `_place` refusal  -- never place a run below the floor
#   3. HARD_FLOOR        -- never split into one
# ATOMIC_SPLIT is force-disabled under STRICT: it works BY creating sub-floor
# runs (it took PCR from 0.1 % to 7.9 %), so the two are mutually exclusive.
STRICT_FLOOR = os.environ.get("PLANNER_STRICT_LOT_FLOOR", "1") != "0"
_HF = os.environ.get("PLANNER_HARD_FLOOR", "budget")
HARD_FLOOR = _HF == "1" or STRICT_FLOOR
NO_FLOOR = (_HF == "off") and not STRICT_FLOOR
if STRICT_FLOOR:
    ATOMIC_SPLIT_PLANTS = set()      # it works BY creating sub-floor runs
_subfloor_spent: dict = {"PCR": 0, "TBR": 0}

# ---- PLACEMENT-FAILURE INSTRUMENTATION (read-only, default OFF) ----------
# `_place` returns a bare False, so a refused run says nothing about WHICH gate
# turned it away -- t0, the earliness cap, R5, or the WIP rail. Without that the
# only available diagnosis is "no machine took it", which is exactly the wrong
# granularity: the whole question under a hard lot floor is whether the refused
# volume is blocked by CAPACITY or by TIMING, and those two have opposite fixes.
# Writes `l7_place_diag.parquet`; costs nothing when off.
# ---- ANTI-SLIVER PACKING -------------------------------------------------
# How large a hole is worth leaving behind a just-in-time release, as a multiple
# of a FLOOR-SIZED run on that machine. 0 disables (the pre-fix behaviour); 1.0
# means "never leave a hole no legal run could occupy". See the block in _place.
#
# PER-PLANT DEFAULTS, and why TBR's is 0 (measured 2026-08-09, both months).
# Anti-sliver and `_make_room` repair the SAME defect and are strongly
# anti-synergistic: anti-sliver pre-commits the slack that make-room needs to
# compact into. With MAKEROOM=1 -- which is the default -- turning sliver ON
# costs TBR 0.54 / 0.37 pt and drops make-room rescues from 124 to 75. On PCR it
# is still worth +0.14 pt, so the two plants get different values. This is the
# same rule as SLICE_MULT and PARTITION_PLANTS (DO-NOT #15): nothing tuned on
# PCR is assumed to transfer to TBR.
#
# The default used to be 1.0 on both, so every shipped run had to pass
# PLANNER_SLIVER_TBR=0 on the command line or silently get the worse arm. The
# default now IS the measured-best setting, so "run with the defaults"
# reproduces the shipped July and August packs exactly.
SLIVER = {"PCR": float(os.environ.get("PLANNER_SLIVER_PCR", "1.0")),
          "TBR": float(os.environ.get("PLANNER_SLIVER_TBR", "0"))}

# ---- FULL AVAILABILITY AT t0 (stated planning assumption, B-ASSUME-1) -----
# PLANT RULING, 2026-08-09: everything is staged and available at hour 0 -- no
# ramp-up, no warm-up, no staging delay. See the matching block in
# l5_cure_master.py for the full statement and for what it does NOT license.
# In L7 it switches opening stock from a MEDIAN-age screen to an exact per-tyre
# shelf-life ladder. R5 itself is untouched and is still enforced on every tyre.
#
# MEASURED, AND DEFAULTED OFF. The median screen was NEVER BINDING on how much
# stock is drawn: opening-stock consumption is IDENTICAL to the tyre in all four
# arms -- PCR 4,190 and TBR 1,010 with the ladder on or off. Stock is exhausted
# by early cures long before the median wall matters, so there is no withheld
# stock for the exact screen to release. It only changes WHICH tyres go to which
# slice, which reshuffles run grouping: PCR 96.95 -> 96.86 %, TBR unchanged,
# day-1/2 unfed PCR 3,869 -> 4,399, R5 max 65.1 -> 68.3 h. Kept behind the flag
# because the median-as-constraint shape is still the §1 defect class and a
# future baseline may make the exact version pay.
FULL_AVAIL_T0 = os.environ.get("PLANNER_FULL_AVAILABILITY_T0", "0") != "0"
# Sub-switch so the two halves of the ruling can be measured ONE AT A TIME
# (§29 / DO-NOT: never change two variables at once). This is the L7 half --
# the opening-stock shelf-life ladder. The L5 half is PLANNER_FULL_AVAIL_RAMP.
FULL_AVAIL_LADDER = os.environ.get(
    "PLANNER_FULL_AVAIL_LADDER", "1" if FULL_AVAIL_T0 else "0") != "0"

# Targeted LNS repair when a floor-sized run cannot find a contiguous hole.
MAKEROOM = os.environ.get("PLANNER_L7_MAKEROOM", "1") != "0"
# C -- pool same-GT sub-floor remainders inside one R5 window. Satisfies
# B12 by MERGING to a legal run; never places anything sub-floor.
POOL_TAILS = os.environ.get("PLANNER_POOL_TAILS", "0") != "0"
# How many insertion points per machine `_make_room` may try, latest first.
# MEASURED: 1 is the maximum. 6 leaves July identical (113/75 rescues) and costs
# August PCR 148 -> 129, because every slot earlier than the latest one puts the
# same stock on the floor sooner and the WIP rail refuses it -- rail bails go
# 217 -> 1,242. The latest legal slot is the only one worth trying; the search is
# kept because the bail counters are what prove that.
MR_POINTS = int(os.environ.get("PLANNER_L7_MR_POINTS", "1"))

DIAG = os.environ.get("PLANNER_L7_DIAG", "0") == "1"
_diag_last: dict = {}
_diag_rows: list = []


def _starve_cause(d: dict) -> str:
    """Name WHICH rule refused this run, from the per-call refusal counters.

    `build_starved.parquet` used to carry one bucket, "no feasible release", for
    every starved slice -- 3,519 PCR tyres on July 2026 under a single label.
    That is unactionable: the four things it can mean have four different owners.

        release_before_t0   the build would have to start before the month opens.
                            A COLD-START artefact, not a capacity fact -- it is
                            the open ~4 h carry-in question, which needs a plant
                            ruling, not a code change.
        r5_shelf_life       no gap inside the run's own 72 h band (R5). Curing
                            must move, not building.
        gt_wip_rail         the G8 rail refused the GT this run would create.
        early_cap           EARLY_CAP_H refused it -- our own knob, not a plant rule.
        machine_busy        every eligible machine was genuinely occupied. THE
                            ONLY ONE THAT MEANS "BUY CAPACITY".
        below_min_lot       B12 floor (already named separately upstream).

    Reported as the counter with the most refusals, so a run blocked on three
    machines by R5 and one by the rail reads as R5.

    `machine_busy` IS THE FALL-THROUGH, so it is the one to distrust. The first
    version of this function shipped with all four counters still behind
    `if DIAG:` inside `_place`, so a run without PLANNER_L7_DIAG=1 could not
    write them and every starved slice fell through: it printed "PCR starved
    4,934 tyres -- machine_busy 100.0%", which reads as "buy capacity" and was
    entirely an artefact. The counters are unconditional now. If this ever prints
    a clean 100 % again, check that they still are before believing it.
    """
    counts = {"release_before_t0": int(d.get("before_t0", 0) or 0),
              "r5_shelf_life": int(d.get("r5", 0) or 0),
              "gt_wip_rail": int(d.get("wip_rail", 0) or 0),
              "early_cap": int(d.get("early_cap", 0) or 0),
              "below_min_lot": int(d.get("floor", 0) or 0)}
    top = max(counts, key=lambda k: counts[k])
    return top if counts[top] > 0 else "machine_busy"

# DIAGNOSTIC ONLY -- NEVER A SHIPPED LEVER. R5 is a scrap limit; relaxing it
# does not produce a runnable plan. It exists so the question "how much of the
# strict-floor loss is contention INSIDE each run's own 72 h band, as opposed to
# a shortage of machine time?" can be answered by measurement instead of
# argument. Requires PLANNER_L7_DIAG=1, so it cannot be switched on by accident.
# DIAGNOSTIC ONLY. Hours of machine time BEFORE the horizon that a run may use --
# i.e. what a warm start from the previous month's calendar would provide. It
# prices the COLD START (a cure early on day 1 whose build must precede day 1)
# without pretending the resulting file is a shippable month plan.
DIAG_PRE_H = float(os.environ.get("PLANNER_DIAG_PRE_H", "0")) if DIAG else 0.0

# ---- BOUNDED PRE-t0 BUILDING WINDOW (PLANNER_L7_PRE_T0_H) ------------------
# WHAT THIS DOES / WHY IT EXISTS -- a measured defect, found 2026-08-21.
#   The horizon starts hard at t0 = the 1st, 07:00, and building may not be
#   released before it. The PLANT HAS NO SUCH WALL: 48.8-57.6 % of everything it
#   cured in a month's first 12 h was built in the previous month, and 98-100 %
#   of presses carry the same mould across the boundary. A cure seated on day 1
#   needs its green tyre finished at t_cure - tau*, which for the earliest seats
#   is BEFORE the month exists, so the run is refused and its tyres starve.
#   August 2026: 55 % of PCR starvation (6,810 tyres) and 81 % of TBR's (1,341)
#   is cause `release_before_t0`, and 100 % of it is short by LESS THAN 8 h
#   (PCR p50 3.50 h max 4.45 h, TBR p50 2.15 h max 6.73 h).
#
#   This flag opens a BOUNDED window of `H` hours before t0 in which building
#   -- and only building -- may be released. Cure campaigns do not move, t0 and
#   month_end are unchanged, R5 is enforced on every slice exactly as before,
#   and the WIP rail still books the resulting stock (its hour grid clamps a
#   pre-t0 credit into hour 0, which is where that stock physically is at t0).
#
#   READ THE LABEL CAREFULLY BEFORE READING THE RESULT. Section 4aj proved that
#   `release_before_t0` does NOT mean "the deadline precedes the horizon" -- the
#   placer walks BACKWARDS past every conflicting interval, so it means "the
#   backward cascade ran out of calendar". A slice with t_cure on the 27th and
#   t0_short_h = 3.8 h is a machine-CONTENTION failure. Widening the calendar
#   backwards relieves both classes, because nothing is booked before t0, so a
#   recovery here is NOT evidence that the starvation was a cold start.
#
# THE TYRES ARE BUILT IN THE PREVIOUS MONTH AND ARE NOT THIS MONTH'S OUTPUT.
#   Any scorer that sums `build_schedule.qty` unfiltered will absorb them into
#   BUILT and read this flag as free volume. It is not free: it spends the
#   PREVIOUS month's machine hours. Split on `end_ts <= t0` and report the two
#   halves separately -- see PARTITION_AND_CHANGEOVER.md 4ak.1, which is the
#   same defect one boundary along.
#
# SHIPS OFF (H = 0 -> byte-identical to the closed-box behaviour: verified,
# all nine plan artefacts sha1-equal to base).
# MEASURED 2026-08-21, AUGUST 2026, BOTH PLANTS. Fresh arms via
# scripts/run_arm.py on partition stamped 2026-08, all `check_arm_fresh` FRESH.
# Baseline = the shipped defaults + PLANNER_L7_CLOSING_BUFFER=1.
#
#   PCR    BUILT(Aug)  dBUILT  pre_t0  in-month   ful%  starved  bt0    r5   L11
#   base      409,511      +0       0   397,326  92.59   12,477  6,810  5,044  32
#   H=4       408,551    -960   1,383   397,823  92.70   12,125  1,409  9,170  31
#   H=8       408,448  -1,063   2,732   398,890  92.95   10,879    305  9,493  32
#   H=12      406,424  -3,087   3,334   397,786  92.69   12,135  1,380  9,111  31
#
#   TBR    BUILT(Aug)  dBUILT  pre_t0  in-month   ful%  starved  bt0    r5
#   base       98,003      +0       0    96,932  97.89    1,654  1,341     87
#   H=4        98,363    +360     378    97,670  98.64      916    603     87
#   H=8        97,576    -427     804    97,309  98.27    1,277    964     87
#   H=12       97,609    -394     817    97,356  98.32    1,231    332    341
#
# WHAT IT ESTABLISHES, AND THE TRAP IN READING IT.
#   The window does what it was built to do: `release_before_t0` collapses on
#   PCR, 6,810 -> 305 at H = 8 (-95 %). It does NOT produce tyres. `r5_shelf_life`
#   rises 5,044 -> 9,493 in the same arm, so 75 % of the relieved starvation is
#   simply re-refused one gate down, and TOTAL starvation falls only 1,598.
#   THE 72 h SHELF LIFE WAS ALWAYS THE BINDING CONSTRAINT; t0 was in front of it.
#   Give the backward cascade more calendar and it walks back until R5 stops it.
#
#   **Read only in-month, H = 8 looks like +1,564 tyres and +0.36 pt. It is not.**
#   August's own BUILT FALLS 1,063 while 2,732 tyres appear that JULY built. The
#   entire in-month gain, and more, is imported across the boundary. This is
#   4ak.1's defect one boundary along: a scorer that sums build_schedule.qty
#   unfiltered reads a 1,063-tyre loss as a 1,669-tyre win.
#
#   The mechanism of the PCR loss is visible in the counters: `gt_wip_rail`
#   refusals go 623 -> 1,081 (H=8) / 1,546 (H=4). Building earlier IS inventory,
#   and PCR already sits at its rail margin (daily-mean max 4,522 against a
#   4,800 rail x 0.94 = 4,512 enforcement point). The window buys starvation
#   relief with rail headroom that does not exist.
#
#   Same-size share pays for it too: 69.3 -> 66.6 % (H=8) and weighted changeover
#   fails L11 outright at H=4 (74.3) and H=12 (74.8) against the plant's 74.0.
#   H is NOT monotone in anything -- H=12 is worse than H=8 on every PCR line and
#   TBR's only positive BUILT cell is H=4 (+360), which reverses at H=8 (-427).
#   A lever whose sign flips with its own magnitude is a reshuffle, not a
#   mechanism (DO-NOT #15's cousin).
#
# AND THE HOURS ARE NOT AVAILABLE. Checked per machine against `runs/SHIP2_jul`,
# the shipped July plan -- not against a month total, because a month total of
# free hours says nothing about any one machine (DO-NOT #19, #22):
#
#   H=8 PCR: claim 52.7 machine-h, of which **18.95 h double-books a machine
#            July already has running**. 6 of 10 machines over.
#   H=8 TBR: claim 50.2 machine-h, **16.96 h double-booked**, 4 of 9 over.
#
# July's last 8 h hold 48.1 free PCR / 45.6 free TBR machine-hours in aggregate,
# which reads as ample -- and TBMPCR9/10 are 100 % busy while TBMPCR2/4/6/7 are
# idle. The window takes the busy ones anyway. So even the measured recovery is
# an OVER-estimate of what a real carry-in could deliver.
#
# AND IT DOES NOT EXPORT. `scripts/verify_export.py` on the H=8 pack adds a
# third HARD violation the baseline does not have -- "build row outside the
# plant month: 66 start outside" -- plus two EXPORT reconciliation failures
# (sheet1 404,463 vs sheet7 400,428 PCR; 97,440 vs 96,332 TBR), because the
# shift pack's sheet 1 keeps the pre-t0 rows and its sheet 7 daily summary does
# not. The pack disagrees with itself about how many tyres August built. Shipping
# this needs the export layer to learn the concept as well, which is more work
# than the measurement justifies.
#
# VERDICT: the carry-in question is REAL and this is the first bounded, runnable
# measurement of it -- but at the sizes the plant can actually lend, it is worth
# roughly 1,600 tyres of starvation relief on PCR for 1,063 tyres of August
# output, 2.7 pt of same-size share and a rail it cannot afford. **The prize
# named in the brief is not there.** Put the number to the plant; do not ship it.
# Full record: PARTITION_AND_CHANGEOVER.md 4aq.
#
# THIS IS NOT `PLANNER_DIAG_PRE_H`. That one is DIAG-gated, unbounded in intent,
# and its output is explicitly not a runnable plan. This one is bounded, ships in
# a normal run, and tags its rows so the accounting stays honest.
PRE_T0_H = float(os.environ.get("PLANNER_L7_PRE_T0_H", "0"))
if DIAG and os.environ.get("PLANNER_DIAG_SHELF_H"):
    GT_SHELF_LIFE_H = float(os.environ["PLANNER_DIAG_SHELF_H"])
    print(f"  !! DIAGNOSTIC ONLY: R5 shelf life overridden to "
          f"{GT_SHELF_LIFE_H:.0f} h -- this plan is NOT runnable")

# Never build a GT outside its rim's locked machines. Turns the rim lock from a
# priced preference into a constraint, so the WIP cap cannot break it.
HARD_LOCK = os.environ.get("PLANNER_HARD_LOCK", "1") != "0"

# Last-resort use of the plant's APPROVED allowable matrix.  HARD_LOCK is a
# sequencing preference mined from historical rim purity; it is not a plant
# prohibition.  Previously, when every home/rim-locked window failed, the run
# was starved even if another machine explicitly approved for that GT had a
# legal pre-deadline window.  July's clearest case is GT 1482 UHL: PCR3/PCR4/PCR7
# are approved, PCR4 is its home, and PCR3/PCR7 retain idle time while 2,419
# tyres go unfed.
#
# This pass is deliberately LAST: home/rim placement and make-room are tried
# first, so normal work stays size-coherent.  `_place` still enforces reserved
# setup, the strict lot floor, R5, the WIP rail, cadence and machine overlap.
# Its candidates come from `_cand`, which has already been restricted by the
# plant allowable matrix.  Matched July/August runs both recovered fed volume,
# so this is now the default; set the flag to 0 only to reproduce the control.
ALLOWABLE_RESCUE = os.environ.get("PLANNER_ALLOWABLE_RESCUE", "1") != "0"

# ---- RESCUE, BUT NOT ONTO A HARD-TIER MACHINE ------------------------------
# `ALLOWABLE_RESCUE` is the only path that leaves a machine's mined rim lock,
# and it does not distinguish tiers. Measured 2026-08-20 against the plant's own
# July MES (400,336 rows with a known rim):
#
#            PLANT off-lock     OURS
#   hard          0.0 %         7.7 %      <- the plant NEVER breaks a hard lock
#   primary       9.8 %        20.1 %
#   flex         23.7 %        52.9 %
#   TOTAL         5.3 %        15.7 %
#
# A `hard` machine is defined as single-rim at ~100 % purity over 8 months; the
# plant honours that exactly. We put 16,957 (Jul) / 14,794 (Aug) foreign-rim
# tyres on them. Turning the whole rescue off fixes it (hard 7.6 -> 1.1 %) and
# costs PCR -4,980 / -16,154 BUILT, so a blanket refusal is not the answer.
# This flag is the middle path: keep the rescue for `primary` and `flex`, where
# the plant itself mixes, and refuse it on `hard`.
# SHIPPED ON 2026-08-20 ON PLANT INSTRUCTION, WITH THE COST STATED AND ACCEPTED.
# Cost: PCR -1,076 BUILT / -0.26 pt (Jul), -5,441 / -1.17 pt (Aug). TBR byte-
# identical. Hard-tier off-lock 7.6 -> 0.9 % (Jul), 6.2 -> 0.5 % (Aug). L11
# 31 -> 32. The blanket `ALLOWABLE_RESCUE=0` alternative was measured and is
# strictly worse on both axes: -4,980 / -16,154 BUILT for WORSE hard-tier
# compliance (1.1 / 0.7 %). Full record: PARTITION_AND_CHANGEOVER.md 4ao.
# Set to 0 to reproduce the pre-2026-08-20 control.
RESCUE_SKIP_HARD = os.environ.get("PLANNER_RESCUE_SKIP_HARD", "1") == "1"

# ---- WHICH TIERS THE OFF-LOCK RESCUE MAY NOT ENTER (PLANNER_RESCUE_SKIP_TIERS)
# WHAT THIS DOES / WHY IT EXISTS -- a measured gap, found 2026-08-21.
#   `RESCUE_SKIP_HARD` above closed the `hard` tier and left `primary` open. On
#   August PCR the plan then sits at 29.1 % off-lock on `primary` against the
#   plant's own 9.8 % -- three times looser on the tier that carries 42,195 of
#   our 67,770 off-lock tyres. `flex` is EXEMPT BY DESIGN: the plant's own flex
#   machine runs 23.7 % off-lock and mixing is what it is for (4q.6, DO-NOT #33).
#
#   The comma-separated list generalises the boolean. `hard` reproduces the
#   shipped behaviour exactly; `hard,primary` is the arm this exists to test.
#   `RESCUE_SKIP_HARD=0` still empties the set, so the pre-2026-08-20 control is
#   unchanged.
#
# THE HYPOTHESIS IT WAS BUILT TO TEST.
#   A diff-size changeover costs 42-60 min against 22-28 for same-size, so
#   refusing off-lock rescues on `primary` should buy back machine hours and
#   partly pay for its own volume loss. That is a hypothesis, not a reason --
#   it is to be TESTED, and the same-size share and the weighted changeover
#   minutes per machine-day must be reported beside BUILT or the test is not a
#   test. Result table below, filled in from the run, whatever its sign.
#
# SHIPS AS `hard` (= the shipped behaviour: verified byte-identical to base on
# all nine plan artefacts).
# MEASURED 2026-08-21, AUGUST 2026, BOTH PLANTS. Fresh arms via
# scripts/run_arm.py, partition stamped 2026-08, `check_arm_fresh` FRESH.
#
#   PCR              BUILT   dBUILT  in-month   ful%  starved  same%  wCO   occ%  L11
#   hard (shipped) 409,511       +0   397,326  92.59   12,477   69.3  73.6  83.7   32
#   hard,primary   398,272  -11,239   388,690  90.57   23,465   74.7  66.5  81.5   33
#
#   primary off-lock 29.1 % -> 16.0 %   ·   hard off-lock 0.5 % -> 0.9 % (WORSE)
#   R5 max 63.3 -> 71.9 h (against a 72 h cap)  ·  carry-out tail 12,620 -> 10,268
#   weighted setup 458.6 h -> 426.0 h  ·  changeovers 838 -> 817
#   TBR: every aggregate IDENTICAL (BUILT 98,003, starved 1,654, R5 71.7 h,
#   same-size 100.0 %) -- 8 of its 9 machines are hard/primary, and none of them
#   was carrying a foreign rim to begin with, so closing them only reshuffles
#   which press each slice feeds. (Note for 4ao, which says TBR is *byte*-
#   identical: at `hard,primary` the per-row assignment DOES move -- qty, cure_ts
#   and wait_h all differ -- while every total holds. Identical KPIs, different
#   file.)
#
# THE HYPOTHESIS IS REFUTED, AND BOTH HALVES OF IT MOVED THE PREDICTED WAY.
#   Same-size share rose 5.4 pt and weighted changeover fell 7.1 min/machine-day
#   -- 32.6 hours of setup genuinely freed, and it bought back nothing. Machine
#   OCCUPANCY FELL 83.7 -> 81.5 %, which on 11 machines x 744 h is ~180 machine-
#   hours LOST. We saved 33 hours of setup and lost 180 hours of production.
#   The refused runs do not become cheaper work somewhere else; they become no
#   work at all. `r5_shelf_life` starvation goes 5,044 -> 18,362 and R5 max
#   climbs to 71.9 h, one tenth of an hour under the cap.
#
#   It also does not reach compliance. `primary` lands at 16.0 %, still above the
#   plant's own 9.8 %, and hard-tier off-lock gets WORSE (0.5 -> 0.9 %) because
#   runs the rescue would have taken are placed by other paths that also leave
#   the lock. That is 4ao's own finding reproduced one tier up: refusing more is
#   both dearer and dirtier. The second, untraced leak named in 4ao's "Residual"
#   is what puts the floor under both numbers.
#
#   The one thing bought is a single invariant: "PCR same-size share of build
#   changeovers" flips FAIL -> PASS (L11 32 -> 33). 11,239 tyres for one green
#   cell is not a trade; it is a metric being satisfied at the plan's expense.
#
# VERDICT: DEFAULT STAYS `hard`. Kept gated, with its number, so nobody re-runs
# it blind. Full record: PARTITION_AND_CHANGEOVER.md 4ar.
_SKIP_TIERS_ENV = os.environ.get("PLANNER_RESCUE_SKIP_TIERS", "hard")
RESCUE_SKIP_TIERS = {t.strip().lower()
                     for t in _SKIP_TIERS_ENV.split(",") if t.strip()}     if RESCUE_SKIP_HARD else set()

# ---- RIM PRIORITY: the inch lock as a PRIORITY with sequential campaigns ----
#
# PLANT REQUEST, in their words: "Remove the hard lock, make it priority-wise.
# If the dominant inch is complete we can switch to another inch. E.g. machine 9
# has priority for 12 inch, but we have less 12-inch demand this month -- so
# after completing 12 inch we switch to 13 inch and make only 13 like that."
#
# THIS IS NOT `HARD_LOCK=0`. That arm lets a machine mix rims freely all month
# and measured same-size 96.5 -> 69.3 %, below the plant's own 91.5 % (§4j.3).
# The request is the disciplined version: ONE rim at a time, run to completion.
#
# WHAT THE DATA SAYS ABOUT THE PREMISE -- measured before implementing, and it
# does not hold on either month. Every PCR rim's CURE demand spans day 0 to day
# 31 (July: R12 0.0-30.4, every other rim 0.0-31.0; August the same), and no rim
# is short: the smallest is R16 at 32,597 tyres against R13's 125,856. There is
# no month in this data where a machine "completes 12 inch" and has time left.
#
# AND R5 FORBIDS THE LITERAL SHAPE. A tyre must cure within 72 h of being built
# (GT wait p50 is 5.8 h), so a rim whose cures run all month must be BUILT all
# month. A machine cannot run R12 for 20 days and R13 for 10 unless the PRESSES
# are campaigned the same way -- the building machine's rim sequence is a shadow
# of the cure schedule's, not an independent choice. A month-long single-rim
# campaign per machine is therefore infeasible here, not merely expensive.
#
# WHAT IS ACHIEVABLE, and what this flag does: campaign at the LENGTH R5 allows.
# Hold a machine on its current rim for a minimum run of hours before letting
# another rim take it, so visits to a rim are FEWER AND LONGER instead of
# interleaved. The deadline always wins -- a run whose cure deadline needs the
# machine now gets it, and the campaign simply ends early. That trade is the
# crux and it is made explicit here: rim purity is a TIE-BREAK, never a refusal.
RIM_PRIORITY = os.environ.get("PLANNER_RIM_PRIORITY", "0") != "0"
# Minimum hours a machine holds its open rim before another rim may prefer it.
# 0 reproduces the shipped `last_gt` continuity exactly.
RIM_MIN_CAMPAIGN_H = float(os.environ.get("PLANNER_RIM_MIN_CAMPAIGN_H", "24"))
# How many DIFFERENT rims a machine may host at once beyond its primary.
# The plant asked for "switch to another inch and make only 13 like that" --
# one adopted rim at a time is that rule.
RIM_MAX_CONCURRENT = int(os.environ.get("PLANNER_RIM_MAX_CONCURRENT", "2"))
# Widen the spill target from the single historical flex machine to any machine
# with partition slack, ranked by CHEAPEST size change then most free hours.
# `0` keeps the shipped flex-only spill (§4j.4, +1.47 pt).
RIM_ADOPT = os.environ.get("PLANNER_RIM_ADOPT", "0") != "0"

# ---- SISTER-SKU GROUPING (plant request) ----------------------------------
# "Group GTs that differ by only ONE component." Consumed as a candidate-machine
# tie-break below rim coherence -- see `_sistkey`. Needs
# `INPUT/derived/gt_sister_group.parquet`; a clean no-op when the file is absent.
SISTER_GROUP = os.environ.get("PLANNER_SISTER_GROUP", "0") != "0"
# Bounded queue reorder: group sisters WITHIN a deadline bucket of this many
# hours. 0 = off (the shipped strict earliest-deadline-first order).
SISTER_BUCKET_H = float(os.environ.get("PLANNER_SISTER_BUCKET_H", "0"))

# ---- CONSTRUCTION-CLUSTER GROUPING ----------------------------------------
# The plant-supplied `SKU_Construction_Clusters_{PCR,TBR}.xlsx` workbooks,
# ingested to `INPUT/derived/sku_con_cluster.parquet` by
# `scripts/build_sku_con_cluster.py`. A cluster is a set of GTs whose building
# component codes agree to within the workbook's linkage cut.
#
# WHY THE QUEUE AND NOT THE MACHINE TIE-BREAK. `SISTER_GROUP` put the same idea
# in `_sistkey` (the candidate-machine sort at ~1739) and it was a MEASURED
# NO-OP: `PIN_RUNS` breaks on the first feasible machine and the partition
# usually leaves exactly one candidate, so ordering a list of one orders
# nothing. Adjacency in time is a property of THIS HEAP. Same shape as
# `SISTER_BUCKET_H`, different grouping key.
#
# WHAT THE PLANT DOES (Feb-Jul, true setup blocks, >1 h gap cutoff):
#   TBR  same-cluster adjacency 67.2 % vs a 44.2 % within-machine permutation
#        null (z = +45.5); realised gap 7.5 min same-cluster vs 15.8 min
#        different-cluster same-rim.
#   PCR  14.1 % vs a 10.0 % null (z = +7.5); gap 2.4 min vs 13.9 min.
# Both are real, and BOTH ARE INVISIBLE TO `v_changeover_build`, which is keyed
# on (machine x same/different size) only and charges both at the same rate.
# The benefit therefore cannot show up in `weighted_setup_h` by construction --
# read `cluster_adj_pct` from `scripts/arm_kpi.py` instead.
CLUSTER_BUCKET_H = float(os.environ.get("PLANNER_CLUSTER_BUCKET_H", "0"))
CLUSTER_PLANTS = {s.strip().upper() for s
                  in os.environ.get("PLANNER_CLUSTER_PLANTS", "PCR,TBR").split(",")
                  if s.strip()}

# ---- CLUSTER SEQUENCING (PLANNER_CLUSTER_SEQ) -----------------------------
# The SAME signal as CLUSTER_BUCKET_H above and a DIFFERENT key, because the
# measurement that motivates it is conditioned differently.
#
# WHAT WAS MEASURED (plant, Feb-Jul, true setup blocks, within-machine
# permutation null, 2,000 reps) -- restricted to SAME-SIZE transitions, which is
# the only population where a construction preference can act at all:
#     PCR   same-cluster share 20.4 % observed vs 15.5 % null  (+4.9 pp, p=5e-4)
#     TBR                      71.9 %          vs 57.1 %       (+14.8 pp, p=5e-4)
#   realised gap, same-cluster vs different-cluster same-size:
#     PCR   2.3 min vs 14.0 min      TBR  6.6 min vs 15.8 min   (~6x)
# So the plant's construction preference is an INTRA-SIZE CAMPAIGN-CONTINUATION
# rule: having chosen a size to run, it picks the next GT inside that size from
# the same construction cluster. It is NOT a cross-size grouping rule.
#
# HENCE THE KEY IS (machine, rim, cluster), NOT (cluster).
# `CLUSTER_BUCKET_H` keys on the bare cluster id, which re-sorts a whole bucket
# by an arbitrary label across every machine and every rim: two cluster-mates
# pinned to DIFFERENT machines are dragged adjacent in the queue, which cannot
# create adjacency on a machine and only costs deadline discipline. Scoping the
# key by machine first confines the reorder to jobs that can actually end up
# next to each other, and by rim second keeps the continuation intra-size, which
# is the form the plant's own signal takes.
#
# WHY THE QUEUE AND NOT THE CANDIDATE-MACHINE SORT. Measured: a candidate
# tie-break is BYTE-IDENTICAL to baseline, because `HARD_PIN` breaks on the
# first feasible machine and the partition usually leaves one candidate.
# Ordering a list of one orders nothing (DO-NOT #35). Adjacency in time is a
# property of THIS HEAP.
#
# COVERAGE, and the fall-through. TBR 99.1 % of tyres, PCR 68.5 % -- the largest
# PCR GT (`GT 1513 XPC1 MSIL`, 17 % of PCR volume) has NO workbook row, and the
# workbook's `GT1513 NEO` is a different tread, so the 4-digit numeric-core join
# is banned (§4r.1). An uncovered GT keeps its real machine and rim terms and
# takes the sentinel "~" for the CLUSTER term only: it stays in plain deadline
# order among the other uncovered jobs of its own machine and rim, and is never
# pushed behind another machine's work. It is a fall-through, not a penalty.
#
# THE KEY MUST STAY HOMOGENEOUS across the heap -- a bare `datetime` beside a
# tuple makes `heapq` compare across types and raise. Out-of-scope plants get
# "~" INSIDE the tuple, never a different key shape.
#
# MEASURED, two months, fresh arms, production config
# (STRICT_LOT_FLOOR=1, L5_TAKT=flat/1.0/TBR/PART=1, SLIVER_PCR=1.0,
#  SLIVER_TBR=0, MAKEROOM=1, MR_POINTS=1), partition rebuilt per month:
#
#   key=mrc          Jul PCR   Jul TBR   Aug PCR   Aug TBR
#     B=2             -0.18     -0.42     -0.51     -0.44
#     B=4             -0.42     -0.81     -0.69     -0.34
#     B=24            -2.26     -4.13       --        --
#   key=rc
#     B=2             -0.10     +0.05     -0.55     +0.40
#     B=4             -0.62     +0.18     -0.73     +0.83
#     B=8             -0.46     -0.10     -0.92     +0.29
#     B=24            -1.53     -0.55       --        --
#
# PCR IS NEGATIVE ON BOTH MONTHS AT EVERY SETTING AND BOTH KEYS, and its cluster
# adjacency does not even rise (2.6 -> 2.0-3.2 under `rc`). That is the §4s.3
# fact, not a tuning failure: only 4 PCR GTs / 6.9 % of July demand sit in a
# multi-GT cluster with a co-active partner, so the reorder is nearly pure cost.
# TBR is single-peaked at B=4 on BOTH months independently.
# Hence the defaults below: OFF, and when switched on, `rc` / TBR / 4 h.
# DEFAULT ON, BOTH PLANTS, BY PLANT INSTRUCTION (2026-08-10). The PCR cost above
# is accepted: -0.62 pt Jul PCR / -0.73 pt Aug PCR buys the plant's own
# construction-cluster campaigning on PCR as well as TBR. `0` reverts.
CLUSTER_SEQ = os.environ.get("PLANNER_CLUSTER_SEQ", "1") != "0"
# Bound on the reorder: deadlines are rounded DOWN to a bucket of this many
# hours and the (machine, rim, cluster) grouping acts only WITHIN a bucket, so
# deadline discipline is preserved to within B. At B -> 0 the key degenerates to
# the shipped one. The sister equivalent measured B=4 better than B=12 on both
# axes, so the sweep starts fine -- and B=4 is where TBR peaks on both months.
CLUSTER_SEQ_H = float(os.environ.get("PLANNER_CLUSTER_SEQ_H", "4"))
# BOTH PLANTS by plant instruction (2026-08-10). `TBR` alone makes the PCR plan
# BIT-IDENTICAL to base -- verified on `build_schedule.parquet` for every arm on
# both months -- and is the fulfilment-maximising scoping; PCR is included
# because the plant wants the cluster campaigning, not because it is free.
CLUSTER_SEQ_PLANTS = {s.strip().upper() for s
                      in os.environ.get("PLANNER_CLUSTER_SEQ_PLANTS",
                                        "PCR,TBR").split(",") if s.strip()}
# Which terms scope the grouping. MEASURED, and the reason this knob exists:
# scoping by machine makes the bucket TOO SPARSE for the cluster term to ever
# fire. Distinct GTs per (bucket, machine) on the July plan --
#     B=2   1.01   only  0.7 % of cells hold more than one GT
#     B=4   1.13         13.2 %
#     B=8   1.53         50.3 %
#     B=24  2.87         89.5 %
# -- against 7.0 / 9.6 / 13.7 / 23.8 distinct GTs per (bucket, PLANT). So under
# `mrc` the cluster term is nearly inert below B=8 while the MACHINE term still
# re-sorts the bucket, i.e. all of the disturbance and none of the mechanism.
#   mrc  (machine, rim, cluster)  the literal "within the same machine and rim"
#   rc   (rim, cluster)           same intra-size scoping, ~20x denser buckets.
#                                 Safe because the workbook clusters are
#                                 single-rim by construction (rule 5 drops
#                                 multi-rim clusters; `multi_rim` sums to 0), so
#                                 the cluster term already implies its rim and
#                                 the machine term is largely redundant with it
#                                 -- 33 of 37 real PCR clusters are already
#                                 single-machine single-rim.
#   c    (cluster)                the unscoped shape, kept ONLY as the control
#                                 contrast against the already-rejected
#                                 `CLUSTER_BUCKET_H`.
# `rc` beats `mrc` on all four plant-months. Do not restore `mrc` as the default
# because it is the literal wording of the request -- it was measured.
CLUSTER_SEQ_KEY = os.environ.get("PLANNER_CLUSTER_SEQ_KEY", "rc").strip().lower()

# ---- MOST-CONSTRAINED-FIRST (PLANNER_L7_PINNED_FIRST) ---------------------
# WHAT THIS DOES / WHY IT EXISTS -- a proposed defect, measured 2026-08-19.
#   A GT allowed on ONE machine has no alternative; a GT allowed on several can
#   be built elsewhere. In the placement heap below, `_n_elig` is the THIRD key
#   -- it only breaks ties among jobs that already share an `_hkey`, and since
#   `_hkey` ends in the exact cure timestamp, two jobs almost never tie. So a
#   FLEXIBLE GT with a marginally earlier deadline takes a PINNED GT's only
#   machine, and the pinned GT starves with nowhere to go. In plant terms: the
#   one press-line that can make GT 1865 gets filled by a tyre that four other
#   lines could have made.
#
#   The fix under test orders LEAST-FLEXIBLE-FIRST *within a deadline bucket* of
#   B hours. It does NOT drop the deadline -- this is a pull system, L5 has
#   already fixed every cure time and building must still arrive before it. The
#   bucketing shape is copied from SISTER_BUCKET_H / CLUSTER_SEQ_H above: round
#   the deadline DOWN to B hours, reorder only inside a bucket, keep the true
#   deadline (and every existing term) below it. At B = 0 the key is byte-
#   identical to the shipped one.
#
#   THE KEY MUST STAY HOMOGENEOUS. `_hkey_base` returns a 5-tuple under
#   CLUSTER_SEQ, a 3-tuple under CLUSTER_BUCKET_H and a bare `datetime`
#   otherwise; this wrapper nests whichever one it gets as the LAST element of a
#   fixed 3-tuple, so every push in a given run has the same shape and `heapq`
#   never compares across types.
#
# SHIPS OFF (0). MEASURED 2026-08-19 ON *BOTH* MONTHS, BOTH PLANTS, FRESH ARMS,
# one frozen partition per month. THE DEFAULT STAYS 0 AND THE AUGUST RUN IS WHY.
#
#   B=8, the July winner, DOES NOT REPRODUCE ON AUGUST:
#     plant   Jul dBUILT   Aug dBUILT
#     PCR         +651       +2,403
#     TBR         +237          -78     <- SIGN FLIPS ACROSS MONTHS
#   and August B=8 turns `TBR median GT wait vs tau*` PASS -> FAIL, an invariant
#   that did not move in ANY July arm. Mixed-sign across months is a REJECT by
#   `scripts/ab_both_months.py`'s own rule, whatever the PCR column says.
#
# Full August table, the two-month verdict for every bucket, and the trap in the
# L11 pass COUNT (it rises 28 -> 29 at B=8 while a TBR invariant goes red) are at
# the heap push in `_release()`. Read PARTITION_AND_CHANGEOVER §4w before
# changing this default. Cross-reference: §4v (EDD, a DIFFERENT change -- that
# one DROPPED the deadline at L5 and lost 3.8 pt; this one keeps it and only
# reorders inside a bucket at L7).
# SHIPS AT 8 ON PCR, OFF ON TBR. Measured 2026-08-20, fresh arms, both months,
# on the closing-buffer baseline. This is the largest single fulfilment lever
# found in this project.
#
#            BUILT      in-month    starved     L11      R5 max
#   Jul PCR  +6,059     +1.52 pt    -6,059      30->31
#   Aug PCR  +5,625     +1.31 pt    -5,625      31->32   62.7 -> 61.4 h
#   Jul TBR    -335     -0.43 pt      +335
#   Aug TBR    -122     -0.16 pt      +122               71.7 -> 68.1 h
#
# The gain is EXACTLY the starvation reduction on both months, i.e. real tyres,
# not relocation. `BUILT = campaigns - starved` is the only equation that moves
# fulfilment on this plant, and this is the only lever measured to move the
# right term.
#
# WHY IT WORKS -- most-constrained-first, and the constraint is real. August PCR
# starvation by machine breadth of the starved GT:
#     base      14,389 of 18,311 (79 %) on GTs allowed 1-2 machines
#     pinned=8   5,942 of 12,686 (47 %)
# `GT 2568 HT2` (ONE allowed machine) starves 4,622 on base and leaves the top
# six entirely under the flag. r5_shelf_life starvation also halves, 4,536 ->
# 2,060, because a constrained GT no longer waits for its only machine.
#
# TBR IS SCOPED OUT. It loses 122-335 tyres on both months. TBR's GTs are not
# machine-constrained the way PCR's are (2 single-machine GTs vs PCR's 12), so
# reordering only displaces work. Plant-scoped via PLANNER_L7_PINNED_FIRST_PLANTS.
#
# DO NOT COMBINE WITH RIM_PRIORITY. Measured together on August: +5,253 against
# +5,625 for pinned alone -- rim's extra switches eat part of the gain. They are
# better apart.
#
# 8 h was not swept. If it is ever tuned, gate on BUILT on BOTH months and BOTH
# plants; a value that helps July and hurts August fails.
_PF_PLANTS = {x.strip().upper() for x in
              os.environ.get("PLANNER_L7_PINNED_FIRST_PLANTS", "PCR").split(",") if x.strip()}
PINNED_FIRST_H = float(os.environ.get("PLANNER_L7_PINNED_FIRST", "8"))

# Try the GT's OWN pinned machine before any rim-mate (P7 / B9 / B10 / B14).
#
# DEFAULT OFF -- MEASURED NET-NEGATIVE, twice. Keep the flag; do not re-derive.
#   arm              1-machine GTs   machines/GT   same-size   weighted setup
#   off (shipped)           27.5 %          2.12      86.9 %           242 h
#   pin to home             32.5 %          2.08      79.4 %           289 h
#   pin ∩ rim lock          37.5 %          1.95      82.3 %           264 h
#   plant                   66.7 %          1.40      91.5 %           172 h
# Stickiness rises and the weighted setup -- the metric that actually costs
# money -- gets WORSE on both arms, because in our formulation machine
# stickiness and rim purity are in tension while in the plant's they are not.
#
# WHY THE PLANT GETS BOTH: its machine->rim map is a PARTITION. Each machine
# runs one rim, and the GTs of a rim are split ONCE across that rim's machines
# and never moved. We assign inside a rim group dynamically by load, so a GT
# bounces between the R13 machines. Forcing it to stop bouncing does not create
# the partition -- it just makes a bad partition rigid. The fix is to BUILD the
# partition (GT -> exactly one machine, chosen once, load-balanced inside the
# rim group), not to pin harder onto a partition we never built.
HARD_PIN = os.environ.get("PLANNER_HARD_PIN", "1") != "0"
# Sentinel GT for a booked plant shutdown on a machine (rule G3). It is a
# GT code no plant will ever issue, so it can never collide with a real one,
# and `_setup_s` returns 0 for it -- a closure is not a changeover.
_HOL_GT = "__PLANT_HOLIDAY__"

# Use the static GT -> machine partition from
# `scripts/build_gt_machine_partition.py` as the horizon assignment, instead of
# assigning inside a rim group dynamically by load. This is what makes HARD_PIN
# worth arming: the pin is only as good as the partition it pins to.
#
# The partition FILE covers both plants (PCR 57 pairs / 44 GTs, TBR 55 / 50).
# It is APPLIED to PCR only, because TBR has nothing to win:
#   applied to    P same   T same   P setup   T setup   T 1-mach   T inv   ful
#   PCR            97.7 %   100 %     181 h     183 h     30.6 %   1,148  93.4 %
#   PCR,TBR        97.7 %   100 %     181 h     185 h     34.7 %   1,122  92.8 %
#   plant          91.5 %   100 %     172 h     167 h     46.4 %   1,108   100 %
# TBR is already at 100 % same-size with or without it -- two sizes across nine
# machines is not a partitioning problem -- so it buys 4.1 pt of stickiness and
# 26 tyres of inventory for 2 h more setup and 0.6 pt of demand. Not worth it.
# (An earlier reading of 96.6 % / 195 h was measured before the per-machine
# cadence fix and overstated the loss; TBR is near-neutral, not harmful.)
#
# RE-MEASURED 2026-08-18, THEN THE MEASUREMENT ITSELF WAS CORRECTED. Read both
# paragraphs -- the first version of this comment reached the right default for
# two wrong reasons and would have taught the next reader both of them.
#
# THE ARMS MUST BE "PCR" vs "PCR,TBR". The first pass ran `PARTITION_PLANTS=`
# (empty) against "PCR,TBR" and drew a conclusion about a setting neither arm
# used. Run the missing arm and PCR is BIT-IDENTICAL between "PCR" and
# "PCR,TBR" on both months, on every column -- so the PCR same-size figures that
# first appeared here (79.0->81.1 Jul, 74.6->75.4 Aug) are the value of
# partitioning PCR, which BOTH candidates do. They are worth 0.0 to this choice.
#
# "TBR GAINS NOTHING" WAS ALSO FALSE. Partitioning TBR removes changeovers on
# both months -- 983->968 (Jul) and 696->672 (Aug), weighted 34.0->33.6 and
# 23.8->23.0 min/machine-day. And TBR same-size cannot be the evidence either
# way: B16 makes the TT/TL split COLLINEAR WITH RIM on both months (TT all R20,
# TL all R22.5), so TBR same-size is pinned at 100 % by B16 itself. The
# partition can neither raise it nor break it. A metric with no variance is not
# a measurement of no effect.
#
# THE ACTUAL REASON, and the only one that survives:
#
#              TBR BUILT      TBR fulfilment
#   Jul          -168            -0.18 pt
#   Aug          +174            +0.18 pt
#
# Mixed sign on tier-1 BUILT across two months. The engine is deterministic --
# identical arms return identical numbers to the tyre -- so this is a real
# month-dependent effect, not noise, which makes it a clean two-month-gate
# FAILURE rather than a null. Fewer changeovers do not buy it a pass when the
# tier above them cannot be shown to improve.
#
# Set PLANNER_PARTITION_PLANTS=PCR,TBR to re-measure -- against "PCR", not
# against empty, and judge on TBR BUILT.
PARTITION_PLANTS = set(
    os.environ.get("PLANNER_PARTITION_PLANTS", "PCR")
    .replace(" ", "").split(","))
USE_PARTITION = PARTITION_PLANTS != {""}

# HARD ceiling on plant GT stock, in tyres. 0 = off. A placement that would push
# the running stock profile above this at any hour is refused.
#
# RUNAWAY RAIL, not a controller. Daily-mean basis, deliberately wide.
#
# G8 (4,500-4,800 PCR / 1,200-1,500 TBR) is a TIER-5 objective; fulfilment is
# TIER 1. Enforcing G8 as a hard per-hour placement refusal inverted the
# lexicographic order and cost 18.4 points of demand -- dead centre of the
# 20-35 % that this file's own design rule predicts for ANY conjunctive
# constraint at 95 % utilisation. G8 exists as a DETECTOR (a 4,147 -> 5,851
# regression once went unnoticed for a whole session); it was never a controller.
#
# Two measurements settle the sizing:
#  * The band IS the plant's actual daily-mean stock. Level = opening + cumsum
#    gives PCR 4,567 and TBR 1,327 on July, and all three statistics
#    (event-mean / calendar-day mean / 24 h-rolling) agree within 0.4 %, because
#    the plant's profile is flat. So the statistic does not matter; the BASIS
#    (mean, not peak) does.
#  * At the plant's W of 8.84 h, full demand needs I = 525 x 8.84 = 4,641 --
#    inside the band. G8 and 100 % fulfilment are NOT in tension: together they
#    are a specification on W (<= 9.14 h). Ours is 9.68.
#
# So the rail sits well above the band: it still catches a 5,851-class runaway
# but never binds on a legitimate placement. Inventory is controlled by W (M4),
# ranked at tier 5 in the objective, and REPORTED by the G8 invariant.
# VALUES COME FROM config.thresholds.gt_wip_rail -- the single source of truth
# (see the SINGLE SOURCE block in config.py). Do NOT write the numbers here.
# The env vars stay for A/B only; RunContext hashes config but NOT env, so always
# run both arms fresh when you use them.
WIP_RAIL = {p: float(os.environ.get(f"PLANNER_WIP_RAIL_{p}",
                                    CONFIG.thresholds.gt_wip_rail.get(p, 0)))
            for p in ("PCR", "TBR")}

# Headroom the rail keeps back so the STATED cap is the one honoured after
# reconciliation. See _cap_ok. NB the 4,800 PCR cap is TIGHTER THAN THE PLANT:
# measured time-weighted, the plant itself runs a 4,832 mean and a 5,379 daily
# max in July. Kept because it was set by instruction, but recorded so it is not
# mistaken for a plant-derived limit -- it is the same "mined stat as a hard
# constraint" shape as tau* and min_lot (PARTITION_AND_CHANGEOVER.md §1).
RAIL_MARGIN = float(os.environ.get("PLANNER_RAIL_MARGIN",
                                   CONFIG.thresholds.gt_wip_rail_margin))

# Common replenishment interval T, in hours. 0 = derive from the GT inventory
# band via T = 2 x (I_target/sum(r_g) - tau*). Set explicitly to sweep.
#
# DEFAULT 16 h, SOLVED WITH n_g -- not tuned against one target in isolation.
# Q = r_g x T with r_g = n_g x press_rate, and I = lambda(tau* + (Q/2)(1/r - 1/b)).
# Pinning the plant's two targets Q=363 and I=4,772 fixes BOTH parameters:
#     W = 9.14 h -> r = 22.8 tyres/h -> n_g = 3.33 (plant 3.28-3.42), T = 15.9 h
# Tuning them separately is what produced every earlier trade: T=24 alone gave
# lot 432 with inventory 7,886; n_g from the D-target alone gave inventory 4,361
# at 89% fulfilment. The month-derived default (T = 6.42 h) leaves r_g x T below
# the B12 floor on 80% of PCR GTs, so the FLOOR sets every lot, not demand.
LOT_INTERVAL_H = float(os.environ.get("PLANNER_LOT_INTERVAL_H", "16"))

# How many hours a run may be released EARLIER than its slice deadlines require
# before another machine is preferred. Soft: if no machine passes the cap the run
# is retried uncapped, so it can never cost a placement.
#
# DEFAULTS OFF. Measured twice, and it is not the free lever it looks like.
#
# 1st (deadline ordering, no rim lock): inventory 5,119 / 4,922 / 4,961 / 5,061
#    at cap off / 12 / 6 / 3 -- noise. The earliness then was a symptom of
#    placing runs GT-by-GT rather than by deadline, not of the backward walk.
# 2nd (with the rim lock, earliness had risen 0.93 -> 2.00 h): the cap DOES cut
#    inventory, but only by trading rim purity, because avoiding an early build
#    means moving machine and that breaks the lock. Measured on PCR:
#        cap off        inv 6,946   same-size 78.2%   setup 265 h
#        lock-scoped 12 inv 6,595   same-size 73.3%   setup 295 h
#        unscoped 12    inv 5,825   same-size 56.2%   setup 397 h
#    Only ~350 of the ~1,048 tyres of earliness have a same-rim alternative; the
#    rest cost the lock. EARLINESS AND RIM PURITY ARE THE SAME RESOURCE.
# Left off because changeovers are the higher priority; the remaining inventory
# lever is the DRAIN term (n_g), not this one.
EARLY_CAP_H = float(os.environ.get("PLANNER_EARLY_CAP_H", "inf"))

# Lifts the run-size ceiling ONLY for GTs whose `r_g x T` falls below the B12
# floor -- i.e. exactly the GTs where min_lot was acting as both floor AND
# ceiling. 1.0 = the old behaviour, bit for bit. See the block at the `target`
# computation in Phase 2a. R5's span_cap remains the hard outer bound.
# PER PLANT BY MEASUREMENT, July on the setup-corrected baseline, all arms fresh:
#   arm       TBR ful  TBR runs  TBR sub-floor  TBR inv  TBR R5  maxrun p10/p25  L11
#   1.0        94.2 %     1,005        34.6 %       935  68.7 h      85 / 87      25
#   2.0 <-     94.3 %       914        30.6 %     1,097  67.4 h    119 / 143      28
#   3.0        93.9 %       900        32.0 %     1,144  70.8 h    116 / 140      28
# 2.0 costs NO fulfilment (+0.1 pt), takes TBR sub-floor from 34.6 % to 30.6 %
# (the plant's own is 30.8 %), improves the R5 margin, and breaks the flat band --
# max-run-per-GT p10/p25 goes 85/87 to 119/143. TBR daily-mean inventory max
# 1,214 -> 1,336 against the 1,400 rail: inside, 64 tyres of margin. +3 invariants.
# 3.0 is past the knee -- it costs 0.3 pt for no further sub-floor gain.
#
# PCR STAYS AT 1.0. PCR sits at its inventory rail on 16 of 33 days, so there is
# no headroom to spend, and only 11 % of PCR volume is floor-bound anyway.
#
# *** SHIPPED AT 1.0 -- 2.0 WAS REVERTED. IT DOES NOT TRANSFER ACROSS MONTHS. ***
# The table above is JULY. Re-measured on AUGUST, the same 2.0 setting COSTS TBR
# fulfilment:
#     month   TBR in-month fulfilment   1.0        2.0
#     July                              94.22 %    94.27 %   (+0.05 pt)
#     August                            92.59 %    91.88 %   (-0.71 pt)
#     net across both months                       -521 tyres
# Tuning on one month and shipping it is the exact failure this project has paid
# for repeatedly (MEMORY §10c "nothing fitted to a month"): a knob validated on a
# single month's demand shape is not validated. The lot-floor-as-ceiling defect is
# REAL and still open -- the flat max-run band (p10 85 / p25 87 on TBR) is
# genuine -- but this particular lever is not the fix. Keep the flag for A/B;
# do not enable it without measuring at least two months.
TARGET_CEIL_MULT = {
    "PCR": float(os.environ.get("PLANNER_TARGET_CEIL_PCR", "1.0")),
    "TBR": float(os.environ.get("PLANNER_TARGET_CEIL_TBR", "1.0")),
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", default="2026-07")
    ap.add_argument("--run", default=None)
    a = ap.parse_args()
    run = ROOT / "runs" / (a.run or f"cmbc_{a.month}")
    holiday.load(a.month)              # rule G3; empty = every call is identity

    P = json.loads(sorted(PARAMS.glob("params_*.json"))[-1].read_text())
    tau = {p: float(P["tau"][p]["tau_star_h"]) for p in ["PCR", "TBR"]}
    tau_min = {p: float(P["tau"][p]["tau_min_h"]) for p in ["PCR", "TBR"]}
    # Release floor: how close to its cure a tyre may be built. tau* is a TARGET
    # (the plant's median), tau_min is the physical floor (R17). Set
    # PLANNER_TAU_RELEASE=star to restore the old tau*-as-a-wall behaviour.
    _tr = os.environ.get("PLANNER_TAU_RELEASE", "min")
    tau_rel = tau if _tr == "star" else tau_min
    cad = pl.read_parquet(PARAMS / P["tables"]["build_cadence"])
    camp = pl.read_parquet(run / "cure_campaigns.parquet")
    # THE PLANT'S ALLOWABLE MACHINE LIST IS A HARD CONSTRAINT.
    # `cap_machine` widens eligibility with an INCH basis at penalty 5000, and a
    # penalty is a price the greedy release will pay -- 19.9 % of July PCR volume
    # (76,254 tyres over 21 GTs) landed on machines the plant does not sanction.
    # This is the single chokepoint: `elig`/`pen` below, the scarcity ordering,
    # the horizon assignment and _place all derive from `cm`.
    # Two hard filters, in this order. The rim lock was ORDERING-only before --
    # `lock_of` merely supplied rim-matched candidates first -- so hard-tier
    # machines still carried 13.7 % foreign-rim volume on July PCR.
    cm = allowable.restrict(pl.read_parquet(D / f"cap_machine_{a.month}.parquet"),
                            label=f"cap_machine_{a.month}")
    cm = allowable.restrict_rimlock(cm, label=f"cap_machine_{a.month}")
    cm = allowable.restrict_rimset(cm, label=f"cap_machine_{a.month}")
    grp = pl.read_parquet(D / f"cap_ttl_groups_{a.month}.parquet")
    tt = pl.read_parquet(paths.INPUT_DERIVED / "tt_tl.parquet")
    # Rim per GT, for size-aware machine selection (R6/R7). The plant's
    # changeover master is BINARY: same size 22-28 min, different size 42-60.
    _sz = pl.read_parquet(paths.INPUT_DERIVED / "gt_size.parquet")
    rim_of = {r["gt_code"]: str(r["rim"]) for r in _sz.iter_rows(named=True)
              if r.get("gt_code") and r.get("rim")}
    # ---- SISTER GROUPS: GTs differing in exactly ONE component slot -------
    # Built by `scripts/build_gt_sister_group.py` from the raw construction
    # workbooks. The plant's definition, in their words: GTs that differ by only
    # one component.
    #
    # WHAT THE RAW DATA SUPPORTS, measured before wiring anything:
    #   TBR  usable. 91 % of July GTs / 96 % of volume carry a signature via an
    #        exact SKU bridge; 13 live slots; the pairwise distance distribution
    #        is a smooth gradient (d=0:24, d=1:36, d=2:19 ... d=10:356) and
    #        76.5 % of July GTs have a distance-1 partner. 22 groups at d<=1.
    #   PCR  NOT usable for grouping, and this is a property of the product, not
    #        of the loader. Every PCR component code is size-specific, so all
    #        six slots change together: across 595 July GT pairs there are
    #        **ZERO at distance 1, 2 or 3** (d=0:2, d>=4:593), and 82 % of
    #        signatures in the workbook are globally unique. The signature is a
    #        FINGERPRINT, not a similarity metric. Every PCR GT is a singleton
    #        and this tie-break is therefore inert on PCR by construction.
    #
    # A previous pass called PCR construction data "unusable" from the derived
    # parquet. That was half right for the wrong reason -- the loader drops 7 of
    # 8 component columns and nulls `rim_dia` with a bad cast (see MEMORY §10q).
    # The data is fine; the SIMILARITY is what is absent.
    sister_of: dict[str, str] = {}
    _sisf = paths.INPUT_DERIVED / "gt_sister_group.parquet"
    if _sisf.exists():
        for _r in pl.read_parquet(_sisf).iter_rows(named=True):
            sister_of[_r["gt_code"]] = str(_r["sister_id"])
    # ---- CONSTRUCTION CLUSTERS (plant workbooks) -------------------------
    # Keyed on (plant, gt_code): PCR and TBR GT keys are disjoint in practice
    # but the pair costs nothing and removes the assumption.
    # COVERAGE IS NOT UNIFORM and must be stated wherever this is used:
    #   TBR  70/75 active GTs, 99.1 % of Feb-Jul volume.
    #   PCR  70/103 active GTs, 68.5 % -- the workbook simply does not contain
    #        the largest PCR GTs (`GT 1513 XPC1 MSIL`, 374,617 tyres, is absent;
    #        the workbook's `GT1513 NEO` is a DIFFERENT tread pattern and
    #        matching on the numeric core would be silently wrong).
    # An uncovered GT gets the sentinel "~", which sorts last inside its bucket
    # and leaves it in plain deadline order.
    cluster_of: dict[tuple, str] = {}
    # Rim used ONLY by the cluster-sequencing heap key. Deliberately a SEPARATE
    # map from `rim_of`: `rim_of` feeds the changeover cost and the rim lock, and
    # widening it would change baseline behaviour under a flag that is off.
    #
    # COALESCED, because neither source alone covers the clustered GTs.
    # `gt_size.parquet` has a rim for only 35 of the 70 clustered PCR GTs; the
    # workbook carries its own `Rim` column for 56. Where both exist they agree
    # on 80/80 rows across the two plants, so the coalesce is a fill, not a
    # reconciliation. gt_size wins ties because it is the master the rest of the
    # engine prices against.
    rim_key_of: dict[tuple, str] = {}
    _clf = paths.INPUT_DERIVED / "sku_con_cluster.parquet"
    if _clf.exists():
        for _r in pl.read_parquet(_clf).iter_rows(named=True):
            if _r.get("con_cluster"):
                cluster_of[(_r["plant"], _r["gt_code"])] = str(_r["con_cluster"])
            _rk = _r.get("rim") or _r.get("wb_rim")
            if _rk:
                rim_key_of[(_r["plant"], _r["gt_code"])] = str(_rk)
    dem = pl.read_parquet(ROOT / "masters" / "demand" / f"demand_{a.month}.parquet")

    cad_s = {r["machine"]: float(r["cadence_s_p50"]) for r in cad.iter_rows(named=True)}
    plant_cad = {p: float(cad.filter(pl.col("plant") == p)["cadence_s_p50"].median())
                 for p in ["PCR", "TBR"]}
    build_band = {p: float(P["campaign_bands"][p]["build"]["hours_p50"])
                  for p in ["PCR", "TBR"]}
    elig: dict[tuple, list] = {}
    pen: dict[tuple, float] = {}
    for r in cm.iter_rows(named=True):
        elig.setdefault((r["plant"], r["gt_code"]), []).append(r["machine"])
        pen[(r["plant"], r["gt_code"], r["machine"])] = float(r["penalty"])
    group_of = {r["machine"]: r["group"] for r in grp.iter_rows(named=True)}
    tmap = tt.filter(pl.col("sku") != "").select(["sku", "tt_tl"]).unique(subset=["sku"])
    gt_tag = {r["gt_code"]: r["tt_tl"]
              for r in dem.join(tmap, on="sku", how="left").iter_rows(named=True)
              if r["tt_tl"] and r["plant"] == "TBR"}

    y, m = int(a.month[:4]), int(a.month[5:7])
    t0 = datetime(y, m, 1, 7, 0)
    _mend = t0 + timedelta(days=calendar.monthrange(y, m)[1])
    # THE BUILD CALENDAR FLOOR, WHICH IS NOT ALWAYS t0.
    # `t0` stays the horizon anchor for everything that is graded, bucketed or
    # reported against the month -- the rail's hour grid, the deadline heap's
    # buckets, the campaign arithmetic. `_t0b` is the earliest instant a BUILD
    # RUN may start, and it is the only thing PLANNER_L7_PRE_T0_H moves. Keeping
    # them as two names is the point: one file, one meaning per symbol, so a
    # later reader cannot widen the horizon by editing the build floor.
    # Identical to t0 when both windows are 0 -> byte-identical plan.
    _t0b = t0 - timedelta(hours=max(PRE_T0_H, DIAG_PRE_H))
    if _t0b < t0:
        print(f"  [pre-t0] building may be released from "
              f"{_t0b:%Y-%m-%d %H:%M} ({(t0 - _t0b).total_seconds()/3600:.1f} h "
              f"before t0) -- PREVIOUS MONTH's machine hours, not this month's")

    # OPENING GT IS SUPPLY AT t0, NOT JUST A DEMAND OFFSET.
    # L4 nets opening stock off demand, but L7 previously tried to BUILD every
    # tyre -- including those already sitting on the floor at t0. A press curing
    # at t0 needs its GT finished at t0 - tau*, which is before the horizon
    # exists, so those slices failed: 87% of all starved volume was day 1.
    # The stock is real and physically able to feed exactly those cures.
    ogf = _opening_gt_path(ROOT, a.month)
    opening: dict[tuple, float] = {}
    _opening0: dict[tuple, float] = {}   # untouched copy, see CLOSING_BUFFER
    opening_life: dict[tuple, float] = {}      # hours of shelf life left at t0
    opening_ladder: dict[tuple, list] = {}     # per-tyre remaining life, ascending
    if ogf.exists():
        og = pl.read_parquet(ogf).filter(pl.col("age_h") <= GT_SHELF_LIFE_H)
        # Opening stock is ALREADY AGED. It can only feed a cure inside its
        # REMAINING shelf life. A first version ignored this and handed stock to
        # whichever campaign came first for that GT -- including campaigns on day
        # 30, producing 700 h waits, 49 R5 breaches and higher inventory than
        # building the tyres would have caused.
        for r in (og.group_by(["plant", "gt_code"])
                  .agg(pl.len().alias("n"), pl.col("age_h").median().alias("age"))
                  .iter_rows(named=True)):
            opening[(r["plant"], r["gt_code"])] = float(r["n"])
            # SNAPSHOT: `opening` is DECREMENTED as stock is drawn (see the
            # `opening[(p, gt)] -= use` below), so the closing-buffer pass at the
            # end of the layer cannot read the level the month STARTED with from
            # it. Keep an untouched copy.
            _opening0[(r["plant"], r["gt_code"])] = float(r["n"])
            opening_life[(r["plant"], r["gt_code"])] =                 GT_SHELF_LIFE_H - float(r["age"])
        # ---- PER-TYRE SHELF-LIFE LADDER (PLANNER_FULL_AVAILABILITY_T0) ------
        # `opening_life` above is the MEDIAN age of the GT's stock, used as a
        # HARD wall (`hold_h > opening_life -> have = 0.0`) that refuses the
        # WHOLE group wholesale. That is the recurring defect class in
        # PARTITION_AND_CHANGEOVER.md §1: a mined median enforced as a
        # constraint. It is wrong in both directions -- July PCR stock spans
        # age 0.4 h (p05) to 55.7 h (max) against a 6.3 h median -- so the
        # freshest tyres are treated as ~66 h old and refused for cures they
        # could legally feed, while the oldest are treated as fresher.
        #
        # The master is ONE ROW PER TYRE, so every tyre's exact remaining life
        # is already known and no proxy is needed. Hold them as a sorted
        # ascending ladder and serve each draw from the SHORTEST life that still
        # covers the hold (FEFO): early cures take the oldest usable stock,
        # which preserves fresh stock for later cures and maximises total draw.
        #
        # This does NOT relax R5 -- it enforces R5 PER TYRE instead of per
        # median, so no tyre is ever cured beyond its own 72 h.
        for r in (og.group_by(["plant", "gt_code"])
                  .agg(pl.col("age_h").alias("ages")).iter_rows(named=True)):
            opening_ladder[(r["plant"], r["gt_code"])] = sorted(
                GT_SHELF_LIFE_H - float(x) for x in r["ages"])
        print(f"  opening GT available at t0: "
              + "  ".join(f"{p} {sum(v for (pp, _g), v in opening.items() if pp == p):,.0f}"
                          for p in ["PCR", "TBR"]) + " tyres\n")

    print("=" * 92)
    print(f"L7  PULL RELEASE OF BUILDING  --  {a.month}")
    print("=" * 92)
    print(f"  release = t_cure - tau* - build_duration      "
          f"tau* PCR {tau['PCR']:.2f} h  TBR {tau['TBR']:.2f} h")
    print(f"  NOT cure_ts = max(cure_ts, supply_ts)\n")

    # PLACE THE MOST-CONSTRAINED GTs FIRST.
    # Placing chronologically lets a GT with 11 machine options take a slot that
    # a GT with 3 options needed, and the constrained one then starves even at
    # 75% machine load -- 6,271 TBR tyres went unfed that way. Ordering by
    # scarcity (fewest eligible machines, then largest volume) gives the GTs with
    # no alternative first claim; flexible GTs adapt around them.
    # Time order is kept as the final tiebreak so the result stays deterministic.
    # ---- 12-MONTH HISTORICAL MACHINE SHARE -- ORDERING ONLY ---------------
    # `machine_gt_share.parquet` is the plant's own running split per GT, e.g.
    # 98 / 1 / 1. The GT should land ~98 % on its dominant machine, but must
    # SPILL the instant that machine has no legal pre-deadline window: waiting
    # starves a press, and a press-hour is gone for good while an early tyre only
    # ages (72 h of shelf life to spend).
    #
    # So the share SORTS the candidate list and does nothing else. It cannot add
    # a machine (candidates are already allowable-filtered, and 41 share rows
    # naming a forbidden machine are ignored) and it cannot block one -- `_place`
    # walks this list and takes the first machine with a legal window, so
    # fallthrough to the 1 % machines is immediate.
    #
    # Deliberately weaker than `machine_rim_lock`, which was tried as a HARD
    # constraint and cost 14 pt of PCR fulfilment by stranding 8 GTs outright.
    _share: dict[tuple, float] = {}
    _sf = paths.INPUT_DERIVED / "machine_gt_share.parquet"
    # SHIPS AT 0 -- MEASURED NEGATIVE. July PCR 95.3 -> 94.9 pt, same-size
    # 82.3 -> 79.6, weighted CO 78.7 -> 84.2, unfed +1,866. The candidate list
    # is already ordered by rim lock and the L4b allocation, both of which know
    # THIS month's capacity; last year's split overwrites that and a GT's
    # historically dominant machine is often not its rim-consistent one.
    # PCR already tracks history to within 1.8 pt WITHOUT this (82.0 % achieved
    # against 83.9 % historical), so it buys 1.6 pt of concordance for 0.4 pt
    # of fulfilment. Set PLANNER_MACHINE_SHARE=1 to re-measure.
    if _sf.exists() and os.environ.get("PLANNER_MACHINE_SHARE", "0") != "0":
        for _r in pl.read_parquet(_sf).iter_rows(named=True):
            _share[(_r["plant"], _r["gt_code"], _r["machine"])] = \
                float(_r["share_pct"])
        print(f"  [share] {len(_share)} (GT, machine) historical shares loaded "
              f"-- ordering preference, never a constraint")

    def _cand(p: str, gt: str) -> list:
        """Eligible machines for a GT, inside its TT/TL group where B16 applies,
        ordered by 12-month historical share (highest first)."""
        e = elig.get((p, gt), [])
        if p == "TBR" and gt_tag.get(gt):
            e = [x for x in e if group_of.get(x) == gt_tag[gt]] or e
        if _share:
            e = sorted(e, key=lambda m: (-_share.get((p, gt, m), 0.0), m))
        return e

    def _n_elig(p: str, gt: str) -> int:
        return len(_cand(p, gt))

    camp = camp.with_columns(
        pl.struct(["plant", "gt_code"]).map_elements(
            lambda r: _n_elig(r["plant"], r["gt_code"]),
            return_dtype=pl.Int64).alias("_scarcity"))

    # ---- FIRST-72 h PRIORITY -------------------------------------------
    # The queue is scarcity-first: a GT with 1 eligible machine claims before one
    # with 5, which is right for the month as a whole. But it means a
    # single-machine GT whose seat is on day 20 is released BEFORE a 5-machine GT
    # whose press is waiting at hour 0 -- and a press-hour lost on day 1 is gone,
    # while the day-20 seat still has three weeks of slack.
    #
    # Measured: at t0 building can only reach 9 GTs in the first two hours while
    # 30 GTs' presses are seated. Which 9 it picks is decided HERE.
    #
    # So campaigns whose cure seat falls inside the opening window are released
    # first, ordered by seat time then scarcity; everything after the window keeps
    # the existing scarcity order untouched. This is a PRIORITY change only -- no
    # floor moves, no campaign is reseated, and the rest of July is unchanged.
    _d1h = float(os.environ.get("PLANNER_D1_PRIORITY_H", "72"))
    if _d1h > 0:
        _t0c = camp["start_ts"].min()
        camp = camp.with_columns(
            ((pl.col("start_ts") - _t0c).dt.total_hours() < _d1h)
            .cast(pl.Int8).alias("_early"))
        camp = camp.sort(["plant", pl.col("_early") * -1, "start_ts",
                          "_scarcity", "gt_code", "press"])
        _ne = int(camp["_early"].sum())
        print(f"  [d1-priority] {_ne} campaigns seated inside the first "
              f"{_d1h:.0f} h released before the rest")
    else:
        camp = camp.sort(["plant", "_scarcity", "start_ts", "gt_code", "press"])

    # ---- HORIZON MACHINE ASSIGNMENT -- the RUN becomes an object --------
    # One machine per GT for the WHOLE horizon, a second only when the first is
    # out of capacity. This is the constraint the tier-8 dedication price was
    # standing in for: a 10,000 penalty paid 40 times over is not a price, it is
    # an absent constraint. Assigning here rather than per slice is what lets
    # consecutive slices merge into a run that clears the B12 floor.
    days = calendar.monthrange(y, m)[1]
    # ---- PLANNING SPAN vs REPORTING SPAN (PLANNER_HORIZON_MODE=extend) ----
    # `days * 24` is the REPORT window and stays the basis for fulfilment
    # (`_hzn`), the WIP rail and every KPI. But when L5 plans on month + tail,
    # cure seats exist past hour 744 and building must be allowed to reach them,
    # so every MACHINE-CAPACITY quantity is sized on the planning span instead.
    # Charging the month's hours against a plan that spans month+tail would
    # taper the machines exactly where the ruling says not to.
    _TAIL_H = (float(os.environ.get("PLANNER_HORIZON_TAIL_H", "72"))
               if os.environ.get("PLANNER_HORIZON_MODE", "extend") == "extend"
               else 0.0)
    plan_h = days * 24.0 + _TAIL_H
    cap_h = plan_h * MACH_UTIL_CAP
    # LOAD IS MEASURED IN THE MACHINE'S OWN HOURS, NOT THE PLANT MEDIAN'S.
    #
    # This used to be `qty * plant_cad[plant] / 3600` -- a PLANT-MEDIAN cadence
    # charged against a per-machine `cap_h`, while `_place` below already used
    # each machine's own rate (`cad_s.get(mach, ...)`). Three quantities, two
    # currencies. PCR machines run 49-78 s against a 62 s median, so the error is
    # -21 % to +26 %; TBR 189-219 s against ~207 s, +/-6 %.
    #
    # What it produced: a rim-load table that read R12 113.2 %, R13 100.7 % and
    # R17 101.5 % -- "three rims over capacity, 153 h over-subscribed" -- and was
    # used to argue for relaxing the rim lock. Re-measured in each machine's own
    # cadence the same demand is R12 89.5 %, R13 80.6 %, R17 103.1 %: only ONE
    # rim is genuinely over, by 22 h. R12's machine (TBMPCR4) is the FASTEST on
    # the plant at 49 s, so charging it 62 s invented 168 h of load that does not
    # exist. Realised occupancy confirms it -- every PCR machine finishes the
    # month between 61 % and 83 %, none is full.
    #
    # This is EXPERT_AUDIT §4b, and the same "flat plant cadence" defect already
    # fixed once in scripts/build_gt_machine_partition.py (PARTITION §3). Third
    # instance of that bug class; grep before assuming it is gone.
    #
    # The machine is not known until the loop below chooses it, so demand is
    # carried in TYRES -- the only unambiguous unit -- and converted to hours
    # against whichever machine is actually charged. `need_h` survives only as
    # an ordering key, computed on the GT's own eligible-set mean rather than the
    # plant median so scarce/slow GTs sort correctly.
    need_q: dict[tuple, float] = {}
    for r in camp.iter_rows(named=True):
        k = (r["plant"], r["gt_code"])
        need_q[k] = need_q.get(k, 0.0) + float(r["qty"])

    # PLANT BUILDING CYCLE TIMES -- per GT x machine MAKE, replacing the mined
    # per-machine median. This is a GRANULARITY change, not a value swap: the
    # mined table has one number per machine (PCR 49-78 s); the plant's file has
    # one per (GT, make) -- CONTI 40-82 s, BJ 43-84 s. A machine's cadence is not
    # a property of the machine alone; it is the machine's MAKE crossed with what
    # the machine is building.
    #
    # BJ = TBMPCR1..5, CONTI = TBMPCR6..11. Three sources agree: the changeover
    # master splits the eleven machines 28/60 vs 22/42 at exactly that boundary,
    # PROJECT_STATE.md:176 and CMBC_BUILD_LOG.md:145-146 name the two makes, and
    # the 34NN -> TBMPCR<NN>Stage2 identity is measured at 98-100 % purity. TBR's
    # nine machines are one make (SAV + MESNAC, identical 10/24 changeover) and
    # its file supplies one CT per GT accordingly.
    #
    # Fallback order is always plant CT -> mined machine -> plant median, so a GT
    # the plant file does not name behaves exactly as it did before.
    PCT = plant_ct.get()

    def _cad(p: str, mach, gt=None) -> float:
        """Seconds per tyre. THE ONE cadence lookup in this module."""
        if CAD_BASIS == "plant":
            return plant_cad[p]
        if gt is not None:
            v = PCT.build_ct_s(p, gt, mach)
            if v:
                return v
        return cad_s.get(mach, plant_cad[p]) if mach else plant_cad[p]

    def _est_cad(p: str, gt: str) -> float:
        """Mean cadence over the GT's eligible machines -- the best estimate
        available before one is chosen. Falls back to the plant median only when
        the GT has no eligible machine with a mined cadence."""
        if CAD_BASIS == "plant":
            return plant_cad[p]
        ms = [m for m in _cand(p, gt) if m in cad_s]
        return (sum(_cad(p, m, gt) for m in ms) / len(ms)) if ms \
            else (PCT.build_ct_s(p, gt, None) or plant_cad[p])

    def _chg_cad(p: str, mach: str, gt=None) -> float:
        """Cadence a machine is CHARGED at when load is booked against it."""
        return _cad(p, mach, gt)

    need_h: dict[tuple, float] = {k: q * _est_cad(*k) / 3600.0
                                  for k, q in need_q.items()}

    # RIM LOCK -- the plant's own 8-month machine->rim assignment.
    # `machine_rim_lock.parquet` (scripts/build_machine_rim_lock.py) holds the
    # dominant rim per machine mined over Dec 2025 - Jul 2026, with purity and a
    # tier. It reproduces the load arithmetic exactly: PCR needs R13 3.0 · R15 1.4
    # · R18 1.2 · R12 1.1 · R17 1.0 · R14 0.9 · R16 0.8 = 9.4 machine-equivalents,
    # and the plant assigns 3+2+2+1+1+1+1 = 11 machines.
    #
    # Book in the plant's order: HARD (purity >= 99.5%) first, then PRIMARY
    # (85-99.5%), then FLEX (< 85%) which serves its own rim AND absorbs other
    # rims' tails. On PCR that flex machine is TBMPCR2 at 66.4% -- the data names
    # the overflow machine, we do not have to nominate one.
    #
    # SOFT, not a gate: R12 needs 1.1 machines and has 1 (46,656 tyres against a
    # 42,514 ceiling), so ~4,142 tyres MUST spill. Spill is allowed, priced by the
    # eligibility penalty, and counted in the "spilled past their pin" report.
    _tier_rank = {"hard": 0, "primary": 1, "flex": 2}
    lock_of: dict[tuple, list] = {}          # (plant, rim) -> machines, best first
    _hard_mc: set = set()
    _lockf = paths.INPUT_DERIVED / "machine_rim_lock.parquet"
    if _lockf.exists():
        for r in (pl.read_parquet(_lockf)
                  .sort(["plant", "locked_rim", "tier", "rank"])
                  .iter_rows(named=True)):
            lock_of.setdefault((r["plant"], r["locked_rim"]), []).append(
                (_tier_rank.get(r["tier"], 3), int(r["rank"]), r["machine"]))
        for k in lock_of:
            lock_of[k] = [m for _t, _r, m in sorted(lock_of[k])]
        if RESCUE_SKIP_TIERS:
            _hard_mc.update(
                (r["plant"], r["machine"])
                for r in pl.read_parquet(_lockf).iter_rows(named=True)
                if str(r["tier"]).lower() in RESCUE_SKIP_TIERS)
            print(f"  [rescue] {','.join(sorted(RESCUE_SKIP_TIERS))}-tier machines "
                  f"closed to off-lock rescue: {len(_hard_mc)}")

    # ---- TARGETED RIM SPILL (§12: a quantified exception, never a global relax)
    #
    # A rim whose OWN locked machines cannot hold its demand is given the plant's
    # DESIGNATED FLEX MACHINE as a last-resort candidate, budgeted to the measured
    # excess. Everything here is derived at run time; nothing is hardcoded, so it
    # transfers to another month without a rebuild.
    #
    # WHY NOT JUST RELAX THE LOCK. Measured on the v31/v32 PCR arm (identical):
    # HARD_LOCK off recovers 8,085 tyres (+1.64 pt) but takes same-size share
    # 96.5 % -> 69.3 % -- BELOW the plant's 91.5 % -- and weighted setup 392 h ->
    # 500 h against a plant 344 h. EXPERT_AUDIT §5 names 91.5 % as the reject
    # line, so a global relax is rejected on the audit's own criterion.
    #
    # AND WHY THE EXCESS IS SMALL. The "three rims over 100 %" reading (R12
    # 113.2 %, R13 100.7 %, R17 101.5 %) was produced with the PLANT-MEDIAN
    # cadence. Charged at each rim's own machines: R12 89.5 %, R13 80.6 %, R17
    # 103.1 % -- ONE rim over, by 22 h. Realised occupancy agrees (R17 96.4 %;
    # every other PCR rim 63-83 %). Sizing a spill off the flat table would have
    # moved ~150 h of work that has somewhere to go already.
    #
    # The flex machine is NOT chosen by us -- `machine_rim_lock.parquet` tags it
    # `tier == "flex"` from 8 months of MES (PCR: TBMPCR2, purity 66.4 %, 4 rims
    # seen, and it has historically run R17 as 13 % of its volume). TBR has no
    # flex machine, so this is inert there -- which is correct, TBR is already at
    # 100 % same-size.
    # WHICH RIMS GET THE FLEX MACHINE -- and the budget is NOT the lever.
    #
    # Sized on nominal excess alone (SPILL_MULT sweep, July 2026, all arms fresh)
    # the budget saturates immediately and buys nothing:
    #     mult  PCR ful   same-size    -- 1.0: 95.75 / 96.7 %
    #                                     3.0: 95.71 / 96.4 %
    #                                     6.0: 95.71 / 96.4 %   (saturated)
    #                                    12.0: 95.71 / 96.4 %
    # More hours on ONE rim does nothing, because no PCR rim is nominally full:
    # realised occupancy is 61-83 % on every machine, and total PCR idle is
    # 1,430 h in 508 gaps at p50 1.72 h against a p50 run of 5.27 h. The binding
    # constraint is TEMPORAL fragmentation, not capacity.
    #
    # What DOES pay is WHICH rims may reach the flex machine at all. An arm run
    # with the old flat cadence granted spill to R12, R13 and R17 (not just R17)
    # and reached PCR 96.51 % -- +0.85 pt -- at same-size 95.3 %. The gain was
    # not the extra hours; it was R12, which carries 4,680 of the 13,743 starved
    # tyres, getting a second machine with differently-shaped gaps.
    #
    # THE CRITERION IS THEREFORE ELIGIBILITY, NOT LOAD, and the plant's own
    # geometry names it: 4 of 7 PCR rims (R12, R14, R16, R17) have exactly ONE
    # locked machine, so for those a spill is the ONLY alternative to waiting --
    # any other machine is a size change (PARTITION §4c). A rim with 2-3 machines
    # already has same-size room and does not need the flex machine.
    #   -> eligible = (one locked machine) OR (measured over its own capacity)
    # Month-independent, derived from the lock master, no tuned constant.
    #
    # The budget then only stops one rim monopolising the flex machine, so it is
    # an equal share of that machine's month, not a capacity figure.
    SPILL_MULT = float(os.environ.get("PLANNER_SPILL_MULT", "1.0"))
    RIM_SPILL = os.environ.get("PLANNER_RIM_SPILL", "1") != "0"
    spill_to: dict[tuple, str] = {}          # (plant, rim) -> flex machine
    spill_budget_h: dict[tuple, float] = {}  # (plant, rim) -> hours allowed
    spill_used_h: dict[tuple, float] = {}
    if RIM_SPILL and _lockf.exists():
        _lkdf = pl.read_parquet(_lockf)
        _flex = {r["plant"]: r["machine"] for r in
                 _lkdf.filter(pl.col("tier") == "flex").iter_rows(named=True)}
        _rim_need: dict[tuple, float] = {}
        for (p, gt), q in need_q.items():
            r = rim_of.get(gt)
            if r:
                _rim_need[(p, r)] = _rim_need.get((p, r), 0.0) + q
        # ---- RIM_ADOPT: who receives the spill --------------------------
        # SHIPPED: the plant's own designated flex machine, tagged `tier=="flex"`
        # in `machine_rim_lock` from 8 months of MES (PCR: TBMPCR2). Worth
        # +1.47 pt (§4j.4).
        #
        # THE OBJECTION THIS FLAG TESTS. TBMPCR2 is also the DEAREST PCR machine
        # to change size on (60 min against 42 on machines 6-11), and it is NOT
        # the machine with the most room: on the July partition it holds 211 h
        # free while TBMPCR11 holds 317 h and TBMPCR5 310 h, both at 42 min. So
        # the shipped spill routes overflow to the expensive machine and leaves
        # the cheap idle one alone -- precisely the situation the partition's own
        # G4 repair pass exists to undo (PARTITION §2).
        #
        # RIM_ADOPT ranks receivers by (cheapest size change, most free hours)
        # from the month's own partition, so the choice is derived, not historic.
        # The flex machine stays in the list; it simply has to earn the work.
        _adopt_rank: dict[str, list] = {}
        if RIM_ADOPT:
            _cof2 = D / "cap_changeover.parquet"
            _dm2: dict[str, float] = {}
            if _cof2.exists():
                for _r2 in pl.read_parquet(_cof2).iter_rows(named=True):
                    _dm2[_r2["machine"]] = float(_r2["diff_min"])
            _free_h: dict[tuple, float] = {}
            # The partition is read again further down; this block runs BEFORE
            # that, so it opens the file itself rather than reaching forward.
            _partf2 = (paths.INPUT_DERIVED / "gt_machine_partition.parquet")
            if USE_PARTITION and _partf2.exists():
                _pf2 = allowable.restrict(pl.read_parquet(_partf2),
                                          label="partition/free_h", quiet=True)
                for _r2 in (_pf2.group_by(["plant", "machine"])
                            .agg(pl.col("hours").sum()).iter_rows(named=True)):
                    _free_h[(_r2["plant"], _r2["machine"])] = \
                        cap_h - float(_r2["hours"])
            for p_ in ("PCR", "TBR"):
                ms_ = sorted({m for (pp, m) in _free_h if pp == p_})
                _adopt_rank[p_] = sorted(
                    ms_, key=lambda m: (_dm2.get(m, 99.0),
                                        -_free_h.get((p_, m), 0.0), m))
        _elig_rims: list = []
        for (p, r), q in sorted(_rim_need.items()):
            ms = lock_of.get((p, r), [])
            fx = _flex.get(p)
            if RIM_ADOPT:
                # First ranked machine that is not already this rim's own.
                fx = next((m for m in _adopt_rank.get(p, []) if m not in ms), fx)
            if not ms or not fx or fx in ms:
                continue
            # charge the rim at ITS OWN machines' cadence -- see CAD_BASIS
            _avg = (sum(cad_s.get(m, plant_cad[p]) for m in ms) / len(ms)
                    if CAD_BASIS != "plant" else plant_cad[p])
            need_hh = q * _avg / 3600.0
            cap_hh = len(ms) * cap_h
            solo = len(ms) == 1          # no same-size alternative exists at all
            if solo or need_hh > cap_hh:
                _elig_rims.append((p, r, fx, max(need_hh - cap_hh, 0.0), solo))
        # Equal share of the flex machine's month per eligible rim -- the budget
        # exists to stop one rim monopolising it, not to model capacity.
        # ---- RIM_PRIORITY: cap the DISTINCT RIMS a receiving machine may host
        #
        # This is the clause the plant actually asked for -- "after completing 12
        # inch we switch to 13 inch and make only 13 like that" -- expressed
        # where it can bind. The candidate-machine tie-break CANNOT deliver it:
        # `HARD_PIN` breaks on the first feasible machine and most runs have a
        # single candidate, so ordering the candidate list is a no-op (measured:
        # RIM_PRIORITY as a pure tie-break was BYTE-IDENTICAL to baseline on
        # every KPI). The number of rims a machine ends up carrying is decided
        # HERE, when the spill is assigned, and nowhere else.
        #
        # A machine's own primary rim counts as one, so RIM_MAX_CONCURRENT=2
        # admits exactly one adopted rim. Rims compete for the slots by measured
        # excess, largest first; a rim that misses out simply gets no spill and
        # waits on its own machine.
        _per_plant = {}
        for p_, r_, fx_, exc_, solo_ in _elig_rims:
            _per_plant[p_] = _per_plant.get(p_, 0) + 1
        if RIM_PRIORITY:
            _slots = max(RIM_MAX_CONCURRENT - 1, 0)
            _taken: dict[str, int] = {}
            _kept = []
            for row in sorted(_elig_rims, key=lambda t: -t[3]):
                if _taken.get(row[2], 0) < _slots:
                    _taken[row[2]] = _taken.get(row[2], 0) + 1
                    _kept.append(row)
                else:
                    print(f"    {row[0]} {row[1]:<5} -> NO SPILL "
                          f"({row[2]} already hosts {_slots} adopted rim(s), "
                          f"RIM_MAX_CONCURRENT={RIM_MAX_CONCURRENT})")
            _elig_rims = _kept
            _per_plant = {}
            for p_, r_, fx_, exc_, solo_ in _elig_rims:
                _per_plant[p_] = _per_plant.get(p_, 0) + 1
        for p_, r_, fx_, exc_, solo_ in _elig_rims:
            share = cap_h / max(_per_plant[p_], 1)
            spill_to[(p_, r_)] = fx_
            spill_budget_h[(p_, r_)] = max(exc_, share) * SPILL_MULT
        if spill_to:
            print("  TARGETED RIM SPILL (derived; rim over its own locked capacity)")
            for (p, r), fx in sorted(spill_to.items()):
                _why = "1 locked machine" if len(lock_of.get((p, r), [])) == 1                     else "over own capacity"
                print(f"    {p} {r:<5} -> {fx:<16} budget "
                      f"{spill_budget_h[(p, r)]:6.1f} h   ({_why})")
        else:
            print("  TARGETED RIM SPILL: no rim exceeds its own locked capacity")

    # GT -> HOME MACHINE, mined over 8 months (scripts/build_gt_home_machine.py).
    # Measured: a PCR GT changes machine between consecutive runs 0 % of the time
    # in seven of eight months (3 % in Dec); TBR 0-14 %. Home-machine share is
    # p50 100 % on PCR (mean 90 %, 78 of 108 GTs >= 90 %) and p50 79 % on TBR.
    # A GT does not float across its rim's machines -- it belongs to one.
    #
    # FINER than the rim lock: R13 has three machines and several GTs; the rim
    # lock says which machines run R13, this says which of them THIS GT uses.
    # An earlier per-GT pin failed, but it pinned by capacity and penalty -- an
    # assignment we invented. This is the plant's own, feasible by construction.
    home_of: dict[tuple, list] = {}
    _homef = paths.INPUT_DERIVED / "gt_home_machine.parquet"
    if _homef.exists():
        for r in (pl.read_parquet(_homef).sort(["plant", "gt_code", "rank"])
                  .iter_rows(named=True)):
            home_of.setdefault((r["plant"], r["gt_code"]), []).append(r["machine"])

    # STATIC GT -> MACHINE PARTITION (scripts/build_gt_machine_partition.py).
    # Booked the plant's way: the PURE machines (one size at >=95 % of 8 months)
    # are filled FIRST with their own size, then the 2-3 size machines take their
    # historical size set, then the 5-size flex machine absorbs the tail, and any
    # remainder goes to the machine with the most free hours -- which is then
    # LOCKED TO THAT SIZE, so an overflow never costs a size change.
    #
    # This is the object the pin experiments were missing. Pinning HARDER onto
    # our load-balanced assignment made setup worse (242 -> 264/289 h) because a
    # rigid bad partition beats nothing; the partition itself seats 97.7 % of PCR
    # GTs (100 % TBR) on exactly ONE machine, against the plant's 66.7 %, with
    # every GT capacity-feasible and one machine carrying two sizes.
    part_of: dict[tuple, list] = {}
    part_book: dict[tuple, float] = {}   # (plant, gt, machine) -> booked hours
    part_done: dict[tuple, float] = {}   # ... -> hours actually placed so far
    _partf = paths.INPUT_DERIVED / "gt_machine_partition.parquet"
    if USE_PARTITION and _partf.exists():
        # The partition is mined from the 8-month machine x size matrix, not from
        # the allowable list, so it can pin a machine the plant forbids -- it
        # pinned TBMPCR7 tier-1 for GT 1503 NEO MSIL, which the matrix excludes.
        # Restrict it here too, or the pin re-introduces what `cm` just removed.
        _pf = allowable.restrict(pl.read_parquet(_partf), label="partition")
        # STALENESS GUARD. The partition is sized against ONE month's demand and
        # that month's calendar hours. Silently reusing July's partition for
        # August would pin every GT to a machine chosen for the wrong demand --
        # a wrong answer that looks like a right one. Refuse instead.
        _pm = (str(_pf["month"][0]) if "month" in _pf.columns and _pf.height
               else None)
        if _pm != a.month:
            # HARD FAILURE, not a warning. Measured 2026-08-09: every July arm in
            # the ledger ran on August's partition, printed this line, and fell
            # back silently. It cost July PCR 0.58 pt of fulfilment and 10.3 pt of
            # same-size (81.4 % vs 91.7 %) -- in the run the project was quoting as
            # its reference. A printed warning is not a gate; nobody read it.
            # PLANNER_ALLOW_STALE_PARTITION=1 restores the old fall-back for a
            # deliberate A/B, and says so loudly.
            _msg = (f"PARTITION IS FOR {_pm}, THIS RUN IS {a.month}. Rebuild it:\n"
                    f"    PYTHONPATH=. python scripts/build_gt_machine_partition.py {a.month}\n"
                    f"  (or set PLANNER_ALLOW_STALE_PARTITION=1 to fall back to the"
                    f" dynamic assignment -- measured WORSE, see PARTITION §4o)")
            if os.environ.get("PLANNER_ALLOW_STALE_PARTITION", "0") == "1":
                print(f"  !! {_msg}")
                print("  !! RUNNING ON THE DYNAMIC FALLBACK BY EXPLICIT OVERRIDE.")
                _pf = _pf.clear()
            else:
                raise SystemExit(f"L7 REFUSED TO PLAN: {_msg}")
        for r in (_pf.sort(["plant", "gt_code", "hours"],
                           descending=[False, False, True]).iter_rows(named=True)):
            if r["plant"] in PARTITION_PLANTS:
                part_of.setdefault((r["plant"], r["gt_code"]), []).append(r["machine"])
                # The partition also says HOW MUCH of a split GT belongs on each
                # machine, and that quantity was being thrown away: `part_of` is
                # consumed as a strict preference ORDER and `_place` takes the
                # first feasible machine, so a GT the partition split into three
                # equal thirds is re-split by first-fit. Measured July PCR,
                # GT 1513 XPC1 MSIL booked 260.8/250.6/250.6 h on
                # TBMPCR10/5/7 and delivered 251.0/342.4/120.2 -- the machine
                # that happens to sort LAST becomes a residual, not a co-equal.
                # `LOAD_TIEBREAK=share` reads this back.
                part_book[(r["plant"], r["gt_code"], r["machine"])] = \
                    part_book.get((r["plant"], r["gt_code"], r["machine"]), 0.0) \
                    + float(r["hours"])

    def _spill(p: str, gt: str) -> list:
        """The flex machine, LAST, and only while this rim's budget holds.
        Returning it from `_locked` is what keeps HARD_LOCK intact: the run is
        still inside its own lock set, the set has simply been widened by one
        machine for one over-subscribed rim, by exactly the measured excess."""
        r = rim_of.get(gt, "")
        fx = spill_to.get((p, r))
        if fx and spill_used_h.get((p, r), 0.0) < spill_budget_h.get((p, r), 0.0):
            return [fx]
        return []

    def _locked(p: str, gt: str) -> list:
        """Preferred machines: the GT's own home machines first (the plant's
        revealed order), then any machine locked to its rim, then -- only for a
        rim measured over its own capacity -- the designated flex machine."""
        if (p, gt) in part_of:
            # The partition IS the answer -- it is already rim-coherent and
            # capacity-feasible. Keep the rim's other machines behind it as
            # spill so a deadline miss can still be rescued.
            pm = part_of[(p, gt)]
            out = pm + [m for m in lock_of.get((p, rim_of.get(gt, "")), [])
                        if m not in pm]
        else:
            home = home_of.get((p, gt), [])
            rimlk = lock_of.get((p, rim_of.get(gt, "")), [])
            out = home + [m for m in rimlk if m not in home]
        return out + [m for m in _spill(p, gt) if m not in out]

    load_h: dict[str, float] = {}
    # ---- B: L4b FAMILY-MINIMISING ALLOCATION AS A PREFERENCE --------------
    # `l4b_alloc_<month>.parquet` assigns each GT's hours to machines with a
    # second objective the max-flow does not have: minimise the number of RIM
    # FAMILIES a machine must carry. Measured headroom, July PCR: 2.40 families
    # per machine as L7 currently produces, against 1.40 in the allocation.
    # Every family removed from a machine is a different-size changeover
    # eliminated, and the rim-fill result showed that returns CONTIGUOUS hours
    # (weighted CO 129.2 -> 112.8 and fulfilment UP on both plants).
    #
    # WIRED AS A PREFERENCE, NOT A PIN. It is the FIRST sort key in the pool
    # choice below, so an allocated machine wins ties against load -- but the
    # capacity test that follows is unchanged, so a GT whose allocated machine is
    # full still spills to its next allowable one. The allocation can therefore
    # reorder choices but can never make a GT unplaceable.
    #
    # It also cannot widen eligibility: `pool` comes from `_cand`, already
    # filtered by the plant's allowable matrix. PLANNER_L4B_ALLOC=0 disables.
    _arank: dict[tuple, int] = {}
    if os.environ.get("PLANNER_L4B_ALLOC", "1") != "0":
        _af = D / f"l4b_alloc_{a.month}.parquet"
        if _af.exists():
            _ad = pl.read_parquet(_af).sort("alloc_h", descending=True)
            for _i, _r in enumerate(_ad.iter_rows(named=True)):
                _k = (_r["plant"], _r["gt_code"], _r["machine"])
                if _k not in _arank:
                    _arank[_k] = 0 if _r["alloc_h"] > 0 else 9
            print(f"  [l4b-alloc] {len(_arank)} (GT, machine) preferences loaded")

    gt_machines: dict[tuple, list] = {}
    # Most-constrained first: a GT with 3 options must claim before one with 11,
    # or it is left with nothing and starves at 75% load (6,271 TBR tyres did).
    for (p, gt) in sorted(need_h, key=lambda k: (_n_elig(*k), -need_h[k], k[1])):
        cand = _cand(p, gt)
        if not cand:
            continue
        # The partition already answered this, whole-GT and capacity-checked.
        # Re-deriving it here by load is exactly the bouncing it exists to stop.
        if (p, gt) in part_of:
            gt_machines[(p, gt)] = list(part_of[(p, gt)])
            # Split the TYRES evenly, then charge each machine its OWN hours --
            # an even split of HOURS silently gives the fast machine more tyres.
            _shq = need_q[(p, gt)] / max(len(part_of[(p, gt)]), 1)
            for _m in part_of[(p, gt)]:
                load_h[_m] = load_h.get(_m, 0.0) + \
                    _shq * _chg_cad(p, _m, gt) / 3600.0
            continue
        # Locked machines first, in tier order; the rest stay available as spill.
        _lk = [m for m in _locked(p, gt) if m in cand]
        _rank = {m: i for i, m in enumerate(_lk)}
        chosen: list = []
        left_q = need_q[(p, gt)]              # TYRES, not hours -- see above
        pool = list(cand)
        while left_q > 1e-9 and pool:
            mm = min(pool, key=lambda x: (_arank.get((p, gt, x), 5),
                                          _rank.get(x, 99),
                                          pen.get((p, gt, x), 0.0),
                                          load_h.get(x, 0.0), x))
            pool.remove(mm)
            _c = _chg_cad(p, mm, gt)
            free_h = max(cap_h - load_h.get(mm, 0.0), 0.0)
            take_h = min(free_h, left_q * _c / 3600.0)
            if take_h <= 1e-9:
                continue                      # machine full, try the next one
            load_h[mm] = load_h.get(mm, 0.0) + take_h
            chosen.append(mm)
            left_q -= take_h * 3600.0 / _c
        # ---- PIN WIDTH (PLANNER_L7_PIN_MIN) -----------------------------
        # THE LOOP ABOVE SIZES THE PIN BY VOLUME AND NOTHING ELSE. A GT is given
        # machine 2 only when machine 1 has no HOURS left (cap_h = 775 h of an
        # 816 h span). Measured utilisation is 69-76 %, so that never happens and
        # most GTs end the assignment pinned to exactly ONE machine.
        #
        # But hours are not what refuses these groups -- CONTIGUITY is. Measured
        # on 2026-08 at the placement diagnostic:
        #
        #        n_cand at refusal   allowable matrix   free_h   best contiguous gap   needed
        #   TBR                  1               3.44    11.76                 0.97     4.72
        #   PCR                  2               3.20    54.78                 2.72     2.91
        #
        # PCR misses by TWELVE MINUTES on a machine with 54 free hours, while 1-2
        # other allowable machines are never even candidates. The gate asks "is
        # this machine full?" (no, 76 %) when the question is "can it host a
        # 4.7 h run?" (no).
        #
        # MACH_UTIL_CAP CANNOT BE LOWERED TO FIX THIS -- it is the same constant
        # L4b uses as machine CAPACITY in the feasibility max-flow, and lowering
        # it makes August read infeasible and exits the gate. Two jobs, opposite
        # requirements, one constant. So the pin gets its own rule and L4b's
        # capacity is left alone.
        #
        # PIN_MIN widens the pin to at least N eligible machines regardless of
        # load. It does NOT relax eligibility -- every machine added is already
        # in the allowable set for this GT -- and it does not force work onto
        # them; placement still picks. 1 = the shipped volume-only behaviour.
        #
        # SHIPS AT 1. MEASURED 2026-08-14 AND NEGATIVE, FOR THE REASON THE PIN
        # EXISTS:
        #     arm     PCR BUILT   dBUILT   unfed    TBR BUILT  dBUILT   L11
        #     base      407,307       +0   8,580       91,603      +0   26/42
        #     PIN_MIN=2 407,047     -260   8,840       91,600      -3   27/42
        #     PIN_MIN=3 407,028     -279   8,859       91,424    -179   27/42
        #
        # UNFED RISES. Widening the pin shortens each machine's run for that GT,
        # and B12 then refuses the shorter pieces -- trading a contiguity problem
        # for a fragmentation one, and losing on the trade. The diagnosis was
        # right (n_cand 1 vs 3.44 allowable, best gap 0.97 h vs 4.72 h needed);
        # the remedy is wrong. Contiguity cannot be bought by adding machines
        # while the lot floor is hard -- the two constraints pull opposite ways.
        #
        # KEPT because the CONSTANT SPLIT is a real fix regardless: MACH_UTIL_CAP
        # was serving as both L4b's feasibility capacity and L7's pin-release
        # threshold, and those want opposite values. Lowering it to widen the pin
        # made August read INFEASIBLE in L4b and exited the gate. Two jobs, one
        # constant, opposite requirements -- that conflation is now gone even
        # though widening the pin is not the answer.
        if PIN_MIN > 1 and len(chosen) < PIN_MIN:
            for _extra in sorted(cand, key=lambda x: (_arank.get((p, gt, x), 5),
                                                      _rank.get(x, 99),
                                                      pen.get((p, gt, x), 0.0), x)):
                if _extra not in chosen:
                    chosen.append(_extra)
                if len(chosen) >= PIN_MIN:
                    break
        # Over-subscribed even across every eligible machine: keep the whole
        # eligible set so placement can still try, and let L6/L11 report it.
        gt_machines[(p, gt)] = chosen or list(cand)

    if PIN_RUNS:
        n_multi = {p: sum(1 for (pp, _g), v in gt_machines.items()
                          if pp == p and len(v) > 1) for p in ["PCR", "TBR"]}
        print("  HORIZON MACHINE ASSIGNMENT (one machine per GT, capacity outranks the lock)")
        for p in ["PCR", "TBR"]:
            gts = [k for k in gt_machines if k[0] == p]
            if not gts:
                continue
            tot = sum(len(gt_machines[k]) for k in gts)
            print(f"    {p}: {len(gts)} GTs -> {tot} (GT, machine) pairs, "
                  f"p50 {tot/max(len(gts),1):.2f} machines/GT, "
                  f"{n_multi[p]} GTs needed a 2nd machine on capacity")
        print()

    # ---- PHASE 1: expand each cure campaign into dated cure demands ------
    # Slicing is unchanged -- the slice is the DELIVERY grain and it sets how
    # finely cure timing is tracked. What changes is that slices are no longer
    # PLACED here. A GT is cured on 6.4 presses at once on PCR, so its campaigns
    # are 6.4 concurrent draws of ~6.4 tyres/h each. Placing each campaign's
    # slices independently is what produced 0.84 h runs: no single campaign is
    # ever big enough to be worth a setup. Batched at the GT, the same demand is
    # ~41 tyres/h against a machine that builds at ~62 -- a real run.
    # machine -> [(start, end, gt, rim)]. The GT and rim are carried because the
    # SETUP a transition costs depends on BOTH neighbours, and placement is not
    # chronological -- a run inserted later can land between two already-placed
    # runs and change what each of them needs.
    busy: dict[str, list] = {}
    # ---- PLANT HOLIDAY (rule G3) -- see planner/cmbc/holiday.py -------------
    # BLOCKING, NOT PAUSING, AND THAT IS THE RIGHT MODEL HERE. On the press side
    # (L5) a campaign is 8-11 days, so a closure has to be absorbed by pausing
    # it. A build run is `build_band` hours -- 5-8 -- and the floor does not
    # leave half a run in a machine over a shutdown; it finishes the run before
    # the plant closes. So the closure is booked as an INTERVAL on every machine
    # and the existing setup-aware backward walk does the rest: a run that would
    # straddle it is pushed to end at 07:00 on the holiday, exactly as if
    # another run were already sitting there.
    #
    # BOOKING IT IS ALSO WHAT MAKES IT A CONSTRAINT RATHER THAN A PREFERENCE.
    # `_place` never consults a calendar; it consults `busy`. PARTITION §6 item
    # 27 -- "gate the resource where it is CREATED" -- is the reason this goes
    # into the occupancy list itself and not into a filter around it.
    #
    # The sentinel GT is `_HOL_GT`, and `_setup_s` returns 0 for it: a shutdown
    # is not a changeover and must not buy or charge one.
    _mach_plant: dict[str, str] = {}
    for (_p_, _g_), _ms in elig.items():
        for _m_ in _ms:
            _mach_plant[_m_] = _p_
    _hol_iv: dict[str, list] = {}
    if holiday.ACTIVE:
        for _m_, _p_ in _mach_plant.items():
            _hol_iv[_m_] = [(_s, _e, _HOL_GT, _HOL_GT)
                            for _s, _e in holiday.windows(_p_)]
        for _m_, _iv in _hol_iv.items():
            if _iv:
                busy[_m_] = list(_iv)
        print(f"  {holiday.summary()}  -- booked on "
              f"{sum(1 for v in _hol_iv.values() if v)} machines")
    last_gt: dict[str, str] = {}
    # RIM CAMPAIGN STATE -- see RIM_PRIORITY. `open_rim` is the rim a machine is
    # currently campaigning, `rim_hold_h` how long it has held it, `mach_rims`
    # every rim it has touched, `rim_opens` how many campaigns it has started.
    open_rim: dict[str, str] = {}
    rim_hold_h: dict[str, float] = {}
    mach_rims: dict[str, set] = {}
    rim_opens: dict[str, int] = {}

    # ---- CHANGEOVER MUST OCCUPY THE MACHINE, NOT MERELY BE COSTED ----------
    # DEFECT FIXED 2026-08-09. `_place` used to book exactly `dur = qty x cadence`
    # and test only interval OVERLAP, so two different GTs could sit back-to-back
    # with a ZERO gap. Setup was scored in the KPIs and in `weighted setup h` but
    # never happened on the timeline.
    #
    # Measured before the fix, July `runs/f_solo` / August `runs/aug_v1`:
    #     setup owed          PCR 381.9 h  TBR 169.0 h  (Aug 601.3 / 127.3)
    #     NOT reserved        PCR 173.9 h (45.5%)  TBR 71.0 h (42.0%)
    #     zero-gap transitions  350/856 PCR · 398/1,014 TBR
    #     machine-days over 24 h  PCR 55 (16.3%)  TBR 35 (12.5%), worst 28.83 h
    # Control: the plant's OWN July MES never exceeds 24 h on any of its 341 PCR
    # or 279 TBR machine-days (max 23.17 / 23.56 h). The cadence was right; the
    # plan was over-committed.
    #
    # THE FORMULATION IS A MINIMUM GAP, NOT AN ADDED BLOCK. Requiring
    # `gap >= setup` reserves only the SHORTFALL: ~55 % of owed setup already fit
    # in a gap the backward walk had left, and those transitions do not move at
    # all. Adding a fixed setup block to every run instead would double-charge
    # them and cost far more volume than the defect is worth.
    #
    # Both directions are checked because placement is out of order: a run may be
    # inserted BEFORE an existing one, in which case it is the existing run that
    # needs the gap after us.
    _cof = D / "cap_changeover.parquet"
    _same_min: dict[str, float] = {}
    _diff_min: dict[str, float] = {}
    if _cof.exists():
        for _r in pl.read_parquet(_cof).iter_rows(named=True):
            _same_min[_r["machine"]] = float(_r["same_min"])
            _diff_min[_r["machine"]] = float(_r["diff_min"])
    else:                       # never silently free -- fall back and say so
        print("  !! cap_changeover.parquet missing -- using PCR 22/42, TBR 10/24")
    _co_fb = {"PCR": (22.0, 42.0), "TBR": (10.0, 24.0)}

    def _setup_s(plant: str, mach: str, gt_a: str | None, gt_b: str) -> float:
        """Seconds the machine needs between GT a and GT b. 0 if it is the same GT."""
        if gt_a is None or gt_a == gt_b:
            return 0.0
        # A PLANT SHUTDOWN IS NOT A CHANGEOVER. The holiday block carries a
        # sentinel GT so it participates in the backward walk; charging a setup
        # across it would both invent machine time and let a run "buy" adjacency
        # to a closure. Neither neighbour of the closure pays for it -- the run
        # before it and the run after it are still a real transition to each
        # other only if they meet, and if the closure is between them they do
        # not meet at all.
        if gt_a == _HOL_GT or gt_b == _HOL_GT:
            return 0.0
        sm = _same_min.get(mach, _co_fb[plant][0])
        dm = _diff_min.get(mach, _co_fb[plant][1])
        same = rim_of.get(gt_a, "@") == rim_of.get(gt_b, "#")
        return (sm if same else dm) * 60.0
    slices, starved = [], []
    # Every booked run, per machine, as a movable object (see `_make_room`).
    placed: dict = {}
    _mr_used: dict = {}
    _mr_fail: dict = {}
    _mr_gap: list = []
    _b12_infeasible: dict = {}
    demands: dict[tuple, list] = {}     # (plant, gt) -> [{t_cure, qty, press}]

    # ---- HARD GT INVENTORY CAP (ledger envelope) ------------------------
    # A slice raises stock by its qty over [build_end, cure_ts]; the plant-level
    # profile is the running sum of those steps. A placement that would push the
    # profile above the cap at ANY hour is refused, and the run falls through to
    # the next machine / split / starve.
    #
    # WARNING FROM config.gt_wip_cap: this was measured INFEASIBLE at PCR 5,000 /
    # TBR 1,500 on the v2 engine. I = lambda x W, so refusing placements cuts
    # lambda (throughput), not W (lead time) -- there the cap cost 80 % of curing
    # while inventory still rose. That note says to arm it only once W is under
    # 9 h; ours is 13.11 h. Retested here on the CMBC path because the mechanism
    # differs, but expect throughput loss, not an inventory win.
    # Grid must extend PAST the horizon: carry-out lets a campaign finish after
    # hour 744, and clamping those cures into the last bucket drains them early,
    # so the grid under-reports stock. The reconciliation assertion caught this
    # on TBR at -7 % (39 PCR / 31 TBR slices, 2,733 tyres, cure past hour 744).
    # plan_h, not days*24: under `extend` cures live up to the planning tail and
    # a slice feeding one may be built a shelf life before it.
    _cap_h = int(plan_h) + int(GT_SHELF_LIFE_H) + 2
    inv_grid = {p: np.zeros(_cap_h + 2) for p in ("PCR", "TBR")}

    def _hr(ts) -> int:
        return max(0, min(_cap_h, int((ts - t0).total_seconds() // 3600)))

    def _cap_apply(p: str, adds: list, sign: float = 1.0) -> None:
        g = inv_grid[p]
        for e, c, q in adds:
            g[_hr(e)] += sign * q
            g[_hr(c)] -= sign * q

    def _daily_mean_max(p: str) -> float:
        """Highest CALENDAR-DAY MEAN of the stock profile.

        Mean, not peak. A peak test is maximally sensitive to the warm-up: all
        183 excursions above mean+3 sigma sat in hours 6.9-26.8, driven by the
        4,190 tyres of opening stock landing at t=0 before curing has drained
        anything. Steady state (h > 72) peaks at 5,084 against a mean of 4,218 --
        the profile is tight (CV 0.19), not peaky.
        """
        lvl = np.cumsum(inv_grid[p])
        n = (len(lvl) // 24) * 24
        if n == 0:
            return float(lvl.max())
        return float(lvl[:n].reshape(-1, 24).mean(axis=1).max())

    def _cap_ok(p: str, adds: list) -> bool:
        """PURE CHECK -- always rolls back. Commit happens at placement, so a
        candidate that is evaluated and then not chosen leaves no trace.

        Checked against rail x RAIL_MARGIN, not the rail itself. The check is a
        PRE-FLIGHT on the grid at hourly resolution; the shipped plan is then
        reconciled (FIFO reallocation moves cure times) and the grid buckets
        truncate to whole hours, so the realised profile drifts a little above
        what was approved. Measured drift on v24: PCR daily max 4,803 against a
        4,800 rail (+0.06 %), TBR 1,414 against 1,400 (+1.0 %). A 1 % margin
        absorbs it so the STATED cap is the one actually honoured.
        """
        rail = float(WIP_RAIL.get(p, 0) or 0) * RAIL_MARGIN
        if rail <= 0:
            return True
        _cap_apply(p, adds, +1.0)
        ok = _daily_mean_max(p) <= rail
        _cap_apply(p, adds, -1.0)
        return ok

    for r in camp.iter_rows(named=True):
        p, gt = r["plant"], r["gt_code"]
        qty, hrs = float(r["qty"]), max(float(r["hours"]), 1e-9)
        if not _cand(p, gt):
            starved.append({**{k: r[k] for k in ("plant", "gt_code", "press")},
                            "qty": qty, "reason": "no eligible machine",
                            "cause": "no_eligible_machine",
                            "t_cure": r.get("start_ts"),
                            "before_t0": 0, "r5": 0, "wip_rail": 0,
                            "early_cap": 0, "t0_short_h": 0.0,
                            "r5_worst_h": 0.0, "n_cand": 0})
            continue
        # SLICE COUNT COMES FROM BUILD HOURS, NOT CURE HOURS.
        # n = cure_hours / build_band divided a 159 h cure campaign into 29
        # slices regardless of how long each takes to BUILD -- giving TBR slices
        # of 10 tyres (34 min) against the plant's observed 86-tyre build runs,
        # a 9x over-fragmentation. What the band describes is the length of a
        # BUILD campaign, so the divisor must be build hours.
        # SLICE COUNT FROM CURE HOURS -- deliberately, despite the shape cost.
        #
        # Sizing slices from BUILD hours (n = build_h / build_band) gives slice
        # sizes that match the plant almost exactly: PCR 386 vs 363 observed,
        # TBR 88 vs 86. It is the correct SHAPE. But it costs the two things
        # that matter more:
        #     head p50   5.71 h -> 13.50 h   (worse than the old engine's 7.4)
        #     fulfilment  98.4% ->   94.2%
        # A larger slice occupies a machine longer, so contention rises, the
        # backward walk pushes releases further early, and every pushed tyre
        # waits longer. Head is what this architecture exists to fix, at 516
        # tyres per hour on PCR.
        #
        # The plant achieves BOTH -- 86-tyre runs AND a 4.8 h head -- because it
        # runs continuously with campaigns already in progress at the month
        # boundary. From a cold start the two cannot both be had, so this keeps
        # the coupling and accepts fragmented build slices as a stated limitation.
        # ---- SLICE COUNT FROM THE TWO CONSTRAINTS, NOT FROM A MULTIPLIER ----
        # A campaign of Q tyres drawn over H hours, delivered in n slices:
        #     slice qty  = Q/n
        #     slice wait = H/n + (Q/n)*cadence      (window + its own build time)
        # so the two rules that actually bound the slice are:
        #     R5  (HARD, shelf life)  wait <= SHELF - tau_min  =>  n >= (H+Q*cad)/71.7
        #     B12 (floor)             Q/n  >= min_lot          =>  n <= Q/min_lot
        # Take n = n_R5, the SMALLEST legal n, which is the LARGEST legal slice.
        # It satisfies B12 automatically whenever n_R5 <= n_B12.
        #
        # Measured on the July campaign set: n_R5 is 3 (PCR) / 4 (TBR) against the
        # 13 the multiplier produced, and BOTH rules are satisfiable on 100 % of
        # PCR and 99 % of TBR campaigns. The resulting slice is p50 361 tyres on
        # PCR -- the plant's own build run is 363. The plant's lot size is not a
        # policy we have to copy; it falls out of the shelf life and the floor.
        #
        # SLICE_MULT is kept only as an escape hatch: set it non-zero to restore
        # the old n = H/(band*MULT) behaviour for A/B. Default 0 = derived.
        _cad_h = _est_cad(p, gt) / 3600.0
        _n_r5 = int(np.ceil((hrs + qty * _cad_h)
                            / max(GT_SHELF_LIFE_H - tau_min[p] - R5_SAFETY_H, 1e-9)))
        _fl_p = float(CONFIG.thresholds.min_lot_units.get(p, 1) or 1)
        _n_b12 = int(float(qty) // max(_fl_p, 1.0))
        # PICK THE SMALLEST LEGAL SLICE, NOT THE BIGGEST.
        # n_R5 is the MINIMUM n (largest slices); n_B12 is the MAXIMUM n that
        # still keeps every slice at or above the floor. Since n_B12 >= n_R5 on
        # 100 % of PCR and 99 % of TBR campaigns, ANY n in [n_R5, n_B12] is legal
        # on both rules -- and the choice inside that window is a pure
        # placement trade:
        #     n = n_R5   slice p50 342, lot 368, setup 193 h ... but 78.0 % fed
        #     n = n_B12  slice just above the floor, easiest to place
        # A larger slice occupies its machine longer, so contention rises and
        # volume starves. Take the SMALLEST legal slice: it satisfies B12 by
        # construction and leaves placement the most freedom.
        # SLICE_AGGR interpolates: 0 = n_R5 (largest slice), 1 = n_B12 (smallest).
        _lo, _hi = max(1, _n_r5), max(1, _n_b12)
        n = max(_lo, int(round(_lo + SLICE_AGGR * (max(_hi, _lo) - _lo))))
        if SLICE_MULT[p] > 0:                       # legacy arm, A/B only
            n = max(1, int(round(hrs / max(build_band[p] * SLICE_MULT[p], 1e-9))))
        elif _n_b12 >= 1 and _n_r5 > _n_b12:
            # R5 and B12 cannot both hold: the campaign draws too slowly to be
            # fed in floor-sized batches. R5 is a physical limit and wins; the
            # sub-floor slices are counted so L11 can report them honestly.
            _b12_infeasible[p] = _b12_infeasible.get(p, 0.0) + float(qty)
        # A SLICE IS A WHOLE NUMBER OF TYRES. Dividing qty/n left 16,801 of
        # 18,092 slices fractional (48.3 tyres). Apportion by largest remainder
        # so the slices are integral and still sum to the campaign exactly.
        base, extra = divmod(int(round(qty)), n)
        slice_qty = [base + (1 if i < extra else 0) for i in range(n)]
        for i in range(n):
            # the moment this slice's tyres begin to be consumed
            per = float(slice_qty[i])
            if per <= 0:
                continue
            # ---- PLANT HOLIDAY (rule G3): DRAW ON WORKING TIME ------------
            # `t_cure` is the moment this slice's tyres begin to be CONSUMED by
            # the press, and it is the single most load-bearing timestamp in the
            # layer: `gt_events` uses it as the -qty instant, `_cap_ok` prices
            # the WIP rail from it, R5 is measured to it, and the exported
            # `7_daily_summary.cured` and `2_cure_schedule_shift` are bucketed
            # on it (NOT on `cure_by_shift`, which is L10's own spread).
            #
            # THE DEFECT THIS FIXES, caught by the exported pack on 2026-08-20
            # after L5/L10 already looked clean. A campaign that meets a closure
            # PAUSES, so `hours` is 24 h longer than the press-hours it draws.
            # Interpolating the draw linearly on that WALL-CLOCK span put slices
            # inside the shut window and diluted the rest: the export read PCR
            # 5,766 tyres cured on the closed day and only 10,094 / 13,091 on the
            # two days after, against ~14,600 either side -- i.e. exactly the
            # "the neighbours sag" symptom, arriving through the one path that
            # does not go through `cure_by_shift`. `cure_by_shift` was already
            # 0 / 14,623 / 14,631, so the two artefacts DISAGREED and only the
            # export was wrong. Two views of the same plan is how this stays
            # findable; one would have shipped.
            #
            # So interpolate on the campaign's PRODUCTIVE span and advance
            # through the calendar. The `else` branch is the shipped expression,
            # character for character, so no-calendar runs cannot move.
            if holiday.ACTIVE:
                _wh = holiday.work_seconds(p, r["start_ts"], r["end_ts"])
                t_cure = holiday.add_work(p, r["start_ts"], _wh * i / n)
            else:
                t_cure = r["start_ts"] + timedelta(hours=hrs * i / n)
            # draw on opening stock first -- it is already built and already aged
            have = opening.get((p, gt), 0.0)
            # usable only if the cure happens before this stock expires
            hold_h = (t_cure - t0).total_seconds() / 3600.0
            _ladder = opening_ladder.get((p, gt)) if FULL_AVAIL_LADDER else None
            if _ladder is not None:
                # PER-TYRE R5. Tyres are sorted ascending by remaining life, so
                # everything from the first index whose life covers `hold_h` is
                # usable. Consume from that index (FEFO) -- the shortest-lived
                # usable tyres go first, leaving the freshest for later cures.
                _k = bisect.bisect_left(_ladder, hold_h)
                have = float(len(_ladder) - _k)
            elif have > 0 and hold_h > opening_life.get((p, gt), 0.0):
                have = 0.0
            if have > 0:
                use = min(have, per)
                opening[(p, gt)] = opening.get((p, gt), 0.0) - use
                if _ladder is not None:
                    del _ladder[_k:_k + int(round(use))]
                slices.append({"plant": p, "gt_code": gt, "machine": "OPENING_STOCK",
                               "press": r["press"], "start_ts": t0, "end_ts": t0,
                               "qty": round(use, 1), "cure_ts": t_cure,
                               "wait_h": round((t_cure - t0).total_seconds() / 3600, 3)})
                # LEAK FIX: opening stock is real stock and must be visible to
                # the rail. It is contributed in PHASE 1 and never passes through
                # _place, so the grid used to miss 4,190 PCR tyres -- making the
                # effective ceiling 8,990 rather than the 4,800 it claimed, which
                # is exactly the 9,357 peak that was measured.
                _cap_apply(p, [(t0, t_cure, use)], +1.0)
                per_left = per - use
                if per_left <= 1e-6:
                    continue
            else:
                per_left = per
            demands.setdefault((p, gt), []).append(
                {"t_cure": t_cure, "qty": per_left, "press": r["press"]})

    # ---- PHASE 2: batch demands into RUNS and release each run whole -----
    # THE RUN IS THE UNIT OF RELEASE, and the unit B12's floor applies to.
    # A run is one visit to a machine: build_band hours of work, never less than
    # the B12 floor, and never spanning more shelf life than it has. Its release
    # is computed once, from the FIRST cure it feeds; the run's tyres are then
    # laid out inside its build window in cure order, so each slice keeps its own
    # honest wait and the run is contiguous on the machine.
    # ---- LOT SIZE FROM TIME AND DEMAND -----------------------------------
    # A flat quantity gives a 12,000-tyre/month GT and a 400-tyre/month GT the
    # same lot, which is wrong in both directions: the fast one is rebuilt far
    # too often and the slow one carries a month of cover. Size the lot as a
    # TIME SUPPLY instead --  Q_g = r_g x T  -- where r_g is the GT's own cure
    # draw rate over its active window.
    #
    # T IS THE INVENTORY DIAL, and that is the whole point. Each GT runs a Q/2
    # sawtooth between replenishments, so
    #       I  =  sum_g Q_g/2  =  (T/2) x sum_g r_g
    # Total GT inventory is therefore SET by the common interval and nothing
    # else -- not by a base-stock target, which is why cutting that target 4x
    # previously moved realised inventory by only 7%.
    #
    # The sawtooth is not the whole stock, though. Every tyre also carries the
    # coupling buffer tau* before its own cure, which is standing inventory the
    # lot size cannot touch:
    #       I  =  sum_g r_g x (T/2 + tau*)
    # Omitting the tau* term derived T = 15.06 h and delivered 10,303 tyres
    # against a 4,650 target -- the formula was solving for the wrong quantity.
    # Inverted correctly:
    #       T*  =  2 x (I_target / sum_g r_g  -  tau*)
    # tau* alone costs PCR 618 x 4.32 = 2,670 tyres, so more than half the band
    # is spent before a single lot is sized. If T* comes out negative the band
    # is unreachable at this tau* and the floor governs -- reported, not hidden.
    # The prediction is printed against the realised ledger below; two routes to
    # one quantity must reconcile before the number is trusted.
    floor_units = CONFIG.thresholds.min_lot_units
    rate: dict[tuple, float] = {}
    for k, ds_ in demands.items():
        ds_.sort(key=lambda d: d["t_cure"])
        span = (ds_[-1]["t_cure"] - ds_[0]["t_cure"]).total_seconds() / 3600.0
        qsum = sum(d["qty"] for d in ds_)
        rate[k] = qsum / span if span > 1e-6 else qsum

    interval: dict[str, float] = {}
    print("  LOT SIZING -- Q_g = r_g x T,  T = 2 x (I_target/sum(r_g) - tau*)")
    print(f"  {'plant':<6}{'GTs':>5}{'sum r_g':>10}{'I target':>10}"
          f"{'tau* cost':>11}{'T (h)':>8}{'basis':>12}")
    for p in ["PCR", "TBR"]:
        rsum = sum(v for k, v in rate.items() if k[0] == p)
        th = CONFIG.thresholds
        i_target = 0.5 * (th.gt_wip_min.get(p, 0) + th.gt_wip_max.get(p, 0))
        tau_cost = rsum * tau[p]
        # A plant-specific override permits TBR campaign consolidation without
        # perturbing PCR's independently calibrated timing.  The shared value
        # remains the fallback for backward compatibility.
        _plant_interval = float(os.environ.get(
            f"PLANNER_LOT_INTERVAL_{p}", str(LOT_INTERVAL_H)))
        if _plant_interval > 0:
            interval[p], basis = _plant_interval, "env"
        elif rsum > 1e-9 and i_target > 0:
            t_star = 2.0 * (i_target / rsum - tau[p])
            interval[p] = max(t_star, 0.0)
            basis = "derived" if t_star > 0 else "tau*-bound"
        else:
            interval[p], basis = 24.0, "fallback"
        print(f"  {p:<6}{sum(1 for k in rate if k[0] == p):>5}{rsum:>10,.0f}"
              f"{i_target:>10,.0f}{tau_cost:>11,.0f}{interval[p]:>8.2f}{basis:>12}")
    print()

    # ---- PRE-FLIGHT FEASIBILITY: is the inventory band reachable at all? ----
    # I = lambda x W is an identity, so the band and full demand JOINTLY specify
    # W: W_max = I_band_top / lambda_full. Asserting it here reports the ceiling
    # BEFORE the run instead of discovering it in an RCA afterwards.
    # At the plant's W of 8.84 h, PCR full demand needs I = 4,641 -- inside the
    # 4,500-4,800 band. The band and 100 % fulfilment are NOT in tension.
    print("  PRE-FLIGHT  I = lambda x W  (band and full demand jointly fix W)")
    print(f"  {'plant':<6}{'lambda_full':>12}{'tau* floor':>12}{'W_max @band':>13}"
          f"{'W modelled':>12}{'verdict':>10}")
    for p in ["PCR", "TBR"]:
        rsum = sum(v for k, v in rate.items() if k[0] == p)
        if rsum <= 0:
            continue
        lam = sum(sum(d["qty"] for d in v) for k, v in demands.items()
                  if k[0] == p) / (days * 24.0)
        th = CONFIG.thresholds
        top = float(th.gt_wip_max.get(p, 0) or 0)
        w_max = top / lam if lam > 0 else 0.0
        qbar = np.median([rate[k] * interval[p] for k in rate if k[0] == p]) \
            if any(k[0] == p for k in rate) else 0.0
        r_eff, b_eff = max(rsum / max(sum(1 for k in rate if k[0] == p), 1), 1e-9), \
            3600.0 / plant_cad[p]
        w_mod = tau[p] + (qbar / 2) * max(1 / r_eff - 1 / b_eff, 0.0)
        print(f"  {p:<6}{lam:>12,.0f}{lam*tau[p]:>12,.0f}{w_max:>13.2f}"
              f"{w_mod:>12.2f}{'OK' if w_mod <= w_max else 'BAND UNREACHABLE':>10}")
    print()

    # ---- PHASE 2a: SIZE every GT's runs. No placement happens here. ------
    jobs = []          # one entry per run: (deadline, scarcity, plant, gt, cand, grp)
    for (p, gt) in sorted(demands, key=lambda k: (k[0], _n_elig(*k),
                                                  -sum(d["qty"] for d in demands[k]),
                                                  k[1])):
        ds = sorted(demands[(p, gt)], key=lambda d: d["t_cure"])
        cand = _cand(p, gt)
        if PIN_RUNS and cand:
            # Pinned machines first, then the rest of the eligible set as a last
            # resort. A press-hour lost to starvation is unrecoverable, so the
            # lock yields rather than starve -- but every yield is counted and
            # printed below, never silent.
            pinned = gt_machines.get((p, gt), [])
            cand = pinned + [x for x in cand if x not in pinned]
        if not cand:
            continue
        cadence = _cad(p, cand[0], gt)
        # SPAN CAP = THE RUN'S OWN TIME SUPPLY, NOT THE SHELF LIFE.
        # R5 bounds the span at GT_SHELF_LIFE_H - tau* - 2 = 65.7 h, and that was
        # used as the grouping cap. But a run of Q = r_g x T tyres is only T hours
        # of supply; letting it collect 65.7 h of cure demand means its first
        # tyres wait the whole span. Measured on v11: run cure span p50 15.2 h but
        # p90 25.2 and max 270, with 11% of runs over 24 h -- and that tail is
        # 2.1 h of the 6.79 h drain term, ~1,100 tyres of standing GT.
        # n_g is NOT the cause: ours is 3.04 against the plant's 3.25, and runs
        # already draw at 22.1/h against a 20.8/h nominal. The width of the cure
        # window a single run absorbs is.
        # R5 still applies as the hard outer bound, checked per slice in _place.
        span_cap = min(GT_SHELF_LIFE_H - tau[p] - 2.0,
                       max(interval[p] * SPAN_MULT, 4.0))
        # Q_g = r_g x T, floored by B12 and capped by the shelf life.
        # The two bounds can conflict on a very slow GT: a 150-tyre floor at
        # 1 tyre/h spans 150 h, well past R5. R5 is HARD and B12 is a policy
        # floor, so the shelf life wins and the shortfall is reported by L11's
        # `build runs below min_lot` rather than silently breaching R5.
        r_g = rate.get((p, gt), 0.0)
        if PIN_RUNS:
            _floor = float(floor_units.get(p, 0)) * RUN_MULT
            target = max(_floor, r_g * interval[p])
            # ---- `target` IS A CEILING, AND min_lot MUST NOT BE ONE ----------
            # The cut below is `acc >= target`, so `target` bounds the run from
            # ABOVE. When a GT draws slowly, `r_g x T` falls under the B12 floor
            # and the max() pins `target` at the floor -- so min_lot becomes the
            # run's floor AND its ceiling. Measured fingerprint on TBR: 119 runs
            # of exactly 87 tyres (= 70 rounded up to the next 29-tyre slice) and
            # max-run-per-GT p10 = p25 = 87.0. A flat quantile band is a wall,
            # not a distribution (PARTITION_AND_CHANGEOVER §1).
            # It binds on 31.4 % of TBR volume and 11.0 % of PCR volume, and R5
            # is NOT what stops a bigger run: 0 of 92 GTs are bound by span_cap,
            # which would allow runs 3.0x (PCR) / 3.1x (TBR) larger.
            #
            # TARGET_CEIL_MULT lifts the ceiling ONLY where the floor is what set
            # it. 1.0 reproduces the old behaviour exactly. R5's span_cap is
            # still the hard outer bound, so this can never breach the shelf life.
            if r_g * interval[p] < _floor:
                target = min(_floor * TARGET_CEIL_MULT[p],
                             r_g * span_cap if r_g > 1e-9 else _floor)
                target = max(target, _floor)
            if r_g > 1e-9:
                target = min(target, r_g * span_cap)
        else:
            target = 0.0
        groups, group, acc = [], [], 0.0
        for d in ds:
            span_h = ((d["t_cure"] - group[0]["t_cure"]).total_seconds() / 3600.0
                      if group else 0.0)
            if group and (acc >= target or span_h > span_cap):
                groups.append(group)
                group, acc = [], 0.0
            group.append(d)
            acc += d["qty"]
        if group:
            groups.append(group)
        # ---- STRICT B12: NO GROUP MAY BE BORN BELOW THE FLOOR --------------
        # THIS is where the sub-floor runs actually came from, and it is neither
        # L4.5's lot sizes nor L7's splits. Measured on `runs/s4_*`: of 80/334
        # (Jul) and 111/138 (Aug) sub-floor runs, **99 % had the SAME GT already
        # above the floor on the SAME machine** -- the volume was there, the
        # grouping cut it into pieces. Two cuts do it:
        #   * the TRAILING REMAINDER -- `ds` does not divide evenly by `target`,
        #     so whatever is left after the last `acc >= target` cut becomes a
        #     group of any size at all. Dominant source.
        #   * a `span_cap` cut -- R5 forces the group closed before `target`.
        # `_place` never checked `gq` against the floor, so a small group was
        # placed with no gate whatsoever; HARD_FLOOR only ever guarded the SPLIT
        # path, which is why it plateaued at 3.6 %/4.5 % instead of reaching 0.
        #
        # Repair by MERGING, right to left: fold a sub-floor group into its
        # predecessor whenever the merged span still respects `span_cap`. R5 is
        # hard and B12 is policy, so a merge is refused when it would breach the
        # span -- and under STRICT that residue is reported as shortfall with its
        # own reason rather than placed as a sub-floor run. That is the trade the
        # plant asked for: no sub-floor run, ever, even at the cost of volume.
        if STRICT_FLOOR and groups:
            _flr = float(floor_units.get(p, 0))
            _rep: list = []
            for _g in groups:
                if (_rep and sum(d["qty"] for d in _rep[-1]) + 0.0 < _flr):
                    # previous is still short -- keep feeding it
                    _cand_g = _rep[-1] + _g
                elif sum(d["qty"] for d in _g) >= _flr:
                    _rep.append(_g)
                    continue
                elif _rep:
                    _cand_g = _rep[-1] + _g
                else:
                    _rep.append(_g)
                    continue
                _sp = ((_cand_g[-1]["t_cure"] - _cand_g[0]["t_cure"])
                       .total_seconds() / 3600.0)
                if _sp <= span_cap:
                    _rep[-1] = _cand_g
                else:
                    _rep.append(_g)          # R5 wins; strict gate handles it
            groups = _rep
        for grp in groups:
            jobs.append((grp[0]["t_cure"], _n_elig(p, gt), p, gt, cand, grp))

    def _place(p: str, gt: str, cand: list, grp: list, cap_h: float) -> bool:
            """Place one run whole. False if no machine can take it."""
            gq = sum(d["qty"] for d in grp)
            # STRICT B12: a run below the floor is never placed, on any machine.
            # The last line of defence -- the grouping repair above should have
            # merged it, and HARD_FLOOR should have refused to create it.
            # THE RESET IS UNCONDITIONAL -- it used to be inside `if DIAG`, which
            # made the counters cumulative over the whole run whenever DIAG was
            # off, so `_diag_last` described every refusal that had ever happened
            # rather than this one. Attribution needs them per-call, and the
            # refusal counters themselves (before_t0/early_cap/r5/wip_rail) are
            # already written unconditionally further down -- only the gap
            # geometry is DIAG-gated. Clearing always costs nothing and is what
            # lets build_starved.parquet name a cause without PLANNER_L7_DIAG=1.
            if STRICT_FLOOR and gq < float(floor_units.get(p, 0)):
                _diag_last.clear()
                _diag_last["floor"] = _diag_last.get("floor", 0) + 1
                return False
            _diag_last.clear()
            t_first = grp[0]["t_cure"]
            # Cumulative tyres AFTER each slice: slice j is off the machine once
            # cums[j] tyres have been built.
            cums, _acc = [], 0.0
            for d in grp:
                _acc += d["qty"]
                cums.append(_acc)
            best = None
            # SIZE-AWARE MACHINE CHOICE -- the free changeover lever.
            # The plant's changeover master is binary: SAME size 22-28 min,
            # DIFFERENT size 42-60. Measured July: 91.8% of the plant's PCR build
            # changeovers are same-size; ours were 32.3%, because L7 had no size
            # term at all (L5 has a same_rim tiebreak for presses; the build side
            # never got one). That cost ~971 machine-hours of PCR setup against
            # the plant's ~334 -- 2.9x the TIME on 1.9x the COUNT.
            # Preferring a machine whose current GT shares this rim changes no run
            # size, no run count, no inventory and no fulfilment -- only which run
            # follows which. Unlike the same-GT preference (reverted above) this
            # is a broad condition, so it actually fires.
            # Rank the SPILL by the LOCK, not by the machine's last GT.
            # Ordering on `last_gt`'s rim only helps if that machine happens to be
            # holding the right rim at this instant; ordering on the machine's
            # HORIZON LOCK keeps the spill inside the rim's own machine group,
            # which is what the plant does. Measured: last_gt ordering left 24.4%
            # of PCR volume off-lock.
            # LOAD-AWARE THIRD KEY -- committed hours, AFTER lock and rim.
            #
            # DEFAULT OFF -- MIXED SIGN ACROSS MONTHS, which is this repo's
            # signature failure mode (DO-NOT 14). Measured in the cumulative
            # stack, fulfilment points:
            #        PCR      TBR
            #   Jul  -0.41   -0.24
            #   Aug  +0.74   +0.73
            # It was originally measured on August alone (+0.19 / +0.75) and
            # reproduces there; July inverts it. The average is positive and the
            # idea is sound, so the flag stays -- but it is not shipped on two
            # months of evidence that disagree.
            #
            # ONE REASON TO REVISIT: it buys R5 margin exactly where the takt cap
            # is tightest. July TBR R5 max 71.5 h -> 66.3 h against a hard 72,
            # and TBR inventory daily max 1,314 -> 1,220 against a 1,400 rail.
            # If the R5 margin ever becomes binding, this is the lever.
            # `PIN_RUNS` defaults to 1 and `break`s on the first feasible
            # machine, so the "prefer the latest feasible release" preference
            # below it is dead code and the candidate ORDER is the only lever
            # left. Ordering the remaining ties by how much work a machine has
            # already been given spreads runs onto the idle end of each rim
            # group instead of re-filling the machine that happens to sit first
            # in the list. Strictly a tie-break: it never overrides the lock or
            # the same-rim preference, so it cannot cost same-size %.
            _r = rim_of.get(gt)
            _lkset = set(_locked(p, gt))
            _idx = {m: i for i, m in enumerate(cand)}
            # `hours` ranks by committed TIME, so among machines of different
            # cadence it hands the next run to whichever has idled longest --
            # which on PCR is systematically the SLOW machine, because the same
            # tyres cost it more hours. `cap` ranks by REMAINING TYRE CAPACITY,
            # (H - committed)/cadence, so a 49 s machine outranks a 78 s machine
            # at equal idle time. Identical to `hours` inside a rim group of one
            # cadence (R13 is 49/49/51); the two differ on R15 (57/66) and
            # R18 (66/78). Both are still only the THIRD key, after the lock and
            # the same-rim preference, so neither can cost same-size %.
            _ltb = LOAD_TIEBREAK if p in LTB_PLANTS else ""
            if _ltb == "share":
                # Rank the GT's OWN pinned machines by how much of THIS GT's
                # partition book each still owes. The partition already solved
                # the split, capacity-feasibly and rim-coherently; this simply
                # stops `_place` from silently re-solving it by first-fit.
                # Machines with no book for this GT (rim spill, flex) keep 0 and
                # therefore stay behind every machine that still owes work.
                _load = {m: -max(part_book.get((p, gt, m), 0.0)
                                 - part_done.get((p, gt, m), 0.0), 0.0)
                         for m in cand}
            elif _ltb == "cap":
                _load = {m: -(plan_h * 3600.0 - sum(
                    (e - s).total_seconds() for (s, e, _g, _rr) in busy.get(m, []))
                ) / _cad(p, m, gt) for m in cand}
            elif _ltb:
                _load = {m: sum((e - s).total_seconds()
                                for (s, e, _g, _rr) in busy.get(m, []))
                         for m in cand}
            else:
                _load = {}
            # ---- KEY 2: RIM CONTINUITY -------------------------------------
            # SHIPPED: "does this machine's LAST block share my rim?" -- a
            # one-block memory. It took PCR same-size 32.3 % -> 91.7 % and is
            # live and dominant, so RIM_PRIORITY extends it rather than
            # replacing it.
            #
            # RIM_PRIORITY: "is this machine currently CAMPAIGNING my rim, and
            # has the rim it is on earned the right to be interrupted?" The
            # machine holds an OPEN rim; another rim may prefer that machine
            # only once the open rim has run RIM_MIN_CAMPAIGN_H. Ranks, best
            # first:
            #   0  machine is campaigning MY rim            -- extend it
            #   1  machine has no open rim yet              -- free to adopt
            #   2  open rim has served its minimum          -- may switch
            #   3  machine is mid-campaign on another rim   -- do not interrupt
            # Rank 3 is a PREFERENCE, not a refusal: if no better machine can
            # take the run, the sort still returns it and the cure deadline is
            # met. That is the whole trade -- purity yields to the deadline.
            def _rimkey(x: str) -> int:
                if _r is None:
                    return 0
                if not RIM_PRIORITY:
                    return int(rim_of.get(last_gt.get(x, ""), None) != _r)
                orim = open_rim.get(x)
                if orim == _r:
                    return 0
                if orim is None:
                    return 1
                # A machine already hosting RIM_MAX_CONCURRENT distinct rims may
                # not become the preferred home for yet another one. This is the
                # "make only 13 like that" clause -- one adopted rim at a time.
                if (len(mach_rims.get(x, set()) | {_r}) > RIM_MAX_CONCURRENT
                        and _r not in mach_rims.get(x, set())):
                    return 4
                return 2 if rim_hold_h.get(x, 0.0) >= RIM_MIN_CAMPAIGN_H else 3

            # ---- KEY 3: SISTER CONTINUITY (PLANNER_SISTER_GROUP) -----------
            # Prefer a machine whose last block was a SISTER of this GT -- same
            # construction family, differing in at most one component slot.
            #
            # WHY A TIE-BREAK AND NOT A QUEUE RE-SORT. Grouping by rim in QUEUE
            # ORDER was measured and cost 25,549 tyres at L5: reordering the
            # queue moves work away from its cure deadline, and the deadline is
            # the thing that cannot move. Resource SELECTION is free -- it
            # chooses between machines that are all equally legal at the same
            # instant. So sisterhood is expressed here, strictly below the lock
            # (key 1) and below rim coherence (key 2), where it can never cost a
            # placement or a size change.
            #
            # INERT ON PCR BY CONSTRUCTION -- see `sister_of` above: PCR has no
            # distance-1 pairs at all, so every PCR GT is its own group and this
            # key is constant. It is a TBR lever only, and that is a property of
            # the plant's product range, not a limitation of the flag.
            def _sistkey(x: str) -> int:
                if not SISTER_GROUP:
                    return 0
                s = sister_of.get(gt)
                if s is None:
                    return 1
                return 0 if sister_of.get(last_gt.get(x, "")) == s else 1

            cand = sorted(cand, key=lambda x: (
                x not in _lkset,
                _rimkey(x),
                _sistkey(x),
                _load.get(x, 0.0),
                _idx[x]))
            # TRIED AND REVERTED: preferring a machine whose `last_gt` is this GT,
            # to recover the continuity that GT-ordered placement gave for free.
            # It does the opposite -- machines/GT 4 -> 5 and changeovers
            # 4.10 -> 4.31 per machine-day. Under deadline ordering the GTs
            # interleave, so by the time a GT's next run comes up the machine has
            # moved on; the preference almost never fires, and when it does it
            # takes a worse-fitting machine. Continuity has to come from the
            # ORDER, not from the machine choice.
            for mach in cand:
                c = _cad(p, mach, gt)
                dur = timedelta(seconds=gq * c)
                # RELEASE AS LATE AS EVERY SLICE ALLOWS, NOT A WHOLE RUN EARLY.
                # Starting a run `dur` before its FIRST cure finishes the entire
                # run before the campaign begins, so the run's last tyres wait
                # the whole draw span: head p50 16.8 h and inventory 10,303
                # against a 4,500-4,800 band. Building runs faster than a press
                # draws, so a run may overlap the campaign it feeds -- exactly
                # what the plant does. The latest admissible start is the
                # tightest per-slice deadline.
                # RELEASE FLOOR IS tau_min, NOT tau*.
                # tau* is the plant's MEDIAN coupling buffer, not its minimum:
                # 47 % of PCR and 50 % of TBR tyres are cured in LESS than tau*,
                # p01 is 0.50 h and p25 is 2.45 h. We were using tau* as a hard
                # wall on every slice -- our p01/p05/p10 were all exactly 4.32 --
                # which inflates W for every tyre, and W is the only term the
                # inventory cap actually constrains (I = lambda x W).
                # R17's floor, and the one L11 checks, is tau_min = 0.27 h.
                _floor_h = tau_rel[p]
                ideal = min(d["t_cure"] - timedelta(hours=_floor_h)
                            - timedelta(seconds=cu * c)
                            for d, cu in zip(grp, cums))
                # ---- TAIL-BUILD PULL (PLANNER_L7_TAIL_BUILD_PULL) -----------
                # A run whose JUST-IN-TIME release finishes AFTER the month end
                # is built outside the month even though R5 would allow it
                # inside. Measured 2026-08 PCR: 80 slices / 14,291 tyres finish
                # after 1 Sep 07:00, every one of them with an R5-earliest build
                # date inside August, against 51-78 idle machine-hours per day
                # on days 21-30.
                #
                # Pull ONLY those runs, and only back to the R5 bound. Slice j
                # finishes at st + cums[j]*c and must be no older than
                # SHELF - safety at its cure, so the earliest legal start is
                #     max_j( t_cure_j - (SHELF - safety) - cums[j]*c )
                # -- the MAX, not the min: the tightest slice governs the run.
                #
                # THIS CREATES NO TYRES. It moves the build of tyres that are
                # already planned from just-outside to just-inside the boundary.
                # in-month BUILD rises; in-month CURE does not move at all, and
                # the GT sits longer against the R5 limit. Judge it on the build
                # line only, and never quote it as fulfilment.
                if (EARLY_RELEASE or (TAIL_PULL and (ideal + dur) > _mend)):
                    _r5e = max(d["t_cure"]
                               - timedelta(hours=GT_SHELF_LIFE_H - R5_SAFETY_H)
                               - timedelta(seconds=cu * c)
                               for d, cu in zip(grp, cums))
                    _cand = max(t0, _r5e)
                    if _cand < ideal:
                        ideal = _cand
                st = ideal
                if DIAG:
                    # (ideal - t0) is the run's UNCONTESTED window: how much room
                    # it would have on a completely empty machine. Negative means
                    # the deadline itself precedes the horizon -- a cold start, not
                    # contention. Positive-but-refused means the machine calendar
                    # is what blocks it, and that has a different fix entirely.
                    _sl = (ideal - t0).total_seconds() / 3600.0
                    _diag_last["ideal_slack_h"] = max(
                        _diag_last.get("ideal_slack_h", -1e9), _sl)
                    _diag_last["dur_h"] = dur.total_seconds() / 3600.0
                # walk BACKWARDS past conflicts: earlier ages the tyre (72 h to
                # spend), later starves the press (unrecoverable)
                # SETUP-AWARE BACKWARD WALK. `st` must clear the previous run's
                # end by the setup THAT transition costs, and must end early
                # enough for the next run to get ITS setup. Same shape as the old
                # overlap walk, padded on both sides -- see the block at `busy`.
                for (ivs, ive, ivgt, _ivr) in reversed(sorted(
                        busy.get(mach, []), key=lambda x: (x[0], x[1]))):
                    need_after = _setup_s(p, mach, ivgt, gt)    # iv -> us
                    need_before = _setup_s(p, mach, gt, ivgt)   # us -> iv
                    if st + dur + timedelta(seconds=need_before) > ivs \
                            and st < ive + timedelta(seconds=need_after):
                        st = ivs - timedelta(seconds=need_before) - dur
                # ---- ANTI-SLIVER PACKING (B12's real price, and its fix) ----
                # Every run is released as late as its slices allow, so each one
                # leaves a hole behind it between the previous run's end and its
                # own start. Those holes are the machine's idle time, and they
                # are sized by DEADLINE SPACING -- nothing makes them as long as
                # a run. Measured July, strict floor: TBR idle 1,294 h in 711
                # holes, p50 1.30 h, against a floor-minimal TBR run of 5.05 h;
                # PCR holes p50 1.00 h against 2.84 h. A run that may not be cut
                # below the floor needs ONE contiguous hole, so it cascades back
                # through every sliver to t0 and is refused -- while 1,294 idle
                # hours sit on the same machines. That is the whole mechanism by
                # which the floor costs volume: the permissive arm split the run
                # into halves that fit the slivers, which is precisely what makes
                # its runs sub-floor.
                # It is NOT R5 and NOT the WIP rail: relaxing the shelf life to
                # 144 h is worth +0.47 pt TBR / -0.14 pt PCR, and lifting both
                # rails to 99,999 is worth +0.15 pt PCR / 0.00 pt TBR.
                # So do not create the sliver. If releasing at `st` would leave a
                # hole too small for any legal run, abut the previous run
                # instead: the same idle time then accumulates AFTER us, where it
                # is contiguous and a later run can use it. The run moves earlier
                # by less than one floor-minimal run, and R5/rail still gate it.
                # Try the ABUTTED start first and the just-in-time one second, so
                # closing a hole can never cost a placement: if abutting breaks
                # R5 or the rail, the original start is still evaluated.
                _tries = [st]
                if SLIVER[p] > 0.0:
                    _pe = _t0b
                    for (ivs, ive, ivgt, _ivr) in busy.get(mach, []):
                        if ive <= st:
                            _pe = max(_pe, ive + timedelta(
                                seconds=_setup_s(p, mach, ivgt, gt)))
                    _hole = (st - _pe).total_seconds() / 3600.0
                    # The hole is useless if no LEGAL run could ever occupy it --
                    # i.e. shorter than a floor-sized run on this machine. Scaled
                    # by SLIVER[p] so the aggressiveness is a measured knob.
                    if 0.0 < _hole < SLIVER[p] * float(
                            floor_units.get(p, 0)) * c / 3600.0:
                        _tries = [_pe, st]
                _hit = None
                for _st in _tries:
                    _en = _st + dur
                    # COUNTERS ARE UNCONDITIONAL. They used to sit behind
                    # `if DIAG:`, so build_starved.parquet's attribution read
                    # 100 % machine_busy on every run without PLANNER_L7_DIAG=1
                    # -- not because the machines were busy, but because the
                    # other four counters could not be written. Four dict
                    # increments on a rejected candidate cost nothing next to the
                    # datetime arithmetic already in this loop.
                    if _st < _t0b:
                        _diag_last["before_t0"] = _diag_last.get("before_t0", 0) + 1
                        _diag_last["t0_short_h"] = max(
                            _diag_last.get("t0_short_h", 0.0),
                            (t0 - _st).total_seconds() / 3600.0)
                        continue
                    # EARLINESS CAP -- building early is not free.
                    # The backward walk was the ONLY response to a busy machine,
                    # and it is pure inventory: 36% of PCR runs were pushed early,
                    # mean 2.86 h, max 60 h, worth 1,495 tyres of standing GT --
                    # while 24% of machine capacity sat idle elsewhere in the
                    # month. Reject a machine that would push this run more than
                    # `cap_h` early so the next candidate is tried instead. The
                    # caller retries uncapped if no machine passes, so this can
                    # never cost a placement.
                    if (ideal - _st).total_seconds() / 3600.0 > cap_h:
                        _diag_last["early_cap"] = _diag_last.get("early_cap", 0) + 1
                        continue
                    # R5 ON EVERY SLICE, NOT ON THE RUN.
                    # Checking `t_last - run_end` looked at the wrong endpoint:
                    # when slices share a cure time the FIRST-BUILT slice waits
                    # longest, so 10 breaches passed a guard that only ever saw
                    # the last one.
                    # AND THE SAME ERROR ONE LEVEL DOWN (R5_FIRST_TYRE, off by
                    # default). `cu` is the cumulative AFTER slice j, so
                    # `_st + cu*c` is that slice's END -- the last tyre in it.
                    # The FIRST tyre of slice j comes off at
                    # `_st + (cu - qty)*c` and waits its whole span longer, so
                    # it is the one that expires. Measured on MD_base (August):
                    # the plan's true max is 65.58 h PCR / 73.45 h TBR against
                    # the 63.27 / 71.71 this test sees, and 26 TBR tyres are
                    # past 72 h. Enforcing it REMOVES legal placements, so it is
                    # a flag and it is measured, not assumed -- see 4bf.
                    worst = max((d["t_cure"]
                                 - (_st + timedelta(
                                     seconds=(cu - (d["qty"] if R5_FIRST_TYRE
                                                    else 0.0)) * c)))
                                .total_seconds() / 3600.0
                                for d, cu in zip(grp, cums))
                    if worst > GT_SHELF_LIFE_H:
                        _diag_last["r5"] = _diag_last.get("r5", 0) + 1
                        _diag_last["r5_worst_h"] = max(
                            _diag_last.get("r5_worst_h", 0.0), worst)
                        continue
                    # HARD CAP: would this placement breach the stock envelope?
                    _adds = [(_st + timedelta(seconds=cu * c), d["t_cure"],
                              d["qty"]) for d, cu in zip(grp, cums)]
                    if not _cap_ok(p, _adds):
                        _diag_last["wip_rail"] = _diag_last.get("wip_rail", 0) + 1
                        continue
                    _hit = (_st, _en)
                    break
                if _hit is None:
                    continue
                st, en = _hit
                if PIN_RUNS:
                    best = (mach, c, st, en)   # first feasible keeps the run whole
                    break
                wait = (t_first - en).total_seconds() / 3600.0
                key = (-wait, mach)            # prefer the LATEST feasible release
                if best is None or key < (-(t_first - best[3]).total_seconds()
                                          / 3600.0, best[0]):
                    best = (mach, c, st, en)
            if best is None:
                return False
            mach, c, st, en = best
            busy.setdefault(mach, []).append((st, en, gt, rim_of.get(gt, "")))
            # Charge the targeted rim spill so its budget is real, not advisory.
            _rr = rim_of.get(gt, "")
            if spill_to.get((p, _rr)) == mach:
                spill_used_h[(p, _rr)] = spill_used_h.get((p, _rr), 0.0) \
                    + (en - st).total_seconds() / 3600.0
            last_gt[mach] = gt
            # ---- RIM CAMPAIGN STATE (RIM_PRIORITY) -------------------------
            # Placement runs in cure-deadline order, so this advances roughly
            # with the clock: `rim_hold_h` accumulates the hours the machine has
            # spent on its OPEN rim, and resets when the rim genuinely changes.
            # It is maintained unconditionally so the counters are comparable
            # between arms; only `_rimkey` reads it.
            _rim_now = rim_of.get(gt, "")
            if open_rim.get(mach) != _rim_now:
                open_rim[mach] = _rim_now
                rim_hold_h[mach] = 0.0
                rim_opens[mach] = rim_opens.get(mach, 0) + 1
            rim_hold_h[mach] = rim_hold_h.get(mach, 0.0) \
                + (en - st).total_seconds() / 3600.0
            mach_rims.setdefault(mach, set()).add(_rim_now)
            part_done[(p, gt, mach)] = part_done.get((p, gt, mach), 0.0) \
                + (en - st).total_seconds() / 3600.0
            _adds2 = [(st + timedelta(seconds=cu * c), d["t_cure"], d["qty"])
                      for d, cu in zip(grp, cums)]
            _cap_apply(p, _adds2, +1.0)          # commit the chosen placement
            # THE RUN IS KEPT AS AN OBJECT, NOT FLATTENED STRAIGHT TO ROWS.
            # `_make_room` below has to be able to MOVE a run that is already
            # booked, and a bag of slice dicts cannot be moved -- recomputing its
            # start, its per-slice stamps and its ledger entries needs `grp`,
            # `cums` and the machine cadence. Rows are emitted once, after the
            # whole placement loop, from this list.
            placed.setdefault(mach, []).append(
                {"plant": p, "gt": gt, "st": st, "c": c, "grp": grp,
                 "cums": cums, "dur": dur, "mach": mach})
            return True

    def _rows_of(rec: dict) -> list:
        """Slice rows for one placed run, at its CURRENT start."""
        out, cum, c = [], 0.0, rec["c"]
        for d in rec["grp"]:
            s0 = rec["st"] + timedelta(seconds=cum * c)
            s1 = s0 + timedelta(seconds=d["qty"] * c)
            cum += d["qty"]
            out.append({"plant": rec["plant"], "gt_code": rec["gt"],
                        "machine": rec["mach"], "press": d["press"],
                        "start_ts": s0, "end_ts": s1,
                        "qty": round(d["qty"], 1), "cure_ts": d["t_cure"],
                        "wait_h": round((d["t_cure"] - s1).total_seconds()
                                        / 3600.0, 3)})
        return out

    def _adds_of(rec: dict) -> list:
        return [(rec["st"] + timedelta(seconds=cu * rec["c"]), d["t_cure"],
                 d["qty"]) for d, cu in zip(rec["grp"], rec["cums"])]

    def _r5_floor(rec: dict):
        """Earliest start this run may legally take: R5 on every slice, and the
        build calendar floor `_t0b` (= t0 unless PLANNER_L7_PRE_T0_H opens a
        bounded pre-horizon window). R5 is unconditional either way.

        Under R5_FIRST_TYRE the offset is the slice's START (cu - qty), not its
        end -- the first tyre off the drum is the one that expires. That is a
        LATER floor, i.e. strictly less room, which is the honest direction.
        Kept in step with the identical expression in `_place`."""
        return max([_t0b] + [d["t_cure"] - timedelta(hours=GT_SHELF_LIFE_H)
                           - timedelta(seconds=(cu - (d["qty"] if R5_FIRST_TYRE
                                                      else 0.0)) * rec["c"])
                           for d, cu in zip(rec["grp"], rec["cums"])])

    def _make_room(p: str, gt: str, cand: list, grp: list) -> bool:
        """LAST RESORT: open a contiguous hole by pulling incumbents EARLIER.

        Placement is one greedy pass in deadline order and never revisits a
        booking, so a run that arrives late finds the machine's free time cut
        into pieces by runs that were placed first. Anti-sliver packing stops new
        holes being created; it cannot repair a machine whose holes were already
        set by the ORDER things were placed in. Measured after that fix, July
        TBR: 50 % of the remaining refusal has no hole >= its own duration
        anywhere in its R5 band, on machines running at 74-87 % -- the time is
        there, in the wrong shape.

        So destroy and repair, on the smallest neighbourhood that can work: one
        machine. Left-compact the runs that sit before our deadline -- each one
        moves only as far as its OWN R5 floor, t0 and its predecessor allow, and
        never later -- then look for an insertion point in the space that
        coalesces at the right-hand end. Every constraint is re-checked, the WIP
        rail last and on the whole bundle; if anything fails, the machine is
        rolled back exactly as it was. Nothing here can relax a rule: a
        compaction that would breach R5, t0 or the rail simply does not happen.
        """
        gq = sum(d["qty"] for d in grp)
        if STRICT_FLOOR and gq < float(floor_units.get(p, 0)):
            return False
        for mach in cand:
            c = _cad(p, mach, gt)
            cums, _a = [], 0.0
            for d in grp:
                _a += d["qty"]
                cums.append(_a)
            dur = timedelta(seconds=gq * c)
            ideal = min(d["t_cure"] - timedelta(hours=tau_rel[p])
                        - timedelta(seconds=cu * c) for d, cu in zip(grp, cums))
            mine = {"plant": p, "gt": gt, "st": ideal, "c": c, "grp": grp,
                    "cums": cums, "dur": dur, "mach": mach}
            lo = _r5_floor(mine)
            if ideal < lo or ideal < _t0b:
                _mr_fail["cold"] = _mr_fail.get("cold", 0) + 1
                continue
            recs = sorted(placed.get(mach, []), key=lambda r: r["st"])
            if not recs:
                continue
            # --- LATEST insertion point, then a MINIMAL left cascade ---------
            # Compacting the whole prefix was the first attempt and it failed on
            # the WIP rail 699 times against 6 successes: moving every earlier
            # run to the front of the month buys 2.16 h of hole (the median
            # shortfall) at the price of days of extra standing GT, and both
            # plants already sit ON their rail. So shift only the runs that
            # actually block the hole, and only by the hours the hole is short.
            # Each one still stops at its own R5 floor and at t0.
            # MORE THAN ONE INSERTION POINT. The latest slot is the cheapest
            # in inventory, so it is tried first -- but when the run that would
            # have to move is already at its own R5 floor, or the rail refuses
            # the extra stock, an EARLIER slot on the same machine can still be
            # legal. Try the slot before each existing run, latest first, and
            # stop at MR_POINTS: this is a bounded neighbourhood search, not a
            # solver, and the first legal insertion wins.
            tail = [r for r in recs if r["st"] >= ideal]
            ub0 = ideal
            if tail:
                ub0 = min(ub0, tail[0]["st"]
                          - timedelta(seconds=_setup_s(p, mach, gt, tail[0]["gt"]))
                          - dur)
            ubs = [ub0] + [r["st"] - timedelta(
                seconds=_setup_s(p, mach, gt, r["gt"])) - dur
                for r in recs if r["st"] <= ideal]
            # HOLIDAY: `_make_room` does NOT go through `_place`'s backward walk,
            # so the closure in `busy` cannot protect it -- it computes its own
            # insertion points from `placed` and would happily drop a run into a
            # shut plant. Snap each candidate to the latest start whose whole
            # span is clear. Identity with no calendar configured.
            ubs = [holiday.fit_before(p, u, dur) for u in ubs]
            ubs = sorted({u for u in ubs if u >= max(lo, _t0b)}, reverse=True)
            if not ubs:
                _mr_fail["late"] = _mr_fail.get("late", 0) + 1
                continue
            for ub in ubs[:MR_POINTS]:
                keep = [r for r in recs if r["st"] < ub + dur]
                if not keep:
                  _mr_fail["empty"] = _mr_fail.get("empty", 0) + 1
                  continue
                old = [r["st"] for r in keep]
                limit, ok_cascade = ub, True
                for i in range(len(keep) - 1, -1, -1):
                    r = keep[i]
                    r["mach"] = mach
                    lim = limit - timedelta(seconds=_setup_s(p, mach, r["gt"], gt)
                                            if i == len(keep) - 1 else
                                            _setup_s(p, mach, r["gt"],
                                                     keep[i + 1]["gt"]))
                    if r["st"] + r["dur"] <= lim:
                        break                       # clear, and so is everything before
                    # HOLIDAY: compaction moves incumbents EARLIER, which is
                    # exactly how a run lands inside a closure. Same snap.
                    want = holiday.fit_before(r["plant"], lim - r["dur"], r["dur"])
                    floor_r = max(_r5_floor(r), _t0b)
                    if want < floor_r:              # cannot get out of the way
                        ok_cascade = False
                        _mr_gap.append(((want - floor_r).total_seconds() / 3600.0,
                                        dur.total_seconds() / 3600.0))
                        break
                    r["st"] = want
                    limit = r["st"]
                if not ok_cascade:
                    _mr_fail["noroom"] = _mr_fail.get("noroom", 0) + 1
                    for r, o in zip(keep, old):        # rollback: nothing gained
                        r["st"] = o
                    continue
                st = ub
                # --- one rail check on the WHOLE bundle, then commit or revert ----
                # The moved runs are already on the ledger at their OLD starts. Swap
                # them to the new ones, then ask whether OUR run still fits under the
                # rail. Compaction only ever moves stock EARLIER, so this is the one
                # place the bundle can breach, and it is checked as a bundle.
                mine["st"] = st
                moved = [(r, o) for r, o in zip(keep, old) if r["st"] != o]
                for r, o in moved:
                    _cap_apply(p, _adds_of({**r, "st": o}), -1.0)
                    _cap_apply(p, _adds_of(r), +1.0)
                if not _cap_ok(p, _adds_of(mine)):
                    for r, o in moved:                  # put the machine back
                        _cap_apply(p, _adds_of(r), -1.0)
                        r["st"] = o
                        _cap_apply(p, _adds_of(r), +1.0)
                    _mr_fail["rail"] = _mr_fail.get("rail", 0) + 1
                    continue
                _cap_apply(p, _adds_of(mine), +1.0)
                placed.setdefault(mach, []).append(
                    {"plant": p, "gt": gt, "st": st, "c": c, "grp": grp,
                     "cums": cums, "dur": dur, "mach": mach})
                # THE CLOSURE MUST SURVIVE THIS REBUILD. `busy[mach]` is
                # reconstructed from `placed`, which holds only real runs, so a
                # naive rebuild silently DELETES the booked holiday and every
                # later `_place` on this machine is free to schedule into a shut
                # plant. This is the "a table you load and never read" defect
                # (PARTITION §6 item 25) in its mirror form -- a table you write
                # and then overwrite.
                busy[mach] = [(r["st"], r["st"] + r["dur"], r["gt"],
                               rim_of.get(r["gt"], ""))
                              for r in placed.get(mach, [])] + _hol_iv.get(mach, [])
                _mr_used[p] = _mr_used.get(p, 0) + 1
                return True
        return False

    # ---- PHASE 2b: PLACE IN GLOBAL CURE-DEADLINE ORDER ------------------
    # Runs used to be placed GT by GT (all of GT A's, then all of GT B's), in an
    # order set by eligibility and volume -- nothing to do with WHEN a run is
    # needed. So a run due on day 30 could take the machine slot a run due on
    # day 3 required, and the day-3 run, placed later, was shoved backwards.
    # Measured on the GT-ordered plan: runs due in days 0-7 were pushed early
    # 45% of the time (TBR 60%) at a mean of 3.32 h, falling monotonically to
    # 30%/32% and 2.62 h for days 21-31 -- the runs needed FIRST were displaced
    # MOST, and they have the least room because they cannot move before t0.
    # Earliest deadline first, scarcity only as a tiebreak.
    #
    # SPLIT BEFORE STARVING -- placement is never all-or-nothing. A run that
    # cannot be placed is halved and retried, down to the single slice; volume is
    # never lost to a shape constraint. Halves re-enter the queue at their own
    # deadline so the global ordering is preserved.
    # ---- BOUNDED SISTER BUCKETING (PLANNER_SISTER_BUCKET_H) ----------------
    # The candidate tie-break cannot raise sister adjacency, because `HARD_PIN`
    # gives most runs a single feasible machine and the ORDER of blocks in time
    # is set by this heap, not by machine choice (measured: SISTER_GROUP alone
    # moved TBR sister adjacency 43.0 % -> 42.4 %, i.e. not at all). Adjacency
    # is a property of the QUEUE.
    #
    # A full queue re-sort by similarity is the change that cost 25,549 tyres at
    # L5, so this is bounded instead: deadlines are rounded DOWN to a bucket of
    # B hours and sisters are grouped only WITHIN a bucket. Deadline discipline
    # is preserved to within B; at B = 0 the key is exactly the shipped one. The
    # true deadline stays in the key after the sister term, so ordering inside a
    # bucket is still earliest-first among non-sisters.
    # CONSTRUCTION CLUSTERS use the same bucketing shape, one level up in
    # priority. THE KEY MUST STAY HOMOGENEOUS across the whole heap: if a
    # plant outside CLUSTER_PLANTS returned a bare `t` while another returned a
    # 3-tuple, `heapq` would compare a datetime against a tuple and raise. Out
    # -of-scope plants and uncovered GTs therefore get the SENTINEL "~" inside
    # the tuple, not a different key shape -- "~" sorts after every digit, so
    # they fall to the tail of their bucket in plain deadline order.
    # ---- CLUSTER SEQUENCING KEY (PLANNER_CLUSTER_SEQ) ---------------------
    # Primary machine per GT, resolved ONCE from the horizon assignment. A GT
    # split across two machines by the partition still gets ONE key, so its own
    # jobs never split into two groups; `sorted()[0]` only has to be
    # deterministic, not optimal, because the term exists to CONFINE the reorder
    # to jobs that can be adjacent, not to choose a machine.
    _mach_key: dict[tuple, str] = {
        k: (sorted(v)[0] if v else "~") for k, v in gt_machines.items()}

    def _cseq_key(p: str, g: str) -> tuple:
        # ALWAYS a 3-tuple, whatever the key shape: dropping a term would change
        # the key ARITY between plants and `heapq` would compare a str against a
        # datetime. Terms that are switched off carry the sentinel instead.
        if p not in CLUSTER_SEQ_PLANTS:
            return ("~", "~", "~")
        m = _mach_key.get((p, g), "~") if "m" in CLUSTER_SEQ_KEY else "~"
        r = (rim_key_of.get((p, g)) or rim_of.get(g) or "~") \
            if "r" in CLUSTER_SEQ_KEY else "~"
        return (m, r, cluster_of.get((p, g), "~"))

    def _hkey_base(t, g: str, p: str = ""):
        if CLUSTER_SEQ and cluster_of:
            m, r, c = _cseq_key(p, g)
            return (int((t - t0).total_seconds() // (CLUSTER_SEQ_H * 3600.0)),
                    m, r, c, t)
        if CLUSTER_BUCKET_H > 0.0 and cluster_of:
            return (int((t - t0).total_seconds() // (CLUSTER_BUCKET_H * 3600.0)),
                    cluster_of.get((p, g), "~") if p in CLUSTER_PLANTS else "~",
                    t)
        if SISTER_BUCKET_H <= 0.0 or not sister_of:
            return t
        return (int((t - t0).total_seconds() // (SISTER_BUCKET_H * 3600.0)),
                sister_of.get(g, "~"), t)

    # ---- MOST-CONSTRAINED-FIRST WRAPPER (PLANNER_L7_PINNED_FIRST) ---------
    # See the flag's block at the top of this file. `_n_elig` is memoised here
    # because `_cand` sorts on every call and this runs once per heap push, and
    # because a cache makes the term provably constant for a (plant, GT) across
    # the whole solve -- an ordering key that could change mid-run is a heap
    # invariant violation, not a tuning choice.
    _nel_memo: dict = {}

    def _pf_nelig(p: str, g: str) -> int:
        k = (p, g)
        if k not in _nel_memo:
            _nel_memo[k] = _n_elig(p, g)
        return _nel_memo[k]

    def _hkey(t, g: str, p: str = ""):
        k = _hkey_base(t, g, p)
        if PINNED_FIRST_H <= 0.0:
            return k                       # byte-identical to the shipped key
        # PLANT SCOPE: measured -122/-335 BUILT on TBR both months, so TBR is
        # excluded. It must keep the SAME TUPLE SHAPE -- both plants share one
        # heap, and returning a bare `k` for TBR while PCR returns a 3-tuple
        # makes heapq compare str against int and abort the run. Constant
        # (0, 0) prefix leaves TBR ordering decided entirely by `k`, i.e.
        # byte-identical to the shipped key, while PCR gets the real buckets.
        if p and p.upper() not in _PF_PLANTS:
            return (0, 0, k)
        # (deadline bucket, eligible-machine count, <the whole shipped key>).
        # The shipped key is NESTED, not replaced: inside a bucket the pinned
        # jobs go first, and everything below -- cluster sequencing, the true
        # deadline -- still decides the order among equally-constrained jobs.
        return (int((t - t0).total_seconds() // (PINNED_FIRST_H * 3600.0)),
                _pf_nelig(p, g), k)

    if PINNED_FIRST_H > 0.0:
        _pf = sorted({(_p, _g): _pf_nelig(_p, _g)
                      for (_t, _s, _p, _g, _c, _gr) in jobs}.items())
        _pin1 = sum(1 for _k, _v in _pf if _v <= 1)
        print(f"  PINNED FIRST  B={PINNED_FIRST_H} h  -- least-flexible-first "
              f"within a {PINNED_FIRST_H} h deadline bucket · "
              f"{_pin1}/{len(_pf)} (plant, GT) pairs have <=1 eligible machine")

    if CLUSTER_SEQ:
        # Report coverage on JOBS, not on the file: the file's 140 rows say
        # nothing about how much of THIS month's queue carries a cluster.
        _nc = sum(1 for (_t, _s, _p, _g, _c, _gr) in jobs
                  if (_p, _g) in cluster_of and _p in CLUSTER_SEQ_PLANTS)
        _nr = sum(1 for (_t, _s, _p, _g, _c, _gr) in jobs
                  if _cseq_key(_p, _g)[1] != "~")
        print(f"  CLUSTER SEQ  B={CLUSTER_SEQ_H} h  key={CLUSTER_SEQ_KEY}"
              f"  plants={sorted(CLUSTER_SEQ_PLANTS)}"
              f"  -- {_nc}/{len(jobs)} jobs carry a cluster, {_nr} carry a rim"
              f"{'  (NO CLUSTER FILE -- INERT)' if not cluster_of else ''}")

    if CLUSTER_BUCKET_H > 0.0:
        _ncov = sum(1 for (_t, _s, _p, _g, _c, _gr) in jobs
                    if (_p, _g) in cluster_of and _p in CLUSTER_PLANTS)
        print(f"  CLUSTER BUCKETING  {CLUSTER_BUCKET_H} h  plants={sorted(CLUSTER_PLANTS)}"
              f"  -- {_ncov}/{len(jobs)} jobs carry a cluster id"
              f"{'  (NO CLUSTER FILE -- INERT)' if not cluster_of else ''}")

    # ---- ITEM 6: DAY-1 BUILD PRIORITY (PLANNER_L7_D1_BUILD_PRIO) ---------
    # Release building for the GTs whose day-1 presses are WAITING, ranked by
    # the curing output that recovers rather than by deadline alone:
    #
    #     score = waiting_presses x cure_rate x hours_waiting
    #
    # `off` (default) · `hard` = every waiting-GT job jumps the queue ·
    # `capped` = only jobs that are SHORT (duration <= the plant median), so a
    # monthly-critical long run is never displaced by a day-1 rescue.
    #
    # WHAT THIS CANNOT DO, AND THE MEASUREMENT MUST SHOW IT.
    # L5 fixes every campaign's start_ts BEFORE L7 runs. A press seated at
    # t0 + 11.86 h starts then whether its GT was built at hour 3 or hour 9.
    # So this can only pay where BUILDING was the binding constraint on a
    # day-1 press. Where the press is held by L5's floor instead, priority
    # building just makes GT earlier and it waits -- which is the case that
    # proves the dynamic L5<->L7 availability interface is required.
    # Report first-build completion against press start, not just fulfilment.
    #
    # SHIPS OFF. MEASURED 2026-08-13, BOTH MONTHS, THREE ARMS -- REJECTED.
    #
    #            Jul PCR BUILT  Jul day1   Aug PCR BUILT  Aug day1   L11
    #   base           385,084    10,576         410,652    10,540   27 · 27
    #   hard            -3,285    10,323          -5,393    10,105   29 · 24
    #   capped          +1,917    10,881            -291    10,227   27 · 25
    #   (TBR: hard -247/-450 · capped -172/+259)
    #
    # `hard` displaces monthly-critical building on both months. `capped` is
    # MIXED-SIGN across months AND plants and day-1 curing FALLS on August PCR
    # and July TBR. Fails the two-month gate.
    #
    # THE REASON IT CANNOT WORK, AND THIS IS THE POINT OF THE EXPERIMENT:
    # `presses seated at t0` is 33 PCR / 36 TBR IN EVERY ARM INCLUDING hard.
    # Priority building cannot move a press start, because L5 fixes every
    # campaign's start_ts before L7 runs. And in the BASELINE, with no priority
    # at all, the GT is ALREADY THERE:
    #
    #     PCR  18 of 21 waiting GTs (48 presses) have fresh build complete
    #          BEFORE the press starts -- 361 idle press-h ~ 2,306 tyres
    #     TBR  11 of 13 (35 presses) -- 215 idle press-h ~ 384 tyres
    #     GT 1503 NEO MSIL   built 2.28 h · 9 presses start 11.86 h · idle 9.58 h
    #     10.00 R 20 JUH5    built 2.32 h · 9 presses start 10.18 h · idle 7.86 h
    #
    # Building is binding on only 12 of the 96 waiting presses. You cannot
    # accelerate a build that already finished nine hours early.
    #
    # WHAT THIS ESTABLISHES: the ~2,690 tyres of day-1 output are reachable ONLY
    # by a dynamic L5<->L7 availability interface -- the press starts when the
    # GT LEDGER confirms stock, not at a static tau*+band wall. No build-side
    # priority, and no opening-stock change, can substitute for it.
    _d1p = os.environ.get("PLANNER_L7_D1_BUILD_PRIO", "off").lower()
    _d1_prio: dict = {}
    if _d1p in ("hard", "capped"):
        _cam = pl.read_parquet(run / "cure_campaigns.parquet")
        _t0c = _cam["start_ts"].min()
        _w = (_cam.with_columns(
                  ((pl.col("start_ts") - _t0c).dt.total_seconds() / 3600).alias("_h"))
                 .filter((pl.col("_h") > 0.5) & (pl.col("_h") < 24.0)))
        # Cure rate comes from the campaigns themselves (qty / press-hours),
        # which IS this GT's draw rate on one press -- no plant median needed.
        for _r in (_w.group_by(["plant", "gt_code"])
                     .agg(pl.col("press").n_unique().alias("np"),
                          pl.col("_h").mean().alias("hw"),
                          (pl.col("qty").sum()
                           / pl.col("hours").sum().clip(lower_bound=1e-9)).alias("rt"))
                     .iter_rows(named=True)):
            _d1_prio[(_r["plant"], _r["gt_code"])] = float(
                _r["np"] * max(_r["rt"], 0.0) * _r["hw"])
        _med = {}
        for _pp in ("PCR", "TBR"):
            _ds = [sum(d["qty"] for d in g) for (_t, _s, _p2, _g, _c, g) in jobs
                   if _p2 == _pp]
            _med[_pp] = (sorted(_ds)[len(_ds) // 2] if _ds else 0.0)
        print(f"  [d1-build-prio] mode={_d1p} · {len(_d1_prio)} (plant, GT) pairs "
              f"have day-1 presses waiting")

    def _prio(p: str, gt: str, grp) -> tuple:
        """Leading heap term. 0 = jump the queue, 1 = normal. Arity is fixed."""
        sc = _d1_prio.get((p, gt))
        if sc is None:
            return (1, 0.0)
        if _d1p == "capped" and sum(d["qty"] for d in grp) > _med.get(p, 0.0):
            return (1, 0.0)          # long run -- do not displace monthly work
        return (0, -sc)              # negative so the biggest score sorts first

    # ---- MOST-CONSTRAINED-FIRST -- THE MEASUREMENT (PLANNER_L7_PINNED_FIRST) --
    # `sc` IS `_n_elig(p, gt)` (set where `jobs` is built). It is the THIRD key
    # here, so it only breaks ties among jobs that already share an `_hkey` --
    # and `_hkey` ends in the exact cure timestamp, so two jobs essentially never
    # tie. Scarcity therefore does NOT order this queue; the deadline does.
    # PLANNER_L7_PINNED_FIRST=B lifts it above the deadline WITHIN a B-hour
    # bucket. See the flag block at the top of this file for the mechanism.
    #
    # SHIPS OFF (B=0). MEASURED 2026-08-19, JULY 2026, BOTH PLANTS, FIVE FRESH
    # `main.py plan` ARMS IN ONE INVOCATION, one frozen July partition
    # (sha1 8a47e109e27d, stamped 2026-07, CP-SAT OPTIMAL both plants),
    # post-F1 masters (net_requirement 41c8d601d747). B=0 is BYTE-IDENTICAL to
    # the pre-flag engine on build_schedule / cure_campaigns / gt_events /
    # build_starved / cure_campaigns_reconciled / mould_changes /
    # l11_invariants -- verified by running the reverted file as its own arm.
    #
    #   PCR      BUILT   dBUILT  in-month  ful%    unfed  same%  wCO_h  R5max  L11
    #   B=0    389,949       +0   384,621  96.8    8,312   83.7  552.0   71.9  31/48
    #   B=4    390,440     +491   385,112  96.9    7,821   83.2  557.1   71.8  31/48
    #   B=8    390,600     +651   385,272  97.0    7,661   82.9  553.1   70.6  31/48
    #   B=24   391,048   +1,099   385,720  97.1    7,213   81.6  567.7   71.9  31/48
    #
    #   TBR      BUILT   dBUILT  in-month  ful%    unfed  same%  wCO_h  R5max  L11
    #   B=0     96,318       +0    94,095  96.6    2,625  100.0  174.3   71.8  31/48
    #   B=4     96,555     +237    94,332  96.8    2,388  100.0  172.5   71.8  31/48
    #   B=8     96,555     +237    94,332  96.8    2,388  100.0  173.2   71.8  31/48
    #   B=24    96,204     -114    93,990  96.5    2,739  100.0  172.3   71.8  31/48
    #
    # The carry-out tail is 10,806 PCR / 3,540 TBR in EVERY bucket arm, identical
    # to base. So this is NOT the §4v/v14 relocation pattern -- in-month and BUILT
    # move together, by the same amount, with the tail pinned. It creates output.
    #
    # WHERE THE TYRES COME FROM, exactly. `GT 1865 ROYL RENO` is the only GT
    # allowed on TBMPCR3 alone; at B=0 it builds 2,244 of 3,029 and leaves 785
    # unfed (26 % of its own requirement). At B>=4 it builds 3,029 and leaves
    # ZERO -- a flexible rim-mate no longer takes its only machine first. That
    # single GT is the whole PCR gain at B=4.
    #
    # AND WHAT IT CANNOT TOUCH, which is the more useful half. The other three
    # single-machine PCR GTs do not move one tyre at ANY bucket:
    #   GT 1402 XPC TATA  20,539 built / 176 unfed   TBMPCR4
    #   GT 2568 HT2       15,364 built / 172 unfed   TBMPCR9
    #   GT 1412 XPC MM     4,942 built / 239 unfed   TBMPCR4
    # Their residual starves as `release_before_t0`, not as contention -- the
    # build would have to start before the month opens. That is the ~4 h carry-in
    # question (a PLANT RULING, §9.1), and no queue order reaches it. Do not
    # re-tune this flag expecting to collect them.
    #
    # B=24 IS THE TRAP. It is the best PCR arm (+1,099) and it FAILS the gate:
    # TBR BUILT goes -114, PCR same-size falls 83.7 -> 81.6 while weighted
    # changeover rises 552.0 -> 567.7 h, and L7's own daily-mean-max prints
    # PCR 4,821 OVER the 4,800 G8 rail. Mixed-sign across plants is a fail
    # (DO-NOT #14), however good the headline plant looks.
    #
    # THE PRICE AT B=8, IN ONE SENTENCE: +651 PCR / +237 TBR BUILT costs PCR
    # same-size 83.7 -> 82.9 pt and PCR weighted changeover 552.0 -> 553.1 h,
    # with R5 max IMPROVING 71.9 -> 70.6 h and B12 sub-floor 0.00 % throughout.
    # B=8 is also the only bucket where L7's own rail check is clean on BOTH
    # plants (PCR 4,753 / TBR 1,398); B=4 prints TBR 1,401 OVER by one tyre.
    # `scripts/arm_kpi.py` recomputes the same quantity post-reconcile and
    # disagrees by ~+85 PCR / -3 TBR (PCR 4,838, TBR 1,399, against a BASE that
    # is itself 1,402 i.e. already over on TBR) -- that spread is the known §1f
    # pre-flight-vs-post-reconcile drift, so quote both bases or neither.
    # No arm changes ANY of the 48 L11 invariant statuses: 31/48 in all four,
    # with the same 17 failures, and the PCR G8 mean stays BELOW its band
    # (4,004 -> 3,906) which is the intentional under-run, not a breach.
    #
    # ---- AUGUST, THE OUTSTANDING GATE -- RUN 2026-08-19. THE ANSWER IS NO. ----
    #
    # The note above ended "run August before moving the default". This is that
    # run: four fresh arms, one frozen AUGUST partition (sha1 8bcb10c113bf,
    # stamped 2026-08, CP-SAT OPTIMAL, 95 rows, provenance re-verified against
    # the masters the arms themselves regenerate -- all five input sha1s equal),
    # PLANNER_OPENING_GT=opening_gt_manual_2026-08.parquet, LOT_INTERVAL_H=8,
    # TH_GT_WIP_RAIL_MARGIN=1.0. All four `check_arm_fresh` FRESH; all four
    # `build_schedule.parquet` sha1s distinct; `cure_campaigns.parquet` IDENTICAL
    # in all four, which is the correct signature of an L7-only queue change.
    #
    #   PCR      BUILT   dBUILT  in-month  ful%    unfed  starv  same%  wCO_h  R5max  L11
    #   B=0    408,349       +0   387,837  90.9    8,212  4,513   75.9  645.7   71.4  28/48
    #   B=4    410,916   +2,567   389,116  91.2    5,645  1,946   76.7  645.9   71.7  29/48
    #   B=8    410,752   +2,403   389,087  91.2    5,809  2,110   76.9  641.2   71.6  29/48
    #   B=24   410,916   +2,567   389,116  91.2    5,645  1,946   75.9  632.4   72.0  29/48
    #
    #   TBR      BUILT   dBUILT  in-month  ful%    unfed  starv  same%  wCO_h  R5max  L11
    #   B=0     97,182       +0    94,176  95.4    2,799  1,810  100.0  137.7   71.1  28/48
    #   B=4     97,188       +6    94,187  95.4    2,793  1,804  100.0  136.0   65.6  29/48
    #   B=8     97,104      -78    94,099  95.3    2,877  1,888  100.0  136.7   67.7  29/48
    #   B=24    97,386     +204    94,396  95.6    2,595  1,606  100.0  133.2   66.8  29/48
    #
    # THE TWO-MONTH VERDICT, per bucket, which is the only reading that decides:
    #   B=4    Jul PCR  +491  TBR  +237 | Aug PCR +2,567  TBR    +6   non-negative
    #   B=8    Jul PCR  +651  TBR  +237 | Aug PCR +2,403  TBR   -78   REJECT
    #   B=24   Jul PCR +1,099 TBR  -114 | Aug PCR +2,567  TBR  +204   REJECT
    #
    # B=8 IS REJECTED ON TWO INDEPENDENT COUNTS, either of which is sufficient.
    #   1. TBR BUILT -78. July gave +237 at the same setting. A flag whose sign
    #      flips by month on a plant is fitted to a month, not an improvement --
    #      that is exactly the +0.6/-0.5 pattern that cost v12 two flags.
    #   2. It creates a NEW L11 FAILURE: `TBR median GT wait vs tau*`
    #      PASS -> FAIL (5.05 h -> 5.78 h). NO July arm moved ANY of the 48
    #      statuses. B=24 breaks the same invariant (5.05 -> 6.10 h).
    #
    # THE PASS COUNT IS A TRAP HERE AND MUST NOT BE READ ALONE. Every bucket
    # reads 28/48 -> 29/48, which looks like a strict improvement. It is a net of
    # three flips at B=8: PCR median GT wait FAIL->PASS, PCR throughput vs plant
    # rate FAIL->PASS, and TBR median GT wait PASS->FAIL. The aggregate rises
    # WHILE A TBR INVARIANT GOES RED -- the per-segment-regression-hidden-by-an-
    # aggregate failure mode named in EXPERT_AUDIT. Diff the STATUS COLUMN, never
    # the count.
    #
    # RAILS (G8 daily-mean-max). BOTH BASES, NEITHER PICKED -- the ~+-85 §1f
    # pre-flight-vs-post-reconcile spread is live here and the two disagree about
    # who breaches. AUGUST BASE IS ALREADY OVER ON BOTH PLANTS ON BOTH BASES, so
    # no arm can be credited with "no breach"; read these as movement, not as a
    # gate:
    #   arm    L7 PCR / TBR        arm_kpi.py PCR / TBR     (rails 4,800 / 1,400)
    #   B=0    4,845 OVER / 1,407 OVER    4,852 OVER / 1,403 OVER
    #   B=4    4,841 OVER / 1,402 OVER    4,848 OVER / 1,399 OK
    #   B=8    4,795 ok   / 1,402 OVER    4,859 OVER / 1,404 OVER
    #   B=24   4,830 OVER / 1,420 OVER    4,763 OK   / 1,410 OVER
    # B=8 is clean on PCR by L7's own check and OVER on PCR by arm_kpi's -- the
    # two bases contradict each other on the single arm under decision, which is
    # itself the reason the rail cannot carry this call.
    #
    # WHERE AUGUST DIFFERS FROM JULY MECHANICALLY, so this is not a mystery. The
    # August gain is ~4x July's on PCR and it comes from a DIFFERENT cause:
    # `r5_shelf_life` starvation collapses 2,592 -> 0 (B=4) / 320 (B=8), while
    # `release_before_t0` barely moves (1,921 -> 1,946 / 1,790). July's gain was
    # contention on `GT 1865 ROYL RENO`'s single machine. August is a 99.5 %-
    # loaded month (L2 plannable/capacity), so reordering the queue changes WHEN
    # green tyres wait, and the R5 72 h clock -- not machine contention -- is what
    # was killing them. That also explains the TBR cost: PCR pulls earlier, TBR
    # green tyres wait longer, median GT wait rises past tau* and 78 tyres die.
    #
    # SO: DEFAULT STAYS 0. B=4 is the only bucket non-negative on both plants in
    # both months, but its August TBR movement is +6 tyres = +0.006 pt, an order
    # of magnitude BELOW `ab_both_months.py`'s own 0.05 pt noise floor -- so the
    # honest description of B=4 is PCR-POSITIVE, TBR-NEUTRAL, not "positive on
    # both", and it is a CANDIDATE for a separate adoption decision, not a
    # default this run is entitled to move. Do not re-tune B to make TBR pass;
    # that is retuning until the metric agrees, which is how a gate gets gamed.
    heap = []
    for _i, (t_due, sc, p, gt, cand, grp) in enumerate(jobs):
        heapq.heappush(heap, (_prio(p, gt, grp), _hkey(t_due, gt, p), sc, _i,
                              p, gt, cand, grp))
    _seq = len(jobs)
    _pool_hold: dict = {}          # C: sub-floor remainders held for pooling
    while heap:
        _pk, t_due, sc, _i, p, gt, cand, grp = heapq.heappop(heap)
        # THREE PASSES, and the order is the whole point.
        # Capping earliness and spilling off-lock are ALTERNATIVES, not
        # independent levers: when the locked machine is busy you either build
        # early (keeping rim purity) or move machine (losing it). Measured with a
        # flat cap over the full candidate list: inventory 6,946 -> 5,825 but
        # same-size 78.2% -> 56.2% and weighted setup 265 -> 397 h -- it simply
        # bought inventory with rim purity.
        # So cap earliness only where a SAME-RIM machine can absorb it; never
        # break the lock to avoid building early; and only then spill.
        # PASS 0 -- THE GT's OWN PINNED MACHINE, EXCLUSIVELY (P7 / B9 / B10).
        # `_lk` below is the RIM lock, which is a set of machines, so it protects
        # same-size but lets any rim-mate take the run. Measured consequence: the
        # horizon assignment produces 45 (GT, machine) pairs on PCR -- 1.12
        # machines per GT -- and placement turns that into 85 pairs, 2.12 per GT.
        # 47 % of pairs, carrying 24 % of PCR volume, are created HERE by falling
        # through to a rim-mate, against a plant that keeps 66.7 % of its GTs on
        # exactly one machine all month.
        # So try the pin alone first, and let the run WAIT on its own machine
        # (uncapped earliness) before it is allowed to move. Only then widen.
        # THE PIN MUST BE RIM-CONSISTENT. `gt_home_machine` and `machine_rim_lock`
        # are two independently mined masters and they DISAGREE for some GTs: the
        # machine a GT historically ran on is not always the machine whose
        # dominant rim is that GT's. Pinning to the raw home machine measured
        # +5.0 pt stickiness but -7.5 pt same-size (86.9 -> 79.4 %) and +47 h of
        # setup, because it parks a GT on a machine that then changes rim around
        # it. Intersect the two: stick to the home machine only where it is also
        # rim-locked for this GT, else let the rim lock choose and stay sticky
        # within it.
        _pin = [m for m in cand if m in set(gt_machines.get((p, gt), []))
                and m in set(_locked(p, gt))]
        if HARD_PIN and _pin and (_place(p, gt, _pin, grp, EARLY_CAP_H)
                                  or _place(p, gt, _pin, grp, float("inf"))):
            continue
        _lk = [m for m in cand if m in set(_locked(p, gt))] or cand
        _passes = [_place(p, gt, _lk, grp, EARLY_CAP_H),
                   lambda: _place(p, gt, _lk, grp, float("inf"))]
        if _passes[0] or _passes[1]():
            continue
        # THIRD PASS -- off-lock spill. Skipped under HARD_LOCK.
        # The WIP cap rejects candidates, and when it rejects every LOCKED
        # machine the run used to fall through to any eligible one -- which is
        # why arming the cap took PCR same-size from 78.2 % to 47.9 %. Under
        # HARD_LOCK the run is split or reported instead of leaving its rim.
        if not HARD_LOCK and _place(p, gt, cand, grp, float("inf")):
            continue
        # MAKE ROOM before giving up. Under a hard lot floor a run cannot be cut
        # down to fit a hole, so the hole has to be made to fit the run. Tried on
        # the locked machines only -- this opens space, it never widens the lock.
        if MAKEROOM and _make_room(p, gt, _lk, grp):
            continue
        # LAST-RESORT APPROVED-MATRIX RESCUE.  A historical rim lock is a
        # preference, not a legal ban.  Only machines that `_cand` admitted from
        # the plant matrix can enter `_extra`; all physical/time gates remain in
        # `_place`.  Trying the extras separately also means a flexible machine
        # cannot displace a feasible home/rim placement above.
        _extra = [m for m in cand if m not in set(_lk)]
        if _hard_mc:
            # A hard-tier machine is single-rim in 8 months of MES and the plant
            # breaks it 0.0 % of the time. Do not let the last-resort pass be the
            # thing that does. `flex` always stays open -- mixing is its job --
            # and `primary` stays open unless PLANNER_RESCUE_SKIP_TIERS names it,
            # which is measured NEGATIVE (4ar) and is not the default.
            _extra = [m for m in _extra if (p, m) not in _hard_mc]
        if ALLOWABLE_RESCUE and _extra:
            if (_place(p, gt, _extra, grp, EARLY_CAP_H)
                    or _place(p, gt, _extra, grp, float("inf"))
                    or (MAKEROOM and _make_room(p, gt, _extra, grp))):
                continue
        # SPLIT AT THE FLOOR ON A PLANT-CALIBRATED BUDGET (B12).
        # Split-before-starve rescues volume, but every halving can produce a run
        # below min_lot. The plant does that itself 14 % (PCR) / 31 % (TBR) of the
        # time, so refusing outright is stricter than the plant and cost 30,615
        # tyres; allowing it freely is looser than the plant. Spend the plant's
        # own sub-floor count, then refuse -- see SUBFLOOR_BUDGET.
        # FLOOR-AWARE SPLIT POINT, NOT A BLIND HALVING.
        # Build slices are a fixed quantum -- PCR 48 tyres at p25, p50 AND p75,
        # TBR 29 -- so a run is an integer number of slices and the split point
        # can only land on a slice boundary. The median PCR run is 6 slices =
        # 288 tyres, and halving it gives 144 + 144 against a 150 floor: BOTH
        # halves breach, by 6 tyres. A blind midpoint therefore manufactures two
        # sub-floor runs where an uneven 4 + 2 split (192 + 96) makes only one,
        # halving what each rescue costs the B12 budget. 59 % of all starvation
        # (13,833 PCR tyres) arrived through this door.
        # Search split points outward from the middle and take the first that
        # keeps BOTH sides at or above the floor; failing that, the one that
        # keeps the LARGER side compliant so only the remainder is charged.
        _fl = float(floor_units.get(p, 0))
        _mid = len(grp) // 2
        if len(grp) > 1:
            _pre = np.cumsum([d["qty"] for d in grp])
            _tot = float(_pre[-1])
            _order = sorted(range(1, len(grp)),
                            key=lambda i: (abs(i - len(grp) / 2.0), i))
            _both = [i for i in _order
                     if float(_pre[i - 1]) >= _fl and _tot - float(_pre[i - 1]) >= _fl]
            if _both:
                _mid = _both[0]                  # a legal split exists
            else:
                _one = [i for i in _order
                        if max(float(_pre[i - 1]), _tot - float(_pre[i - 1])) >= _fl]
                if _one:
                    _mid = _one[0]               # only the remainder is sub-floor
        _breach = len(grp) > 1 and min(
            sum(d["qty"] for d in grp[:_mid]),
            sum(d["qty"] for d in grp[_mid:])) < _fl
        if not _breach or NO_FLOOR:
            _halves_ok = len(grp) > 1
        elif HARD_FLOOR:
            _halves_ok = False
        else:                                   # budgeted: charge one setup
            _halves_ok = _subfloor_spent[p] < SUBFLOOR_BUDGET.get(p, 0)
            if _halves_ok:
                _subfloor_spent[p] += 1
        if _halves_ok:
            for _half in (grp[:_mid], grp[_mid:]):
                heapq.heappush(heap, (_prio(p, gt, _half), _hkey(_half[0]["t_cure"], gt, p), sc, _seq, p, gt,
                                      cand, _half))
                _seq += 1
            continue
        # ATOMIC SLICE -- ONE HALVING, CHARGED TO THE SAME BUDGET.
        # Split-before-starve terminated at `len(grp) == 1`: a run that is a
        # SINGLE slice has no boundary to cut on, so it went straight to
        # `no feasible release`. That is where the volume was going -- 27,203
        # PCR tyres in August against a sub-floor budget of 180 that had spent
        # about 9. The budget was not binding; the geometry was.
        # A slice is a delivery to a press, not a physical indivisible unit
        # (PARTITION §4f), so it may be cut. The halves carry a DEPTH COUNTER and
        # stop at SPLIT_DEPTH, which bounds the recursion and keeps the fragment
        # size honest. Charged to the same plant-calibrated B12 budget.
        #
        # WHY DEPTH IS A KNOB (measured 2026-08-14). Depth was hard-wired at 1.
        # One cut of a ~155-tyre PCR slice gives ~77 tyres = ~1.6 h, and the
        # fragmented runs' largest in-window shard is 1.46 h p50 -- so the halves
        # still do not fit and the volume still starves. The binding constraint
        # was never the budget (July PCR spent ~11 of 180); it is how small a
        # piece the geometry is allowed to reach. SPLIT_MIN floors the fragment
        # in TYRES so depth can be raised without shredding a run to dust.
        _dep = int(grp[0].get("_splitn", 0))
        if (p in ATOMIC_SPLIT_PLANTS and len(grp) == 1
                and _dep < SPLIT_DEPTH
                and not HARD_FLOOR
                and _subfloor_spent[p] < SUBFLOOR_BUDGET.get(p, 0)):
            _q = int(grp[0]["qty"])
            _a, _b = _q // 2, _q - _q // 2
            if _a >= max(1, SPLIT_MIN) and _b >= max(1, SPLIT_MIN):
                _subfloor_spent[p] += 1
                for _qq in (_a, _b):
                    _d2 = dict(grp[0])
                    _d2["qty"] = float(_qq)      # integral -- never fractional
                    _d2["_splitn"] = _dep + 1
                    heapq.heappush(heap, (_prio(p, gt, [_d2]), _hkey(_d2["t_cure"], gt, p), sc, _seq, p, gt,
                                          cand, [_d2]))
                    _seq += 1
                continue
        if DIAG:
            _gq = sum(d["qty"] for d in grp)
            # IS THE FREE TIME THERE, AND IS IT IN ONE PIECE? The refusal only
            # says "no machine took it". Measure, per candidate, the LARGEST
            # CONTIGUOUS free window inside [t0, ideal] and compare it with the
            # run's own duration. free_h >> dur_h with best_gap_h < dur_h is
            # fragmentation, not capacity -- and fragmentation has a repair.
            _bg, _fr, _cap_free = 0.0, 0.0, 0.0
            _bg5, _w5, _nfit5 = 0.0, 0.0, 0
            # CUMULATIVE in-window free, alongside the largest single shard.
            # `r5_best_gap_h < dur_h` proves the run does not fit in ONE piece.
            # It does NOT prove the hours are absent -- and the two have opposite
            # fixes. Only `r5_free_in_window_h >= dur_h` says a non-contiguous
            # placement would have somewhere to go; if the cumulative is short
            # too, the window is genuinely full and defragmenting buys nothing.
            # Measure before building the repair.
            _cum5, _nfitc5, _ngap5 = 0.0, 0, 0
            for _m in (_lk or cand):
                _c = _cad(p, _m, gt)
                _cums, _a = [], 0.0
                for _d in grp:
                    _a += _d["qty"]
                    _cums.append(_a)
                _idl = min(_d["t_cure"] - timedelta(hours=tau_rel[p])
                           - timedelta(seconds=_cu * _c)
                           for _d, _cu in zip(grp, _cums))
                if _idl <= t0:
                    continue
                # THE LEGAL BAND IS NOT [t0, ideal]. R5 forbids building a tyre
                # more than 72 h before its cure, so `st` is also bounded BELOW:
                #   st >= max_j (t_cure_j - 72 h - cums_j x c)
                # The band is therefore ~65-70 h wide no matter how long the
                # month is, and month-wide idle hours say nothing about whether
                # this run can be placed.
                _lo = max([t0] + [_d["t_cure"] - timedelta(hours=GT_SHELF_LIFE_H)
                                  - timedelta(seconds=_cu * _c)
                                  for _d, _cu in zip(grp, _cums)])
                for _a0, _lbl in ((t0, "all"), (_lo, "r5")):
                    if _idl <= _a0:
                        continue
                    _iv = sorted((s2, e2) for (s2, e2, _g2, _r2) in busy.get(_m, [])
                                 if e2 > _a0 and s2 < _idl)
                    _prev, _gaps = _a0, []
                    for s2, e2 in _iv:
                        if s2 > _prev:
                            _gaps.append((s2 - _prev).total_seconds() / 3600.0)
                        _prev = max(_prev, e2)
                    if _idl > _prev:
                        _gaps.append((_idl - _prev).total_seconds() / 3600.0)
                    _mx = max(_gaps) if _gaps else 0.0
                    if _lbl == "all":
                        _bg = max(_bg, _mx)
                        _fr = max(_fr, sum(_gaps))
                        _cap_free = max(_cap_free,
                                        (_idl - t0).total_seconds() / 3600.0)
                    else:
                        _bg5 = max(_bg5, _mx)
                        _w5 = max(_w5, (_idl - _a0).total_seconds() / 3600.0)
                        # `_c` is this machine's cadence, so the need is per
                        # machine -- never compare one machine's gap with
                        # another's duration.
                        _nd = _gq * _c / 3600.0
                        _sm = sum(_gaps)
                        _nfit5 += int(_mx >= _nd)
                        _nfitc5 += int(_sm >= _nd)
                        if _sm > _cum5:
                            _cum5, _ngap5 = _sm, len(_gaps)
            _diag_last["best_gap_h"] = _bg
            _diag_last["free_h"] = _fr
            _diag_last["window_h"] = _cap_free
            _diag_last["r5_best_gap_h"] = _bg5
            _diag_last["r5_window_h"] = _w5
            _diag_last["r5_n_machines_fit"] = _nfit5
            _diag_last["r5_free_in_window_h"] = _cum5
            _diag_last["r5_n_gaps"] = _ngap5
            _diag_last["r5_n_fit_cum"] = _nfitc5
            _diag_rows.append({
                "plant": p, "gt_code": gt, "qty": float(_gq),
                "n_slices": len(grp),
                # `t_due` on the heap is the composite ordering key returned by
                # _hkey, not a timestamp.  Record the actual cure window so the
                # failed placement can be attributed to the month boundary.
                "cure_first": grp[0]["t_cure"],
                "cure_last": grp[-1]["t_cure"],
                "floor": float(floor_units.get(p, 0)),
                "n_cand": len(cand), "n_lock": len(_lk),
                "splittable": bool(len(grp) > 1 and not _breach),
                "hdr_2xfloor": bool(_gq >= 2 * float(floor_units.get(p, 0))),
                "before_t0": int(_diag_last.get("before_t0", 0)),
                "t0_short_h": float(_diag_last.get("t0_short_h", 0.0)),
                "r5": int(_diag_last.get("r5", 0)),
                "r5_worst_h": float(_diag_last.get("r5_worst_h", 0.0)),
                "wip_rail": int(_diag_last.get("wip_rail", 0)),
                "early_cap": int(_diag_last.get("early_cap", 0)),
                "floor_gate": int(_diag_last.get("floor", 0)),
                "ideal_slack_h": float(_diag_last.get("ideal_slack_h", -1e9)),
                "dur_h": float(_diag_last.get("dur_h", 0.0)),
                "best_gap_h": float(_diag_last.get("best_gap_h", 0.0)),
                "free_h": float(_diag_last.get("free_h", 0.0)),
                "window_h": float(_diag_last.get("window_h", 0.0)),
                "r5_best_gap_h": float(_diag_last.get("r5_best_gap_h", 0.0)),
                "r5_window_h": float(_diag_last.get("r5_window_h", 0.0)),
                "r5_n_fit": int(_diag_last.get("r5_n_machines_fit", 0)),
                # cumulative free inside the R5 band, and how many shards it is
                # split into. r5_n_fit==0 with r5_n_fit_cum>0 is the exact
                # signature of a run that a non-contiguous placement could seat.
                "r5_free_in_window_h": float(
                    _diag_last.get("r5_free_in_window_h", 0.0)),
                "r5_n_gaps": int(_diag_last.get("r5_n_gaps", 0)),
                "r5_n_fit_cum": int(_diag_last.get("r5_n_fit_cum", 0)),
            })
        _rsn = ("would breach min_lot" if len(grp) > 1 else "no feasible release")
        if STRICT_FLOOR and sum(d["qty"] for d in grp) < float(
                floor_units.get(p, 0)):
            # Named separately so the price of the strict rule is never mixed in
            # with genuine capacity starvation.
            _rsn = "below min_lot (strict B12)"
        # ---- C: HOLD SUB-FLOOR REMAINDERS FOR POOLING --------------------
        # A group rejected for being under the B12 floor is not necessarily
        # unbuildable: several such remainders of the SAME GT often sit within
        # one shelf-life window, and pooled they CLEAR the floor. Measured July:
        # 163 tyres on PCR, 719 on TBR are poolable this way; the other 2,656 have
        # seats more than 72 h apart and no pooling can reach them.
        #
        # This SATISFIES B12 rather than relaxing it -- the merged run is a legal
        # run. Nothing sub-floor is ever placed.
        if POOL_TAILS and "min_lot" in _rsn:
            _pool_hold.setdefault((p, gt), []).append((grp, cand))
            continue
        # `reason` keeps its three historical values so nothing reading it moves;
        # `cause` is the attributed rule and the counters behind it.
        _cz = _starve_cause(_diag_last)
        _snap = {"before_t0": int(_diag_last.get("before_t0", 0) or 0),
                 "r5": int(_diag_last.get("r5", 0) or 0),
                 "wip_rail": int(_diag_last.get("wip_rail", 0) or 0),
                 "early_cap": int(_diag_last.get("early_cap", 0) or 0),
                 "t0_short_h": float(_diag_last.get("t0_short_h", 0.0) or 0.0),
                 "r5_worst_h": float(_diag_last.get("r5_worst_h", 0.0) or 0.0),
                 "n_cand": len(cand)}
        for _d in grp:
            starved.append({"plant": p, "gt_code": gt,
                            "press": _d["press"], "qty": _d["qty"],
                            "reason": _rsn, "cause": _cz,
                            "t_cure": _d["t_cure"], **_snap})

    # ---- C: THE POOLING PASS -------------------------------------------
    if POOL_TAILS and _pool_hold:
        _pooled_ok = _pooled_q = 0
        for (p, gt), items in sorted(_pool_hold.items()):
            _fl = float(floor_units.get(p, 0))
            _all = [d for grp, _c in items for d in grp]
            _all.sort(key=lambda d: d["t_cure"])
            _q = sum(d["qty"] for d in _all)
            _span = ((_all[-1]["t_cure"] - _all[0]["t_cure"]).total_seconds() / 3600
                     if len(_all) > 1 else 0.0)
            # one run can only feed seats inside ONE shelf-life window
            if _q >= _fl and _span <= GT_SHELF_LIFE_H:
                _cand = items[0][1]
                if (_place(p, gt, _cand, _all, EARLY_CAP_H)
                        or _place(p, gt, _cand, _all, float("inf"))):
                    _pooled_ok += 1
                    _pooled_q += _q
                    continue
            for grp, _c in items:
                for _d in grp:
                    starved.append({"plant": p, "gt_code": gt,
                                    "press": _d["press"], "qty": _d["qty"],
                                    "reason": "would breach min_lot",
                                    "cause": "below_min_lot",
                                    "t_cure": _d["t_cure"],
                                    "before_t0": 0, "r5": 0, "wip_rail": 0,
                                    "early_cap": 0, "t0_short_h": 0.0,
                                    "r5_worst_h": 0.0, "n_cand": len(_c)})
        if _pooled_ok:
            print(f"  POOL-TAILS: {_pooled_ok} same-GT remainder groups merged "
                  f"into legal runs, {_pooled_q:,.0f} tyres recovered "
                  f"(floor never breached)")

    _ = _pool_hold
    for _m, _rs in placed.items():
        for _r in _rs:
            slices.extend(_rows_of(_r))
    if MAKEROOM and _mr_fail:
        print("  MAKE-ROOM bail reasons: " + "  ".join(
            f"{k}={v}" for k, v in sorted(_mr_fail.items())))
        if _mr_gap:
            import numpy as _np
            _g = _np.array([x[0] for x in _mr_gap])
            _d = _np.array([x[1] for x in _mr_gap])
            print(f"  MAKE-ROOM shortfall h p50 {_np.median(-_g):.2f} "
                  f"vs dur p50 {_np.median(_d):.2f}")
    if MAKEROOM and _mr_used:
        print("\n  MAKE-ROOM (compact-and-insert) runs rescued: "
              + "  ".join(f"{k} {v}" for k, v in sorted(_mr_used.items())))
    bs = pl.DataFrame(slices)
    st_df = (pl.DataFrame(starved, strict=False, infer_schema_length=None)
             if starved else pl.DataFrame(
                 schema={"plant": pl.Utf8, "gt_code": pl.Utf8, "qty": pl.Float64,
                         "reason": pl.Utf8, "cause": pl.Utf8}))

    # ---- THE RUN, AS AN EMITTED COLUMN ----------------------------------
    # A run is a maximal consecutive same-GT block on one machine. It is what
    # the plant sets up for and what B12's floor is about, so it must be in the
    # file the planner reads -- not recomputed by every consumer with its own
    # idea of the boundary. OPENING_STOCK is not a machine and gets no run.
    _fresh = bs.filter(pl.col("machine") != "OPENING_STOCK").sort(
        ["plant", "machine", "start_ts"])
    _fresh = _fresh.with_columns(
        ((pl.col("gt_code") != pl.col("gt_code").shift(1).over(["plant", "machine"]))
         .fill_null(True)).alias("_new"))
    _fresh = _fresh.with_columns(
        (pl.col("plant") + "|" + pl.col("machine") + "|"
         + pl.col("_new").cum_sum().over(["plant", "machine"]).cast(pl.Utf8)
         ).alias("run_id")).drop("_new")
    bs = pl.concat([
        _fresh,
        bs.filter(pl.col("machine") == "OPENING_STOCK")
          .with_columns(pl.lit(None, dtype=pl.Utf8).alias("run_id")),
    ], how="vertical").sort(["plant", "machine", "start_ts"])

    # ================= CLOSING GT BUFFER (PLANNER_L7_CLOSING_BUFFER) ========
    # THE MONTH IS NOT SELF-CONSISTENT WITHOUT THIS, AND THE NUMBERS PROVE IT:
    #
    #                opening GT   closing (carry-forward)   delta
    #     Jul PCR         4,820                       161  -4,659
    #     Aug PCR         4,847                     2,345  -2,502
    #
    # July inherits 4,820 tyres of floor stock and hands August 161. Part of its
    # fulfilment is therefore paid for with inventory it never replaces -- run
    # the same plan twice and the second month starts with nothing. The plant
    # does not work that way: its opening stock is ~4,800 every month because it
    # ends each month with a floor it rebuilt.
    #
    # So: after the pull release, top the closing balance back up to the OPENING
    # level, GT for GT, using machine time that is genuinely idle in the last
    # 72 h. These tyres are real -- real machines, real hours, inside the month --
    # and they cure next month, which is exactly what carry-forward means.
    #
    # WHAT IT DOES NOT DO, and this must stay true:
    #   * it does NOT raise this month's CURED output. Those tyres cure next
    #     month. in-month fulfilment is untouched by construction.
    #   * it does NOT invent stock. The build is scheduled on an allowable
    #     machine, in a free window, at the GT's own cadence.
    #   * it does NOT breach R5. Built inside the last 72 h, cured next month,
    #     so age at hand-off is < 72 h by construction (asserted below).
    #   * it does NOT breach B12. A top-up run below the lot floor is skipped,
    #     not shrunk.
    # It raises BUILT, and it raises next month's opening stock. Report it on the
    # build line and on the hand-off, never as fulfilment.
    if CLOSING_BUFFER:
        _win = _mend - timedelta(hours=GT_SHELF_LIFE_H - R5_SAFETY_H)
        _open_q: dict = {}
        _open_q = dict(_opening0)
        # ---- CLOSING MIX: COVER THE NEXT MONTH'S COLD PRESSES ---------------
        # (PLANNER_L7_CLOSING_MIX = inherit | cover;  inherit = the old default)
        #
        # THE DEFECT. `dict(_opening0)` targets the mix this month INHERITED --
        # ~50 GTs at ~90 tyres each. Every per-GT deficit is therefore sub-floor
        # BY CONSTRUCTION, so the `_def < _floor` test below refuses it and the
        # buffer builds almost nothing useful. Measured on August: 21 PCR GTs
        # carried a deficit totalling 2,240 tyres and 15 of them (1,008 tyres)
        # were under the 150 floor. Deficit quantiles [17, 47, 84, 156, 181].
        # Hours were never the constraint -- 153 idle PCR machine-hours sit in
        # the same window, ~11,000 tyres of build capacity against a 2,240 ask.
        # The explicit CLOSING_GT_* target cannot fix it either: it only SCALES
        # the same mix, so it spreads thinner rather than concentrating.
        #
        # WHAT THE STOCK IS ACTUALLY FOR. Next month's cold presses -- the ones
        # NOT mid-campaign at t0 -- each need tau*+band hours of cover before
        # they can start. A press that IS mid-campaign needs none. So the target
        # is not "the mix we inherited", it is "one seat of cover per cold press,
        # on a GT that press can actually run".
        #
        # MOUNTED MOULD FIRST, THEN CHEAPEST SEAT. Measured on the 27 cold PCR
        # presses: keying purely on seat cost fills all 27 but puts only 6 on the
        # mould already mounted, forcing 21 day-1 mould changes = 126 press-h.
        # Keying on (mounted?, cost) fills all 27 with 25 on the mounted mould --
        # 2 changes, 12 press-h. 60 extra tyres of cover buys back 114 press-h,
        # about 719 tyres of cure. The rule cannot even be expressed without the
        # full press-state snapshot (idle presses keep their mould), which is why
        # this ships after that change and not before.
        if CLOSING_MIX in ("cover", "both"):
            _nm = (f"{int(a.month[:4]) + (int(a.month[5:7]) == 12)}-"
                   f"{(int(a.month[5:7]) % 12) + 1:02d}")
            _nrf = D / f"net_requirement_{_nm}.parquet"
            _dem_next: set = set()
            if _nrf.exists():
                _nx = pl.read_parquet(_nrf)
                if "residual" in _nx.columns:
                    _nx = _nx.filter(~pl.col("residual"))
                _dem_next = {(r["plant"], r["gt_code"])
                             for r in _nx.iter_rows(named=True)}
            if not _dem_next:
                print(f"  [closing-mix] next month ({_nm}) has no "
                      f"net_requirement -- cannot target cold presses, "
                      f"falling back to the inherited mix")
            else:
                # Each press's LAST campaign: mounted mould, and busy-at-t0.
                _lastc: dict = {}
                for _r in camp.iter_rows(named=True):
                    _k = (_r["plant"], str(_r["press"]))
                    if _k not in _lastc or _r["end_ts"] > _lastc[_k]["end_ts"]:
                        _lastc[_k] = _r
                # Seat cost: tyres one press eats over tau*+band at this GT's
                # own cure rate, read off the campaign that ran it.
                _seat: dict = {}
                for _r in camp.iter_rows(named=True):
                    _k = (_r["plant"], _r["gt_code"])
                    _hr = float(_r["hours"]) or 0.0
                    if _hr > 0:
                        _seat.setdefault(_k, []).append(float(_r["qty"]) / _hr)
                _cover_h = {_p: tau_rel[_p] + float(
                    P["campaign_bands"][_p]["build"]["hours_p50"])
                    for _p in ("PCR", "TBR")}
                _mould_cap: dict = {}
                _cmf = D / f"cap_mould_{a.month}.parquet"
                if _cmf.exists():
                    for _r in pl.read_parquet(_cmf).iter_rows(named=True):
                        _mould_cap[(_r["plant"], _r["gt_code"])] = int(
                            _r.get("moulds") or 0)
                _ask: dict = {}
                _n_cold = {"PCR": 0, "TBR": 0}
                _n_mounted = {"PCR": 0, "TBR": 0}
                _seats: dict = {}
                for (_p, _pr), _r in sorted(_lastc.items()):
                    if _r["end_ts"] > _mend:
                        continue                      # busy at t0 -> needs no cover
                    _n_cold[_p] += 1
                    _mnt = (_p, _r["gt_code"])
                    _cands = [k for k in _dem_next if k[0] == _p
                              and _seat.get(k)
                              and _seats.get(k, 0) < max(_mould_cap.get(k, 99), 1)]
                    if not _cands:
                        continue
                    _cands.sort(key=lambda k: (
                        0 if k == _mnt else 1,
                        _cover_h[k[0]] * (sum(_seat[k]) / len(_seat[k])),
                        k[1]))
                    _pick = _cands[0]
                    if _pick == _mnt:
                        _n_mounted[_p] += 1
                    _seats[_pick] = _seats.get(_pick, 0) + 1
                    _ask[_pick] = _ask.get(_pick, 0.0) + _cover_h[_p] * (
                        sum(_seat[_pick]) / len(_seat[_pick]))
                # Floor every per-GT ask at B12 so the buffer cannot refuse its
                # own target, and cap the plant total at the G8 rail. Print what
                # is dropped -- never silent (DO-NOT: no silent caps).
                # "cover"  = the cold-press ask REPLACES the inherited mix
                # "both"   = per-GT MAX of the two. Measured 2026-08-14: pure
                #   `cover` hands PCR 2,167 tyres where `inherit` hands 4,050,
                #   and day-1 press-hours FELL 714 -> 686. Concentrating the mix
                #   is right, but halving the total is not -- the broad tail of
                #   the inherited mix was doing more work than its sub-floor
                #   per-GT deficits suggested. Keep both and take the larger.
                _prev = dict(_open_q)
                _open_q = {}
                for _k, _v in _ask.items():
                    _open_q[_k] = float(max(_v, float(floor_units.get(_k[0], 0) or 0)))
                if CLOSING_MIX == "both":
                    for _k, _v in _prev.items():
                        _open_q[_k] = max(_open_q.get(_k, 0.0), float(_v))
                for _p in ("PCR", "TBR"):
                    _tot = sum(v for (q, _g), v in _open_q.items() if q == _p)
                    _rail = (CONFIG.thresholds.gt_wip_rail[_p]
                             * CONFIG.thresholds.gt_wip_rail_margin)
                    if _tot > _rail > 0:
                        _k2 = _rail / _tot
                        for _key in list(_open_q):
                            if _key[0] == _p:
                                _open_q[_key] *= _k2
                        print(f"  [closing-mix] {_p}: ask {_tot:,.0f} exceeds "
                              f"rail {_rail:,.0f} -- scaled by {_k2:.3f}, "
                              f"{_tot - _rail:,.0f} tyres of cover DROPPED")
                    print(f"  [closing-mix] {_p}: {_n_cold[_p]} cold presses at "
                          f"{_nm} t0, {_n_mounted[_p]} kept on their mounted "
                          f"mould -> ask {min(_tot, _rail if _rail > 0 else _tot):,.0f} "
                          f"tyres over {sum(1 for k in _open_q if k[0] == _p)} GTs "
                          f"(rail {_rail:,.0f})")
        # ---- EXPLICIT CLOSING TARGET (PLANNER_CLOSING_GT_PCR / _TBR) --------
        # By default the buffer rebuilds the floor GT-for-GT against what the
        # month OPENED with, which perpetuates whatever mix was inherited. An
        # explicit plant target (e.g. 4,500 PCR / 1,200 TBR) scales that mix to
        # the requested total, keeping the per-GT shape but fixing the level.
        # The scale is applied per plant and never above the G8 rail.
        for _pl in ("PCR", "TBR"):
            _t = float(os.environ.get(f"PLANNER_CLOSING_GT_{_pl}", "0") or 0)
            if _t <= 0:
                continue
            _cur = sum(v for (q, _g), v in _open_q.items() if q == _pl)
            if _cur <= 0:
                continue
            _k = min(_t / _cur, CONFIG.thresholds.gt_wip_rail[_pl] / _cur)
            for _key in list(_open_q):
                if _key[0] == _pl:
                    _open_q[_key] = _open_q[_key] * _k
            print(f"  [closing-target] {_pl}: {_cur:,.0f} -> {_t:,.0f} "
                  f"(scale {_k:.3f}, same GT mix)")
        _have: dict = {}
        for _r in (bs.filter((pl.col("machine") != "OPENING_STOCK")
                             & (pl.col("end_ts") <= pl.lit(_mend))
                             & (pl.col("cure_ts") > pl.lit(_mend)))
                     .group_by(["plant", "gt_code"]).agg(pl.col("qty").sum())
                     .iter_rows(named=True)):
            _have[(_r["plant"], _r["gt_code"])] = float(_r["qty"])
        # machine occupancy inside the window, from bs itself -- no shared state
        # THE GT TRAVELS WITH THE INTERVAL (third element). Without it a gap has
        # no idea which GT it abuts, and a changeover cannot be priced -- which
        # is exactly why this pass charged none for the whole project. It costs
        # one string per row and is read only by the BUFFER_SETUP block below,
        # so with the flag off every gap is byte-identical to before.
        _occ: dict = {}
        for _r in bs.filter((pl.col("machine") != "OPENING_STOCK")
                            & (pl.col("end_ts") > pl.lit(_win))).iter_rows(named=True):
            _occ.setdefault(_r["machine"], []).append(
                (max(_r["start_ts"], _win), _r["end_ts"], _r["gt_code"]))
        # HOLIDAY: the closing buffer finds IDLE GAPS and fills them. A shut
        # plant reads as the largest idle gap in the month, so book it here too.
        # (CLOSING_BUFFER ships OFF, so this is correctness, not measurement.)
        # The sentinel GT is what `_setup_s` prices at zero -- a shutdown is not
        # a changeover, so a run may abut a closure with no reservation.
        for _m, _iv in _hol_iv.items():
            for _s2, _e2 in [(x[0], x[1]) for x in _iv]:
                if _e2 > _win:
                    _occ.setdefault(_m, []).append((max(_s2, _win), _e2, _HOL_GT))
        for _m in _occ:
            _occ[_m].sort()
        _add, _built_extra = [], {"PCR": 0.0, "TBR": 0.0}
        for (_p, _g), _want in sorted(_open_q.items(), key=lambda kv: -kv[1]):
            _def = _want - _have.get((_p, _g), 0.0)
            _floor = float(floor_units.get(_p, 0) or 0)
            if _def < _floor:
                continue
            # `_cad` is THE cadence lookup in this module -- do not reach for an
            # L5 variable here. Resolved per machine below so a slow machine is
            # not credited with a fast one's rate.
            for _mach in sorted(elig.get((_p, _g), [])):
                if _def < _floor:
                    break
                # latest free gap on this machine inside the window
                # Each gap carries the GT on its LEFT and on its RIGHT so the
                # changeover at either edge can be priced. `_pg` is the GT of
                # whichever block currently ends at the frontier `_cur`; it is
                # None for the very first gap of the window (nothing precedes
                # it inside the scan) and for the trailing gap's right edge
                # (nothing follows it before month end).
                _iv = _occ.get(_mach, [])
                _cur = _win
                _pg = None
                _gaps = []
                for _a, _b2, _ggt in _iv:
                    if _a > _cur:
                        _gaps.append((_cur, _a, _pg, _ggt))
                    if _b2 >= _cur:
                        _cur, _pg = _b2, _ggt
                if _cur < _mend:
                    _gaps.append((_cur, _mend, _pg, None))
                # ---- RESERVE THE CHANGEOVER AT BOTH EDGES (BUFFER_SETUP) ----
                # See the flag block at the top of the file. This is the whole
                # fix for "machine-day over 24 h" and "changeover time not
                # reserved": both are the same unbooked setup, counted twice.
                # Applied BEFORE the month-end clip so the right edge is priced
                # against the true next occupancy even when that block starts
                # after `_mend` (the carry-out tail).
                if BUFFER_SETUP:
                    # `_setup_s` takes None only on its FIRST argument (it means
                    # "nothing ran before"); a None on the second would fall
                    # through to rim_of.get(None) and silently charge a
                    # different-size changeover to an edge that has no next run.
                    def _edge(_ga2, _gb2):
                        return 0.0 if (_ga2 is None or _gb2 is None) \
                            else _setup_s(_p, _mach, _ga2, _gb2)
                    _gaps = [
                        (_ga + timedelta(seconds=_edge(_lg, _g)),
                         _gb - timedelta(seconds=_edge(_g, _rg)),
                         _lg, _rg)
                        for (_ga, _gb, _lg, _rg) in _gaps]
                # CLIP EVERY GAP TO THE MONTH END. Existing runs can finish
                # AFTER month end (the carry-out tail), so a gap derived from
                # them can start and end in September. Unclipped, the buffer
                # scheduled runs on 1-2 Sep -- outside the month it was meant to
                # close, with cure_ts BEFORE end_ts, which surfaced as 7 R17
                # "cured before minimum ageing" violations.
                _gaps = [(a, min(b, _mend)) for a, b, _lg, _rg in _gaps
                         if a < _mend]
                # LATEST-ENDING GAP FIRST, NOT LARGEST.
                #
                # This buffer exists to hand the NEXT month a stocked floor, so
                # every tyre it builds is consumed after the boundary and its
                # shelf life is spent waiting. Ordering by gap SIZE put top-up
                # runs in the roomiest hole rather than the latest one: measured
                # age at hand-off on the July PCR buffer was p50 11.7 h but max
                # 63.6 h -- a tyre with 8.4 h of R5 left, offered as cover for a
                # press that needs 11.86 h. Only 88 % of the hand-off was legal
                # for a full tau*+band.
                # Building later costs nothing here (the gap is idle either way)
                # and buys shelf life one-for-one, so this is strictly dominant.
                # Judge it on carry_forward_gt age_h, not on BUILT: if BUILT
                # moves at all, the gap search is doing something undocumented.
                _gaps = sorted(_gaps, key=lambda x: -x[1].timestamp())
                if CLOSING_GAP_ORDER == "size":     # escape hatch for A/B only
                    _gaps = sorted(_gaps, key=lambda x: -(x[1] - x[0]).total_seconds())
                for _gs, _ge in _gaps:
                    _h = (_ge - _gs).total_seconds()
                    if _h <= 0:
                        continue
                    _cd = _cad(_p, _mach, _g)
                    _n = min(_def, _h / _cd)
                    if _n < _floor:
                        continue
                    _n = float(int(_n))
                    # BUILD AT THE END OF THE GAP, NOT THE START. The gap is idle
                    # either way, so shifting the run to abut the NEXT occupancy
                    # costs no machine time and buys shelf life one-for-one. On
                    # the last gap `_ge` is `_mend`, so the tyre is handed over
                    # minutes old instead of hours. Same reasoning as the gap
                    # ordering above -- both are pure R5 margin.
                    if CLOSING_GAP_ORDER != "size":
                        _en = _ge
                        _gs = _ge - timedelta(seconds=_n * _cd)
                    else:
                        _en = _gs + timedelta(seconds=_n * _cd)
                    _add.append({"plant": _p, "gt_code": _g, "machine": _mach,
                                 "press": None, "start_ts": _gs, "end_ts": _en,
                                 "qty": _n,
                                 "cure_ts": _mend + timedelta(hours=1.0),
                                 # WAIT IS TO THE NOTIONAL CURE, NOT THE BOUNDARY.
                                 # Using (month_end - end_ts) reported the age at
                                 # HAND-OFF as the cure wait, so a run finishing
                                 # inside tau_min of 07:00 read as cured before
                                 # minimum ageing: R17 went 0 -> 3 violations on
                                 # July PCR. cure_ts is month_end + 1 h, so the
                                 # wait is measured to that and is >= 1 h always.
                                 "wait_h": ((_mend + timedelta(hours=1.0))
                                            - _en).total_seconds() / 3600.0,
                                 "run_id": bs["run_id"][0] if bs.height else ""})
                    _occ.setdefault(_mach, []).append((_gs, _en, _g))
                    _occ[_mach].sort()
                    # ---- ANY PATH THAT APPENDS TO `bs` MUST WRITE `busy` -----
                    # PARTITION_AND_CHANGEOVER.md 4av, the seventh always-passing
                    # guard found in this project. `busy` is populated only in
                    # `_place` and `_make_room`, and BOTH of L7's own feasibility
                    # gates below iterate it -- so for the whole life of the
                    # closing buffer they printed
                    #     machine double-booking          : 0  PASS
                    #     setup not reserved (changeover) : 0 of N  PASS
                    # while the exported pack carried different-size changeovers
                    # at a 0.0 min gap on TBMPCR10Stage2 and others, and only the
                    # independent verifier ever saw them.
                    # Written UNCONDITIONALLY, not under BUFFER_SETUP: nothing
                    # downstream of here reads `busy` except the gates, so this
                    # changes what is REPORTED and cannot change a single tyre.
                    # With BUFFER_SETUP off the gates now FAIL, which is the
                    # true state of the plan they are grading.
                    busy.setdefault(_mach, []).append(
                        (_gs, _en, _g, rim_of.get(_g, "")))
                    _def -= _n
                    _built_extra[_p] += _n
                    if _def < _floor:
                        break
        if _add:
            bs = pl.concat([bs, pl.DataFrame(_add, schema=bs.schema)],
                           how="vertical").sort(["plant", "machine", "start_ts"])
            print(f"  [closing-buffer] {len(_add)} top-up runs · "
                  f"PCR +{_built_extra['PCR']:,.0f} · TBR +{_built_extra['TBR']:,.0f} "
                  f"tyres built in the last {GT_SHELF_LIFE_H - R5_SAFETY_H:.0f} h "
                  f"and handed to next month")

    # ---- PRE-t0 CLAIM REPORT (PLANNER_L7_PRE_T0_H) ------------------------
    # The window's whole cost is machine hours the PREVIOUS month has to be able
    # to give us. Print the claim per plant AND per machine so it can be checked
    # against the previous month's own plan before anyone quotes the recovery.
    # Printed, not persisted -- runs/<arm>/log_l7_pull_release.txt is the record.
    if _t0b < t0:
        _pre = bs.filter((pl.col("machine") != "OPENING_STOCK")
                         & (pl.col("start_ts") < pl.lit(t0)))
        if _pre.height:
            _u = _pre.group_by(["plant", "machine", "gt_code",
                                "start_ts", "end_ts"]).agg(pl.col("qty").sum())
            print("  [pre-t0] MACHINE HOURS BORROWED FROM THE PREVIOUS MONTH:")
            for _p in ("PCR", "TBR"):
                _up = _u.filter(pl.col("plant") == _p)
                if not _up.height:
                    continue
                _h = sum((min(r["end_ts"], t0)
                          - r["start_ts"]).total_seconds() / 3600.0
                         for r in _up.iter_rows(named=True))
                _q = float(_up.filter(pl.col("end_ts") <= pl.lit(t0))["qty"].sum())
                print(f"    {_p}  {_h:8.1f} machine-h  {_q:8,.0f} tyres finished "
                      f"before t0 (NOT this month's output)  "
                      f"{_up['machine'].n_unique()} machines")

    bs.write_parquet(run / "build_schedule.parquet")
    # ---- C3: BUILD CARRY-OUT -- the building half of the boundary ----------
    # THE ASYMMETRY THIS FIXES. `carry_out.parquet` hands the next month every
    # press's state, so curing starts warm. NOTHING does the same for building,
    # so every month the building machines start genuinely cold while the presses
    # do not. Measured on July fp1: day-1 cure reaches 10,624 (82 % of interior)
    # while day-1 build reaches 9,385 (74 %) -- building is the laggard at the
    # boundary purely because it has no equivalent of carry-in.
    #
    # `machine_warm_<M>.parquet` already expresses the idea, but it is derived by
    # scripts/derive_machine_warm.py from OPENING-STOCK build timestamps and does
    # not exist for 2026-08 at all. The planner's own build_schedule holds the
    # answer exactly: the last slice on each machine before the boundary IS the
    # run that machine is mid-way through at t0. No MES needed.
    #
    # Read by L5's `_warm` set: a GT whose machine is already mid-run at t0 may
    # cure at t0 + tau_min instead of t0 + tau* + build_band, because its first
    # tyres are on the machine now rather than 11.86 h away.
    _bco = (bs.filter((pl.col("machine") != "OPENING_STOCK")
                      & (pl.col("start_ts") < pl.lit(_mend)))
              .sort(["plant", "machine", "start_ts"]))
    if _bco.height:
        _bco = (_bco.group_by(["plant", "machine"]).agg(
            pl.col("gt_code").last().alias("gt_code"),
            pl.col("end_ts").last().alias("ends"),
            pl.col("qty").last().alias("qty"))
            .with_columns(pl.lit(_mend).alias("carry_from"),
                          (pl.col("ends") > pl.lit(_mend)).alias("busy_at_t0"))
            .sort(["plant", "machine"]))
        _bco.write_parquet(run / "build_carry_out.parquet")
        _bz = int(_bco.filter(pl.col("busy_at_t0")).height)
        print(f"  build carry-out: {_bco.height} machine states "
              f"({_bz} mid-run at the boundary, {_bco.height - _bz} idle with a "
              f"known last GT) -> build_carry_out.parquet")
    st_df.write_parquet(run / "build_starved.parquet")
    if DIAG and _diag_rows:
        # Diagnostic counters are populated incrementally, so a sparse field
        # can first appear as a numeric sentinel and later as text.  Coercion is
        # acceptable here—the file is audit evidence, not a planning input—and
        # prevents the diagnostic mode itself from aborting a valid plan.
        pl.DataFrame(_diag_rows, strict=False,
                     infer_schema_length=None).write_parquet(
                         run / "l7_place_diag.parquet")
    # NEVER SILENT (§12). A rim that left its lock says so, with how much.
    if spill_to:
        print("\n  RIM SPILL USED")
        for (p_, r_), fx in sorted(spill_to.items()):
            used = spill_used_h.get((p_, r_), 0.0)
            bud = spill_budget_h[(p_, r_)]
            print(f"    {p_} {r_:<5} -> {fx:<16} {used:6.1f} h of {bud:5.1f} h "
                  f"({100 * used / max(bud, 1e-9):5.1f}% of budget)")

    print(f"  {'plant':<6}{'slices':>9}{'tyres':>11}{'machines':>10}"
          f"{'slices/campaign':>17}")
    for p in ["PCR", "TBR"]:
        s = bs.filter(pl.col("plant") == p)
        nc = camp.filter(pl.col("plant") == p).height
        if not s.height:
            continue
        print(f"  {p:<6}{s.height:>9,}{int(s['qty'].sum()):>11,}"
              f"{s.filter(pl.col('machine') != 'OPENING_STOCK')['machine'].n_unique():>10}"
              f"{s.height/max(nc,1):>17.1f}")

    # ---- BUILD RUNS -- the object B12's floor is actually about ---------
    floor = floor_units
    runs = (bs.filter(pl.col("machine") != "OPENING_STOCK")
            .group_by(["plant", "machine", "run_id"])
            .agg(pl.col("gt_code").first(),
                 pl.col("qty").sum().alias("run_qty"),
                 pl.col("start_ts").min().alias("t0"),
                 pl.col("end_ts").max().alias("t1"),
                 pl.len().alias("n_slices"))
            .with_columns(((pl.col("t1") - pl.col("t0")).dt.total_seconds()
                           / 3600.0).alias("run_h")))
    print(f"\n  BUILD RUNS  (maximal consecutive same-GT block on one machine)")
    print(f"  {'plant':<6}{'runs':>8}{'qty p50':>10}{'< floor':>10}"
          f"{'hours p50':>11}{'in band':>10}{'slices/run':>12}{'chg/mach-day':>14}")
    for p in ["PCR", "TBR"]:
        rp = runs.filter(pl.col("plant") == p)
        if not rp.height:
            continue
        lo, hi = RUN_BAND_H[p]
        fl = floor.get(p, 0)
        below = rp.filter(pl.col("run_qty") < fl).height
        inb = rp.filter((pl.col("run_h") >= lo) & (pl.col("run_h") <= hi)).height
        bp = bs.filter((pl.col("plant") == p) & (pl.col("machine") != "OPENING_STOCK"))
        mdays = (bp.with_columns(pl.col("start_ts").dt.date().alias("d"))
                 .select(["machine", "d"]).unique().height)
        chg = rp.height - bp["machine"].n_unique()
        print(f"  {p:<6}{rp.height:>8,}{rp['run_qty'].median():>10,.0f}"
              f"{100*below/rp.height:>9.1f}%{rp['run_h'].median():>11.2f}"
              f"{100*inb/rp.height:>9.1f}%{rp['n_slices'].median():>12.0f}"
              f"{chg/max(mdays,1):>9.2f}/{CO_PER_MDAY[p]:.2f}")
        gm = bp.select(["gt_code", "machine"]).unique().group_by("gt_code").len()
        spill = sum(1 for k, v in gt_machines.items()
                    if k[0] == p and len(v) < int(
                        gm.filter(pl.col("gt_code") == k[1])["len"].sum() or 0))
        print(f"         machines/GT p50 {gm['len'].median():.0f} "
              f"(max {gm['len'].max()}) · {spill} GTs spilled past their pin")

    # ---- the measurement that matters: GT wait --------------------------
    print("\n  GT WAIT  (the head -- Phase 0: plant 4.4 / 4.8 h, old engine 7.4 h)")
    print(f"  {'plant':<6}{'p05':>8}{'p50':>8}{'mean':>8}{'p95':>8}{'max':>8}"
          f"{'tau*':>8}{'vs tau*':>10}")
    for p in ["PCR", "TBR"]:
        s = bs.filter(pl.col("plant") == p)
        if not s.height:
            continue
        w = np.array(s["wait_h"], float)
        print(f"  {p:<6}{np.percentile(w,5):>8.2f}{np.percentile(w,50):>8.2f}"
              f"{w.mean():>8.2f}{np.percentile(w,95):>8.2f}{w.max():>8.2f}"
              f"{tau[p]:>8.2f}{100*np.percentile(w,50)/tau[p]-100:>9.0f}%")

    # ---- gates -----------------------------------------------------------
    print("\n  GATES")
    ov = 0
    _mach_of = {}
    for mach, iv in busy.items():
        iv = sorted(iv, key=lambda x: (x[0], x[1]))
        _mach_of[mach] = iv
        for i in range(1, len(iv)):
            if iv[i][0] < iv[i - 1][1]:
                ov += 1
    print(f"    machine double-booking          : {ov}  {'PASS' if ov == 0 else 'FAIL'}")
    # SETUP RESERVATION -- an overlap check is NOT a feasibility check. This
    # counts transitions whose gap is shorter than the changeover that
    # transition physically costs. It must be 0.
    _short, _short_h, _trans = 0, 0.0, 0
    for mach, iv in _mach_of.items():
        _pl = "PCR" if "PCR" in mach else "TBR"
        for i in range(1, len(iv)):
            need = _setup_s(_pl, mach, iv[i - 1][2], iv[i][2])
            if need <= 0:
                continue
            _trans += 1
            gap = (iv[i][0] - iv[i - 1][1]).total_seconds()
            if gap + 1e-6 < need:
                _short += 1
                _short_h += (need - gap) / 3600.0
    print(f"    setup not reserved (changeover) : {_short} of {_trans} transitions"
          f"  {'PASS' if _short == 0 else f'FAIL ({_short_h:.1f} h short)'}")
    # GRADED ON THE FIRST TYRE OF THE SLICE. `wait_h` is cure_ts - end_ts, the
    # LAST tyre off the drum; the first waits the whole slice longer and it is
    # the first that expires. Same defect as the L11 invariant of this name --
    # see the block there for the four-cell table. This gate read PASS on August
    # while 26 TBR tyres were past 72 h.
    late = bs.filter(((pl.col("cure_ts") - pl.col("start_ts"))
                      .dt.total_seconds() / 3600.0) > GT_SHELF_LIFE_H).height
    _late_end = bs.filter(pl.col("wait_h") > GT_SHELF_LIFE_H).height
    print(f"    GT wait > {GT_SHELF_LIFE_H:.0f} h (R5, 1st tyre): {late}  "
          f"{'PASS' if late == 0 else 'FAIL'}"
          f"   (graded at slice end: {_late_end})")
    early = bs.filter(pl.col("wait_h") < 0).height
    print(f"    built AFTER cure (impossible)   : {early}  "
          f"{'PASS' if early == 0 else 'FAIL'}")
    below = {p: bs.filter((pl.col("plant") == p)
                          & (pl.col("wait_h") < tau_min[p])).height
             for p in ["PCR", "TBR"]}
    nb = sum(below.values())
    print(f"    GT wait < tau_min (R17)         : {nb}  "
          f"{'PASS' if nb == 0 else 'WARN -- press starvation risk'}")
    print(f"    slices with no feasible release : {st_df.height}  "
          f"{'PASS' if st_df.height == 0 else 'see build_starved.parquet'}")
    # WHICH RULE REFUSED THEM. Printed because a single "no feasible release"
    # bucket cannot be acted on -- only `machine_busy` means buy capacity, and
    # `release_before_t0` is the cold start, which is a plant question.
    if st_df.height and "cause" in st_df.columns:
        for _p in ("PCR", "TBR"):
            _s = st_df.filter(pl.col("plant") == _p)
            if not _s.height:
                continue
            _b = (_s.group_by("cause")
                  .agg(pl.len().alias("n"), pl.col("qty").sum().alias("q"))
                  .sort("q", descending=True))
            _tot = float(_s["qty"].sum())
            print(f"      {_p} starved {_tot:,.0f} tyres by cause:")
            for _r in _b.iter_rows(named=True):
                print(f"        {_r['cause']:<22}{_r['q']:>8,.0f} tyres "
                      f"({100 * _r['q'] / max(_tot, 1e-9):>5.1f}%) "
                      f"in {_r['n']:>3} slices")

    # ---- inventory -------------------------------------------------------
    ev = pl.concat([
        bs.select([pl.col("plant"), pl.col("gt_code"), pl.col("end_ts").alias("ts"),
                   pl.col("qty").alias("d")]),
        bs.select([pl.col("plant"), pl.col("gt_code"), pl.col("cure_ts").alias("ts"),
                   (-pl.col("qty")).alias("d")])]).sort("ts")
    ev.write_parquet(run / "gt_events.parquet")
    # THE SAWTOOTH PREDICTION, CHECKED AGAINST THE LEDGER.
    # If lots are a time supply then I = sum_g Q_g/2 by construction. Printing
    # the prediction beside the realised balance is the reconciliation that
    # measurement rule 2 demands: if these two diverge, the lot sizing is not
    # doing what the arithmetic says it is, and the interval is not a dial.
    def _time_weighted(e: pl.DataFrame) -> tuple:
        """(mean, daily-mean max) of the stock profile, weighted BY TIME.

        MEAN OVER EVENTS IS NOT MEAN OVER TIME, and the difference is not
        cosmetic. Events cluster where activity is high -- which is exactly where
        stock is high -- so an event-weighted mean is biased upward: measured
        +0.9 % on PCR but **+5.7 % on TBR**. That single discrepancy is what made
        the RAIL/LEDGER MISMATCH assertion fire on TBR on every run: the grid is
        time-weighted, the ledger figure it was compared against was
        event-weighted, so the assertion was comparing two different statistics
        and reporting the difference as a leak.
        Inventory is a stock held over TIME, so time-weighted is the physical
        quantity and the only one to gate or report.
        """
        ts = np.array([(x - t0).total_seconds() / 3600.0 for x in e["ts"]], float)
        bal = np.array(e["bal"], float)
        n_h = int(days * 24)
        idx = np.searchsorted(ts, np.arange(n_h) + 0.5, side="right") - 1
        h = np.where(idx >= 0, bal[np.clip(idx, 0, len(bal) - 1)], 0.0)
        nd = (n_h // 24) * 24
        return float(h.mean()), (float(h[:nd].reshape(-1, 24).mean(axis=1).max())
                                 if nd else float(h.max()))

    print("\n  GT INVENTORY (build - cure, from the released plan; TIME-weighted)")
    print(f"  {'plant':<6}{'predicted':>11}{'mean':>9}{'daymax':>9}{'max':>9}"
          f"{'band':>14}{'rail':>8}")
    for p in ["PCR", "TBR"]:
        e = ev.filter(pl.col("plant") == p).sort("ts").with_columns(
            pl.col("d").cum_sum().alias("bal"))
        if not e.height:
            continue
        _tw_mean, _tw_daymax = _time_weighted(e)
        qg = (runs.filter(pl.col("plant") == p)
              .group_by("gt_code").agg(pl.col("run_qty").mean().alias("q")))
        pred = float(qg["q"].sum()) / 2.0 if qg.height else float("nan")
        th = CONFIG.thresholds
        lo, hi = th.gt_wip_min.get(p, 0), th.gt_wip_max.get(p, 0)
        _rl = float(WIP_RAIL.get(p, 0) or 0)
        print(f"  {p:<6}{pred:>11,.0f}{_tw_mean:>9,.0f}{_tw_daymax:>9,.0f}"
              f"{float(e['bal'].max()):>9,.0f}{f'{lo:,}-{hi:,}':>14}"
              f"{_rl:>8,.0f}{'  OVER' if _rl and _tw_daymax > _rl else ''}")
        # RECONCILIATION: the rail's grid and the reported ledger must be the
        # same quantity. A constraint blind to a phase-1 contribution (opening
        # stock) once made the effective ceiling 8,990 against a stated 4,800;
        # asserting the two agree is what stops that recurring in another form.
        # Compare over the HORIZON ONLY -- the grid runs 72 h past it to hold
        # carry-out cures, and averaging the empty tail drags its mean down.
        # BOTH SIDES MUST BE TIME-WEIGHTED. Comparing the time-weighted grid
        # against an event-weighted ledger mean made this fire on TBR every run
        # at -5 %, which was the weighting bias, not a leak. See _time_weighted.
        _g = float(np.cumsum(inv_grid[p])[:int(days * 24)].mean())
        if abs(_g - _tw_mean) > 0.05 * max(_tw_mean, 1.0):
            print(f"    ** RAIL/LEDGER MISMATCH {p}: grid mean {_g:,.0f} vs "
                  f"ledger mean {_tw_mean:,.0f} "
                  f"({100*(_g-_tw_mean)/max(_tw_mean,1):+.0f}%) -- the rail is "
                  f"not measuring what the report measures")
    # ---- RECONCILE THE CURE PLAN TO WHAT BUILDING ACTUALLY FEEDS ---------
    # L5 places cure without knowing the build constraint, so it can promise more
    # than L7 can release. A cure campaign with no build behind it is not a plan;
    # it is press starvation waiting to happen on the floor.
    # The architecture's rule is "if infeasible, reshape at L5, never patch
    # downstream" -- so rather than shipping an inconsistent pair, the cure plan
    # is trimmed to the fed quantity and the honest fulfilment is reported.
    # FED QUANTITY IS ALLOCATED, NOT BROADCAST.
    # `fed` is one row per (plant, gt_code, press); `camp` is not -- 75 of 346
    # keys carry more than one campaign. A plain left join therefore handed EVERY
    # campaign on a key the FULL fed quantity, and `min(qty, fed_qty)` counted it
    # once per campaign: TBR reported 95,411 fed against 94,962 tyres that exist,
    # 449 of them supported by no build row at all. That is measurement rule 2 --
    # two routes to one quantity, and the downstream one is the one nobody checks.
    # Allocate FIFO by campaign start instead, so the key's fed quantity is
    # consumed exactly once.
    fed = (bs.group_by(["plant", "gt_code", "press"])
           .agg(pl.col("qty").sum().alias("fed_qty")))
    rec = (camp.join(fed, on=["plant", "gt_code", "press"], how="left")
           .with_columns(pl.col("fed_qty").fill_null(0.0))
           .sort(["plant", "gt_code", "press", "start_ts"]))
    rec = rec.with_columns(
        (pl.col("qty").cum_sum().over(["plant", "gt_code", "press"])
         - pl.col("qty")).alias("_claimed"))
    rec = rec.with_columns(
        pl.min_horizontal(
            pl.col("qty"),
            (pl.col("fed_qty") - pl.col("_claimed")).clip(lower_bound=0.0),
        ).alias("qty_fed"))
    rec = rec.with_columns(
        (pl.col("qty") - pl.col("qty_fed")).alias("qty_unfed")).drop("_claimed")
    # ---- FULFILMENT IS IN-MONTH OUTPUT ------------------------------------
    # DEFECT FIXED 2026-08-09. `qty_fed` had NO horizon clip, so a campaign that
    # starts on day 30 and finishes in the next month contributed its WHOLE
    # quantity to this month's fulfilment. A tyre cured on 5 August does not
    # satisfy July demand, and the denominator (`gross_build`) is strictly this
    # month's requirement -- so numerator and denominator covered different
    # periods. Measured overstatement:
    #     July  PCR 94.99 -> 94.57 %  (1,687 tyres, 17 campaigns cross)
    #           TBR 95.17 -> 94.22 %  (929 tyres, 7 campaigns)
    #     Aug   PCR 91.67 -> 90.64 %  (4,219 tyres, 39 campaigns)
    #           TBR 92.72 -> 92.59 %  (104 tyres, 2 campaigns)
    #
    # OPENING STOCK IS NOT CLIPPED AND MUST NOT BE. A green tyre built last month
    # and cured this month is genuine output against THIS month's demand -- that
    # is what the buffer is for, and the plant runs the same way. It is separated
    # in the KPI sheet as "consumed opening stock" so it is never mistaken for
    # production, but it belongs in the fulfilment numerator.
    #
    # The ROWS stay. A campaign starting on day 30 really does occupy its press
    # into next month, and deleting it would let the next month double-book that
    # press. Keep the commitment, count only the output.
    _hzn = t0 + timedelta(days=days)      # end of the PLANT month, 07:00


    _dur = (pl.col("end_ts") - pl.col("start_ts")).dt.total_seconds()
    rec = rec.with_columns(
        pl.when(pl.col("end_ts") <= pl.lit(_hzn)).then(1.0)
        .when(pl.col("start_ts") >= pl.lit(_hzn)).then(0.0)
        .otherwise((pl.lit(_hzn) - pl.col("start_ts")).dt.total_seconds()
                   / _dur.clip(lower_bound=1.0))
        .alias("frac_in_month"))
    rec = rec.with_columns([
        (pl.col("qty_fed") * pl.col("frac_in_month")).round(0)
        .alias("qty_fed_in_month"),
        (pl.col("end_ts") > pl.lit(_hzn)).alias("carry_out"),
    ])
    rec.write_parquet(run / "cure_campaigns_reconciled.parquet")

    print("\n  RECONCILED PLAN  (cure trimmed to what building actually feeds)")
    print(f"  {'plant':<6}{'cure placed':>13}{'cure fed':>12}{'unfed':>10}{'% fed':>8}")
    for p in ["PCR", "TBR"]:
        r2 = rec.filter(pl.col("plant") == p)
        if not r2.height:
            continue
        q = float(r2["qty"].sum())
        fq = float(r2["qty_fed"].sum())
        print(f"  {p:<6}{q:>13,.0f}{fq:>12,.0f}{q - fq:>10,.0f}"
              f"{100 * fq / max(q, 1):>7.1f}%")
    tp = float(rec["qty"].sum())
    tf = float(rec["qty_fed"].sum())
    print(f"  {'TOTAL':<6}{tp:>13,.0f}{tf:>12,.0f}{tp - tf:>10,.0f}"
          f"{100 * tf / max(tp, 1):>7.1f}%")
    # ---- CONSERVATION IS AN ASSERTION, NOT A REPORT ---------------------
    # Every tyre fed to a press must be a tyre that exists: a slice built on a
    # machine, or a tyre drawn from opening stock. Reporting this was not enough
    # -- TBR shipped 449 phantom tyres for a full release cycle because the
    # number was computed on a different path from the schedule that produced it.
    bad = []
    for p in ["PCR", "TBR"]:
        supplied = float(bs.filter(pl.col("plant") == p)["qty"].sum())
        claimed = float(rec.filter(pl.col("plant") == p)["qty_fed"].sum())
        if claimed > supplied + 0.5:
            bad.append(f"{p}: {claimed:,.0f} fed vs {supplied:,.0f} supplied "
                       f"(+{claimed - supplied:,.0f} phantom)")
    print("\n  CONSERVATION")
    print(f"    fed <= supplied                 : {len(bad)} breaches  "
          f"{'PASS' if not bad else 'FAIL'}")
    if bad:
        for b in bad:
            print(f"      {b}")
        raise SystemExit("L7 CONSERVATION FAILED -- refusing to emit a plan that "
                         "cures tyres no build row supports")

    reqf = D / f"net_requirement_{a.month}.parquet"
    if reqf.exists():
        req = pl.read_parquet(reqf).filter(~pl.col("residual"))
        need = float(req["gross_build"].sum())
        tfi = float(rec["qty_fed_in_month"].sum())
        print(f"\n  TRUE FULFILMENT vs plannable demand (IN-MONTH output): "
              f"{tfi:,.0f} of {need:,.0f} = {100 * tfi / max(need, 1):.1f}%")
        print(f"    incl. carry-out tail (NOT this month's output): "
              f"{tf:,.0f} = {100 * tf / max(need, 1):.1f}%  "
              f"-- difference {tf - tfi:,.0f} tyres cured after month end")
        for p in ["PCR", "TBR"]:
            rp = rec.filter(pl.col("plant") == p)
            npd = float(req.filter(pl.col("plant") == p)["gross_build"].sum())
            if npd <= 0:
                continue
            _op = float(bs.filter((pl.col("plant") == p)
                                  & (pl.col("machine") == "OPENING_STOCK"))["qty"].sum())
            print(f"    {p}: in-month {float(rp['qty_fed_in_month'].sum()):>9,.0f} "
                  f"({100*float(rp['qty_fed_in_month'].sum())/npd:5.2f}%) · "
                  f"carry-out tail {float(rp['qty_fed'].sum()-rp['qty_fed_in_month'].sum()):>7,.0f} · "
                  f"opening stock consumed {_op:>6,.0f}")

    # ---- OPENING STOCK THAT EXPIRES UNUSED -- REPORTED, NEVER SILENT -----
    #
    # WHAT THIS DOES / WHY IT EXISTS -- a measured defect, found 2026-08-21
    #   The line above says how much opening stock was CONSUMED and stops there.
    #   The gap between what the month opened with and what it drew was never
    #   named anywhere: August PCR 5,132 held / 3,453 drawn / **1,679 expired**,
    #   TBR 1,266 / 794 / **472**; July PCR 4,820 / 3,951 / **869**, TBR 1,297 /
    #   855 / **442**. 3,462 already-built tyres over two months, none of them
    #   aged out at t0 (August age p50 6-15 h, max 55.9 h, ZERO rows over 72 h).
    #   They are legal on arrival and die because their GT's first cure campaign
    #   is 11-30 days away.
    #
    #   THIS PROJECT ALREADY HAD A LINE FOR IT AND THE LINE COULD NOT FIRE. The
    #   guard 90 lines below printed when `_og_tot - _og_used > 0.5`, where
    #   `_og_tot` is the DECREMENTED residual (i.e. the unused stock itself) and
    #   `_og_used` is the consumption. It therefore asked "is the leftover bigger
    #   than the amount drawn?" and answered no on every plant-month the project
    #   has ever run -- 1,679 vs 3,453 on August PCR. Fifth always-passing guard
    #   after `l4b_capacity_flow` (§4al), the L4.5 R5 gate (§4ai), B16's one-sided
    #   feasibility (§4k) and the staleness warning (§4o). Fixed at its site.
    #
    # THE SPLIT IS LOAD-BEARING, so it is printed and not just the total. Stock
    # on a GT with NO cure campaign this month is a DEMAND fact -- the plan has
    # nothing to cure it into and no placement change can reach it (August: 656
    # of 1,679 PCR, 240 of 472 TBR). Only the stock on a GT the plan DOES cure
    # is addressable by scheduling, and that is the number to grade. Quoting the
    # total as a scheduling opportunity overstates it by ~40 %.
    _first_camp: dict[tuple, object] = {}
    for _r in (rec.group_by(["plant", "gt_code"])
               .agg(pl.col("start_ts").min().alias("st")).iter_rows(named=True)):
        _first_camp[(_r["plant"], _r["gt_code"])] = _r["st"]
    _exp_addr: dict[str, float] = {}
    print("\n  OPENING STOCK THAT EXPIRES UNUSED  (already built, in shelf life "
          "at t0, never drawn)")
    for p in ["PCR", "TBR"]:
        _rows = sorted(((g, q) for (pp, g), q in opening.items()
                        if pp == p and q > 0.5), key=lambda x: -x[1])
        _held = sum(v for (pp, _g), v in _opening0.items() if pp == p)
        _tot = sum(q for _g, q in _rows)
        _nc = sum(q for g, q in _rows if (p, g) not in _first_camp)
        _late = sum(q for g, q in _rows
                    if (p, g) in _first_camp
                    and ((_first_camp[(p, g)] - t0).total_seconds() / 3600.0
                         > opening_life.get((p, g), 0.0)))
        _exp_addr[p] = _tot - _nc
        print(f"    {p}: held {_held:>6,.0f}  drawn {_held - _tot:>6,.0f}  "
              f"EXPIRED {_tot:>6,.0f} on {len(_rows):>2} GTs   "
              f"[no campaign this month {_nc:,.0f} · first cure past its own "
              f"shelf life {_late:,.0f} · other {_tot - _nc - _late:,.0f}]")
        for g, q in _rows[:8]:
            _fc = _first_camp.get((p, g))
            _fh = ((_fc - t0).total_seconds() / 3600.0) if _fc is not None else None
            print(f"        {g:<32}{q:>7,.0f}  life left {opening_life.get((p, g), 0.0):>5.1f} h"
                  f"  first cure "
                  + ("none this month" if _fh is None else f"t0+{_fh:,.0f} h"))
        if len(_rows) > 8:
            print(f"        ... and {len(_rows) - 8} more GTs")

    # ---- CARRY-FORWARD GT: THE MONTH-END HAND-OFF ------------------------
    # The point of planning past the boundary. A green tyre BUILT inside the
    # month whose cure falls OUTSIDE it is not waste and not lost demand -- it is
    # next month's opening stock, and emitting it is what turns the month
    # boundary from a wall into a hand-off.
    #
    # DEFINITION, and why it is the ledger's own: `ev` credits a slice WHOLE at
    # `end_ts`, so "built in the month" is `end_ts <= month_end` and nothing
    # else. Using a pro-rated straddle here would produce a number that does not
    # equal the closing balance the rail was graded on -- two routes to one
    # quantity, which is the defect class this project keeps paying for. The
    # identity is asserted below rather than assumed.
    #
    # R5 AT HAND-OFF. Age at hand-off is `month_end - built_ts`, and every one of
    # these tyres has `cure_ts > month_end` with `cure_ts - built_ts <= 72 h`
    # already gated above, so age at hand-off is BOUNDED BY 72 h by
    # construction. It is measured anyway -- a constructive proof that is never
    # checked is how the C4 gate passed for a whole project on a 10x denominator.
    _cf = bs.filter((pl.col("machine") != "OPENING_STOCK")
                    & (pl.col("end_ts") <= pl.lit(_hzn))
                    & (pl.col("cure_ts") > pl.lit(_hzn)))
    _nm = f"{y + (m == 12)}-{(m % 12) + 1:02d}"
    _rows = []
    for r in _cf.iter_rows(named=True):
        n = int(round(float(r["qty"])))
        if n <= 0:
            continue
        s, e = r["start_ts"], r["end_ts"]
        span = max((e - s).total_seconds(), 0.0)
        for i in range(n):
            # one row per tyre, exactly like the MES-derived opening_gt master
            bt = e - timedelta(seconds=span * (n - 1 - i) / max(n, 1))
            _rows.append({"plant": r["plant"], "gt_code": r["gt_code"],
                          "built_ts": bt,
                          "age_h": (_hzn - bt).total_seconds() / 3600.0,
                          "as_of": _hzn})
    cf = (pl.DataFrame(_rows) if _rows else pl.DataFrame(
        schema={"plant": pl.Utf8, "gt_code": pl.Utf8, "built_ts": pl.Datetime,
                "age_h": pl.Float64, "as_of": pl.Datetime}))
    cf.write_parquet(run / "carry_forward_gt.parquet")
    _cfd = ROOT / "masters" / "opening_gt"
    _cfd.mkdir(parents=True, exist_ok=True)
    # Written under its OWN name, never over `opening_gt_<month>.parquet`: that
    # file is MES-derived ground truth for the August arms and a planner output
    # must not silently replace it. Point the next month at this one with
    # PLANNER_OPENING_GT=<path>.
    _cff = _cfd / f"carryforward_gt_{_nm}.parquet"
    cf.write_parquet(_cff)

    print("\n  CARRY-FORWARD GT  (built in-month, cured after the boundary "
          "-> next month's opening stock)")
    print(f"  {'plant':<6}{'tyres':>9}{'GTs':>6}{'age p50':>9}{'age p95':>9}"
          f"{'age max':>9}{'> 72 h (scrap)':>16}{'closing ledger':>16}")
    for p in ["PCR", "TBR"]:
        s = cf.filter(pl.col("plant") == p)
        # closing GT balance from the ledger the rail was graded on
        _bal = float(ev.filter((pl.col("plant") == p)
                               & (pl.col("ts") <= pl.lit(_hzn)))["d"].sum())
        if not s.height:
            print(f"  {p:<6}{0:>9}{0:>6}{'-':>9}{'-':>9}{'-':>9}{0:>16}"
                  f"{_bal:>16,.0f}")
            continue
        a = np.array(s["age_h"], float)
        scrap = int((a > GT_SHELF_LIFE_H).sum())
        print(f"  {p:<6}{s.height:>9,}{s['gt_code'].n_unique():>6}"
              f"{np.percentile(a,50):>9.1f}{np.percentile(a,95):>9.1f}"
              f"{a.max():>9.1f}{scrap:>16,}{_bal:>16,.0f}")
        # RECONCILIATION, not a report. Carry-forward IS the closing balance.
        #
        # `_og_left` USED TO BE `max(0, residual - consumed)` AND WAS ALWAYS 0.
        # `opening` is decremented as stock is drawn, so that expression asked
        # whether the leftover exceeds the draw -- 1,679 vs 3,453 on August PCR
        # -- and answered no in every plant-month, which is the only reason this
        # check has ever passed. The right answer is 0 for a REASON, not by
        # arithmetic accident: undrawn opening stock is never credited into
        # `gt_events` (only the OPENING_STOCK slices that were actually fed are,
        # +use at t0 and -use at the cure), so it is not in `_bal` and must not
        # be added to `s.height`. Verified on 2026-08: PCR carry 4,514 == closing
        # 4,514, TBR 793 == 793, with 1,679 / 472 undrawn outside both.
        # The undrawn quantity is reported in its own block above.
        _og_left = 0.0
        if abs((s.height + _og_left) - _bal) > 1.0:
            print(f"    ** CARRY/LEDGER MISMATCH {p}: carry {s.height:,} + "
                  f"undrawn opening {_og_left:,.0f} vs closing balance "
                  f"{_bal:,.0f}")
    for p in ["PCR", "TBR"]:
        # THE ALWAYS-PASSING GUARD, FIXED. This printed when
        # `_og_tot - _og_used > 0.5` -- residual minus consumption -- so it asked
        # whether the leftover was bigger than the draw and never fired on any
        # plant-month this project has run (August PCR 1,679 vs 3,453). The
        # quantity it was written to name is `_og_tot` itself, because `opening`
        # is already decremented by every draw. Full breakdown, with the cause
        # split and the GT list, is in the OPENING STOCK THAT EXPIRES UNUSED
        # block above; this line stays because it is the one that sits beside the
        # hand-off and says the tyres do NOT roll forward.
        _og_tot = sum(v for (pp, _g), v in opening.items() if pp == p)
        if _og_tot > 0.5:
            print(f"    {p}: {_og_tot:,.0f} tyres of THIS month's "
                  f"opening stock were never drawn -- they are >{GT_SHELF_LIFE_H:.0f} h "
                  f"old at hand-off and do NOT roll forward (scrap, not inventory)")
    print(f"    -> {_cff.relative_to(ROOT)}   "
          f"(next month: PLANNER_OPENING_GT={_cff.name})")

    print(f"\n  -> {run.name}/build_schedule.parquet · gt_events.parquet "
          f"· cure_campaigns_reconciled.parquet · carry_forward_gt.parquet")


if __name__ == "__main__":
    main()
