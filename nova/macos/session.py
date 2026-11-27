"""macOS stand-in for ``nova.session``: tells Nova when the screen locks / unlocks (polls the session dictionary)."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

import Quartz

_log = logging.getLogger(__name__)
POLL_S = 1.0


def _locked() -> bool:
    session = Quartz.CGSessionCopyCurrentDictionary() or {}
    return bool(session.get("CGSSessionScreenIsLocked", False))


class LockWatcher:
    def __init__(self, on_change: Callable[[bool], None]) -> None:
        self._on_change = on_change
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="nova-lock-watch", daemon=True)
        self._state = False
        self.error: str | None = None

    def start(self) -> bool:
        self._thread.start()
        return True

    def close(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(2)

    def simulate(self, locked: bool) -> None:
        self._deliver(locked)

    def _deliver(self, locked: bool) -> None:
        if locked != self._state:
            self._state = locked
            _log.info("screen %s", "locked" if locked else "unlocked")
            self._on_change(locked)

    def _run(self) -> None:
        while not self._stop.wait(POLL_S):
            try:
                self._deliver(_locked())
            except Exception:  # noqa: BLE001
                _log.debug("lock check failed", exc_info=True)
