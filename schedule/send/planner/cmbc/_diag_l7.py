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
import calendar
import heapq
import json
import os
import math
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import polars as pl

from planner.config import CONFIG, GT_SHELF_LIFE_H

_DIAG_R: list = []
_DIAG_LAST: list = []
_DIAG_NCAND: list = [0]
_DIAG_FREE: list = [0.0]
_DIAG_LO: list = [0.0]
_DIAG_SPLIT: list = [0, 0, 0]
# DIAG-ONLY EXPERIMENT: allow a single atomic SLICE to be halved before starving.
DIAG_SLICE_SPLIT_MIN = float(os.environ.get('DIAG_SLICE_SPLIT_MIN', '0'))  # 0 = off
_diag_splits = {'PCR': 0, 'TBR': 0}


ROOT = Path(__file__).resolve().parent.parent.parent
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
_HF = os.environ.get("PLANNER_HARD_FLOOR", "budget")
HARD_FLOOR = _HF == "1"
NO_FLOOR = _HF == "off"
_subfloor_spent: dict = {"PCR": 0, "TBR": 0}

# Never build a GT outside its rim's locked machines. Turns the rim lock from a
# priced preference into a constraint, so the WIP cap cannot break it.
HARD_LOCK = os.environ.get("PLANNER_HARD_LOCK", "1") != "0"

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
# Set PLANNER_PARTITION_PLANTS=PCR,TBR to enable it there.
PARTITION_PLANTS = set(
    os.environ.get("PLANNER_PARTITION_PLANTS", "PCR").replace(" ", "").split(","))
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
    cm = pl.read_parquet(D / f"cap_machine_{a.month}.parquet")
    grp = pl.read_parquet(D / f"cap_ttl_groups_{a.month}.parquet")
    tt = pl.read_parquet(ROOT.parent.parent / "INPUT" / "derived" / "tt_tl.parquet")
    # Rim per GT, for size-aware machine selection (R6/R7). The plant's
    # changeover master is BINARY: same size 22-28 min, different size 42-60.
    _sz = pl.read_parquet(ROOT.parent.parent / "INPUT" / "derived" / "gt_size.parquet")
    rim_of = {r["gt_code"]: str(r["rim"]) for r in _sz.iter_rows(named=True)
              if r.get("gt_code") and r.get("rim")}
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

    # OPENING GT IS SUPPLY AT t0, NOT JUST A DEMAND OFFSET.
    # L4 nets opening stock off demand, but L7 previously tried to BUILD every
    # tyre -- including those already sitting on the floor at t0. A press curing
    # at t0 needs its GT finished at t0 - tau*, which is before the horizon
    # exists, so those slices failed: 87% of all starved volume was day 1.
    # The stock is real and physically able to feed exactly those cures.
    ogf = ROOT / "masters" / "opening_gt" / f"opening_gt_{a.month}.parquet"
    opening: dict[tuple, float] = {}
    opening_life: dict[tuple, float] = {}      # hours of shelf life left at t0
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
            opening_life[(r["plant"], r["gt_code"])] =                 GT_SHELF_LIFE_H - float(r["age"])
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
    def _cand(p: str, gt: str) -> list:
        """Eligible machines for a GT, inside its TT/TL group where B16 applies."""
        e = elig.get((p, gt), [])
        if p == "TBR" and gt_tag.get(gt):
            e = [x for x in e if group_of.get(x) == gt_tag[gt]] or e
        return e

    def _n_elig(p: str, gt: str) -> int:
        return len(_cand(p, gt))

    camp = camp.with_columns(
        pl.struct(["plant", "gt_code"]).map_elements(
            lambda r: _n_elig(r["plant"], r["gt_code"]),
            return_dtype=pl.Int64).alias("_scarcity")).sort(
        ["plant", "_scarcity", "start_ts", "gt_code", "press"])

    # ---- HORIZON MACHINE ASSIGNMENT -- the RUN becomes an object --------
    # One machine per GT for the WHOLE horizon, a second only when the first is
    # out of capacity. This is the constraint the tier-8 dedication price was
    # standing in for: a 10,000 penalty paid 40 times over is not a price, it is
    # an absent constraint. Assigning here rather than per slice is what lets
    # consecutive slices merge into a run that clears the B12 floor.
    days = calendar.monthrange(y, m)[1]
    cap_h = days * 24.0 * MACH_UTIL_CAP
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

    def _est_cad(p: str, gt: str) -> float:
        """Mean cadence over the GT's eligible machines -- the best estimate
        available before one is chosen. Falls back to the plant median only when
        the GT has no eligible machine with a mined cadence."""
        if CAD_BASIS == "plant":
            return plant_cad[p]
        ms = [m for m in _cand(p, gt) if m in cad_s]
        return sum(cad_s[m] for m in ms) / len(ms) if ms else plant_cad[p]

    def _chg_cad(p: str, mach: str) -> float:
        """Cadence a machine is CHARGED at when load is booked against it."""
        return plant_cad[p] if CAD_BASIS == "plant" \
            else cad_s.get(mach, plant_cad[p])

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
    _lockf = ROOT.parent.parent / "INPUT" / "derived" / "machine_rim_lock.parquet"
    if _lockf.exists():
        for r in (pl.read_parquet(_lockf)
                  .sort(["plant", "locked_rim", "tier", "rank"])
                  .iter_rows(named=True)):
            lock_of.setdefault((r["plant"], r["locked_rim"]), []).append(
                (_tier_rank.get(r["tier"], 3), int(r["rank"]), r["machine"]))
        for k in lock_of:
            lock_of[k] = [m for _t, _r, m in sorted(lock_of[k])]

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
        _elig_rims: list = []
        for (p, r), q in sorted(_rim_need.items()):
            ms = lock_of.get((p, r), [])
            fx = _flex.get(p)
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
    _homef = ROOT.parent.parent / "INPUT" / "derived" / "gt_home_machine.parquet"
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
    _partf = ROOT.parent.parent / "INPUT" / "derived" / "gt_machine_partition.parquet"
    if USE_PARTITION and _partf.exists():
        _pf = pl.read_parquet(_partf)
        # STALENESS GUARD. The partition is sized against ONE month's demand and
        # that month's calendar hours. Silently reusing July's partition for
        # August would pin every GT to a machine chosen for the wrong demand --
        # a wrong answer that looks like a right one. Refuse instead.
        _pm = (str(_pf["month"][0]) if "month" in _pf.columns and _pf.height
               else None)
        if _pm != a.month:
            print(f"  !! PARTITION IS FOR {_pm}, THIS RUN IS {a.month} -- ignoring it"
                  f" and falling back to the dynamic assignment. Rebuild with:"
                  f" python scripts/build_gt_machine_partition.py {a.month}")
            _pf = _pf.clear()
        for r in (_pf.sort(["plant", "gt_code", "hours"],
                           descending=[False, False, True]).iter_rows(named=True)):
            if r["plant"] in PARTITION_PLANTS:
                part_of.setdefault((r["plant"], r["gt_code"]), []).append(r["machine"])

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
                    _shq * _chg_cad(p, _m) / 3600.0
            continue
        # Locked machines first, in tier order; the rest stay available as spill.
        _lk = [m for m in _locked(p, gt) if m in cand]
        _rank = {m: i for i, m in enumerate(_lk)}
        chosen: list = []
        left_q = need_q[(p, gt)]              # TYRES, not hours -- see above
        pool = list(cand)
        while left_q > 1e-9 and pool:
            mm = min(pool, key=lambda x: (_rank.get(x, 99),
                                          pen.get((p, gt, x), 0.0),
                                          load_h.get(x, 0.0), x))
            pool.remove(mm)
            _c = _chg_cad(p, mm)
            free_h = max(cap_h - load_h.get(mm, 0.0), 0.0)
            take_h = min(free_h, left_q * _c / 3600.0)
            if take_h <= 1e-9:
                continue                      # machine full, try the next one
            load_h[mm] = load_h.get(mm, 0.0) + take_h
            chosen.append(mm)
            left_q -= take_h * 3600.0 / _c
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
    last_gt: dict[str, str] = {}

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
        sm = _same_min.get(mach, _co_fb[plant][0])
        dm = _diff_min.get(mach, _co_fb[plant][1])
        same = rim_of.get(gt_a, "@") == rim_of.get(gt_b, "#")
        return (sm if same else dm) * 60.0
    slices, starved = [], []
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
    _cap_h = int(days * 24) + int(GT_SHELF_LIFE_H) + 2
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
                            "qty": qty, "reason": "no eligible machine"})
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
        _cad_h = plant_cad[p] / 3600.0
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
            t_cure = r["start_ts"] + timedelta(hours=hrs * i / n)
            # draw on opening stock first -- it is already built and already aged
            have = opening.get((p, gt), 0.0)
            # usable only if the cure happens before this stock expires
            hold_h = (t_cure - t0).total_seconds() / 3600.0
            if have > 0 and hold_h > opening_life.get((p, gt), 0.0):
                have = 0.0
            if have > 0:
                use = min(have, per)
                opening[(p, gt)] = have - use
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
        if LOT_INTERVAL_H > 0:
            interval[p], basis = LOT_INTERVAL_H, "env"
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
        cadence = cad_s.get(cand[0], plant_cad[p])
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
        for grp in groups:
            jobs.append((grp[0]["t_cure"], _n_elig(p, gt), p, gt, cand, grp))

    def _place(p: str, gt: str, cand: list, grp: list, cap_h: float) -> bool:
            """Place one run whole. False if no machine can take it."""
            _DIAG_R.clear()
            _DIAG_NCAND[0] = len(cand)
            gq = sum(d["qty"] for d in grp)
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
            _r = rim_of.get(gt)
            if _r is not None:
                _lkset = set(_locked(p, gt))
                _bal = os.environ.get("DIAG_BALANCE", "0") != "0"
                _committed = {mm: sum((e2 - s2).total_seconds()
                                      for (s2, e2, _g3, _r3) in busy.get(mm, []))
                              for mm in cand}
                cand = sorted(cand, key=lambda x: (
                    x not in _lkset,
                    rim_of.get(last_gt.get(x, ""), None) != _r,
                    _committed[x] if _bal else 0.0,
                    cand.index(x)))
            # TRIED AND REVERTED: preferring a machine whose `last_gt` is this GT,
            # to recover the continuity that GT-ordered placement gave for free.
            # It does the opposite -- machines/GT 4 -> 5 and changeovers
            # 4.10 -> 4.31 per machine-day. Under deadline ordering the GTs
            # interleave, so by the time a GT's next run comes up the machine has
            # moved on; the preference almost never fires, and when it does it
            # takes a worse-fitting machine. Continuity has to come from the
            # ORDER, not from the machine choice.
            for mach in cand:
                c = cad_s.get(mach, plant_cad[p])
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
                st = ideal
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
                en = st + dur
                if st < t0:
                    _DIAG_R.append("release_before_t0")
                    continue
                # EARLINESS CAP -- building early is not free.
                # The backward walk was the ONLY response to a busy machine, and
                # it is pure inventory: 36% of PCR runs were pushed early, mean
                # 2.86 h, max 60 h, worth 1,495 tyres of standing GT -- while 24%
                # of machine capacity sat idle elsewhere in the month. Reject a
                # machine that would push this run more than `cap_h` early so the
                # next candidate is tried instead. The caller retries uncapped if
                # no machine passes, so this can never cost a placement.
                if (ideal - st).total_seconds() / 3600.0 > cap_h:
                    _DIAG_R.append("earliness_cap")
                    continue
                # R5 ON EVERY SLICE, NOT ON THE RUN.
                # Checking `t_last - run_end` looked at the wrong endpoint: when
                # slices share a cure time the FIRST-BUILT slice waits longest,
                # so 10 breaches passed a guard that only ever saw the last one.
                worst = max((d["t_cure"] - (st + timedelta(seconds=cu * c)))
                            .total_seconds() / 3600.0
                            for d, cu in zip(grp, cums))
                if worst > GT_SHELF_LIFE_H:
                    _DIAG_R.append("R5_shelf_life")
                    continue
                # HARD CAP: would this placement breach the stock envelope?
                _cum, _adds = 0.0, []
                for d, cu in zip(grp, cums):
                    _cum += d["qty"]
                    _adds.append((st + timedelta(seconds=cu * c),
                                  d["t_cure"], d["qty"]))
                if not _cap_ok(p, _adds):
                    _DIAG_R.append("wip_rail")
                    continue
                if PIN_RUNS:
                    best = (mach, c, st, en)   # first feasible keeps the run whole
                    break
                wait = (t_first - en).total_seconds() / 3600.0
                key = (-wait, mach)            # prefer the LATEST feasible release
                if best is None or key < (-(t_first - best[3]).total_seconds()
                                          / 3600.0, best[0]):
                    best = (mach, c, st, en)
            if best is None:
                _DIAG_LAST.clear()
                _DIAG_LAST.extend(_DIAG_R)
                # FREE-WINDOW PROBE: was a legal contiguous slot actually free
                # on ANY candidate machine at this instant?  Window is bounded
                # below by t0 and by R5 (the run may not finish >72 h before its
                # own cures) and above by the latest admissible start.
                _bf = 0.0
                _DIAG_SPLIT[0] = _DIAG_SPLIT[1] = _DIAG_SPLIT[2] = 0
                for _mm in cand:
                    _c = cad_s.get(_mm, plant_cad[p])
                    _durs = gq * _c
                    _id = min(d["t_cure"] - timedelta(hours=tau_rel[p])
                              - timedelta(seconds=cu * _c)
                              for d, cu in zip(grp, cums))
                    _lo = max([t0] + [d["t_cure"] - timedelta(hours=GT_SHELF_LIFE_H)
                                      - timedelta(seconds=cu * _c)
                                      for d, cu in zip(grp, cums)])
                    _hi = _id + timedelta(seconds=_durs)
                    if _hi <= _lo:
                        continue
                    # EXACT: every free gap must also carry the setup on BOTH
                    # sides, exactly as the backward walk requires.
                    _iv = sorted(busy.get(_mm, []), key=lambda x: (x[0], x[1]))
                    _bounds = [(t0 - timedelta(days=400), t0 - timedelta(days=400), None, None)]                         + _iv + [(t0 + timedelta(days=400), t0 + timedelta(days=400), None, None)]
                    _mx = 0.0
                    for _k in range(len(_bounds) - 1):
                        _pg = _bounds[_k][2]
                        _ng = _bounds[_k + 1][2]
                        _ga = max(_bounds[_k][1], _lo)
                        _gb = min(_bounds[_k + 1][0], _hi)
                        if _gb <= _ga:
                            continue
                        _need = _durs
                        if _pg is not None and _bounds[_k][1] >= _lo:
                            _need += _setup_s(p, _mm, _pg, gt)
                        if _ng is not None and _bounds[_k + 1][0] <= _hi:
                            _need += _setup_s(p, _mm, gt, _ng)
                        _mx = max(_mx, (_gb - _ga).total_seconds() / max(_need, 1e-9) * _durs)
                        for _fi, _fr in enumerate((0.5, 0.25, 0.125)):
                            _n2 = _need - _durs + _durs * _fr
                            if (_gb - _ga).total_seconds() >= _n2:
                                _DIAG_SPLIT[_fi] = 1
                    _bf = max(_bf, _mx / max(_durs, 1e-9))
                _DIAG_FREE[0] = _bf
                return False
            mach, c, st, en = best
            busy.setdefault(mach, []).append((st, en, gt, rim_of.get(gt, "")))
            # Charge the targeted rim spill so its budget is real, not advisory.
            _rr = rim_of.get(gt, "")
            if spill_to.get((p, _rr)) == mach:
                spill_used_h[(p, _rr)] = spill_used_h.get((p, _rr), 0.0) \
                    + (en - st).total_seconds() / 3600.0
            last_gt[mach] = gt
            _cum2, _adds2 = 0.0, []
            for d, cu in zip(grp, cums):
                _adds2.append((st + timedelta(seconds=cu * c),
                               d["t_cure"], d["qty"]))
            _cap_apply(p, _adds2, +1.0)          # commit the chosen placement
            cum = 0.0
            for d in grp:
                s0 = st + timedelta(seconds=cum * c)
                s1 = s0 + timedelta(seconds=d["qty"] * c)
                cum += d["qty"]
                slices.append({"plant": p, "gt_code": gt, "machine": mach,
                               "press": d["press"], "start_ts": s0, "end_ts": s1,
                               "qty": round(d["qty"], 1), "cure_ts": d["t_cure"],
                               "wait_h": round((d["t_cure"] - s1).total_seconds()
                                               / 3600.0, 3)})
            return True

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
    heap = []
    for _i, (t_due, sc, p, gt, cand, grp) in enumerate(jobs):
        heapq.heappush(heap, (t_due, sc, _i, p, gt, cand, grp))
    _seq = len(jobs)
    while heap:
        t_due, sc, _i, p, gt, cand, grp = heapq.heappop(heap)
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
                heapq.heappush(heap, (_half[0]["t_cure"], sc, _seq, p, gt,
                                      cand, _half))
                _seq += 1
        elif DIAG_SLICE_SPLIT_MIN > 0 and len(grp) == 1                 and grp[0]["qty"] >= 2 * DIAG_SLICE_SPLIT_MIN                 and _subfloor_spent[p] < SUBFLOOR_BUDGET.get(p, 0):
            # EXPERIMENT: a slice is a DELIVERY with no minimum of its own
            # (this file's own doctrine).  Halve it and re-queue both halves at
            # the same deadline; charge one sub-floor setup from the B12 budget.
            _subfloor_spent[p] += 1
            _diag_splits[p] += 1
            _q = grp[0]["qty"]
            _h1 = round(_q / 2.0)
            for _qq in (_h1, _q - _h1):
                heapq.heappush(heap, (grp[0]["t_cure"], sc, _seq, p, gt, cand,
                                      [{**grp[0], "qty": float(_qq)}]))
                _seq += 1
        else:
            for _d in grp:
                from collections import Counter as _C
                _cnt = _C(_DIAG_LAST)
                starved.append({"plant": p, "gt_code": gt,
                                "press": _d["press"], "qty": _d["qty"],
                                "reason": ("would breach min_lot"
                                           if len(grp) > 1 else
                                           "no feasible release"),
                                "why": ("|".join(f"{k}={v}" for k, v in sorted(_cnt.items()))
                                        or "NO_CANDIDATES"),
                                "why_set": ("|".join(sorted(_cnt)) or "NO_CANDIDATES"),
                                "ncand": _DIAG_NCAND[0],
                                "nslices": len(grp),
                                "free_ratio": round(_DIAG_FREE[0], 3),
                                "fits_half": bool(_DIAG_SPLIT[0]),
                                "fits_quarter": bool(_DIAG_SPLIT[1]),
                                "fits_eighth": bool(_DIAG_SPLIT[2]),
                                "t_cure": _d["t_cure"]})

    print(f"  [DIAG] subfloor budget spent: {_subfloor_spent} of {SUBFLOOR_BUDGET}  slice-splits {_diag_splits}")
    from collections import Counter as _C2
    _rc = _C2()
    for _s in starved:
        _rc[(_s["plant"], _s["reason"], _s.get("nslices"))] += 1
    print(f"  [DIAG] starved groups by (plant, reason, n_slices_in_group): {dict(_rc)}")
    bs = pl.DataFrame(slices)
    st_df = pl.DataFrame(starved) if starved else pl.DataFrame(
        schema={"plant": pl.Utf8, "gt_code": pl.Utf8, "qty": pl.Float64,
                "reason": pl.Utf8})

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

    bs.write_parquet(run / "build_schedule.parquet")
    st_df.write_parquet(run / "build_starved_diag.parquet")
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
    late = bs.filter(pl.col("wait_h") > GT_SHELF_LIFE_H).height
    print(f"    GT wait > {GT_SHELF_LIFE_H:.0f} h (R5)          : {late}  "
          f"{'PASS' if late == 0 else 'FAIL'}")
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

    print(f"\n  -> {run.name}/build_schedule.parquet · gt_events.parquet "
          f"· cure_campaigns_reconciled.parquet")


if __name__ == "__main__":
    main()
