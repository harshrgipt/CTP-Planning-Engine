"""PHASE 3 -- CURING PLAN. The constraint is sized first.

Theory of Constraints: curing is the bottleneck (171 presses, ~9,000 press-hours
against 20 building machines). Subordinate everything to it. Building gets no
objective of its own; it is a feasibility tracker for the press plan.

STRIP PACKING AT FIXED AREA. Each GT is a rectangle whose AREA is fixed by
demand and whose SHAPE is the only decision:

    area_g = N_g / rate     press-days      INVARIANT
    n_g    = presses        integer, free
    D_g    = area_g / n_g   days            DERIVED, never free
    a_g    = start day      free

campaign = window, so       campaigns   = sum_g n_g
                            changeovers = sum_g n_g - |P|      closed form.

TWO FORMULATIONS ARE WITHDRAWN, both empirically:

  (a) D* = argmin_D sum_g ceil(W_g/(24 D)) - |P|
      No interior minimum. The objective decreases monotonically in D and every
      stated constraint bounds D from BELOW, so the argmin is always D = H.

  (b) Selecting D on the PREDICTED stagger peak.
      The peak is only an upper bound on realised load, so it mis-ranks
      configurations -- it chose an 802h plan over a 744h one.

Search the integer n_g lattice instead, and repair on measured placement.
"""
from __future__ import annotations

import math
from datetime import date

import polars as pl

from planner.engine.contract import ordered
from planner.engine.resolve import DEFAULT_RATE, SETUP_DAYS, Masters
from planner.runs.logger import log

FLATTEN_K = 2
MAX_ROUNDS = 60
STALE_ROUNDS = 5


def _place(order: list[tuple[str, float]], n: dict[str, int], D: dict[str, int],
           H: int, early: set[str]) -> tuple[dict[str, int], list[float]]:
    """Best-fit-decreasing on area; place each rectangle where the peak is lowest.

    Windows never start on day 0 -- a press mounted then has nothing to pull,
    because its GT's first tyres are built that same day and only become
    eligible the following shift.

    EXCEPT for GTs holding opening stock, which start on day 0: they already
    have material. Excluding them cost an entire first day -- 513 press-shifts,
    ~15,700 tyres, roughly half the month-end WIP overhang, while 5,948 tyres
    of carry-over sat waiting.
    """
    load = [0.0] * H
    a: dict[str, int] = {}
    for g, _area in order:
        d_g = D[g]
        if g in early:
            best_a = 0
        else:
            best_a, best_key = 1, None
            for s in range(1, max(2, H - d_g + 1)):
                key = (max(load[s:s + d_g]) + n[g], sum(load[s:s + d_g]))
                if best_key is None or key < best_key:
                    best_a, best_key = s, key
        for d in range(best_a, min(best_a + d_g, H)):
            load[d] += n[g]
        a[g] = best_a
    return a, load


def _moulds_per_gt(plant: str, req) -> dict:
    """Active mould sets per GT, from the plant's own mould master.

    Keyed by SKU in the master, so it is rolled up to GT through the demand
    mapping. A GT with no master entry returns nothing and stays unbounded --
    absence of data must not silently tighten the plan.
    """
    from pathlib import Path

    f = Path(__file__).resolve().parent.parent.parent / "masters" /         "Master_Mapping_Mould_SKU.csv"
    if not f.exists() or getattr(req, "demand", None) is None:
        return {}
    try:
        m = pl.read_csv(f, infer_schema_length=0)
        m = m.rename({"Matl.Code": "sku"})
        if "Active Flag" in m.columns:
            m = m.filter(pl.col("Active Flag") == "1")
        m = m.group_by("sku").agg(pl.col("Mould").n_unique().alias("n"))
        # req.demand drops `sku` at the contract boundary, so the SKU->GT
        # rollup has to come from the demand file itself.
        mo = f"{req.plan_start.year:04d}-{req.plan_start.month:02d}"
        df = f.parent / "demand" / f"demand_{mo}.parquet"
        if not df.exists():
            return {}
        d = pl.read_parquet(df).filter(pl.col("plant") == plant)
        j = (d.group_by(["gt_code", "sku"]).agg(pl.col("qty").sum())
             .join(m, on="sku", how="inner")
             .group_by("gt_code").agg(pl.col("n").sum().alias("moulds")))
        return {r["gt_code"]: int(r["moulds"]) for r in j.iter_rows(named=True)}
    except Exception as e:                                      # noqa: BLE001
        log.warning("campaigns.mould_master_failed", err=str(e))
        return {}


def plan_campaigns(req, ms: Masters, opening_qty: dict[tuple[str, str], int],
                   book_margin: dict[str, float] | None = None
                   ) -> tuple[dict, dict, dict]:
    """Return (campaigns, cure_profile, stats).

    campaigns    {(plant, gt): [(press, start_day, end_day), ...]}
    cure_profile {plant: {gt: [tyres per day]}}   -- planned press capacity
    """
    H = req.horizon_days
    campaigns: dict[tuple[str, str], list[tuple[str, int, int]]] = {}
    profile: dict[str, dict[str, list[float]]] = {}
    stats: dict = {}

    tot = (req.demand.group_by(["plant", "gt_code"])
           .agg(pl.col("qty").sum().alias("N")).sort(["plant", "gt_code"]))

    for plant in ordered(tot["plant"].unique().to_list()):
        sub = tot.filter(pl.col("plant") == plant)
        P = ms.presses.get(plant, [])
        if not P:
            continue
        nP = len(P)
        rate = ms.rate.get(plant, DEFAULT_RATE.get(plant, 100.0))
        N_of = {r["gt_code"]: float(r["N"]) for r in sub.iter_rows(named=True)}
        # PER-GT rate where the evidence supports one, plant median otherwise.
        # Cure speed is a property of the GT, not the press: PCR GTs run 41-200
        # tyres/press-day against a 151 median, so a flat rate mis-sizes a third
        # of campaigns by >20%.
        rate_of = {g: ms.gt_rate.get((plant, g), rate) for g in N_of}
        # BOOKING MARGIN, measured not guessed. area = N_g/rate books exactly the
        # work plus its mould changes and NOTHING else, so the plan carried ~1%
        # headroom on the largest GTs and any stock-timing loss (starved 1.5%,
        # idle 1.7%, part-filled shifts) became shortfall. The grid reports what
        # fraction of a mounted press-shift it actually converts, so the margin
        # is another fixed-point quantity rather than a constant -- pass 1 books
        # bare and pass 2 books 1/fill. This is NOT the old f_book: that inflated
        # blindly and merely relabelled idle as starvation, because there was no
        # stable feedback signal to size it against.
        mg = (book_margin or {}).get(plant, 1.0)
        area = {g: v / rate_of[g] * mg for g, v in N_of.items()}
        elig = {g: [p for p in ms.press_of.get((plant, g), []) if p in set(P)]
                for g in N_of}
        # ---- PRESS COUNT BOUNDED BY MOULDS OWNED (R3 / R10) -------------
        # A press cannot be mounted on GT g without a mould set for g, and mould
        # sets are countable physical assets. Master_Mapping_Mould_SKU.csv has
        # them; they were being used for verification only, which is why the
        # plan mounted 9 presses on a TBR GT that owns 8 moulds.
        # This also bounds R_g -- press capacity mounted beyond what building
        # can feed starves BY CONSTRUCTION (median R was 0.88, 103 of 104 GTs
        # under 1.0). Both limits are physical, so take the tighter of them.
        moulds = _moulds_per_gt(plant, req)
        cap = {}
        for g in area:
            phys = len(elig.get(g) or P)
            mo = moulds.get(g)
            if mo:
                phys = min(phys, mo)
            cap[g] = max(1, phys)
        log.info("campaigns.mould_cap", plant=plant, gts_with_mould_data=len(moulds), bounded=sum(1 for g in area if moulds.get(g) and moulds[g] < len(elig.get(g) or P)))
        early = {g for g in area if opening_qty.get((plant, g), 0) > 0}
        order = sorted(area.items(), key=lambda t: (t[0] not in early, -t[1], t[0]))

        # ---- integer n_g lattice with FLATTEN repair --------------------
        # FLATTEN means n_g - 1 (lower height, wider window at the same area).
        # n_g + 1 makes the rectangle TALLER and raises the very peak it is
        # repairing: on PCR that ran 185 rounds, collapsed windows 21d -> 5d
        # and took campaigns 132 -> 443. Reversed, it converges in ~6.
        n = {g: max(1, min(cap[g], math.ceil(area[g] / H))) for g in area}
        best = None
        stale = 0
        rounds = 0
        for rounds in range(1, MAX_ROUNDS + 1):
            D = {g: max(1, min(H, math.ceil(area[g] / n[g] + SETUP_DAYS)))
                 for g in area}
            a, load = _place(order, n, D, H, early)
            peak = max(load) if load else 0.0
            if best is None or peak < best[0]:
                best, stale = (peak, dict(n), dict(D), dict(a), list(load)), 0
            else:
                stale += 1
            if peak <= nP or stale >= STALE_ROUNDS:
                break
            d_star = max(range(H), key=lambda d: (load[d], -d))
            live = [g for g in ordered(area)
                    if a[g] <= d_star < a[g] + D[g] and n[g] > 1 and D[g] < H]
            if not live:
                break
            for g in sorted(live, key=lambda x: (-n[x], x))[:FLATTEN_K]:
                n[g] -= 1
        _pk, n, D, a, load = best

        # ---- press assignment: interval graph colouring ------------------
        # Interval graphs are PERFECT, so a greedy sweep in start-day order is
        # optimal when max daily load <= |P|. Whole-window rectangles give one
        # campaign per (GT, press) and the closed-form changeover count.
        booked: dict[str, list[tuple[int, int]]] = {p: [] for p in P}

        def free(p: str, s: int, e: int) -> bool:
            return all(e <= bs or s >= be for bs, be in booked[p])

        short = 0.0
        frags = 0
        for g in sorted(area, key=lambda x: (a[x], x)):
            s, e = a[g], min(a[g] + D[g], H)
            pref = [p for p in (elig.get(g) or []) if free(p, s, e)]
            rest = [p for p in P if p not in set(pref) and free(p, s, e)]

            def gap(p: str) -> tuple:
                return (-max([b for _x, b in booked[p]] or [0]), p)

            pick = (sorted(pref, key=gap) + sorted(rest, key=gap))[:n[g]]
            chosen = [(p, s, e) for p in pick]
            for p in pick:
                booked[p].append((s, e))
            need = area[g] - sum((e2 - s2) - SETUP_DAYS for _p, s2, e2 in chosen)

            # HYBRID top-up. Whole-window rectangles are the ideal, but strip
            # packing only works while the box has slack: at TBR's 85% fill they
            # land in one round at the theoretical changeover floor, while at
            # PCR's 93% they cannot all be placed. The fragments are NOT waste
            # -- they are what makes a 93%-fill packing feasible.
            if need > 1e-9:
                for tier_i, tier in enumerate((list(elig.get(g) or []), P, P)):
                    scan = 0 if tier_i == 2 else s
                    for p in sorted(tier, key=gap):
                        if need <= 1e-9:
                            break
                        s2 = scan
                        while s2 < H and not free(p, s2, s2 + 1):
                            s2 += 1
                        e2 = s2
                        while e2 < H and free(p, e2, e2 + 1) and (e2 - s2) < D[g]:
                            e2 += 1
                        if e2 <= s2:
                            continue
                        e2 = min(e2, s2 + max(1, math.ceil(need + SETUP_DAYS)))
                        gain = (e2 - s2) - SETUP_DAYS
                        if gain <= 0:
                            continue
                        booked[p].append((s2, e2))
                        chosen.append((p, s2, e2))
                        frags += 1
                        need -= gain
                    if need <= 1e-9:
                        break
            short += max(0.0, need)
            if chosen:
                campaigns[(plant, g)] = sorted(chosen, key=lambda t: (t[1], t[0]))

            # planned cure capacity per day. The FIRST day of a campaign is
            # consumed by the mould change, so it yields only (1 - 1/3).
            live_d = [0.0] * H
            for _p, s2, e2 in chosen:
                for d in range(s2, min(e2, H)):
                    _r = rate_of[g]
                    live_d[d] += _r * (1.0 - SETUP_DAYS) if d == s2 else _r
            profile.setdefault(plant, {})[g] = live_d

        camps = sum(len(v) for (pl_, _g), v in campaigns.items() if pl_ == plant)
        stats[plant] = {
            "presses": nP, "gts": len(area), "rate": rate,
            "area_press_days": round(sum(area.values())),
            "fill_pct": round(100 * sum(area.values()) / (nP * H), 1),
            "flatten_rounds": rounds, "peak_load": round(max(load), 1),
            "campaigns": camps, "changeovers_pred": camps - nP,
            "changeover_floor": sum(max(1, math.ceil(area[g] / H))
                                    for g in area) - nP,
            "fragments": frags, "press_days_short": round(short, 1),
            "D_g_p50": sorted(D.values())[len(D) // 2] if D else 0,
            "day0_gts": len(early),
        }
    log.info("engine.campaigns", **{f"{k}_{kk}": vv for k, v in stats.items()
                                    for kk, vv in v.items()})
    return campaigns, profile, stats
