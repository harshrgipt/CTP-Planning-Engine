"""Fixed shift-grid curing simulation. 31 days x 3 shifts x 480 min.

This replaces the event-driven `_simulate_presses` loop, and the point of it is
that **the horizon is a loop bound, not an output**. The event-driven sim had no
horizon at all: presses kept pulling until stock ran out, so a coupling loss
showed up as span (992h against a 744h month) -- one unattributable number.
Here a press that cannot work simply produces nothing that shift, and the loss
lands in demand fulfilment, which decomposes by GT, press and shift.

    span = 744 h BY CONSTRUCTION.  Do not compute it; report fulfilment.

Expect the headline to flip from "100% of demand in 992h" to "<100% in 744h".
That is not a regression -- it is the same inefficiency, attributed.

The press state is the campaign plan from `window_plan`: press p is mounted on
GT g for days [a_g, a_g + D_g). Because the mount is fixed for the window, two
plant behaviours come out for free rather than needing rules:

  * cure stickiness 100% -- a press never changes GT within a day, and in fact
    never changes within its window (the 1-day minimum hold of v2 s5.6, which
    is what stopped the 5,196-changeover variant);
  * changeovers = sum_g n_g - |P| in closed form, no search.

Stock is a per-GT FIFO of build completions -- presses never own tyres, so the
identity-binding bug class (contiguous McNaughton blocks; due-date queue heads)
cannot recur. A tyre is eligible only from the shift AFTER it is built, which
is the L_min rest and also guarantees no cure can precede its own supply.
"""
from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime

import polars as pl

from planner.runs.logger import log

SHIFT_S = 28800.0          # 480 min
EPOCH = datetime(1970, 1, 1)

# MEASURED, not assumed. Lower tail of the plant's own build->cure lag over all
# 8 months and both plants (2.98M PCR + 0.74M TBR paired tyres):
#     p0.1  0.26-0.30 h  every month, both plants
#     TBR minimum  EXACTLY 0.25 h in 6 of 8 months  <- a hard process floor
# PCR's raw minimum dips to 0.01-0.13h, which is barcode-timing noise rather
# than a shorter rest; the p0.1 agrees with TBR at 0.28h. So 0.25h it is.
# Right-shift previously used 0.5h -- conservative by 2x, now unified here.
TAU_MIN_H = 0.25
TAU_MIN_S = TAU_MIN_H * 3600.0


def simulate_shift_grid(tyres: pl.DataFrame,
                        campaigns: dict[tuple[str, str], tuple[list[str], int, int]],
                        timing, start: datetime,
                        horizon_days: int,
                        quota: dict[tuple[str, str], list[float]] | None = None,
                        ) -> tuple[pl.DataFrame, dict]:
    """Run the grid. Returns (cure rows, stats).

    `tyres` needs [plant, gt_code, avail_ts]; one row per green tyre.
    """
    t0 = (start - EPOCH).total_seconds()
    n_shifts = horizon_days * 3

    # --- supply: per (plant, gt) sorted arrival epochs -----------------------
    arrivals: dict[tuple[str, str], list[float]] = {}
    for (p_, g_), grp in tyres.sort("avail_ts").group_by(["plant", "gt_code"]):
        arrivals[(p_, g_)] = [(x - EPOCH).total_seconds()
                              for x in grp["avail_ts"].to_list()]
    ptr: dict[tuple[str, str], int] = defaultdict(int)
    done: dict[tuple[str, str], int] = defaultdict(int)
    pool: dict[tuple[str, str], deque] = defaultdict(deque)
    built_total = {k: len(v) for k, v in arrivals.items()}

    # --- press state: day -> mounted GT --------------------------------------
    # campaigns[(plant, gt)] = [(press, start_day, end_day), ...] -- each press
    # carries its own interval, so a GT's press-days need not be contiguous or
    # aligned across presses.
    sched: dict[tuple[str, str], list[str | None]] = {}
    for (plant, g), spans in campaigns.items():
        for pr, a, b in spans:
            row = sched.setdefault((plant, pr), [None] * horizon_days)
            for d in range(a, min(b, horizon_days)):
                # window_plan books non-overlapping intervals; if a collision
                # slips through, first writer wins rather than double-producing.
                if row[d] is None:
                    row[d] = g
    press_keys = sorted(sched)
    if not press_keys:
        return pl.DataFrame(), {"note": "no campaigns"}

    # tyres a press can clear in one shift, from its own measured cadence
    cap_shift: dict[tuple[str, str], int] = {}
    cad_of: dict[tuple[str, str], float] = {}
    for key in press_keys:
        cad = max(timing.cure_cadence_s(key[0], key[1]), 1.0)
        cad_of[key] = cad
        cap_shift[key] = max(1, int(SHIFT_S // cad))

    mount: dict[tuple[str, str], str | None] = {k: None for k in press_keys}
    out_pl, out_pr, out_g, out_s, out_e, out_c = [], [], [], [], [], []
    n_co = 0
    shifts = defaultdict(int)      # productive / co / starved / idle / ineligible
    # POTENTIAL cure: what a mounted press COULD have taken that shift, whether
    # or not stock existed. This is the honest feedback signal for the planning
    # fixed point. Feeding back REALISED cure instead makes the loop diverge
    # downward -- realised cure is depressed by starvation, starvation is caused
    # by under-building, so the controller reads the symptom of its own shortfall
    # as a lower requirement and cuts again (built 499,350 -> 481,587 -> 477,159).
    potential: dict[tuple[str, str], list[float]] = defaultdict(
        lambda: [0.0] * horizon_days)

    for s in range(n_shifts):
        day = s // 3
        t_shift = t0 + s * SHIFT_S

        # ---- collect this shift's press slots, grouped by the GT they draw --
        # CONTINUOUS-TIME PRECEDENCE, not a shift-open release gate. Eligibility
        # is tested per SLOT against that slot's own clock (`arr <= st - tau`),
        # so a tyre built at 09:00 can cure at 09:15 the same shift. The old gate
        # pooled only tyres built before t_shift, which forced every tyre to wait
        # for the next shift boundary and made same-shift feed structurally
        # impossible: 0.19% of our volume cured within 2h of build against the
        # plant's 18.60%, and 65% of volume landed at cure_day = build_day + 1
        # against the plant's 29%. That single line was ~10h of the lead time.
        slots_by_gt: dict[tuple[str, str], list[tuple[float, str, float]]] = {}
        active: list[tuple[str, str]] = []
        for key in press_keys:
            plant, press = key
            g = sched[key][day]
            if g is None:
                shifts["ineligible"] += 1
                continue
            if mount[key] != g:
                # mould change consumes the shift (480 min, v2 s19.D)
                if mount[key] is not None:
                    n_co += 1
                mount[key] = g
                shifts["setup"] += 1
                continue
            cap = cap_shift[key]
            cad = cad_of[key]
            potential[(plant, g)][day] += float(cap)
            active.append(key)
            slots_by_gt.setdefault((plant, g), []).extend(
                (t_shift + i * cad, press, cad) for i in range(cap))

        # ---- fill each GT's slots in time order ----------------------------
        # Sorted by (start, press) so two presses mounted on the same GT consume
        # one FIFO stream in clock order and the result is total-ordered, hence
        # reproducible. Filling per press in press order instead would let a
        # late slot on an early press outrank an early slot on a later one.
        got: dict[tuple[str, str], int] = defaultdict(int)
        for k in sorted(slots_by_gt):
            plant, g = k
            arr = arrivals.get(k, ())
            n = len(arr)
            i = ptr[k]
            q = pool[k]
            # PACE TO DEMAND. Without this the grid fills every mounted press
            # slot it can feed, so the plan runs 1,200-1,800 tyres/day AHEAD of
            # the due-date schedule for three weeks, exhausts the month's total
            # by day 28 and then collapses to 44% of demand on the last day.
            # Building chases curing, so it front-loads too and stays ~3,000
            # ahead of it all month -- which is precisely the GT stock carried
            # on top of opening. The plant paces: its daily build CV is 0.031
            # against our 0.233.
            # The cap is CUMULATIVE, not per-day, so a starved day is still
            # recoverable later; only running ahead is forbidden.
            cap_cum = None if quota is None else quota.get(k)
            for st, press, cad in sorted(slots_by_gt[k]):
                if cap_cum is not None:
                    lim = cap_cum[day] if day < len(cap_cum) else cap_cum[-1]
                    if done[k] >= lim:
                        break         # already at the demand line for today
                deadline = st - TAU_MIN_S
                while i < n and arr[i] <= deadline:
                    q.append(arr[i])
                    i += 1
                if not q:
                    continue          # starved at this instant, not this shift
                q.popleft()
                done[k] += 1
                got[(plant, press)] += 1
                out_pl.append(plant); out_pr.append(press); out_g.append(g)
                out_s.append(st); out_e.append(st + cad); out_c.append(cad)
            ptr[k] = i

        # Account over `active` only. Re-deriving eligibility here would
        # double-count the mould-change presses, whose `mount` was already
        # updated to g in the loop above and which have therefore stopped
        # looking like changeovers.
        for key in active:
            took = got.get(key, 0)
            if took <= 0:
                shifts["starved"] += 1
            else:
                shifts["productive" if took == cap_shift[key] else "idle"] += 1

    cured = defaultdict(int)
    for p_, g_ in zip(out_pl, out_g):
        cured[(p_, g_)] += 1
    short = {k: built_total[k] - cured.get(k, 0) for k in built_total}
    short = {k: v for k, v in short.items() if v > 0}

    df = pl.DataFrame({"plant": out_pl, "press": out_pr, "gt_code": out_g,
                       "cycle_s": out_c, "_s": out_s, "_e": out_e})
    total = n_shifts * len(press_keys)
    stats = {
        "tyres": df.height, "changeovers": n_co,
        "press_shifts": total, "horizon_days": horizon_days,
        "pct": {k: round(100.0 * v / max(total, 1), 1) for k, v in shifts.items()},
        "unfilled_gts": len(short),
        "unfilled_tyres": sum(short.values()),
    }
    log.info("curing.shift_grid", **stats)
    # keyed by plant -> gt -> [per-day potential]; the planner's feedback signal
    stats["_potential"] = {}
    for (p_, g_), arr in potential.items():
        stats["_potential"].setdefault(p_, {})[g_] = arr
    return df, stats
