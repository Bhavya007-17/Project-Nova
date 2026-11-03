"""Process CPU, cycle-accurate. Windows bills GetProcessTimes by 15.6 ms clock ticks, which
misreads small periodic work like audio callbacks by 2x either way; cycle counts don't.

``IdleMeter`` counts only stretches when nobody is talking (and a settle time after),
so `nova listen` can report the idle cost even in a room with conversation.
"""

from __future__ import annotations

import ctypes
import time
from collections.abc import Callable
from ctypes import wintypes

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.GetCurrentProcess.restype = wintypes.HANDLE
_k32.GetCurrentThread.restype = wintypes.HANDLE
_k32.QueryProcessCycleTime.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_uint64)]
_k32.QueryThreadCycleTime.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_uint64)]


def process_cycles() -> int:
    value = ctypes.c_uint64()
    _k32.QueryProcessCycleTime(_k32.GetCurrentProcess(), ctypes.byref(value))
    return value.value


def _thread_cycles() -> int:
    value = ctypes.c_uint64()
    _k32.QueryThreadCycleTime(_k32.GetCurrentThread(), ctypes.byref(value))
    return value.value


def cycles_per_core_second(seconds: float = 0.2) -> float:
    """Cycle-counter units one fully busy core accumulates per second (busy-loops for ``seconds``)."""
    c0, t0 = _thread_cycles(), time.perf_counter()
    while time.perf_counter() - t0 < seconds:
        pass
    return (_thread_cycles() - c0) / (time.perf_counter() - t0)


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
