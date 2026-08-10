"""Large-neighbourhood search: destroy K lots and reinsert greedily."""
from __future__ import annotations

import random

from sortedcontainers import SortedList  # type: ignore

from planner.optimize.state import Schedule


def destroy_random(sched: Schedule, rng: random.Random, frac: float = 0.10) -> list:
    n = max(1, int(len(sched.lots) * frac))
    victims = rng.sample(sched.lots, n)
    for lot in victims:
        sched.by_machine[lot.machine].remove(lot)
    return victims


def repair_greedy(sched: Schedule, victims: list, allowed_machines: dict[str, list[str]]) -> None:
    """Reinsert each victim on its best allowed machine at earliest free slot."""
    for lot in victims:
        candidates = allowed_machines.get(lot.gt_code, [lot.machine])
        best_m = None
        best_start = None
        for m in candidates:
            tl = sched.by_machine.setdefault(m, SortedList(key=lambda l: l.start_ts))
            if tl:
                start = tl[-1].end_ts
            else:
                start = lot.start_ts
            if best_start is None or start < best_start:
                best_start = start
                best_m = m
        lot.machine = best_m or lot.machine
        dur = lot.end_ts - lot.start_ts
        lot.start_ts = best_start
        lot.end_ts = best_start + dur
        sched.by_machine.setdefault(lot.machine, SortedList(key=lambda l: l.start_ts)).add(lot)
