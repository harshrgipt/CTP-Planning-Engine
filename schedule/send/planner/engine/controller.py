"""PHASE 4 -- INVENTORY CONTROLLER. Base-stock, derived from the lot policy.

Little's Law is the whole model:  I = lambda x W.  Measured on 8 months, the
plant holds ~9 hours of production as green tyres -- 8.4 to 10.9 h, both plants,
every month -- and its mean build->cure lag is ~9 h. The stock IS the lag.

PER GT the distribution is not uniform, and copying the aggregate gets the mix
wrong (a failure mode this engine has hit three times):

    aggregate (draw-weighted) cover   9.0 h
    MEDIAN GT cover                  12.6 h      <- higher than aggregate
    p25 / p75                    3.9 / 24.9 h
    GT-months holding zero        19% / 10%

Median above aggregate means high-draw GTs carry LESS cover -- they are
replenished more often. That is the signature of a lot-interval policy:

    I*_g = draw_g x T_g / 2

Check: T_g = 24h -> 12h cover (median 12.6h). T_g = 48h -> 24h (p75 24.9h).
T_g = 12h -> 6h (p25 3.9h). The target is DERIVED from lot sizing, not a new
parameter -- once Q_g = draw_g.T_g/24 is fixed, I*_g falls out.

THE CONSTRAINT IS NO TREND, NOT A TIGHT BAND. The plant's own stock swings
sd 530/day on a level of 4,820 and ranges ~3,400-6,200; a days-in-band test
would fail the plant itself. Enforce E[dI] ~ 0 and leave sd(dI) alone.
"""
from __future__ import annotations

import math
import statistics as st

from planner.engine.contract import ordered

A_MAX_DAYS = 7      # freshness valve: never hold an accumulation longer
from planner.runs.logger import log


def cover_law(plant: str) -> tuple[float, float]:
    """Fit cover_h = a * draw^b from the plant's own history. Leak-free.

    THE PLANT DOES NOT USE ONE REPLENISHMENT INTERVAL. Measured over 8 months,
    cover falls hard as draw rises -- r(draw, cover) = -0.650 (PCR) / -0.804
    (TBR):

        draw/day    PCR cover -> T_g      TBR cover -> T_g
        <100         20.1h       40h       14.2h       28h
        100-300      15.7h       31h        7.1h       14h
        300-700      11.3h       22h        3.4h        7h
        >700          5.9h       12h          --        --

    A uniform T_g = 24h is roughly right for mid-runners and TWICE too much for
    high-runners -- and high-runners carry most of the volume, so the aggregate
    is dominated by them. That is the whole of our 24h aggregate cover against
    the plant's 8.8h, and why our median GT cover matched while the aggregate
    did not. Aggregate-right/mix-wrong, for the fourth time.

    Fit the law rather than hard-code tiers: the exponent is a property of the
    plant's replenishment policy, and it is re-derived under the as-of cutoff
    every run.
    """
    import math

    from planner.data.warehouse import duck
    try:
        rows = duck().execute("""
            WITH pr AS (
                SELECT b.itemCode gt, date_trunc('month', c.event_ts) mo,
                       count(*) n, count(DISTINCT CAST(c.event_ts AS DATE)) AS nd,
                       avg(date_diff('second', b.event_ts, c.event_ts)/3600.0) cov
                FROM v_build b JOIN v_curing c ON b.productionID = c.gtbarCode
                WHERE b.stage=2 AND c.statuscritical='Normal'
                  AND c.event_ts >= b.event_ts AND b.plant = ?
                  AND b.itemCode IS NOT NULL
                GROUP BY 1,2 HAVING count(*) > 300)
            SELECT n::DOUBLE/nd AS draw, cov FROM pr WHERE cov > 0
        """, [plant]).fetchall()
        pts = [(math.log(float(d)), math.log(float(c)))
               for d, c in rows if d and c and float(d) > 0 and float(c) > 0]
        if len(pts) < 20:
            return 0.0, 0.0
        mx = sum(p[0] for p in pts) / len(pts)
        my = sum(p[1] for p in pts) / len(pts)
        vx = sum((p[0] - mx) ** 2 for p in pts)
        b = sum((p[0] - mx) * (p[1] - my) for p in pts) / vx if vx else 0.0
        a = math.exp(my - b * mx)
        return a, b
    except Exception as e:  # noqa: BLE001
        log.warning("controller.cover_law_failed", plant=plant, err=str(e))
        return 0.0, 0.0


def run_controller(gts: list[str], H: int,
                   cure_of: dict[str, list[float]],
                   N_of: dict[str, float],
                   area: dict[str, float],
                   opening_of: dict[str, float],
                   scrap: float, zero_area: float,
                   T_g_h: float,
                   law: tuple[float, float] | None = None,
                   target_I: float = 0.0,
                   cap: float = 0.0,
                   real_of: dict[str, list[float]] | None = None,
                   ihat_of: dict[str, list[float]] | None = None,
                   consume_of: dict[str, list[float]] | None = None,
                   floor_of: dict[str, float] | None = None,
                   kp: float = 0.3
                   ) -> tuple[dict[str, list[int]], dict]:
    """Deadbeat base-stock control. Returns (build[g][day], stats).

        build(g,d) = cure(g,d) + (I*_g - I_g(d))

    No gain term: the plan is deterministic, so there is nothing to damp.

    `cure_of` is whichever cure profile you believe. Pass PLANNED press capacity
    on the first pass and the grid's REALISED cure on later ones -- the planner
    credits a campaign day with a full press-day while the grid delivers ~87% of
    it, so a single pass drains the plan while reality accumulates.
    """
    gts = ordered(gts)
    stock = {g: float(opening_of.get(g, 0.0)) for g in gts}
    active = {g: max(1, sum(1 for x in cure_of.get(g, []) if x > 0)) for g in gts}
    draw = {g: N_of[g] / active[g] for g in gts}

    a, b = law or (0.0, 0.0)
    I_star: dict[str, float] = {}
    for g in gts:
        if area.get(g, 0.0) <= zero_area:
            I_star[g] = 0.0                              # one campaign: cure on build
            continue
        if a > 0 and draw[g] > 0:
            # cover_h = a * draw^b, fitted from the plant. b is NEGATIVE:
            # high-runners are replenished more often and carry LESS cover.
            cover_h = a * (draw[g] ** b)
        else:
            cover_h = T_g_h / 2.0                        # fallback: flat draw*T/2
        I_star[g] = max(draw[g] * cover_h / 24.0,
                        draw[g] * 4.0 / 24.0)            # floor: half a shift

    # ---- NORMALISE THE VECTOR TO THE G8 BAND ---------------------------
    # cover_h = T_g/2 = 12.0h put the setpoint at 527/h x 12 = 6,324 tyres --
    # 32% ABOVE the band ceiling of 4,800 before a single unit of execution
    # error. You cannot hold a band whose ceiling sits below your own setpoint,
    # so the band has to set the target rather than the replenishment interval.
    #
    # The SHAPE stays as derived (tau + batching floor + margin); only the LEVEL
    # is rescaled, so the relative buffering between a high-runner and a tail GT
    # is untouched. `target_I` is the band midpoint from CONFIG -- a plant-given
    # business rule (G8), not a constant fitted to one month.
    #
    # ORDER MATTERS: this must land AFTER the release gate is gone. Cutting the
    # target while stock is phase-locked starves the presses instead of freeing
    # inventory -- that is exactly the 99.16 -> 98.74 the cover law produced.
    if target_I and target_I > 0:
        raw = sum(I_star.values())
        if raw > 0:
            k = target_I / raw
            I_star = {g: v * k for g, v in I_star.items()}

    # Gross-up for scrap: the plant loses `scrap` of every tyre built, so
    # delivering N_g finished tyres needs N_g x (1 + scrap) green ones. Do NOT
    # target 1.000 -- you would systematically under-deliver by 0.5-2.0%.
    #
    # NET OFF OPENING GREEN-TYRE STOCK (rule R1). Carry-in is supply that already
    # exists; building the full requirement on top of it delivers more than was
    # ordered. Measured on July: 3,778 tyres of the 5,796 over-production came
    # from opening stock being cured IN ADDITION to a full month's build rather
    # than counting against it. R1 says the requirement is computed "after
    # considering ... available green-tyre inventory" -- this is that subtraction,
    # and it was missing.
    remaining = {}
    for g in gts:
        need = float(int(round(N_of[g] * (1.0 + scrap))))
        remaining[g] = max(0.0, need - float(opening_of.get(g, 0.0)))

    build: dict[str, list[int]] = {g: [0] * H for g in gts}
    trace: list[float] = []
    n_capped = 0
    capped_qty = 0.0
    # TWO PROFILES, TWO PURPOSES -- and conflating them is why a cap on the
    # controller's own projection does nothing. The build TARGET is driven by
    # press POTENTIAL: feeding it realized cure makes the loop read the symptom
    # of its own shortfall as a lower requirement and spiral down. But the CAP
    # has to bind on stock that will actually exist, and potential overstates
    # what the grid cures (it books a full press-day, the grid delivers ~87%).
    # Measured, that gap is 1.75x: the controller projected 4,113 for PCR while
    # the ledger realized 7,332, so a 5,000 cap on the projection never fired.
    # `real_of` is the grid's realized cure; `rstock` is the honest trajectory.
    rstock = dict(stock) if real_of else None
    for d in range(H):
        for g in gts:
            prof = cure_of.get(g) or [0.0] * H
            cure_d = min(prof[d] if d < len(prof) else 0.0, stock[g] + remaining[g])
            # OBSERVER: close the loop on the ledger's TIME-INTEGRAL, not on the
            # controller's own day-endpoint projection. The endpoint misses the
            # area under the intra-day profile -- 3,219 tyres on PCR, 78% of the
            # observable. Kp < 1 damps it: at unity this becomes mechanism #3,
            # the ratchet that re-deferred 5.1M tyres over 31 days.
            if ihat_of is not None and d > 0:
                obs = ihat_of.get(g)
                state = obs[d - 1] if obs else stock[g]
                want = max(0.0, min(cure_d + kp * (I_star[g] - state),
                                    remaining[g]))
            else:
                want = max(0.0, min(cure_d + (I_star[g] - stock[g]), remaining[g]))
            q = int(math.floor(want))
            build[g][d] = q
            # DECREMENT BY WHAT IS ACTUALLY CURED, NOT BY BOOKED POTENTIAL.
            # `cure_d` is press CAPACITY (424,164 booked for PCR) while the grid
            # really cures 394,225 -- so crediting potential as consumption told
            # the controller ~30,000 tyres had left that never did. It read its
            # own stock as draining, kept building, and carried a standing
            # +3,000 build-ahead all month. That is the GT stock sitting on top
            # of opening, and it is why cutting I* 4x moved nothing: the
            # correction term (I* - stock) was computed from a stock that was
            # wrong by more than the whole target.
            #
            # The TARGET still uses potential -- aim to feed every mounted press
            # -- so this is not the realised-cure feedback that spirals down
            # (490,133 -> 79,329 -> 16,582). Only the state update is corrected.
            cons = cure_d
            if consume_of is not None:
                cp = consume_of.get(g)
                if cp is not None:
                    cons = min(cp[d] if d < len(cp) else 0.0,
                               stock[g] + q)
            stock[g] += q - cons
            remaining[g] -= q
            if rstock is not None:
                rp = real_of.get(g) or [0.0] * H
                rstock[g] += q - min(rp[d] if d < len(rp) else 0.0, rstock[g])

        # ---- HARD PLANT-LEVEL CAP on the day's closing stock -------------
        # Enforced HERE, on the build quantities, and not by deferring lots
        # that have already been placed. Deferring placed lots was tried twice
        # and fails structurally: a GT's press is mounted for a fixed window, so
        # supply that slips by more than its replenishment interval (T_c ~ 19h)
        # misses the window entirely and the cure is lost for good. Measured, a
        # 22h mean deferral cost 80% of curing while inventory ROSE to 293,686,
        # because the volume was built and then had nothing to cure it.
        #
        # Trimming the target instead simply moves the tyre to a later day --
        # `remaining` still carries it, so nothing is destroyed, and the builder
        # places lots for the reduced quantity so the press windows still line
        # up with real supply.
        #
        # Release order is by COVER (stock/draw), most-covered first: the GT
        # that can wait longest gives up its build first. Ties broken on name
        # for determinism.
        if cap and cap > 0:
            book = rstock if rstock is not None else stock
            total = sum(book.values())
            if total > cap:
                excess = total - cap
                order = sorted(
                    gts, key=lambda g: (-(book[g] / draw[g]) if draw[g] > 0
                                        else -1e9, g))
                for g in order:
                    if excess <= 0:
                        break
                    give = float(int(min(float(build[g][d]), excess)))
                    if give <= 0:
                        continue
                    build[g][d] -= int(give)
                    stock[g] -= give
                    if rstock is not None:
                        rstock[g] -= give
                    remaining[g] += give
                    excess -= give
                    capped_qty += give
                n_capped += 1
        trace.append(sum(stock.values()))

    # ---- LUMPY RELEASE: (s, Q) REORDER POINT (rule B12 / R9) -------------
    # The controller emits a per-GT-per-DAY quantity. For a GT drawing 2/day
    # that is a 2-tyre lot, and no supervisor sets up a machine for 2 tyres.
    # Measured: 49.6% of TBR lots under 60 units, 349 lots <= 20.
    #
    # A floor in the lot SPLITTER cannot fix it -- splitting caps how large a lot
    # may be, it never merges small days. Nor can a post-hoc accumulator: holding
    # a day's build back starves the press that day, so the deadline flush fires
    # almost every day and nothing merges (measured: TBR <60 only 49.6 -> 41.6%,
    # and <=20 rose).
    #
    # Releasing LESS OFTEN requires CARRYING THE STOCK IN BETWEEN. That is a
    # policy, not a merge, and it is the policy the plant actually runs -- the
    # 8-month fit identified order-up-to as its lowest-CV control variable
    # (CV(S) 0.33-0.41 vs CV(Q) 0.45-0.49).
    #
    #   each day:  stock -= cure_d
    #              if stock < s_g:  release Q_g,  stock += Q_g
    #
    # s_g is one cure-day of cover so the press is never dry at the moment of
    # reorder; Q_g is the plant floor. Total released is capped at `remaining`,
    # so this re-times production without adding any.
    if floor_of:
        for g in gts:
            Q = float(floor_of.get(g, 0.0))
            planned = float(sum(build[g]))
            if Q <= 1 or planned <= 0:
                continue
            prof = cure_of.get(g) or [0.0] * H
            d_rate = planned / max(sum(1 for x in prof if x > 0), 1)
            s_pt = max(d_rate, 1.0)
            stk = float(opening_of.get(g, 0.0))
            left = planned
            out = [0] * H
            for d in range(H):
                stk -= (prof[d] if d < len(prof) else 0.0)
                if left > 0 and stk < s_pt:
                    q = min(Q, left)
                    # top up to the reorder point if one lot will not clear it
                    while stk + q < s_pt and q < left:
                        q = min(q + Q, left)
                    out[d] = int(round(q))
                    stk += q
                    left -= q
            if left > 0:                       # never lose planned volume
                out[H - 1] += int(round(left))
            build[g] = out

    n = len(trace)
    mx = (n - 1) / 2.0
    my = sum(trace) / n
    slope = (sum((i - mx) * (v - my) for i, v in enumerate(trace))
             / (sum((i - mx) ** 2 for i in range(n)) or 1.0))
    covers = [24.0 * I_star[g] / draw[g] for g in gts if draw[g] > 0]
    n_zero = sum(1 for g in gts if I_star[g] == 0)
    stats = {
        "zero_hold_gts": n_zero,
        "zero_hold_pct": round(100.0 * n_zero / max(1, len(gts)), 1),
        "cover_h_p50": round(st.median(covers), 1) if covers else 0.0,
        "I_star_total": int(sum(I_star.values())),
        "cap": int(cap), "cap_days": n_capped, "cap_deferred_qty": int(capped_qty),
        "wip_end": int(trace[-1]), "wip_mean": int(my),
        "wip_slope": round(slope, 1),
        "wip_mean_delta": round((trace[-1] - trace[0]) / max(1, n - 1), 1),
        "unbuilt": int(sum(remaining.values())),
    }
    return build, stats


def controller_converged(prev: dict | None, cur: dict, eps: float = 15.0) -> bool:
    """Fixed-point test on the inventory TREND, not on the level."""
    if prev is None:
        return False
    return abs(cur.get("wip_slope", 0.0) - prev.get("wip_slope", 0.0)) < eps
