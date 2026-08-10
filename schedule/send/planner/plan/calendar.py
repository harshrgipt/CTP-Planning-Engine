"""Machine-time cursor: track available time per machine, honour shift calendar.

For now uses a simple 3-shift 24×7 model (480 min/shift). Real calendars will
plug in via masters/calendar.csv without changing the interface.
"""
from __future__ import annotations

from datetime import datetime, timedelta


class MachineTimer:
    """Cursor per machine — next-available datetime after prior events."""

    def __init__(self, start: datetime):
        self._t: dict[str, datetime] = {}
        self._start = start

    def next_free(self, machine: str) -> datetime:
        return self._t.get(machine, self._start)

    def occupy(self, machine: str, until: datetime) -> None:
        cur = self._t.get(machine, self._start)
        self._t[machine] = max(cur, until)

    def commit(self, machine: str, duration_s: float) -> tuple[datetime, datetime]:
        s = self.next_free(machine)
        e = s + timedelta(seconds=duration_s)
        self.occupy(machine, e)
        return s, e
