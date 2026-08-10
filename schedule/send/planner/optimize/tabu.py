"""Simple ring-buffer tabu list keyed by move signature."""
from __future__ import annotations

from collections import deque


class Tabu:
    def __init__(self, size: int):
        self._q: deque = deque(maxlen=max(1, size))
        self._set: set = set()

    def contains(self, sig: tuple) -> bool:
        return sig in self._set

    def push(self, sig: tuple) -> None:
        if len(self._q) == self._q.maxlen:
            old = self._q[0]
            self._set.discard(old)
        self._q.append(sig)
        self._set.add(sig)
