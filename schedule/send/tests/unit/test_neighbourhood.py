"""Move apply/revert must be inverse — property test."""
from datetime import datetime, timedelta
import random

from planner.optimize.neighbourhood import ShiftLotTime, SwapTwoLots, ReassignMachine
from planner.optimize.state import Lot, Schedule


def _mk_schedule(n: int = 6) -> Schedule:
    s = Schedule()
    t0 = datetime(2026, 1, 1, 0, 0, 0)
    for i in range(n):
        machine = "M1" if i % 2 == 0 else "M2"
        lot = Lot(idx=i, lot_id=f"L{i}", plant="PCR", gt_code=f"G{i%3}",
                  machine=machine, qty=10, setup_s=0.0, cycle_s=600.0,
                  start_ts=t0 + timedelta(minutes=i * 30),
                  end_ts=t0 + timedelta(minutes=i * 30 + 10))
        s.lots.append(lot)
    s.rebuild_index()
    return s


def _snapshot(s: Schedule):
    return [(l.idx, l.machine, l.start_ts, l.end_ts) for l in s.lots]


def test_shift_inverse():
    s = _mk_schedule()
    snap = _snapshot(s)
    m = ShiftLotTime(idx=0, delta_s=1200.0)
    m.apply(s)
    m.revert(s)
    assert _snapshot(s) == snap


def test_swap_inverse():
    s = _mk_schedule()
    snap = _snapshot(s)
    # Two lots on M1: idx 0 and idx 2
    m = SwapTwoLots(machine="M1", idx_a=0, idx_b=2)
    m.apply(s)
    m.revert(s)
    assert _snapshot(s) == snap


def test_reassign_inverse():
    s = _mk_schedule()
    snap = _snapshot(s)
    m = ReassignMachine(idx=0, new_machine="M2")
    m.apply(s)
    m.revert(s)
    assert _snapshot(s) == snap
