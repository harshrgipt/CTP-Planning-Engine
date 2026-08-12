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

from planner.cmbc import allowable, plant_ct
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
    import os
    from pathlib import Path
    d = root / "masters" / "opening_gt"
    ov = os.environ.get("PLANNER_OPENING_GT", "").strip()
    if not ov:
        return d / f"opening_gt_{month}.parquet"
    p = Path(ov)
    return p if p.is_absolute() else d / ov

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

# Basis for the day-1 cure floor. See earliest_cure(). "star" is the legacy
# 11.86 h wall built from two MEDIANS; "min" and "slice" use the physical
# tau_min. NB the EARLY_STOCK note above was measured on the 98.9 %-era engine,
# long before tau_min release, the partition and the derived slice rule -- its
# baseline no longer exists, so re-measure before trusting it.
FLOOR_BASIS = os.environ.get("PLANNER_L5_FLOOR_BASIS", "star")

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
GOV_PLANTS = {x for x in os.environ.get(
    "PLANNER_L5_TAKT_PLANTS", "TBR").split(",") if x}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", default="2026-07")
    ap.add_argument("--out", default=None, help="run directory name")
    a = ap.parse_args()

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
    moulds = {(r["plant"], r["gt_code"]): max(int(r["moulds"]), 1)
              for r in mould.iter_rows(named=True)}
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
    early_budget: dict[tuple, float] = {}
    if ogf.exists():
        for r in (pl.read_parquet(ogf).filter(pl.col("age_h") <= GT_SHELF_LIFE_H)
                  .group_by(["plant", "gt_code"]).len().iter_rows(named=True)):
            early_budget[(r["plant"], r["gt_code"])] = float(r["len"])
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
    _pav = {p: float(P0["press_availability"][p]["availability"])
            for p in ("PCR", "TBR")}
    PCT = plant_ct.get(_pav)
    print("  " + PCT.summary())

    def rate_of(p: str, gt: str) -> float:
        """Tyres per press-hour for ONE press running this GT."""
        r = PCT.press_rate(p, gt)
        return r if r else rate_p[p]

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
        _gap_q = (tau_h[plant] + bband[plant]) * _gr
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
        if FLOOR_BASIS == "star":
            return t0 + timedelta(hours=tau_h[plant] + bband[plant])
        if FLOOR_BASIS == "slice":
            return t0 + timedelta(hours=tau_min_h[plant])
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
          f"build time (tau* PCR {tau_h['PCR']:.2f}h TBR {tau_h['TBR']:.2f}h)\n")

    # explode lots into individual campaigns
    jobs = []
    for r in lots.iter_rows(named=True):
        # L4.5 now emits a per-lot size list carrying the observed length shape;
        # fall back to the flat qty for rows written before that change.
        sizes = r.get("lot_sizes")
        sizes = (list(sizes) if sizes is not None and len(sizes)
                 else [float(r["lot_qty"])] * int(r["n_lots"]))
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
                         "mould_set": r["mould_set"], "qty": float(q), "seq": i})
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
    jobs.sort(key=lambda j: (j["plant"], -j["qty"], j["gt_code"], j["seq"]))

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

    gt_total: dict[tuple, float] = {}
    for _j in jobs:
        k = (_j["plant"], _j["gt_code"])
        gt_total[k] = gt_total.get(k, 0.0) + _j["qty"]

    free: dict[str, datetime] = {}                    # press -> next free time
    last_gt: dict[str, str] = {}                      # press -> GT last run

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
        if not SUBPART:
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
        _n = int(math.ceil(ALPHA * _w / _u - 1e-9))
        _budget[_k] = max(1, min(len(_pset.get(_k, ())) or 1, _n))
    _cnt: dict[tuple, list] = {k: [0] * NH for k in _budget}
    _takt_moved = [0]
    _takt_nowin = [0]
    if GOV != "off":
        print(f"  TAKT  level-loaded press-concurrency budget "
              f"(mode={GOV} alpha={ALPHA} plants={','.join(sorted(GOV_PLANTS))}"
              f" subpart={int(SUBPART)})")
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

    def _takt_commit(pl_: str, gt: str, st: datetime, en: datetime) -> None:
        """Charge the seat to every partition, always -- even for ungoverned and
        backfill placements, or the profile stops describing the plan."""
        h0 = max(0, int((st - t0).total_seconds() // 3600))
        h1 = min(NH, int((en - t0).total_seconds() // 3600) + 1)
        for k in _pkeys(pl_, gt):
            if k in _cnt:
                for x in range(h0, h1):
                    _cnt[k][x] += 1

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
    if _cif.exists():
        for _r in pl.read_parquet(_cif).iter_rows(named=True):
            _pr, _en = str(_r["press"]), _r["ends"]
            if _en is None or _en <= t0:
                continue                   # finished before we start; nothing to do
            free[_pr] = max(free.get(_pr, t0), _en)
            last_gt[_pr] = _r["gt_code"]   # mould already mounted -> no change
            n_ci += 1
        print(f"  carry-in: {n_ci} press(es) still running last month's campaign "
              f"from {_cif.name}")
    elif _ci:
        print(f"  carry-in: {_cif} not found -- running cold (no-op)")
    busy: dict[tuple, list] = {}                      # (plant,gt) -> intervals
    placed, unplaced, carry, changes = [], [], [], 0

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
        # ---- L5<->L6 FEEDBACK -------------------------------------------
        # L6 hands back campaigns building could not feed and asks for them to be
        # RESEATED LATER, not failed. The hint is a per-GT delay in hours, so the
        # cure seat moves instead of the shortfall being discovered slice by slice
        # in L7. Absent file = clean no-op, so a single L5 run is unchanged.
        if _DELAY:
            _d = _DELAY.get((p, gt), 0.0)
            if _d:
                floor_ts = max(floor_ts, t0 + timedelta(hours=_d))
        best = None
        for pr in cand:
            st = max(free.get(pr, t0), floor_ts)
            if last_gt.get(pr) not in (None, gt):
                st = st + timedelta(seconds=mchg_s(p, pr))   # mould change
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
            en = st + dur
            # R3: at most `cap` presses may run this GT concurrently
            overlap = sum(1 for (s2, e2) in busy.get((p, gt), [])
                          if s2 < en and st < e2)
            if overlap >= cap:
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
            if best is None or (st, same_rim, pr) < (best[0], best[3], best[1]):
                best = (st, pr, en, same_rim)
        if best is None:
            # every eligible press is mould-blocked -- defer behind the earliest
            # concurrent run rather than breaking R3
            iv = sorted(busy.get((p, gt), []), key=lambda t: t[1])
            if not iv:
                unplaced.append({**j, "reason": "no feasible press"})
                continue
            after = iv[max(0, len(iv) - cap)][1]
            pr = min(cand, key=lambda x: (max(free.get(x, t0), after, floor_ts), x))
            st = max(free.get(pr, t0), after, floor_ts)
            if last_gt.get(pr) not in (None, gt):
                st = st + timedelta(seconds=mchg_s(p, pr))
            best = (st, pr, st + dur, 1)
        st, pr, en = best[0], best[1], best[2]
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
            fits_h = (horizon - st).total_seconds() / 3600.0
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
                en = st + timedelta(hours=can / max(rate, 1e-9))
                if last_gt.get(pr) not in (None, gt):
                    changes += 1
                free[pr] = en
                last_gt[pr] = gt
                busy.setdefault((p, gt), []).append((st, en))
                _takt_commit(p, gt, st, en)
                placed.append({"plant": p, "gt_code": gt,
                               "mould_set": j["mould_set"], "press": pr,
                               "start_ts": st, "end_ts": en, "qty": float(can),
                               "hours": round((en - st).total_seconds() / 3600, 2)})
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
            early_budget[(p, gt)] = max(
                0.0, early_budget.get((p, gt), 0.0) - gap_tyres[p])
        placed.append({"plant": p, "gt_code": gt, "mould_set": j["mould_set"],
                       "press": pr, "start_ts": st, "end_ts": en,
                       "qty": j["qty"],
                       "hours": round((en - st).total_seconds() / 3600, 2)})

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
                st = st + timedelta(seconds=mchg_s(p, pr))
            gap_h = (horizon - st).total_seconds() / 3600.0
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
            en = st + timedelta(hours=take / rate)
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
        carry = [{"plant": r["plant"], "gt_code": r["gt_code"],
                  "mould_set": r["mould_set"], "seq": 0, "press": r["press"],
                  # tyres of this campaign cured AFTER the boundary -- next
                  # month's output, excluded from this month's fulfilment by
                  # `frac_in_month` in L7.
                  "qty": float(r["qty"]) * max(
                      0.0, min(1.0, (r["end_ts"] - month_end).total_seconds()
                               / max((r["end_ts"] - r["start_ts"]).total_seconds(), 1.0))),
                  "carry_from": month_end, "ends": r["end_ts"]}
                 for r in placed if r["end_ts"] > month_end]
    # Campaigns still running at the horizon: their tail is next month's opening
    # state, not lost demand. Written even when empty so downstream can rely on it.
    (pl.DataFrame(carry) if carry else pl.DataFrame(
        schema={"plant": pl.Utf8, "gt_code": pl.Utf8, "mould_set": pl.Utf8,
                "seq": pl.Int64, "press": pl.Utf8, "qty": pl.Float64,
                "carry_from": pl.Datetime, "ends": pl.Datetime})
     ).write_parquet(run / "carry_out.parquet")
    if carry:
        print(f"  carry-out: {len(carry)} campaigns run past the horizon, "
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
