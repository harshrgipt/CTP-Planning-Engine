"""Mutable schedule state for the optimizer.

Holds build_df as an in-memory python list of lot dicts + per-machine timeline
(sorted-list of (start_ts, end_ts, lot_idx)) for fast neighbourhood moves.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sortedcontainers import SortedList  # type: ignore


@dataclass
class Lot:
    idx: int
    lot_id: str
    plant: str
    gt_code: str
    machine: str
    qty: int
    setup_s: float
    cycle_s: float
    start_ts: datetime
    end_ts: datetime

    @property
    def duration_s(self) -> float:
        return (self.end_ts - self.start_ts).total_seconds()


@dataclass
class Schedule:
    lots: list[Lot] = field(default_factory=list)
    by_machine: dict[str, SortedList] = field(default_factory=dict)

    @classmethod
    def from_build_df(cls, build_df) -> "Schedule":
        s = cls()
        for i, row in enumerate(build_df.iter_rows(named=True)):
            lot = Lot(
                idx=i,
                lot_id=row["lot_id"],
                plant=row["plant"],
                gt_code=row["gt_code"],
                machine=row["machine"],
                qty=int(row["qty"]),
                setup_s=float(row.get("setup_s", 900.0)),
                cycle_s=float(row.get("cycle_s", 45.0 * int(row["qty"]))),
                start_ts=row["start_ts"],
                end_ts=row["end_ts"],
            )
            s.lots.append(lot)
            s.by_machine.setdefault(lot.machine, SortedList(key=lambda l: l.start_ts)).add(lot)
        return s

    def rebuild_index(self) -> None:
        self.by_machine.clear()
        for lot in self.lots:
            self.by_machine.setdefault(lot.machine, SortedList(key=lambda l: l.start_ts)).add(lot)

    def to_polars(self):
        import polars as pl
        return pl.DataFrame([{
            "lot_id": l.lot_id, "plant": l.plant, "gt_code": l.gt_code,
            "stage": "build_s2", "machine": l.machine,
            "start_ts": l.start_ts, "end_ts": l.end_ts, "qty": l.qty,
            "setup_s": l.setup_s, "cycle_s": l.cycle_s,
        } for l in self.lots])
