"""Tiny thread-safe pub/sub. Events are dataclass instances.

Handlers run synchronously on the publisher's thread: exact-type subscribers
first, then base classes in MRO order (subscribe to ``object`` to see all).
A failing handler is logged and never breaks the publisher or other handlers.
UI code must hop to the Qt thread itself (see ``nova.tray``).
"""

from __future__ import annotations

import dataclasses
import logging
import threading
from collections.abc import Callable
from typing import Any, TypeVar

E = TypeVar("E")
Handler = Callable[[Any], None]

_log = logging.getLogger(__name__)


class Bus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Copy-on-write: publish reads this mapping without locking.
        self._subs: dict[type, tuple[tuple[object, Handler], ...]] = {}

    def subscribe(self, event_type: type[E], handler: Callable[[E], None]) -> Callable[[], None]:
        """Register ``handler`` for ``event_type`` (and its subclasses). Returns an unsubscribe function."""
        token = object()
        with self._lock:
            subs = dict(self._subs)
            subs[event_type] = (*subs.get(event_type, ()), (token, handler))
            self._subs = subs

        def unsubscribe() -> None:
            with self._lock:
                entries = self._subs.get(event_type, ())
                kept = tuple(entry for entry in entries if entry[0] is not token)
                if len(kept) == len(entries):
                    return
                subs = dict(self._subs)
                if kept:
                    subs[event_type] = kept
                else:
                    del subs[event_type]
                self._subs = subs

        return unsubscribe

    def publish(self, event: object) -> None:
        if not dataclasses.is_dataclass(event) or isinstance(event, type):
            raise TypeError(f"events must be dataclass instances, got {event!r}")
        subs = self._subs
        for cls in type(event).__mro__:
            for _token, handler in subs.get(cls, ()):
                try:
                    handler(event)
                except Exception:
                    _log.exception("event handler %r failed on %r", handler, event)
