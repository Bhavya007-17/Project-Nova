"""Click by meaning (SPEC §7.4, Stage 07d): when click-by-name finds nothing or a tie, Tier 1 reads the window's rows
and answers ``ui_click{element: N}`` with its confidence. ``nova.actions.pointing`` decides what to do with that
(sure → click after the risk check; unsure → numbers on at most 3 rows; nothing → the old way).

Same model, same static prefix as planning: only the request's own part differs ("said" + "ui"), so llama-server's
prompt cache keeps the ~4k-token prefix and the call reads only the rows.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any, Protocol

from .tier1 import Seen, Thought

_log = logging.getLogger(__name__)


class _Thinks(Protocol):
    def think(self, text: str, seen: Seen, *, ui: Sequence[str] | None = None) -> Thought: ...


def parse(data: Any) -> tuple[int, float] | None:
    """The row number and confidence from the model's plan, or None when it didn't pick one row."""
    if not isinstance(data, dict) or data.get("kind") != "act":
        return None
    steps = data.get("steps")
    if not isinstance(steps, list) or len(steps) != 1 or not isinstance(steps[0], dict):
        return None
    step = steps[0]
    element = step.get("element")
    if step.get("action") != "ui_click" or isinstance(element, bool) or not isinstance(element, int):
        return None
    confidence = data.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        confidence = 0.0
    return element, float(confidence)


def asker(tier1: _Thinks, front: Callable[[], str | None] = lambda: None) -> Callable[[str, Sequence[str]],
                                                                                      tuple[int, float] | None]:
    """``Pointer.meaning`` from Tier 1. ``front``: the app in front, as planning names it."""

    def meaning(name: str, lines: Sequence[str]) -> tuple[int, float] | None:
        thought = tier1.think(f"click {name}", Seen(front=front()), ui=list(lines))
        if thought.error:
            _log.info("click by meaning: no answer (%s)", thought.error)
            return None
        answer = parse(thought.data)
        _log.info("click by meaning: %r -> %s in %.0f ms (%s rows)", name, answer, thought.ms, len(lines))
        return answer

    return meaning
