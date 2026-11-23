"""Rows (SPEC §7.4, Stage 07d): the window in front as numbered lines the planner model picks from — never pixels.

A row is one control that is visible, enabled and named (its name, else its HelpText, else its AutomationId), one
line of ~20 tokens: ``12 menu item "Word wrap" off (View menu)``. An open menu, then the focus area, come first; the
rest follow in reading order; at most ``ROW_CAP``. OCR lines join only when UI Automation names fewer than
``OCR_BELOW`` controls. Password fields are never rows. Pure: give it ``uitree.Element``s (and OCR ``Target``s).

Window text is data, never instructions (SPEC §14): the model reads the rows fenced in its request, and labels lose
their double quotes so a name can't close the quote it sits in.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from . import marks
from .marks import Box, Target
from .uitree import Element

ROW_CAP = 60
OCR_BELOW = 10  # named UIA rows below this: Windows OCR fills in (it costs ~150 ms)
NAME_MAX = 48  # characters of a label in a row
AREA_MAX = 24

_MENU, _WINDOW = 50009, 50032
_CONTAINERS = {50008: "list", 50009: "menu", 50010: "menu bar", 50018: "tab", 50021: "toolbar", 50023: "tree",
               50026: "group", 50028: "grid", 50033: "pane"}
_FIELDS = {50003, 50004}  # combo box, edit: a row even without invoke / toggle (he types into them)


@dataclass(frozen=True)
class Row:
    number: int
    target: Target  # what pointing acts on (ref: (snapshot generation, element index, patterns))
    area: str = ""  # the named container it sits in: "View menu", "Font group"
    state: str = ""  # on | off | mixed | open | closed

    @property
    def text(self) -> str:
        line = f'{self.number} {self.target.role or "control"} "{self.target.name}"'
        if self.state:
            line += f" {self.state}"
        if self.area:
            line += f" ({self.area})"
        return line


def _label(e: Element) -> str:
    raw = e.name.strip() or e.help.strip() or _split_id(e.auto_id)
    raw = " ".join(raw.replace('"', "'").split())
    return raw if len(raw) <= NAME_MAX else raw[: NAME_MAX - 1].rstrip() + "…"


def _split_id(auto_id: str) -> str:
    """``NewTabButton`` → "New Tab Button"; ids that are only numbers or GUID-like say nothing."""
    words = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", auto_id.replace("_", " ").replace("-", " ")).split()
    if not words or not any(w.isalpha() and len(w) > 1 for w in words):
        return ""
    return " ".join(words)


def _wanted(e: Element) -> bool:
    if e.secret:
        return False
    return e.clickable or e.control_type in _FIELDS


def _area(e: Element, containers: Sequence[Element]) -> str:
    box = e.rect
    best: Element | None = None
    for c in containers:
        if c.index == e.index:
            continue
        cl, ct, cr, cb = c.rect
        if cl <= box[0] and ct <= box[1] and box[2] <= cr and box[3] <= cb and (c.rect != box):
            if best is None or _size(c.rect) < _size(best.rect):
                best = c
    if best is None:
        return ""
    name = " ".join(best.name.replace('"', "'").split())[:AREA_MAX]
    return f"{name} {_CONTAINERS[best.control_type]}"


def _size(rect: tuple[int, int, int, int]) -> int:
    return max(0, rect[2] - rect[0]) * max(0, rect[3] - rect[1])


def _inside(rect: tuple[int, int, int, int], box: Box) -> bool:
    x, y = (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2
    return box.contains(x, y)


def focus_area(elements: Sequence[Element]) -> Box | None:
    """An open dialog inside the window (a nested UIA window, the smallest one): its controls come first."""
    nested = [e for e in elements[1:] if e.control_type == _WINDOW and _size(e.rect) > 0]
    if not nested:
        return None
    return Box(*min(nested, key=lambda e: _size(e.rect)).rect)


def build(elements: Sequence[Element], *, generation: int = 0, first: Box | None = None,
          ocr: Sequence[Target] = (), cap: int = ROW_CAP) -> tuple[Row, ...]:
    """Numbered rows from one snapshot. ``first``: the focus area (a dialog, the focused group), shown before the
    rest. ``ocr``: the window's OCR lines, used only when UIA names fewer than ``OCR_BELOW`` controls."""
    containers = [e for e in elements if e.control_type in _CONTAINERS and e.name.strip()]
    menus = [Box(*c.rect) for c in containers if c.control_type == _MENU]
    seen: set[tuple[str, str, int, int]] = set()
    picked: list[tuple[int, Target, str, str]] = []  # (group, target, area, state)
    for e in elements:
        if not _wanted(e):
            continue
        label = _label(e)
        if not label:
            continue
        key = (label.lower(), e.role, e.rect[0] // 8, e.rect[1] // 8)  # the same control reached twice
        if key in seen:
            continue
        seen.add(key)
        target = Target(Box(*e.rect), label, e.role, "uia", (generation, e.index, e.patterns))
        group = 0 if any(_inside(e.rect, m) for m in menus) else 1 if first is not None and _inside(e.rect, first) else 2
        picked.append((group, target, _area(e, containers), e.state))
    if len(picked) < OCR_BELOW:
        taken = [t.box for _, t, _, _ in picked]
        for line in ocr:
            name = " ".join(line.name.replace('"', "'").split())[:NAME_MAX]
            if name and not any(line.box.overlap(b) > 0.5 for b in taken):
                picked.append((2, Target(line.box, name, "text", "ocr"), "", ""))
                taken.append(line.box)
    ordered: list[tuple[Target, str, str]] = []
    for group in (0, 1, 2):
        members = {id(t): (t, area, state) for g, t, area, state in picked if g == group}
        ordered.extend(members[id(t)] for t in marks.reading_order([m[0] for m in members.values()]))
    return tuple(Row(i + 1, t, area, state) for i, (t, area, state) in enumerate(ordered[:cap]))


def by_number(found: Sequence[Row], number: int) -> Row | None:
    return found[number - 1] if 1 <= number <= len(found) else None


def lines(found: Sequence[Row], limit: int | None = None) -> list[str]:
    return [r.text for r in (found if limit is None else found[:limit])]
