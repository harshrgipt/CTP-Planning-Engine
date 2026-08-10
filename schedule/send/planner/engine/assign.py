"""PHASE 5a -- GT -> BUILDING MACHINE ASSIGNMENT, the plant's actual rule.

MINED, not designed. `scripts/mine_assignment.py` over 8 months says:

    a GT's TOP MACHINE carries        94.2% (PCR) / 88.0% (TBR) of its month
    machines per GT (median / max)    1 / 4      (PCR)   2 / 6 (TBR)
    a machine's top-3 GTs             99.7% (PCR) / 78.1% (TBR) of its month
    (machine, GT) run                 active 10 of 13 days -> density 0.83
    GTs per machine-day               2.14 (PCR) / 3.01 (TBR)

So: ONE GT -> ONE MACHINE, run CONTIGUOUSLY, ~3 GTs per machine-month.

THE v3 FORMULA IS WITHDRAWN. `b_g = q_g / c_m` -- machines proportional to a
GT's draw -- does not describe this plant: r(draw, machines) = 0.334 (PCR), and
a GT drawing 900/day gets the SAME ONE MACHINE as one drawing 100/day. Machine
count barely responds to volume; capacity comes from the machine being fast
(1,126 tyres/machine-day PCR), not from adding machines. Building it as
specified would have spread GTs across the fleet -- the opposite of the plant --
and made our 41% stickiness worse.

This is therefore an ASSIGNMENT problem, not an allocation one: choose one
machine per GT so that machine loads balance and the rim lock holds. Greedy
longest-processing-time-first onto the least-loaded eligible machine is the
standard heuristic for P||Cmax and is deterministic, which the engine requires.
"""
from __future__ import annotations

import polars as pl

from planner.engine.contract import ordered
from planner.runs.logger import log

# A GT is split across a second machine only when one machine cannot physically
# carry it inside the horizon. Measured: 94.2% of PCR GT-months sit on a single
# machine, so splitting is the exception, not the design.
SPLIT_HEADROOM = 0.95


def assign_machines(req, ms, timing, size_lock: dict[str, dict[str, str]]
                    ) -> dict[tuple[str, str], list[str]]:
    """Return {(plant, gt): [primary machine, ...]} -- primary first."""
    out: dict[tuple[str, str], list[str]] = {}
    H_s = req.horizon_days * 24 * 3600.0
    tot = (req.demand.group_by(["plant", "gt_code"])
           .agg(pl.col("qty").sum().alias("N")).sort(["plant", "gt_code"]))

    for plant in ordered(tot["plant"].unique().to_list()):
        machines = ms.machines.get(plant, [])
        if not machines:
            continue
        lock = size_lock.get(plant, {})
        sub = tot.filter(pl.col("plant") == plant)
        # seconds of work each GT needs, at that plant's build cadence
        work: dict[str, float] = {}
        for r in sub.iter_rows(named=True):
            g = r["gt_code"]
            cyc = max(timing.build_cycle_s(plant, machines[0]), 1.0)
            work[g] = float(r["N"]) * cyc
        load = {m: 0.0 for m in machines}

        # longest-processing-time-first: place the biggest GT on the emptiest
        # eligible machine. Deterministic given a total tie-break.
        cap = H_s * SPLIT_HEADROOM
        for g, w in sorted(work.items(), key=lambda t: (-t[1], t[0])):
            sz = timing._size_for_gt(g)
            # CERTIFICATION FIRST, size lock only as the fallback. The plant's
            # allowable matrix says what a machine MAY run; the rim lock is a
            # habit mined from what it HAS run. Certification is 1.5x wider
            # (p50 3 machines per GT vs 2), and being short of machines is what
            # made the assignment ineffective.
            certified = ms.cert_machines.get((plant, g))
            if certified:
                same_size = [m for m in certified if m in set(machines)]
            else:
                same_size = [m for m in machines if not sz or lock.get(m, sz) == sz]

            # CAPACITY IS A HARD CAP, and it outranks the size lock. Balancing on
            # load alone while the rim lock restricts eligibility piles every
            # high-volume GT of one size onto the two machines locked to it --
            # measured, one PCR machine drew 1,365h of work in a 744h month, the
            # build span ran to 1,156h and fulfilment fell to 93%. A machine that
            # cannot physically hold the work is not a candidate, whatever its
            # rim history says.
            room = [m for m in same_size if load[m] + w <= cap]
            if not room:
                room = [m for m in machines if load[m] + w <= cap]   # break the lock
            if not room:
                room = machines                                      # nothing fits
            picks: list[str] = []
            rem = w
            for m in sorted(room, key=lambda x: (load[x], x)):
                if rem <= 0:
                    break
                take = min(rem, max(0.0, cap - load[m])) if len(picks) or rem > cap - load[m] else rem
                if take <= 0:
                    continue
                load[m] += take
                rem -= take
                picks.append(m)
            if rem > 0:                       # genuinely over capacity
                m = min(machines, key=lambda x: (load[x], x))
                load[m] += rem
                if m not in picks:
                    picks.append(m)
            out[(plant, g)] = picks

        n1 = sum(1 for k, v in out.items() if k[0] == plant and len(v) == 1)
        tot_g = sum(1 for k in out if k[0] == plant)
        spread = sorted(load.values())
        log.info("engine.assign", plant=plant, gts=tot_g,
                 single_machine_pct=round(100.0 * n1 / max(tot_g, 1), 1),
                 machines=len(machines),
                 load_h_min=round(spread[0] / 3600, 1),
                 load_h_max=round(spread[-1] / 3600, 1),
                 horizon_h=round(H_s / 3600, 1))
    return out
