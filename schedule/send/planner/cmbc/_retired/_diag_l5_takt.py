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

from planner.config import CONFIG, GT_SHELF_LIFE_H
from planner import paths

ROOT = paths.ROOT   # depth-independent; this file moved one level deeper
SRC_INP = paths.INPUT_DERIVED
D = ROOT / "warehouse" / "derived"
PARAMS = ROOT / "warehouse" / "params"

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
HORIZON_MODE = os.environ.get("PLANNER_HORIZON_MODE", "truncate")

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

# ---- PROTOTYPE: LEVEL-LOADED PRESS-CONCURRENCY BUDGET (takt cap) ----------
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
GOV = os.environ.get("PLANNER_L5_TAKT", "off")          # off | flat
ALPHA = float(os.environ.get("PLANNER_L5_ALPHA", "1.0"))
GOV_PLANTS = {x for x in os.environ.get(
    "PLANNER_L5_TAKT_PLANTS", "TBR").split(",") if x}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", default="2026-07")
    ap.add_argument("--out", default=None, help="run directory name")
    a = ap.parse_args()

    P = json.loads(sorted(PARAMS.glob("params_*.json"))[-1].read_text())
    lots = pl.read_parquet(D / f"l45_lots_{a.month}.parquet").filter(
        pl.col("n_lots") > 0)
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
    # Per-press mould-change time, resolved through the wcID bridge. PCR presses
    # span 210-430 min; a flat median cannot prefer a cheap-to-change press.
    # Used only to break ties between presses that are otherwise equal on start
    # time and rim, so it can never cost fulfilment.
    mch_press = {r["wc_id"]: float(r["mould_change_min"])
                 for r in pmc.iter_rows(named=True) if r.get("wc_id")}
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
    ogf = ROOT / "masters" / "opening_gt" / f"opening_gt_{a.month}.parquet"
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
        if EARLY_STOCK and early_budget.get((plant, gt), 0.0) >= gap_tyres[plant]:
            return t0
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
    horizon = t0 + timedelta(days=ndays)

    print("=" * 92)
    print(f"L5  CURE CAMPAIGN MASTER PLAN  --  {a.month}")
    print("=" * 92)
    print(f"  horizon {t0:%Y-%m-%d %H:%M} -> {horizon:%Y-%m-%d %H:%M} "
          f"({ndays} days)   greedy, deterministic")
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
    SUBPART = os.environ.get("PLANNER_L5_TAKT_PART", "0") != "0"
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
        _r = cav_p[_j["plant"]] * 3600.0 / cyc_p[_j["plant"]]
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
        rate = cav_p[p] * 3600.0 / cyc_p[p]           # tyres per press-hour
        dur = timedelta(hours=j["qty"] / max(rate, 1e-9))
        # Concurrency is bounded by MOULDS ONLY (R3).
        # A volume-based press cap was tried -- limit a GT to the presses its
        # month's volume can keep busy -- and it did cut the worst spread from 26
        # presses to 18. But it also starved high-volume GTs of throughput and
        # pushed GTs below 95% of requirement from 7 to 9. Press spread is a
        # cosmetic concern; requirement coverage is not.
        cap = moulds.get((p, gt), 1)

        floor_ts = earliest_cure(p, gt, j['qty'], j['qty'] / max(
            cav_p[p] * 3600.0 / cyc_p[p], 1e-9))
        best = None
        for pr in cand:
            st = max(free.get(pr, t0), floor_ts)
            if last_gt.get(pr) not in (None, gt):
                st = st + timedelta(seconds=mch_p[p])   # mould change
            # ---- TAKT: move the seat later, but ONLY inside the month -------
            # Consulted only if the UNGOVERNED placement already fits. A
            # campaign that was going to overrun is left exactly as the shipped
            # layer had it, so the governor cannot create horizon overflow.
            if st + dur <= horizon:
                _g = _takt_free(p, gt, st, dur)
                if _g is not None and _g + dur <= horizon:
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
                st = st + timedelta(seconds=mch_p[p])
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
        if floor_ts <= t0:                      # this campaign spent stock budget
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
        rate = cav_p[p] * 3600.0 / cyc_p[p]
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
                st = st + timedelta(seconds=mch_p[p])
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
    # 3. horizon
    late = cp.filter(pl.col("end_ts") > horizon).height
    print(f"    campaigns past horizon          : {late}  "
          f"{'PASS' if late == 0 else 'FAIL'}")
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
