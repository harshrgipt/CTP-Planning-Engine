"""Simulated annealing + tabu + LNS driver."""
from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass

from planner.optimize.lns import destroy_random, repair_greedy
from planner.optimize.neighbourhood import random_move
from planner.optimize.objective import score
from planner.optimize.state import Schedule
from planner.optimize.tabu import Tabu
from planner.runs.logger import log


@dataclass
class SAResult:
    best_score: float
    best_terms: dict
    iters: int
    accepted: int
    improved: int
    seed_score: float
    history: list[tuple[int, float, float, bool]]  # (iter, T, score, accepted)


def optimize(
    sched: Schedule,
    *,
    allowed_machines: dict[str, list[str]],
    demand_qty: float,
    scheduled_qty: float,
    budget_seconds: float = 600.0,
    T0: float = 1000.0,
    alpha: float = 0.995,
    T_min: float = 0.1,
    lns_every: int = 500,
    seed: int = 42,
) -> SAResult:
    rng = random.Random(seed)
    tabu = Tabu(size=int(math.sqrt(max(len(sched.lots), 1))))
    seed_sig = [(l.machine, l.gt_code) for l in sched.lots]

    cur_terms = score(sched, demand_qty=demand_qty, scheduled_qty=scheduled_qty,
                      seed_signature=seed_sig)
    cur = cur_terms["total"]
    best = cur
    best_terms = cur_terms
    accepted = 0
    improved = 0
    history: list[tuple[int, float, float, bool]] = []

    T = T0
    start = time.time()
    it = 0
    while time.time() - start < budget_seconds and T > T_min:
        it += 1
        move = random_move(sched, rng, allowed_machines)
        if move is None:
            break
        sig = move.signature()
        if tabu.contains(sig):
            # aspiration allowed later; skip for now
            T *= alpha
            continue
        move.apply(sched)
        new_terms = score(sched, demand_qty=demand_qty, scheduled_qty=scheduled_qty,
                          seed_signature=seed_sig)
        delta = new_terms["total"] - cur
        if delta < 0 or rng.random() < math.exp(-delta / max(T, 1e-6)):
            cur = new_terms["total"]
            cur_terms = new_terms
            accepted += 1
            if cur < best:
                best = cur
                best_terms = new_terms
                improved += 1
            tabu.push(sig)
            history.append((it, T, cur, True))
        else:
            move.revert(sched)
            history.append((it, T, cur, False))

        if it % lns_every == 0:
            victims = destroy_random(sched, rng, frac=0.10)
            repair_greedy(sched, victims, allowed_machines)
            new_terms = score(sched, demand_qty=demand_qty, scheduled_qty=scheduled_qty,
                              seed_signature=seed_sig)
            if new_terms["total"] < cur:
                cur = new_terms["total"]
                cur_terms = new_terms
                if cur < best:
                    best = cur
                    best_terms = new_terms
                    improved += 1
        T *= alpha

    log.info("sa.done", iters=it, accepted=accepted, improved=improved,
             seed_score=history[0][2] if history else best,
             best_score=best)
    return SAResult(best_score=best, best_terms=best_terms,
                    iters=it, accepted=accepted, improved=improved,
                    seed_score=history[0][2] if history else best,
                    history=history)
