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

from ..actions import rows
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
        if answer is not None and 1 <= answer[0] <= len(lines) and answer[1] > 0:
            row = rows.row_name(lines[answer[0] - 1])
            if not any(rows.shares_word(said, row) for said in (name, _named(thought.data))):
                answer = (answer[0], 0.0)  # its confidence is in the request, not the row: numbers, not a click
        _log.info("click by meaning: %r -> %s in %.0f ms (%s rows)", name, answer, thought.ms, len(lines))
        return answer

    return meaning


def _named(data: Any) -> str:
    """What the model called the thing it clicks (its step's "name"): a synonym it saw ("Open" for "the one that
    loads a file") counts as evidence for the row only when that row carries the word."""
    step = data["steps"][0] if isinstance(data, dict) and isinstance(data.get("steps"), list) and data["steps"] else None
    name = step.get("name") if isinstance(step, dict) else None
    return name if isinstance(name, str) else ""
