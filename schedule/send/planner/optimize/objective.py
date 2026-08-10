"""Weighted objective. Lower = better."""
from __future__ import annotations

from planner.config import CONFIG
from planner.optimize.state import Schedule


def _changeovers(sched: Schedule) -> int:
    n = 0
    for machine, timeline in sched.by_machine.items():
        prev = None
        for lot in timeline:
            if prev is not None and prev != lot.gt_code:
                n += 1
            prev = lot.gt_code
    return n


def _wip_area(sched: Schedule) -> float:
    """Sum lot durations × qty as a crude WIP proxy — bigger runs = more WIP hours."""
    return sum(lot.duration_s * lot.qty for lot in sched.lots) / 3600.0


def _stability_edit_distance(sched: Schedule, seed_ids: list[tuple[str, str]]) -> int:
    """Hamming distance between (machine, gt_code) tuples of seed vs current."""
    cur = [(l.machine, l.gt_code) for l in sched.lots]
    seed = seed_ids
    n = max(len(cur), len(seed))
    diff = 0
    for i in range(n):
        a = cur[i] if i < len(cur) else None
        b = seed[i] if i < len(seed) else None
        if a != b:
            diff += 1
    return diff


def score(sched: Schedule, *,
          demand_qty: float,
          scheduled_qty: float,
          seed_signature: list[tuple[str, str]],
          soft_penalty: float = 0.0) -> dict:
    w = CONFIG.weights
    missed = max(0.0, demand_qty - scheduled_qty)
    chg = _changeovers(sched)
    wip = _wip_area(sched)
    # crude curing-wait proxy: max end_ts - min start_ts
    if sched.lots:
        span = (max(l.end_ts for l in sched.lots) - min(l.start_ts for l in sched.lots)).total_seconds()
    else:
        span = 0.0
    stab = _stability_edit_distance(sched, seed_signature)

    terms = {
        "missed_demand":      w.missed_demand * missed,
        "changeover_min":     w.changeover_min * chg,
        "wip_area":           w.wip_area * wip,
        "curing_wait":        w.curing_wait * (span / 3600.0),
        "soft_penalty":       w.soft_penalty * soft_penalty,
        "stability_edit":     w.stability_edit * stab,
    }
    total = sum(terms.values())
    return {"total": total, **terms}
