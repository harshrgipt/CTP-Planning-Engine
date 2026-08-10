"""Neighbourhood moves. Each returns a Move with apply/revert semantics."""
from __future__ import annotations

import random
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import timedelta

from planner.optimize.state import Lot, Schedule


class Move(ABC):
    @abstractmethod
    def apply(self, sched: Schedule) -> None: ...

    @abstractmethod
    def revert(self, sched: Schedule) -> None: ...

    @abstractmethod
    def signature(self) -> tuple: ...


@dataclass
class SwapTwoLots(Move):
    """Swap positions of two adjacent lots on the same machine (recomputes times)."""
    machine: str
    idx_a: int
    idx_b: int
    _saved_a: tuple | None = None
    _saved_b: tuple | None = None

    def apply(self, sched: Schedule) -> None:
        tl = sched.by_machine[self.machine]
        # Find lots by idx
        la = next(l for l in tl if l.idx == self.idx_a)
        lb = next(l for l in tl if l.idx == self.idx_b)
        self._saved_a = (la.start_ts, la.end_ts)
        self._saved_b = (lb.start_ts, lb.end_ts)
        # Swap start times, adjust ends
        la_dur = la.end_ts - la.start_ts
        lb_dur = lb.end_ts - lb.start_ts
        new_a_start = lb.start_ts
        new_b_start = la.start_ts
        tl.remove(la); tl.remove(lb)
        la.start_ts = new_a_start; la.end_ts = new_a_start + la_dur
        lb.start_ts = new_b_start; lb.end_ts = new_b_start + lb_dur
        tl.add(la); tl.add(lb)

    def revert(self, sched: Schedule) -> None:
        tl = sched.by_machine[self.machine]
        la = next(l for l in tl if l.idx == self.idx_a)
        lb = next(l for l in tl if l.idx == self.idx_b)
        tl.remove(la); tl.remove(lb)
        la.start_ts, la.end_ts = self._saved_a
        lb.start_ts, lb.end_ts = self._saved_b
        tl.add(la); tl.add(lb)

    def signature(self) -> tuple:
        return ("swap", self.machine, min(self.idx_a, self.idx_b), max(self.idx_a, self.idx_b))


@dataclass
class ShiftLotTime(Move):
    idx: int
    delta_s: float
    _machine: str = ""

    def apply(self, sched: Schedule) -> None:
        lot = sched.lots[self.idx]
        self._machine = lot.machine
        tl = sched.by_machine[lot.machine]
        tl.remove(lot)
        lot.start_ts += timedelta(seconds=self.delta_s)
        lot.end_ts += timedelta(seconds=self.delta_s)
        tl.add(lot)

    def revert(self, sched: Schedule) -> None:
        lot = sched.lots[self.idx]
        tl = sched.by_machine[self._machine]
        tl.remove(lot)
        lot.start_ts -= timedelta(seconds=self.delta_s)
        lot.end_ts -= timedelta(seconds=self.delta_s)
        tl.add(lot)

    def signature(self) -> tuple:
        return ("shift", self.idx, self.delta_s)


@dataclass
class ReassignMachine(Move):
    idx: int
    new_machine: str
    _prev_machine: str = ""

    def apply(self, sched: Schedule) -> None:
        lot = sched.lots[self.idx]
        self._prev_machine = lot.machine
        sched.by_machine[lot.machine].remove(lot)
        lot.machine = self.new_machine
        sched.by_machine.setdefault(self.new_machine,
                                    __import__("sortedcontainers").SortedList(key=lambda l: l.start_ts)).add(lot)

    def revert(self, sched: Schedule) -> None:
        lot = sched.lots[self.idx]
        sched.by_machine[self.new_machine].remove(lot)
        lot.machine = self._prev_machine
        sched.by_machine.setdefault(self._prev_machine,
                                    __import__("sortedcontainers").SortedList(key=lambda l: l.start_ts)).add(lot)

    def signature(self) -> tuple:
        return ("reassign", self.idx, self.new_machine)


def random_move(sched: Schedule, rng: random.Random, allowed_machines: dict[str, list[str]]) -> Move | None:
    """Pick one random move. `allowed_machines` maps gt_code → list of MPM machines."""
    if not sched.lots:
        return None
    kind = rng.choices(["swap", "shift", "reassign"], weights=[0.5, 0.3, 0.2])[0]
    if kind == "swap":
        # Pick a machine with ≥ 2 lots.
        machines = [m for m, tl in sched.by_machine.items() if len(tl) >= 2]
        if not machines:
            return None
        m = rng.choice(machines)
        tl = sched.by_machine[m]
        i = rng.randrange(len(tl) - 1)
        la, lb = tl[i], tl[i + 1]
        return SwapTwoLots(machine=m, idx_a=la.idx, idx_b=lb.idx)
    if kind == "shift":
        lot = rng.choice(sched.lots)
        delta = rng.choice([-1800, -900, -300, 300, 900, 1800])
        return ShiftLotTime(idx=lot.idx, delta_s=delta)
    # reassign
    lot = rng.choice(sched.lots)
    alts = allowed_machines.get(lot.gt_code, [])
    alts = [m for m in alts if m != lot.machine]
    if not alts:
        return None
    return ReassignMachine(idx=lot.idx, new_machine=rng.choice(alts))
