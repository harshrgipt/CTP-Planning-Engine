"""L4.5 -- LOT SIZING & DEMAND CONSOLIDATION (R9, B12).

    python -m planner.cmbc.l45_lotsize --month 2026-07

Sits between the net requirement (L4) and the cure campaign master (L5). v2.0
had no equivalent step at all: L4 netted demand and L5 placed campaigns, and
nothing decided lot size in between.

    0. below min_demand            -> residual policy, never a silent drop
    1. aggregate to MOULD-SET level, not SKU level
    2. consolidate until min_cure_lot is met
    3. sister consolidation within the mould set
    4. round to cavity multiples and whole cure cycles
    5. cap at max_cure_lot = cure_rate x 72 h            (R5 upper bound)
    6. residuals -> explicit policy, never silent round-up or drop

THE LOT IS THE CURE LOT. BUILD SLICES HAVE NO MINIMUM.
  Phase 0 shows the plant honours R9 at the CURE campaign level (58.5 h / 210.7 h
  campaigns) and deliberately breaks it at the BUILDING level (7.6 h / 5.4 h,
  2.46 / 3.51 changeovers per resource-day). Building absorbs changeovers so
  curing does not have to, and that trade is correct.
  Applying one minimum to both either fragments cure campaigns or forces
  building to run ahead -- which recreates the head gap. Do NOT add a
  build-slice minimum check here or anywhere downstream.

THE FLOOR IS DERIVED, NOT DECREED
      min_cure_lot   = moulds x cavities x (campaign_min_h x 60 / cycle_min)
      campaign_min_h = mould_change_h x (1 - f) / f
  where f is the largest share of press time we accept losing to mould changes.
  The floor falls out of mould count and mount cost; it is not a policy number.
  The fixed B12 floors (PCR 150 / TBR 70) are retained as a FALLBACK for GTs
  whose mould count is unknown, and both are reported so they can be compared.

THE CEILING COMES FROM R5, NOT R9
      max_cure_lot = cure_rate x GT_SHELF_LIFE_H
  A campaign longer than this pushes its own oldest tyre past the shelf life.
  On PCR -- dedicated 1:1 machines at high occupancy -- this binds earlier than
  people expect, so it is computed per GT and never assumed to be slack.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path

import polars as pl

from planner.cmbc import plant_ct
from planner.cmbc import _stamp
from planner.config import CONFIG, GT_SHELF_LIFE_H

ROOT = Path(__file__).resolve().parent.parent.parent
D = ROOT / "warehouse" / "derived"
PARAMS = ROOT / "warehouse" / "params"

# largest share of press time we accept losing to mould changes. The plant's own
# revealed figure is 9.3% (PCR) and 2.8% (TBR), computed as
# mould_change_h / (mould_change_h + observed campaign hours).
MOULD_CHANGE_FRAC_MAX = 0.15



def integer_split(total: float, weights: list[float], floor: float) -> list[int]:
    """Split `total` into whole tyres in proportion to `weights`.

    A TYRE IS AN INTEGER. An earlier version rounded each lot UP to a "cavity
    multiple" using the DERIVED effective cavity count (PCR 3.3958, TBR 2.4140).
    Those are not integers, so every multiple of them was fractional -- 465 of
    573 cure campaigns carried quantities like 3,691.3 tyres. The same round-UP
    also pushed 59 GTs past their requirement by 707 tyres, breaking G1.

    Largest-remainder apportionment fixes both: floor everything, then hand the
    remainder to the largest fractional parts. The result is integral AND sums
    to `total` exactly, so nothing is created or lost.
    """
    n = len(weights)
    if n == 0:
        return []
    w = sum(weights) or 1.0
    raw = [total * x / w for x in weights]
    out = [int(v) for v in raw]
    rem = int(round(total)) - sum(out)
    order = sorted(range(n), key=lambda i: (-(raw[i] - out[i]), i))
    for k in range(max(rem, 0)):
        out[order[k % n]] += 1
    while rem < 0:                      # total was rounded down: claw back
        i = max(range(n), key=lambda j: out[j])
        if out[i] <= 0:
            break
        out[i] -= 1
        rem += 1
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", default="2026-07")
    a = ap.parse_args()

    pj = sorted(PARAMS.glob("params_*.json"))
    P = json.loads(pj[-1].read_text())
    req = pl.read_parquet(D / f"net_requirement_{a.month}.parquet")
    ms = pl.read_parquet(D / f"cie_mould_sets_{a.month}.parquet")
    cie = pl.read_parquet(D / f"cie_proposals_{a.month}.parquet")
    mo = pl.read_parquet(D / f"cap_mould_{a.month}.parquet")
    cav = pl.read_parquet(D / "l3_cavities.parquet")
    pmc = pl.read_parquet(D / "press_mould_change.parquet")
    min_lot = CONFIG.thresholds.min_lot_units
    # Per-(plant, GT) daily demand curve straight from the order book.
    _phase: dict = {}
    _ph_tot: dict = {}
    try:
        _dm = pl.read_parquet(ROOT / "masters" / "demand" / f"demand_{a.month}.parquet")
        if "day" in _dm.columns:
            for _r in (_dm.group_by(["plant", "gt_code", "day"])
                       .agg(pl.col("qty").sum()).sort(["plant", "gt_code", "day"])
                       .iter_rows(named=True)):
                _k = (_r["plant"], _r["gt_code"])
                _phase.setdefault(_k, []).append((int(_r["day"]), float(_r["qty"])))
                _ph_tot[_k] = _ph_tot.get(_k, 0.0) + float(_r["qty"])
            print(f"  order-book phasing: {len(_phase)} GTs carry due dates")
    except Exception as _e:                                     # noqa: BLE001
        print(f"  order-book phasing unavailable ({_e}) -- lots get no deadline")

    # cure-campaign mean hours come from L0 (measured per month, then combined).
    # WHICH STATISTIC? The plant's cure-campaign distribution is right-skewed
    # 2:1 -- PCR p50 85.33 h against mean 174.72, TBR p50 190.09 against
    # 255.94. Sizing on the MEAN aims at the tail, not the typical campaign,
    # and produces campaigns 2.0x (PCR) / 1.4x the plant p50: ours p50 169.2 h
    # and 266.5 h, qty p50 1,170 against the plant's 495.
    # Same defect class as tau* and min_lot (PARTITION section 1): a mined
    # statistic adopted as a target without asking which moment of the
    # distribution the plant actually operates at.
    # PLANNER_L45_CAMP_STAT=p50 targets the median instead.
    _cstat = os.environ.get("PLANNER_L45_CAMP_STAT", "mean")
    CONC_FLOOR = float(os.environ.get("PLANNER_L45_CONC_FLOOR", "0"))
    import calendar as _cal
    HZ_H = _cal.monthrange(int(a.month[:4]), int(a.month[5:7]))[1] * 24.0
    _ckey = "hours_p50" if _cstat == "p50" else "hours_mean"
    mean_camp_h = {p: float(P["campaign_bands"][p]["cure"][_ckey])
                   for p in ["PCR", "TBR"]
                   if _ckey in P["campaign_bands"].get(p, {}).get("cure", {})}
    # ---- p50nz: THE ZERO-STRIPPED MEDIAN -----------------------------------
    #
    # THE BAND IS CONTAMINATED, and both raw statistics are wrong because of it.
    # L0's cure-campaign detector counts single-cycle events as campaigns:
    #   PCR deciles [0.0, 0.0, 0.55, 22.56, 59.45, 114.83, 191.49, ...]  30% ~zero
    #   TBR deciles [0.0, 0.01, 41.78, 95.62, 154.15, 229.15, ...]       20% ~zero
    # So `hours_mean` (174.7 / 255.9) is inflated by the long tail while
    # `hours_p50` (85.3 / 190.1) is DEFLATED by the zeros. Neither describes a
    # real campaign. Targeting p50 measured -0.2 pt on PCR for exactly this
    # reason -- it was chasing an artefact, not the plant.
    #
    # Strip the zeros: if a fraction z of observations are ~0, the median of the
    # REAL campaigns sits at percentile z + (1-z)/2 of the full distribution.
    #   PCR z=30% -> p65 -> ~115 h        TBR z=20% -> p60 -> ~229 h
    # Derived from the stored deciles, so it needs no MES re-mine. The proper
    # fix is in L0's detector (exclude sub-cycle events before taking any
    # statistic); this makes the corrected number usable in the meantime.
    if _cstat == "p50nz":
        for _p in ("PCR", "TBR"):
            _d = P["campaign_bands"].get(_p, {}).get("cure", {}).get(
                "hours_deciles") or []
            if len(_d) < 2:
                continue
            _z = sum(1 for _x in _d if _x < 1.0) / len(_d)
            _q = _z + (1.0 - _z) / 2.0
            _i = min(len(_d) - 1, max(0, int(round(_q * len(_d))) - 1))
            mean_camp_h[_p] = float(_d[_i])
    print(f"  campaign-length target ({_cstat}): "
          + "  ".join(f"{p} {mean_camp_h.get(p, 0):.1f}h" for p in ("PCR", "TBR")))
    # campaign-length SHAPE per line, used to give lots realistic variance
    shape_h = {p: [x for x in P["campaign_bands"][p]["cure"].get("hours_deciles", [])
                   if x > 0]
               for p in ["PCR", "TBR"]}
    from planner.data.warehouse import duck, set_cutoff
    set_cutoff(None)
    _unused_mc = None
    _mc_disabled = """
        WITH cg AS (SELECT c.plant, CAST(c.wcID AS VARCHAR) res, b.itemCode gt,
              CAST(c.mouldNo AS VARCHAR) md, c.event_ts
            FROM v_curing c JOIN v_build b ON c.gtbarCode = b.productionID
            WHERE b.stage=2 AND b.itemCode IS NOT NULL AND c.wcID IS NOT NULL
              AND c.mouldNo IS NOT NULL),
        s AS (SELECT *, lag(gt) OVER (PARTITION BY plant,res ORDER BY event_ts) pg,
              lag(md) OVER (PARTITION BY plant,res ORDER BY event_ts) pm FROM cg),
        r AS (SELECT *, sum(CASE WHEN pg IS DISTINCT FROM gt OR pm IS DISTINCT FROM md
                                 THEN 1 ELSE 0 END)
              OVER (PARTITION BY plant,res ORDER BY event_ts) run FROM s)
        SELECT 1"""

    cav_p = {p: float(cav.filter(pl.col("plant") == p)["cavities"].median())
             for p in ["PCR", "TBR"]}
    cyc_p = {p: float(cav.filter(pl.col("plant") == p)["cycle_s"].median())
             for p in ["PCR", "TBR"]}
    mch_p = {p: float(pmc.filter(pl.col("plant") == p)["mould_change_min"].median())
             / 60.0 for p in ["PCR", "TBR"]}
    # PRESS AVAILABILITY REMOVED, BOTH PLANTS. Plant instruction 2026-08-19.
    # L5 dropped the mined MTBF/MTTR haircut (PCR 0.8897, TBR 0.8282) at the same
    # instruction; this site was the SECOND reader and would have left L4.5
    # sizing lots against a slower press than L5 seats them on -- the same limit
    # written twice with two values, which is the duplicated-constant defect
    # PARTITION section 1g records. `PLANNER_PRESS_AVAIL` overrides both plants
    # here exactly as it does in L5, so the two layers can never diverge again.
    _av45 = os.environ.get("PLANNER_PRESS_AVAIL", "")
    PCT = plant_ct.get({q: (float(_av45) if _av45 else 1.0)
                        for q in ("PCR", "TBR")})

    print("=" * 92)
    print(f"L4.5  LOT SIZING & DEMAND CONSOLIDATION  --  {a.month}  (R9, B12)")
    print("=" * 92)
    print("  THE LOT IS THE CURE LOT. Build slices have NO minimum.\n")

    f = MOULD_CHANGE_FRAC_MAX
    print(f"  derived floor: campaign_min_h = mould_change_h x (1-{f})/{f}")
    print(f"  {'plant':<6}{'mould chg h':>13}{'campaign_min h':>16}"
          f"{'cavities':>10}{'cycle s':>9}{'min_cure_lot':>14}{'B12 fixed':>11}")
    camp_min, derived_floor = {}, {}
    for p in ["PCR", "TBR"]:
        ch = mch_p[p]
        cmin = ch * (1 - f) / f
        camp_min[p] = cmin
        lot = cav_p[p] * (cmin * 3600.0 / cyc_p[p])
        derived_floor[p] = lot
        print(f"  {p:<6}{ch:>13.2f}{cmin:>16.1f}{cav_p[p]:>10.2f}"
              f"{cyc_p[p]:>9.0f}{lot:>14.0f}{min_lot[p]:>11}")
    print("  -> derived and fixed floors agree within ~40%, from independent routes")
    # REFERENCE ONLY -- not used for sizing. Sizing cure lots from
    # press_hours / mean_campaign_hours reproduces the plant's campaign SHAPE
    # exactly (213 campaigns vs 210, lot 1,848 vs 1,874, mould changes 371->82)
    # but drops fulfilment from 453,812 to 377,208 tyres: a 263 h campaign cannot
    # be packed into a COLD 744 h window without large unusable tails.
    # The plant does not plan cold -- 23% (PCR) / 36% (TBR) of its July campaigns
    # were already running on 1 July. Until L5 supports warm start and horizon
    # carry-out, the visit law packs better even though its shape is wrong.
    print(f"\n  cure lots sized from press-hours / mean cure-campaign hours (L0): "
          + "  ".join(f"{p} {mean_camp_h.get(p, 0):.1f}h" for p in ["PCR", "TBR"]))

    # ---- 1. aggregate to mould-set level --------------------------------
    r = (req.join(ms.select(["plant", "mould_set"]).unique(), on="plant", how="left")
         if "mould_set" not in req.columns else req)
    gsets = pl.read_parquet(D / f"cie_mould_sets_{a.month}.parquet")
    # cie_mould_sets is per (plant, mould_set); re-derive the GT->set map
    from planner.cmbc.l25_cie import mould_sets
    dem = pl.read_parquet(ROOT / "masters" / "demand" / f"demand_{a.month}.parquet")
    g2s = mould_sets(dem)
    r = req.join(g2s, on=["plant", "gt_code"], how="left")
    r = r.with_columns(pl.col("mould_set").fill_null(
        pl.concat_str([pl.lit("SOLO::"), pl.col("plant"), pl.lit("::"),
                       pl.col("gt_code")])))
    r = r.join(mo.select(["plant", "gt_code", "moulds"]),
               on=["plant", "gt_code"], how="left").with_columns(
        pl.col("moulds").fill_null(0))

    # ---- 2-5. size the lots ---------------------------------------------
    rows = []
    for x in r.iter_rows(named=True):
        p, gt = x["plant"], x["gt_code"]
        # THE CURE LOT IS SIZED FROM THE CURE REQUIREMENT, NOT FROM gross_build.
        # This layer sizes CURE lots (see the module docstring: "the cure lot;
        # build slices have no minimum"), and `gross_build` is a BUILD quantity --
        # it has already had opening green stock subtracted. Using it here planned
        # `from_stock` fewer press cycles than the month needs, every month, on
        # both plants: 4,820 / 1,251 (Jul PCR/TBR), 4,476 / 1,026 (Aug), equal to
        # `from_stock` to the tyre. Opening stock decides WHO FEEDS a press cycle,
        # never how many cycles exist. Fixed 2026-08-18 with the column L4 was
        # missing; fall back only for a pre-2026-08-18 artefact.
        need = float(x.get("cure_requirement", x["gross_build"]) or 0.0)
        if x["residual"] or need <= 0:
            rows.append({**{k: x[k] for k in ("plant", "gt_code", "mould_set")},
                         "need": need, "n_lots": 0, "lot_qty": 0.0,
                         "min_lot": 0.0, "max_lot": 0.0,
                         "policy": "residual: below min_demand", "capped": False})
            continue
        moulds = int(x["moulds"]) or 1
        # The floor answers "is it worth mounting a mould AT ALL?", so it is
        # per-MOULD, not per mould-set. Scaling it by the full mould count gave
        # a GT with 43 moulds a floor of 9,288 tyres, which is a capacity
        # statement, not an economic minimum. Mounting more moulds is a choice
        # that raises throughput; it does not raise the minimum worth running.
        # PLANT CURE TIME per GT. The plant-median press rate charged every GT
        # the same tyres/press-hour; the plant's own file spans 10-20 min/cycle
        # on PCR, so both the economic floor and the R5 ceiling below were out by
        # up to +/-25 % per GT. Fallback is the old plant-median value, so a GT
        # the plant file does not name behaves exactly as before.
        _r1 = PCT.press_rate(p, gt)
        rate_h_1 = _r1 if _r1 else cav_p[p] * 3600.0 / cyc_p[p]  # tyres/PRESS-h
        mn = max(rate_h_1 * camp_min[p], float(min_lot[p]))
        rate_h = moulds * rate_h_1                    # tyres/h across its moulds
        # R5 ceiling: a campaign may not outlast the shelf life of its own tyres
        mx = rate_h * GT_SHELF_LIFE_H
        if need < mn:
            # Genuinely cannot form an economic lot. Step 6 forbids both a
            # silent round-up (builds dead stock) and a silent drop (loses
            # demand), so it goes to the residual policy with quantity intact.
            rows.append({"plant": p, "gt_code": gt, "mould_set": x["mould_set"],
                         "need": need, "n_lots": 0, "lot_qty": 0.0,
                         "min_lot": round(mn, 1), "max_lot": 0.0,
                         "policy": f"residual: need {need:.0f} < min_cure_lot "
                                   f"{mn:.0f}", "capped": False})
            continue
        # SIZE FROM CURE PRESS-HOURS, NOT A BUILD-DERIVED VISIT COUNT.
        # CIE's n_campaigns comes from the visit law, which L0 fits on v_build.
        # Build campaigns are 7.6 h / 5.4 h; cure campaigns are 169.6 h / 270.0 h.
        # The build:cure ratio is 7.7:1 on PCR and 39:1 on TBR, so a build-derived
        # count errs in OPPOSITE directions per line -- PCR came out 2.02x the
        # band, TBR 0.33x. Sizing on cure hours removes the mismatch at source.
        mean_h = mean_camp_h.get(p)
        if not mean_h:
            raise SystemExit(f"L0 params carry no cure hours_mean for {p}")
        n = max(1, math.ceil((need / max(rate_h_1, 1e-9)) / mean_h))
        # ---- CONCURRENCY IS CHOSEN, NOT INHERITED (PLANNER_L45_CONC_FLOOR) --
        #
        # `n` above is a LENGTH decision -- how many campaigns of ~mean_h hours
        # this GT's press-hours make. Concurrency then falls out of it as a
        # by-product, because a GT can never run on more presses at once than it
        # has lots. Measured on GT 1513: 35 moulds, 44 eligible presses, the
        # plant ran 21 CONCURRENTLY; we peak at 15 because the decomposition
        # never produced enough lots to do better.
        #
        # The floor is arithmetic, not mined: a GT needs at least
        #     k_min = ceil( need / (press rate x horizon hours) )
        # presses running together to finish inside the month at all. Below that
        # the campaign MUST spill past the boundary -- which is exactly the
        # "plant completes its SKUs in-month, we exceed" symptom.
        # CONC_FLOOR scales it, capped by the physical mould count (R3).
        if CONC_FLOOR > 0.0:
            _kmin = max(1, math.ceil(need / max(rate_h_1 * HZ_H, 1e-9)))
            _kf = min(int(moulds), int(math.ceil(CONC_FLOOR * _kmin)))
            n = max(n, _kf)
        # RESTORE VARIANCE. Splitting need into n EQUAL lots made every campaign
        # the same length -- PCR p50 172.3 == p90 174.9 == max 174.9, against a
        # plant spread of 153 -> 535 -> 744 h. Uniform blocks leave an unusable
        # tail on every press. Distribute across the observed decile shape
        # instead, rescaled so total press-hours are unchanged.
        # Deterministic: quantile positions, no RNG (determinism contract).
        sh = shape_h.get(p) or []
        if n > 1 and len(sh) >= 2:
            picks = [sh[(i * len(sh)) // n] for i in range(n)]
            if sum(picks) > 0:
                lot_list = integer_split(need, picks, mn)
                # a lot below the economic floor is merged into its neighbour
                # rather than rounded up, which would overproduce
                while len(lot_list) > 1 and min(lot_list) < mn:
                    i = min(range(len(lot_list)), key=lambda k: lot_list[k])
                    j = max(range(len(lot_list)), key=lambda k: lot_list[k])
                    lot_list[j] += lot_list[i]
                    lot_list.pop(i)
                n = len(lot_list)
            else:
                lot_list = None
        else:
            lot_list = None
        qty = need / n
        pol = []
        if qty < mn:                       # consolidate: fewer, larger lots
            n = max(1, int(math.floor(need / mn)))
            qty = need / n
            pol.append("consolidated to floor")
        if qty > mx:                       # split: shelf life forbids the size
            n = int(math.ceil(need / mx))
            qty = need / n
            pol.append("split at 72 h ceiling")
        qty = float(int(round(qty)))          # whole tyres, never a fraction
        capped = qty > mx
        # ---- PER-LOT DEADLINE, FROM THE ORDER BOOK ------------------------
        #
        # THE SIGNAL WE WERE THROWING AWAY. `demand_<M>.parquet` carries
        # `due_date` and `day` per SKU line -- the only real timing information
        # the plant gives us. Neither `net_requirement` nor `l45_lots` carried it
        # forward, so by L5 the plan had NO deadlines at all: it sorted by -qty
        # and placed at earliest-feasible, which produces a flat concurrency
        # curve by construction.
        #
        # Verified 2026-08-14 -- the plant's "S-curve" is not a scheduling
        # behaviour, it is its due dates:
        #     GT 1513  plant day-21 cumulative 57.2%  |  57.2% DUE by day 21
        #     GT 1503                          59.0%  |  59.0% DUE
        #     GT 2476                          69.6%  |  69.6% DUE
        #     GT1915   finishes day 21                |  last due day 26
        # Exact match on every one. The plant simply builds to its order book.
        #
        # That is also why "mined statistic as a constraint" keeps recurring in
        # this codebase: the real timing signal was discarded at L4, so timing
        # had to be reconstructed from tau*, hours_mean and the visit law.
        #
        # Each lot gets the day by which its own cumulative share is due, so a
        # GT whose demand ends on day 26 is pulled forward and one with 43% due
        # after day 21 becomes late-heavy -- without naming either explicitly.
        _dl = None
        _ph = _phase.get((p, gt))
        if _ph:
            _sizes = lot_list if lot_list else [qty] * n
            _cum = 0.0
            _dl = []
            _tot = sum(_sizes) or 1.0
            for _q in _sizes:
                _cum += _q
                _target = _cum / _tot
                _run = 0.0
                _day = _ph[-1][0]
                for _d, _dq in _ph:
                    _run += _dq
                    if _run / max(_ph_tot.get((p, gt), 1.0), 1e-9) >= _target:
                        _day = _d
                        break
                _dl.append(int(_day))
        rows.append({"plant": p, "gt_code": gt, "mould_set": x["mould_set"],
                     "need": need, "n_lots": n, "lot_qty": round(qty, 1),
                     "lot_sizes": ([round(v, 1) for v in lot_list]
                                   if lot_list else [round(qty, 1)] * n),
                     "lot_deadlines": _dl,
                     "min_lot": round(mn, 1), "max_lot": round(mx, 1),
                     "policy": "; ".join(pol) or "cure-hours shape",
                     "capped": capped})
    lots = pl.DataFrame(rows)
    lots.write_parquet(D / f"l45_lots_{a.month}.parquet")
    _stamp.write(D / f"l45_lots_{a.month}.parquet")

    act = lots.filter(pl.col("n_lots") > 0)
    print("\n  LOT PLAN")
    print(f"  {'plant':<6}{'GTs':>5}{'lots':>7}{'qty/lot p50':>13}"
          f"{'consolidated':>14}{'split@72h':>11}{'below floor':>13}")
    for p in ["PCR", "TBR"]:
        s = act.filter(pl.col("plant") == p)
        if not s.height:
            continue
        con = s.filter(pl.col("policy").str.contains("consolidated")).height
        spl = s.filter(pl.col("policy").str.contains("split")).height
        bad = s.filter(pl.col("lot_qty") < pl.col("min_lot")).height
        print(f"  {p:<6}{s.height:>5}{int(s['n_lots'].sum()):>7}"
              f"{float(s['lot_qty'].median()):>13,.0f}{con:>14}{spl:>11}{bad:>13}")

    # ---- 6. residual policy ---------------------------------------------
    res = lots.filter(pl.col("n_lots") == 0)
    print(f"\n  RESIDUAL POLICY (step 6 -- never a silent drop)")
    print(f"    {res.height} GTs, {int(res['need'].sum()):,} tyres held for:")
    print("      1. consolidate into the next campaign for that mould set")
    print("      2. over-produce to stock if carrying < changeover cost")
    print("      3. surface as a priced exception with BOTH costs shown")
    print("    -> decided in the cost table, not here")

    # ---- gates -----------------------------------------------------------
    g1 = act.filter(pl.col("lot_qty") < pl.col("min_lot")).height
    g2 = act.filter(pl.col("capped")).height
    tot = int(act["n_lots"].sum())
    print(f"\n  GATES")
    print(f"    lots below min_cure_lot : {g1}  {'PASS' if g1 == 0 else 'FAIL'}")
    print(f"    lots over the 72 h bound: {g2}  {'PASS' if g2 == 0 else 'FAIL'}")
    print(f"    total cure lots         : {tot}")
    print(f"    build-slice minimum     : NOT CHECKED -- deliberate (see docstring)")
    print(f"\n  -> l45_lots_{a.month}.parquet")


if __name__ == "__main__":
    main()
