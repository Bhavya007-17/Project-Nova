"""macOS stand-in for ``nova.cpu``: CPU time in nanoseconds stands in for cycle counts (macOS bills CPU time
exactly, so the Windows tick problem doesn't exist here)."""

from __future__ import annotations

import time
from collections.abc import Callable


def process_cycles() -> int:
    return time.process_time_ns()


def _thread_cycles() -> int:
    return time.thread_time_ns()


def cycles_per_core_second(seconds: float = 0.2) -> float:
    return 1e9


class IdleMeter:
    """CPU % of one core, counted only while idle: not talking, and ``settle_s`` after the last utterance."""

    def __init__(
        self,
        hz: float,
        settle_s: float = 1.0,
        cycles: Callable[[], int] = process_cycles,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._hz = hz
        self._settle = settle_s
        self._cycles = cycles
        self._clock = clock
        self._busy: set[object] = set()
        self._quiet_from = clock() + settle_s
        self._last: tuple[int, float] | None = None
        self.idle_cycles = 0
        self.idle_seconds = 0.0

    def busy(self, key: object) -> None:
        """Something is going on (an utterance, a closed mic): stop counting until it's freed."""
        self._busy.add(key)

    def free(self, key: object) -> None:
        self._busy.discard(key)
        self._quiet_from = self._clock() + self._settle

    def sample(self) -> None:
        now, cycles = self._clock(), self._cycles()
        last = self._last
        if last is not None and not self._busy and last[1] >= self._quiet_from:
            self.idle_cycles += cycles - last[0]
            self.idle_seconds += now - last[1]
        self._last = (cycles, now)

    @property
    def percent(self) -> float | None:
        if self.idle_seconds <= 0:
            return None
        return 100.0 * self.idle_cycles / self._hz / self.idle_seconds
