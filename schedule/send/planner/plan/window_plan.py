"""Curing-first window planner. Curing is decided BEFORE building.

The structural fact the engine was missing: **campaign = window**. One
(GT, press) pair spans the GT's entire active window, so

    campaigns = sum_g n_g          changeovers = sum_g n_g - |P|

in closed form, with no search and no ceiling loss.

Order is inverted relative to the old pipeline. Previously building committed
its schedule and curing consumed whatever appeared, so the probability a press
had stock when mounted was set downstream and could not be repaired -- nine
allocation-side variants each fixed their own metric and left the cure span
between 883 and 1,356h. Here curing is planned first: this module emits the
press campaigns that `plan_curing` executes on a fixed shift grid, and building
is handed a per-(GT, day) target table derived from them.

STRIP PACKING AT FIXED AREA (v3 s2). Each GT is a rectangle whose AREA is fixed
by demand and whose SHAPE is the only decision:

    area_g = N_g / rate        press-days          INVARIANT
    height = n_g               presses             integer, free
    width  = D_g               days                = booked_g / n_g
    origin = a_g               start day           free

Because n_g * D_g is invariant, integerising n_g costs nothing -- it only
re-derives D_g -- and it removes campaign fragmentation by construction.

SWEEP THE INTEGER n_g LATTICE, NOT D (v3 s1, s3.4). The earlier formulation
    D* = argmin_D sum_g ceil(W_g/(24 D)) - |P|
has NO interior minimum: the objective decreases monotonically in D and every
stated constraint bounds D from BELOW, so the argmin is always D = H. Withdrawn.
Selecting D on the *predicted* stagger peak was also withdrawn -- the peak is
only an upper bound on realised load, so it mis-ranks configurations (it chose
802h over 744h). Start at the minimum sum n_g -- the fewest possible campaigns
-- and raise n_g ONLY on the GTs that overload the peak day. Flatten the
offender; do not move it.

NOTHING HERE IS FITTED TO A MONTH. `rate` comes from the same capacity model the
shift grid executes with, and `f_book` from the plant's own measured press-active
days. A constant read off one month's plan cannot be detected as wrong from that
month's own KPIs.
"""
from __future__ import annotations

import math
import statistics as st
from datetime import date, timedelta

import polars as pl

from planner.config import CONFIG
from planner.data.warehouse import duck
from planner.runs.logger import log

# Last-resort fallbacks ONLY, used if a plant has no measured data at all.
DEFAULT_RATE = {"PCR": 156.0, "TBR": 48.0}
DEFAULT_ACTIVE_FRAC = 0.95
# Never book more than this share of the press box. Strip packing degrades
# sharply as fill approaches 1: at 93% it needs a handful of fragment
# campaigns, at 99% it is effectively impossible (the stagger peak hit 131
# against 92 presses). Leave the packer room to place the rectangles.
BOX_FILL_MAX = 0.97
# One 480-min mould change = one shift = 1/3 of a press-day. Structural (the
# plant runs 3 x 480 min shifts), not fitted.
SETUP_DAYS = 1.0 / 3.0
# How many GTs to reshape per repair round. 1-2 per v3 s3.4.
FLATTEN_K = 2

_PRESS_CACHE: dict[str, set[str]] | None = None
_ACTIVE_CACHE: dict[str, float] | None = None


def real_presses() -> dict[str, set[str]]:
    """Presses that physically exist, per plant.

    The derived capability matrix leaks a handful of cross-plant press ids --
    it gave PCR 114 presses against a real 92, so n_g was sized against phantom
    capacity. Intersect against what v_curing has actually seen.
    """
    global _PRESS_CACHE
    if _PRESS_CACHE is None:
        out: dict[str, set[str]] = {}
        for plant, press in duck().execute(
                "SELECT DISTINCT plant, wcID::VARCHAR FROM v_curing").fetchall():
            out.setdefault(plant, set()).add(press)
        _PRESS_CACHE = out
    return _PRESS_CACHE


def press_reserve_ratio() -> dict[str, float]:
    """How many presses the plant MOUNTS per press-day of actual work.

        reserve = presses_used / (cured_per_day / rate)

    The plant runs 87 PCR presses to do 80.6 press-days of work per day, and 80
    TBR presses to do 65.4 -- i.e. it deliberately keeps ~7% (PCR) / ~18% (TBR)
    of its presses mounted-but-not-flat-out. That reserve is what absorbs
    building variance, and both inputs are stable across all 8 months
    (cured_per_day CV 0.033, presses_used CV 0.013), so this is a measured
    property of the plant rather than a figure fitted to one month.

    Booking only the bare work instead leaves ~10% of press-shifts with no
    campaign at all, and WIP then climbs monotonically (4.3x opening by month
    end) because there is no press mounted to absorb it -- idle presses and a
    growing queue at the same time.
    """
    out: dict[str, float] = {}
    try:
        rows = duck().execute("""
            WITH d AS (
                SELECT plant, CAST(event_ts AS DATE) AS dd, count(*) AS n
                FROM v_curing WHERE statuscritical = 'Normal' GROUP BY 1,2
            ), m AS (
                SELECT plant, quantile_cont(n, 0.5) AS per_day FROM d GROUP BY 1
            ), p AS (
                SELECT plant, count(DISTINCT wcID::VARCHAR) AS np
                FROM v_curing WHERE statuscritical = 'Normal' GROUP BY 1
            )
            SELECT m.plant, m.per_day, p.np FROM m JOIN p USING (plant)
        """).fetchall()
        for plant, per_day, np_ in rows:
            out[plant] = (float(np_), float(per_day))
    except Exception as e:  # noqa: BLE001
        log.warning("window.reserve_failed", err=str(e))
    return out


def press_active_frac() -> dict[str, float]:
    """Fraction of a month's days on which a press is active, per plant.

    A FRACTION, not a day count. Measured across 8 months the count is a median
    of 30.0 days -- but February has only 28 calendar days, so dividing the
    horizon by a pooled day count yields f_book = 28/30 = 0.933 in February and
    UNDER-books the box. The defect is invisible in any 31-day month, which is
    exactly the class of bug a single-month test cannot catch.

    Measured (8 months, both plants): the fraction is ~1.0 -- presses are active
    essentially every day, so the booking slack comes from the per-campaign mould
    change charged in the lattice loop, not from downtime. The reference plant's
    28.3/31 (f_book 1.095) does not hold here; using it over-books PCR by 6% and
    recreates the infeasible stagger peak it is meant to fix.
    """
    global _ACTIVE_CACHE
    if _ACTIVE_CACHE is not None:
        return _ACTIVE_CACHE
    out: dict[str, float] = {}
    try:
        rows = duck().execute("""
            WITH pd AS (
                SELECT plant, wcID::VARCHAR AS p,
                       date_trunc('month', event_ts) AS mo,
                       count(DISTINCT date) AS d,
                       date_diff('day', date_trunc('month', event_ts),
                                 date_trunc('month', event_ts)
                                 + INTERVAL 1 MONTH) AS dim
                FROM v_curing WHERE statuscritical = 'Normal' GROUP BY 1,2,3
            )
            SELECT plant, quantile_cont(d::DOUBLE / dim, 0.5) FROM pd GROUP BY 1
        """).fetchall()
        for plant, f in rows:
            if f and 0.0 < float(f) <= 1.0:
                out[plant] = float(f)
    except Exception as e:  # noqa: BLE001
        log.warning("window.active_frac_failed", err=str(e))
    _ACTIVE_CACHE = out
    return out


def scrap_rate() -> dict[str, float]:
    """Fraction of green tyres BUILT that are never cured, per plant.

    build/cure ratio - 1 is not drift when inventory is trend-flat: the excess
    has to leave the system. Measured directly as "built and never cured
    anywhere in the visible history". The plant runs 1.0032 (PCR) / 1.0189
    (TBR) build/cure while its stock stays flat, and this is where that goes.

    Measured under the as-of cutoff, so it stays leak-free -- and it MUST be,
    because TBR is not stationary: 1.09% (Jan) -> 2.87% (May) -> 2.76% (Jun),
    a 2.6x rise across the window. An 8-month constant would plan July with a
    figure 0.7pp too low. PCR is stationary (0.36-0.65%, no trend).

    Right-censoring: tyres built just before the cutoff have had no chance to
    cure yet, so the last 7 days are excluded from the denominator.
    """
    out: dict[str, float] = {}
    try:
        rows = duck().execute("""
            WITH b AS (
                SELECT plant, productionID pid, event_ts
                FROM v_build
                WHERE stage = 2 AND QualityStatus = '1' AND productionID IS NOT NULL
            ), mx AS (SELECT max(event_ts) - INTERVAL 7 DAY AS cut FROM b),
            c AS (
                SELECT DISTINCT gtbarCode pid FROM v_curing
                WHERE statuscritical = 'Normal'
            )
            SELECT b.plant,
                   count(*) FILTER (WHERE c.pid IS NULL)::DOUBLE / count(*)
            FROM b CROSS JOIN mx LEFT JOIN c ON b.pid = c.pid
            WHERE b.event_ts < mx.cut
            GROUP BY 1
        """).fetchall()
        for plant, f in rows:
            if f is not None and 0.0 <= float(f) < 0.2:
                out[plant] = float(f)
    except Exception as e:  # noqa: BLE001
        log.warning("window.scrap_failed", err=str(e))
    return out


def zero_hold_area() -> dict[str, float]:
    """Area (press-days) below which the plant holds NO stock for a GT.

    19% of PCR GT-months and 10% of TBR hold zero. Those GTs cure a median of
    480 (PCR) / 221 (TBR) a month -- about 3 press-days -- i.e. their whole
    demand is one short campaign, so they are cured as they are built and never
    carried. Taken as the median area of the plant's own zero-holders rather
    than a chosen cut-off.

    NB the literal rule "I* = 0 if N_g <= Q_g" is degenerate: with T_0 = 24h,
    Q_g = draw_g = N_g/active_days, so it reduces to active_days <= 1, which is
    ~0% of GTs rather than the observed 19%.
    """
    out: dict[str, float] = {}
    try:
        rows = duck().execute("""
            WITH pairs AS (
                SELECT b.plant, b.itemCode gt,
                       date_trunc('month', c.event_ts) mo, c.event_ts c_ts
                FROM v_build b JOIN v_curing c ON b.productionID = c.gtbarCode
                WHERE b.stage = 2 AND c.statuscritical = 'Normal'
                  AND b.itemCode IS NOT NULL
            )
            SELECT plant, mo, gt, count(*) cured FROM pairs GROUP BY 1,2,3
        """).fetchall()
        by = {}
        for plant, mo, gt, cured in rows:
            by.setdefault(plant, []).append(float(cured))
        for plant, v in by.items():
            v.sort()
            # the zero-holders are the bottom ~19%; take their upper edge
            cut = v[int(0.19 * len(v))] if v else 0.0
            out[plant] = cut / DEFAULT_RATE.get(plant, 100.0)
    except Exception as e:  # noqa: BLE001
        log.warning("window.zero_area_failed", err=str(e))
    return out


def run_controller(gts: list[str], H: int, cure_of: dict[str, list[float]],
                   N_of: dict[str, float], area: dict[str, float],
                   opening_of: dict[str, float], sc: float, z_area: float,
                   T_g_h: float) -> tuple[dict[str, list[int]], dict]:
    """Base-stock controller. Returns (build[g][day], stats).

        I*_g = draw_g * T_g / 2          derived from the LOT POLICY, not new
        build(g,d) = cure(g,d) + (I*_g - stock_g)

    `cure_of` is whatever cure profile you believe. Pass the PLANNED press
    capacity on the first pass and the grid's REALISED cure on the second --
    the planner credits a campaign day with a full press-day while the grid
    delivers ~87% of it, so a single pass drains the plan while reality
    accumulates.
    """
    stock = {g: float(opening_of.get(g, 0.0)) for g in gts}
    active = {g: max(1, sum(1 for x in cure_of[g] if x > 0)) for g in gts}
    draw = {g: N_of[g] / active[g] for g in gts}
    I_star = {}
    for g in gts:
        if area[g] <= z_area:
            I_star[g] = 0.0                              # one campaign, cure on build
        else:
            I_star[g] = max(draw[g] * T_g_h / 48.0,      # draw * T/2
                            draw[g] * 4.0 / 24.0)        # floor: half a shift
    remaining = {g: float(int(round(N_of[g] * (1.0 + sc)))) for g in gts}
    build: dict[str, list[int]] = {g: [0] * H for g in gts}
    trace = []
    for d in range(H):
        for g in gts:
            cure_d = min(cure_of[g][d], stock[g] + remaining[g])
            want = max(0.0, min(cure_d + (I_star[g] - stock[g]), remaining[g]))
            q = int(math.floor(want))
            build[g][d] = q
            stock[g] += q - cure_d
            remaining[g] -= q
        trace.append(sum(stock.values()))
    n_t = len(trace)
    mx_, my_ = (n_t - 1) / 2.0, sum(trace) / n_t
    cov = sum((i - mx_) * (v - my_) for i, v in enumerate(trace))
    vx = sum((i - mx_) ** 2 for i in range(n_t)) or 1.0
    covers = [24.0 * I_star[g] / draw[g] for g in gts if draw[g] > 0]
    stats = {
        "zero_hold_gts": sum(1 for v in I_star.values() if v == 0),
        "zero_hold_pct": round(100.0 * sum(1 for v in I_star.values() if v == 0)
                               / max(1, len(gts)), 1),
        "cover_h_p50": round(st.median(covers), 1) if covers else 0.0,
        "wip_end": int(trace[-1]), "wip_mean": int(sum(trace) / n_t),
        "wip_slope": round(cov / vx, 1),
        "wip_mean_delta": round((trace[-1] - trace[0]) / max(1, n_t - 1), 1),
        "unbuilt": int(sum(remaining.values())),
    }
    return build, stats


def plant_rate(plant: str, presses: list[str], timing) -> float:
    """Tyres per press-DAY, derived from the same capacity model the grid runs.

        eff_CT      = (raw_dwell + 2.3) / 0.94        [inside cure_cadence_s]
        tyres/shift = floor(480 min / eff_CT) x slots
        tyres/day   = 3 x tyres/shift

    Hardcoding 156/48 pins the planner to the month those were measured in, and
    risks a subtler error: the planner would book press-days at one rate while
    `shift_grid` delivers at `floor(28800/cadence)`. Taking the plant median of
    the grid's OWN per-press capacity keeps them identical by construction.
    Validated: reproduces 156.0/48.0 exactly on July.
    """
    caps = sorted(max(1, int(28800.0 // max(timing.cure_cadence_s(plant, p), 1.0)))
                  for p in presses)
    if not caps:
        return DEFAULT_RATE.get(plant, 100.0)
    return 3.0 * float(caps[len(caps) // 2])


def opening_gt(plan_start: date) -> dict[tuple[str, str], int]:
    """Green tyres carried in from last month, per (plant, GT).

    Built before the horizon and not yet cured. Mirrors
    `GreenTireLedger.load_opening_from_mes`, including its age floor: a tyre
    older than the observed build->cure p99 lag is not waiting to be cured, it
    is scrap, so counting it as stock would invent inventory.
    """
    con = duck()
    try:
        r = con.execute("""
            SELECT quantile_cont(
                       date_diff('second', b.event_ts, c.event_ts) / 3600.0, 0.99)
            FROM v_build b JOIN v_curing c ON b.productionID = c.gtbarCode
            WHERE b.stage = 2 AND b.QualityStatus = '1'
              AND c.statuscritical = 'Normal' AND c.event_ts >= b.event_ts
        """).fetchone()
        max_age_h = float(r[0]) if r and r[0] else 168.0
        rows = con.execute("""
            WITH built AS (
                SELECT plant, itemCode AS gt_code, productionID AS gtbar
                FROM v_build
                WHERE stage = 2 AND QualityStatus = '1'
                  AND event_ts < ?::TIMESTAMP
                  AND event_ts >= ?::TIMESTAMP - INTERVAL (?) HOUR
            ), cured AS (
                SELECT gtbarCode AS gtbar FROM v_curing
                WHERE event_ts < ?::TIMESTAMP AND statuscritical = 'Normal'
            )
            SELECT b.plant, b.gt_code, count(*) FROM built b
            LEFT JOIN cured c ON b.gtbar = c.gtbar
            WHERE c.gtbar IS NULL GROUP BY 1,2
        """, [plan_start, plan_start, max_age_h, plan_start]).fetchall()
        return {(p, g): int(n) for p, g, n in rows}
    except Exception as e:  # noqa: BLE001
        log.warning("window.opening_gt_failed", err=str(e))
        return {}


def _place(order: list[tuple[str, float]], n: dict[str, int],
           D: dict[str, int], H: int,
           early: set[str] | None = None) -> tuple[dict[str, int], list[float]]:
    """Best-fit-decreasing on area: put each rectangle where the peak is lowest.

    Windows never start on day 0 -- a press mounted then has nothing to pull,
    since its GT's first tyres are built that same day and only become eligible
    the following shift. Guaranteed starved press-day (v3 s4).

    GTs in `early` open on DAY 0. They already HAVE stock -- last month's
    carry-over -- so the day-0 rule above does not apply to them: there is
    material to pull from the first shift. Excluding them cost the whole of day
    1: the earliest cure in the month was 02-Jul 08:00, leaving all 171 presses
    idle for 3 shifts (~15,700 tyres, roughly half the month-end WIP overhang)
    while 5,948 tyres of opening stock sat waiting. Opening inventory exists
    precisely to bridge that gap.

    They are also pinned rather than levelled: a late window only ages stock
    that is already made -- 5% of opening inventory previously sat until day
    21.8 and reached 529.8h.
    """
    early = early or set()
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


def plan_windows(demand: pl.DataFrame, plan_start: date, plan_end: date,
                 press_of: dict[tuple[str, str], list[str]],
                 timing,
                 realised: dict | None = None) -> tuple[dict, pl.DataFrame, dict]:
    """Return (campaigns, build_target_demand, stats).

    campaigns: {(plant, gt): [(press, start_day, end_day), ...]}
    """
    days = [plan_start + timedelta(days=i)
            for i in range((plan_end - plan_start).days + 1)]
    H = len(days)
    campaigns: dict[tuple[str, str], list[tuple[str, int, int]]] = {}
    rows: list[dict] = []
    stats: dict = {}
    real = real_presses()
    active = press_active_frac()
    reserve = press_reserve_ratio()
    scrap = scrap_rate()
    zero_area = zero_hold_area()
    opening = opening_gt(plan_start)

    tot = demand.group_by(["plant", "gt_code"]).agg(pl.col("qty").sum().alias("N"))
    for plant in sorted(tot["plant"].unique().to_list()):
        sub = tot.filter(pl.col("plant") == plant)
        alive = real.get(plant, set())
        elig = {g: [p for p in press_of.get((plant, g), []) if p in alive]
                for g in sub["gt_code"].to_list()}
        presses = sorted({p for v in elig.values() for p in v} or alive)
        if not presses:
            continue
        nP = len(presses)
        rate = plant_rate(plant, presses, timing)
        N_of = {r["gt_code"]: float(r["N"]) for r in sub.iter_rows(named=True)}
        area = {g: v / rate for g, v in N_of.items()}          # press-days of work
        tot_area = sum(area.values()) or 1.0

        # BOOK TO THE PLANT'S OWN PRESS RESERVE, capped by what the box holds.
        # Availability alone (presses are active ~every day) gives f_book = 1.0,
        # i.e. no slack at all -- 10% of press-shifts then hold no campaign while
        # WIP climbs to 4.3x opening. The plant instead mounts more presses than
        # the bare work needs; that reserve is what absorbs building variance.
        _rr = reserve.get(plant)
        f_plant = ((_rr[0] * rate / _rr[1]) if _rr and _rr[1] > 0 else 1.0)
        f_avail = 1.0 / min(1.0, max(0.5, active.get(plant, DEFAULT_ACTIVE_FRAC)))
        f_cap = BOX_FILL_MAX * nP * H / tot_area      # never book past the box
        # MEASURED: booking the plant's full reserve (1.078 PCR / 1.179 TBR) does
        # NOT reduce aging -- it only RELABELS the loss. Press-shifts lost went
        # 9.9% ineligible + 1.4% starved -> 3.2% ineligible + 6.3% starved, i.e.
        # ~10% either way: mounting a press it cannot feed starves it instead of
        # leaving it unmounted. Aging 79.6 -> 84.8h, span 730.9 -> 747.3h,
        # building changeovers 1,701 -> 1,999, on-time 90.7 -> 69.8%. The press
        # side is at its limit; the residual is a BUILD-side problem (stickiness
        # 29.5% vs the plant's 99.8%). Book availability only and fix it there.
        f_book = max(1.0, min(f_avail, f_cap))
        booked = {g: v * f_book for g, v in area.items()}      # press-days to hold
        cap = {g: max(1, len(elig.get(g) or presses)) for g in area}
        # GTs holding carry-over stock are placed first, and pinned to day 1 by
        # _place -- they already have material, so a late window only ages it.
        early = {g for g in area if opening.get((plant, g), 0) > 0}
        order = sorted(area.items(), key=lambda t: (t[0] not in early, -t[1]))

        # --- integer n_g lattice search (v3 s3.4) ------------------------------
        # Start at the minimum press count, i.e. the fewest campaigns possible,
        # and raise n_g only where the packing actually fails. This is the
        # corrected D-sweep: it searches the integer lattice driven by measured
        # placement outcome rather than by a predicted bound.
        n = {g: max(1, min(cap[g], math.ceil(b / H))) for g, b in booked.items()}
        D, a, load, rounds = {}, {}, [], 0
        best = None
        stale = 0
        for rounds in range(1, 60):
            # Each of the n_g campaigns loses its first shift to a mould change,
            # so the window must be SETUP_DAYS longer per press. Charged here,
            # where n_g is known, rather than as a blanket utilisation factor.
            D = {g: max(1, min(H, math.ceil(booked[g] / n[g] + SETUP_DAYS)))
                 for g in area}
            a, load = _place(order, n, D, H, early)
            peak = max(load) if load else 0.0
            if best is None or peak < best[0]:
                best, stale = (peak, dict(n), dict(D), dict(a), list(load)), 0
            else:
                stale += 1
            if peak <= nP or stale >= 5:
                break
            # REPAIR DIRECTION: `n_g += 1` makes the rectangle TALLER, which is
            # the opposite of flattening and raises the very peak it is meant to
            # fix. On PCR that escalated for 185 rounds -- windows collapsed to
            # 5 days and campaigns went 102 -> 443. Flatten means LOWER height
            # and a WIDER window at the same area, so reduce n_g. A GT already
            # at n_g = 1 cannot be flattened further; if every offender is there,
            # the peak is structural and no reshaping will remove it.
            d_star = max(range(H), key=lambda d: load[d])
            live_now = [g for g in area
                        if a[g] <= d_star < a[g] + D[g] and n[g] > 1
                        and D[g] < H]
            if not live_now:
                break
            for g in sorted(live_now, key=lambda x: -n[x])[:FLATTEN_K]:
                n[g] -= 1
        if best is not None:
            _pk, n, D, a, load = best

        # --- press assignment: n_g presses hold the WHOLE window --------------
        # One campaign per (GT, press) pair. Because max daily load <= |P| and
        # the windows are intervals, a greedy sweep in start-day order always
        # succeeds -- interval graphs are perfect, so max clique = colours
        # needed. That is what removes the fragment campaigns: no press ever
        # serves part of a window.
        booked_iv: dict[str, list[tuple[int, int]]] = {p: [] for p in presses}

        def _free(p: str, s: int, e: int) -> bool:
            return all(e <= bs or s >= be for bs, be in booked_iv[p])

        short = 0.0
        n_frag = 0
        build_of: dict[str, list[int]] = {}
        cure_of: dict[str, list[float]] = {}
        for g, _ar in sorted(area.items(), key=lambda t: a[t[0]]):
            s, e = a[g], min(a[g] + D[g], H)
            pref = [p for p in (elig.get(g) or []) if _free(p, s, e)]
            rest = [p for p in presses if p not in set(pref) and _free(p, s, e)]
            # history ranks, capability gates: prefer presses that have run this
            # GT, then any free press (40-47% of press-GT pairs are new monthly,
            # so history can never be the feasibility set).
            pick = (sorted(pref, key=lambda p: -max([b for _a2, b in booked_iv[p]] or [0]))
                    + sorted(rest, key=lambda p: -max([b for _a2, b in booked_iv[p]] or [0])))[:n[g]]
            chosen = [(p, s, e) for p in pick]
            for p in pick:
                booked_iv[p].append((s, e))
            # Each campaign yields (days - SETUP_DAYS) of production.
            need = booked[g] - sum((e2 - s2) - SETUP_DAYS for _p, s2, e2 in chosen)

            # HYBRID. Whole-window rectangles are the ideal -- one campaign per
            # (GT, press), changeovers = sum n_g - |P| in closed form. But strip
            # packing only works while the box has slack: at TBR's 85% fill the
            # rectangles land in ONE round with changeovers exactly at the floor,
            # while at PCR's 93% they cannot be placed at all. The "excess"
            # fragment campaigns are therefore not waste -- they are what makes a
            # 93%-fill packing feasible. Take rectangles where they fit and top
            # up the remainder in press-days where they do not, so the tighter
            # construction is used whenever it is available and never at the
            # price of unserved demand.
            if need > 1e-9:
                for tier in (list(elig.get(g) or []), presses):
                    for p in sorted(tier, key=lambda x: -max(
                            [b for _a2, b in booked_iv[x]] or [0])):
                        if need <= 1e-9:
                            break
                        s2 = 1
                        while s2 < H and not _free(p, s2, s2 + 1):
                            s2 += 1
                        e2 = s2
                        while e2 < H and _free(p, e2, e2 + 1) and (e2 - s2) < D[g]:
                            e2 += 1
                        if e2 <= s2:
                            continue
                        e2 = min(e2, s2 + max(1, math.ceil(need)))
                        booked_iv[p].append((s2, e2))
                        chosen.append((p, s2, e2))
                        n_frag += 1
                        need -= (e2 - s2)
                    if need <= 1e-9:
                        break
            short += max(0.0, need)
            # DEMAND IS NEVER DROPPED, even when the packer found no press at
            # all. Skipping the target emission here silently changes the
            # month's planned demand (496,928 -> 490,661 for the same July) and
            # makes every run incomparable -- including against the plant. Fall
            # through to the flat-spread fallback below and let the shortfall
            # surface honestly in curing instead.
            if chosen:
                campaigns[(plant, g)] = chosen

            # --- daily build target = the press plan's own daily capacity -----
            # target(g,d) = cured(g,d), read straight off the packed campaigns
            # and trimmed at N_g. A synthetic prime/steady curve over the window
            # assumed presses were spread evenly across it and put aging at
            # 293.6h; scaling a short plan UP to N_g re-inflates what the presses
            # cannot cure, and under FIFO that surplus ages every tyre behind it.
            tgt_n = int(round(N_of[g]))
            if tgt_n <= 0:
                continue
            live = [0.0] * H
            for _p, s2, e2 in chosen:
                for d in range(s2, min(e2, H)):
                    # A campaign's FIRST day is consumed by the mould change, so
                    # it yields only (1 - SETUP_DAYS) of a press-day. Counting it
                    # in full over-states cure by 1/3 press-day per campaign --
                    # ~8,000 tyres a month -- which is enough to stop the WIP cap
                    # ever binding.
                    live[d] += rate * (1.0 - SETUP_DAYS) if d == s2 else rate
            if sum(live) <= 0:
                live = [float(tgt_n) / max(1, H - 1)] * (H - 1) + [0.0]
            cum, tgt = 0.0, []
            for x in live:
                take = max(0.0, min(x, tgt_n - cum))
                tgt.append(take)
                cum += take
            if cum < tgt_n:
                # Demand the campaigns cannot cover is appended to the LAST live
                # day: still built, so fulfilment stays honest, but built last so
                # it carries the least age and is exactly the part that should
                # surface as unfilled.
                last = max((d for d in range(H) if live[d] > 0), default=H - 2)
                tgt[last] += tgt_n - cum
            # INTEGER split, largest-remainder, summing exactly to tgt_n. A
            # fractional qty becomes an EMPTY int_ranges -> a NULL-timestamped
            # ledger event -> scrambled FIFO ranks -> phantom negative-GT
            # violations. That chain has bitten twice (1,113 and 236).
            base = [int(x) for x in tgt]
            rem = tgt_n - sum(base)
            for d in sorted(range(H), key=lambda i: -(tgt[i] - base[i]))[:max(0, rem)]:
                base[d] += 1
            build_of[g] = base
            cure_of[g] = live

        # --- GT INVENTORY BAND (rule G7) --------------------------------------
        # Hold daily WIP inside the plant's band, on EVERY day including the
        # last. The daily identity keeps WIP flat only if curing executes
        # perfectly; it does not -- presses lose ~11% of shifts to setup, starve
        # and idle -- so the surplus compounded and PCR WIP ramped to 23,400
        # against a 4,500-4,800 band. Walk the days, project WIP forward, and
        # trim any day whose build would breach the ceiling, deferring the
        # trimmed tyres to a later day that has room. Whatever never fits is
        # genuine over-demand and surfaces as shortfall rather than as stock
        # that ages past the shelf life.
        # ---- BASE-STOCK CONTROLLER (rule G8, corrected) ----------------------
        # The defect was never the oscillation -- the plant's own stock swings
        # sd 530/day on a level of 4,820 and ranges 3,400-6,200. The defect is
        # that ours CLIMBS MONOTONICALLY and never comes back. So the constraint
        # is NO TREND, not a tight band:
        #
        #     E[dI] ~ 0        (plant: +38/day)     <- enforce this
        #     sd(dI) ~ 530                          <- leave alone
        #
        # 4,500-4,800 is the band of MONTHLY MEANS, not a daily corridor; a
        # days-in-band test would fail the plant itself.
        #
        # Target is a base-stock level derived from the LOT POLICY, not a new
        # parameter (R3):
        #
        #     I*_g = draw_g * T_g / 2
        #
        # Check against the plant: T_g = 24h -> 12h cover, and the measured
        # median GT cover is 12.6h; T_g = 48h -> 24h, matching p75 = 24.9h;
        # T_g = 12h -> 6h, near p25 = 3.9h. The median (12.6h) sitting ABOVE the
        # draw-weighted aggregate (9.0h) is the signature of exactly this
        # policy: high-draw GTs are replenished more often and so carry less
        # cover. A flat "0.375 x draw" reproduces the aggregate and gets the mix
        # wrong -- a failure mode this engine has already hit three times.
        T_g_h = float(CONFIG.thresholds.replenish_interval_h)
        z_area = zero_area.get(plant, 0.0)
        sc = scrap.get(plant, 0.0)
        gts = list(build_of)
        cure_use = realised.get(plant) if realised else None
        if cure_use:
            # SECOND PASS: drive the controller off what the grid ACTUALLY
            # cured, not off booked press capacity.
            cure_of = {g: cure_use.get(g, [0.0] * H) for g in gts}
        build_of, ctrl = run_controller(
            gts, H, cure_of, N_of, area,
            {g: float(opening.get((plant, g), 0)) for g in gts},
            sc, z_area, T_g_h)
        for g in gts:
            for d in range(H):
                if build_of[g][d] > 0:
                    rows.append({"plant": plant, "gt_code": g,
                                 "due_date": days[d], "qty": float(build_of[g][d])})

        camps = sum(len(v) for (p_, _g), v in campaigns.items() if p_ == plant)
        n_min = sum(max(1, math.ceil(b / H)) for b in booked.values())
        stats[plant] = {
            "presses": nP, "gts": len(area), "rate": rate,
            "f_book": round(f_book, 3), "f_plant": round(f_plant, 3),
            "f_cap": round(f_cap, 3),
            "area_press_days": round(sum(area.values())),
            "booked_press_days": round(sum(booked.values())),
            "fill_pct": round(100.0 * sum(booked.values()) / (nP * H), 1),
            "flatten_rounds": rounds,
            "peak_load": round(max(load) if load else 0, 1),
            "D_g_p50": sorted(D.values())[len(D) // 2] if D else 0,
            "campaigns": camps,
            "changeovers_pred": camps - nP,
            "changeover_floor": n_min - nP,
            "fragment_campaigns": n_frag,
            "press_days_short": round(short, 1),
            "scrap_pct": round(100 * sc, 3),
            "zero_hold_area_pd": round(z_area, 1),
            "pass2": bool(cure_use),
            **ctrl,
        }

    out = pl.DataFrame(rows) if rows else demand
    log.info("window_plan", **{f"{k}_{kk}": vv for k, v in stats.items()
                               for kk, vv in v.items()})
    return campaigns, out, stats
