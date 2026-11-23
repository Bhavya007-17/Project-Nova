"""Pointing by voice (SPEC §8.1): numbered tags on everything clickable, a 3 × 3 grid where there is nothing to tag,
mouse verbs at a tag's centre, and click-by-name on the window in front.

Sources, in order: UI Automation of the window in front (one cached batch read, ``uia.FocusReader.snapshot``), then
Windows' own OCR of the window (or of its monitor for "numbers everywhere"). Chrome exposes its pages through UIA, so
the page needs no extra source here. The overlay (``nova.island.overlay``) only draws what ``MarksShown`` says.

State is one showing at a time: tags (or a grid) on one window, hidden after an action, "hide numbers", Esc or 15 s.
A UIA tag acts through Invoke when its snapshot is still the current one, else by a real click at its centre.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from ..events import MarksHidden, MarksShown
from . import marks, native, screen, uitree
from . import rows as rows_
from .apps import Outcome
from .marks import Box, Mark, Target
from .native import ActionError, Win

_log = logging.getLogger(__name__)

HIDE_AFTER_S = 15.0
SHOW_HOLD_S = 15.0  # the island keeps the "say a number" line up as long as the tags show
CLICK_WATCH_S = 0.5
DIALOG_TEXT = 300  # characters of the window's own text kept for the risk check ("OK" on a delete dialog)
_BUTTONS = {"click": ("left", 1), "right": ("right", 1), "double": ("left", 2), "middle": ("middle", 1)}
_DONE = {"click": "Clicked", "right": "Right-clicked", "double": "Double-clicked", "middle": "Middle-clicked"}
VERBS = frozenset(_BUTTONS) | {"hover"}
SURE = 0.7  # the model's confidence in one row before Nova clicks it without asking (SPEC §7.4)
MEANING_ASK = 3  # unsure: numbers on at most this many rows

# (what he called it, the rows one per line) -> (row number, confidence), or None: no idea / no model
Meaning = Callable[[str, Sequence[str]], "tuple[int, float] | None"]


@dataclass(frozen=True)
class Found:
    """``locate``'s answer when one thing matched."""

    target: Target
    hwnd: int
    dialog: str  # the window's own text (for "OK" on a delete / discard / send dialog)


def verb_for(button: str | None, count: int | None) -> str:
    if button == "hover":
        return "hover"
    if button == "right":
        return "right"
    if button == "middle":
        return "middle"
    return "double" if (count or 1) >= 2 else "click"


def clip(rect: native.Rect, bounds: native.Rect) -> native.Rect:
    left, top = max(rect[0], bounds[0]), max(rect[1], bounds[1])
    right, bottom = min(rect[2], bounds[2]), min(rect[3], bounds[3])
    return (left, top, right, bottom) if right > left and bottom > top else bounds


def _ocr_lines(rect: native.Rect) -> list[Target]:
    from . import ocr  # noqa: PLC0415 - winrt loads only when numbers need text

    try:
        return ocr.lines(screen.grab(rect))
    except ActionError:
        return []


def _monitor_for(hwnd: int) -> native.Monitor:
    found = native.monitors()
    handle = native.monitor_of(hwnd)
    for monitor in found:
        if monitor.handle == handle:
            return monitor
    primary = next((m for m in found if m.primary), None)
    if primary is None:
        raise ActionError("I can't find a screen to put numbers on.")
    return primary


class Pointer:
    def __init__(self, focus: object | None, publish: Callable[[object], None] = lambda event: None, *,
                 ocr_lines: Callable[[native.Rect], list[Target]] = _ocr_lines,
                 monitor_for: Callable[[int], native.Monitor] = _monitor_for,
                 frame: Callable[[int], native.Rect] = native.frame_rect,
                 hide_after: float = HIDE_AFTER_S, cold_wait: float = uitree.COLD_WAIT_S) -> None:
        self._focus = focus  # uia.FocusReader: snapshot / act / generation
        self._publish = publish
        self._ocr = ocr_lines
        self._monitor_for = monitor_for
        self._frame = frame
        self._hide_after = hide_after
        self._cold_wait = cold_wait
        self._lock = threading.RLock()
        self._targets: list[Target] = []  # everything in scope, reading order
        self._marks: list[Mark] = []  # the page on screen
        self._page = 0
        self._grid: Box | None = None
        self._level = 0
        self._win: Win | None = None
        self._monitor: native.Monitor | None = None
        self._texts: list[str] = []  # the window's own text from the last snapshot
        self._timer: threading.Timer | None = None
        self._showing = 0  # bumps with every showing: a stale timer must not hide a newer one
        self.meaning: Meaning | None = None  # click by meaning (Stage 07d): set once Tier 1 exists

    # --- what's showing ---------------------------------------------------------------------------------------

    @property
    def showing(self) -> bool:
        with self._lock:
            return bool(self._marks) or self._grid is not None

    @property
    def grid_on(self) -> bool:
        with self._lock:
            return self._grid is not None

    def target(self, number: int) -> Target | None:
        with self._lock:
            return next((m.target for m in self._marks if m.number == number), None)

    # --- gathering -------------------------------------------------------------------------------------------

    def _elements(self, win: Win) -> tuple[list[Target], list[str]]:
        found, texts, _ = self._snapshot(win)
        return found, texts

    def _snapshot(self, win: Win) -> tuple[list[Target], list[str], list[uitree.Element]]:
        if self._focus is None:
            return [], [], []
        elements = uitree.warm_snapshot(self._focus, win, self._cold_wait)
        generation = getattr(self._focus, "generation", 0)
        found = [Target(Box(*e.rect), e.name, e.role, "uia", (generation, e.index, e.patterns))
                 for e in elements if e.clickable]
        texts = [e.name for e in elements if e.role == "text" and e.name]
        return found, texts, elements

    def window_area(self, hwnd: int) -> native.Rect:
        """The window's frame, clipped to its screen (what a task's pixel signature watches)."""
        return clip(self._frame(hwnd), self._monitor_for(hwnd).rect)

    def rows(self, win: Win) -> tuple[rows_.Row, ...]:
        """The window as numbered rows for the model (Stage 07d): one UIA snapshot; OCR only when it names few."""
        elements = uitree.warm_snapshot(self._focus, win, self._cold_wait) if self._focus is not None else []
        generation = getattr(self._focus, "generation", 0)
        first = rows_.focus_area(elements)
        found = rows_.build(elements, generation=generation, first=first)
        if len(found) < rows_.OCR_BELOW:
            area = self.window_area(win.hwnd)
            found = rows_.build(elements, generation=generation, first=first, ocr=self._ocr(area))
        return found

    def _area(self, win: Win, monitor: native.Monitor, scope: str) -> native.Rect:
        return monitor.rect if scope == "everywhere" else clip(self._frame(win.hwnd), monitor.rect)

    def gather(self, win: Win, scope: str = "here", *, uia: list[Target] | None = None) -> list[Target]:
        """Everything taggable in scope: UIA elements of the window + OCR lines of the window (or its monitor)."""
        monitor = self._monitor_for(win.hwnd)
        if uia is None:
            uia, self._texts = self._elements(win)
        lines = self._ocr(self._area(win, monitor, scope))
        return marks.merge(uia, [], lines, screen=Box(*monitor.rect), scale=monitor.dpi / 96)

    # --- numbers ---------------------------------------------------------------------------------------------

    def show(self, win: Win, scope: str = "here", *, targets: Sequence[Target] | None = None,
             note: str = "") -> Outcome:
        found = list(targets) if targets is not None else self.gather(win, scope)
        if not found:
            return self.grid(win, scope, note=note or "Nothing to number here — say a square")
        monitor = self._monitor_for(win.hwnd)
        with self._lock:
            previous = self._marks if self._win is not None and self._win.hwnd == win.hwnd else ()
            self._targets = marks.reading_order(found)
            self._page = 0
            self._marks = marks.number(self._targets, previous)
            self._grid, self._level = None, 0
            self._win, self._monitor = win, monitor
        return self._announce(note or "Say a number")

    def more(self) -> Outcome:
        with self._lock:
            if not self._marks:
                raise ActionError("No numbers are showing.")
            total = marks.pages(len(self._targets))
            if total == 1:
                return Outcome("That's all of them", "numbers", hold=SHOW_HOLD_S)
            self._page = (self._page + 1) % total
            self._marks = marks.number(self._targets, page=self._page)
        return self._announce(f"Page {self._page + 1} of {total}")

    # --- the grid --------------------------------------------------------------------------------------------

    def grid(self, win: Win, scope: str = "everywhere", *, note: str = "") -> Outcome:
        monitor = self._monitor_for(win.hwnd)
        with self._lock:
            self._grid = Box(*self._area(win, monitor, scope))
            self._level = 1
            self._marks, self._targets = [], []
            self._win, self._monitor = win, monitor
        return self._announce(note or "Say a square to zoom in")

    def _zoom(self, cell: int) -> Outcome:
        with self._lock:
            assert self._grid is not None
            self._grid = marks.grid_zoom(self._grid, cell)
            self._level += 1
            level = self._level
        return self._announce("Say a square to click it" if level >= marks.GRID_LEVELS else "Say a square")

    # --- acting ----------------------------------------------------------------------------------------------

    def pick(self, number: int | None, verb: str | None = None, to: int | None = None) -> Outcome:
        """A tag or a grid square: a bare number clicks a tag / zooms the grid; "right-click 12", "drag 3 to 7"."""
        if verb is not None and verb not in VERBS:
            raise ActionError(f"I can't {verb} that.")
        with self._lock:
            grid, level = self._grid, self._level
            if grid is None and not self._marks:
                raise ActionError("No numbers are showing — say “show numbers” first.")
            if grid is not None:
                cells = marks.grid_cells(grid)
                if number is None:  # "click" on the zoomed area: its centre
                    start = Target(grid, "the square")
                else:
                    if not 1 <= number <= len(cells):
                        raise ActionError(f"Say a square from 1 to {len(cells)}.")
                    if verb is None and to is None and level < marks.GRID_LEVELS:
                        return self._zoom(number)
                    start = Target(cells[number - 1], f"square {number}")
                end = None
                if to is not None:
                    if not 1 <= to <= len(cells):
                        raise ActionError(f"Say a square from 1 to {len(cells)}.")
                    end = Target(cells[to - 1], f"square {to}")
            else:
                if number is None:
                    raise ActionError("Which number?")
                start = self._mark(number)
                end = self._mark(to) if to is not None else None
        self.hide("acted")
        if end is not None:
            native.drag(start.box.centre, end.box.centre)
            return Outcome(f"Dragged {_said(start, number)} to {_said(end, to)}", "drag")
        return self.act(start, verb or "click", number=number)

    def _mark(self, number: int) -> Target:
        found = next((m.target for m in self._marks if m.number == number), None)
        if found is None:
            raise ActionError(f"There's no number {number}.")
        return found

    def act(self, target: Target, verb: str = "click", *, number: int | None = None,
            hwnd: int | None = None) -> Outcome:
        """Click / hover ``target``: Invoke when its UIA snapshot is still current and it's a plain click, else a
        real click at its centre. A real click that changes nothing on screen is a failure."""
        x, y = target.box.centre
        said = _said(target, number)
        if verb == "hover":
            native.hover_at(x, y)
            return Outcome(f"Pointing at {said}", "hover")
        if verb == "click" and target.source == "uia" and isinstance(target.ref, tuple) and self._focus is not None:
            generation, index, patterns = target.ref
            if generation == getattr(self._focus, "generation", -1) and patterns:
                method = self._focus.act(index, patterns)
                if method is None:  # still running: a dialog probably opened
                    return Outcome(f"Clicked {said}", "invoke")
                if method:
                    return Outcome(f"Clicked {said}", method)
        button, count = _BUTTONS[verb]
        area = _around(target.box, self._frame(hwnd) if hwnd else None)
        moved = self._watch(area, lambda: native.click_at(x, y, button, count))
        if not moved:
            raise ActionError(f"I clicked {said} but nothing changed.")
        return Outcome(f"{_DONE[verb]} {said}", f"{verb}-click")

    @staticmethod
    def _watch(rect: native.Rect, act: Callable[[], None]) -> bool:
        from .scrolling import watch  # noqa: PLC0415 - scrolling imports this module's neighbours

        return watch(rect, act, CLICK_WATCH_S)

    # --- click by name ---------------------------------------------------------------------------------------

    def locate(self, win: Win, name: str, role: str = "", near: str = "") -> Found | Outcome:
        """One match → Found. Several, or none: the model reads the window's rows (``meaning``, Stage 07d) — one row
        it is sure of → Found; unsure → numbers on its row and the tied ones. Without an answer: several → numbers on
        those ("Which one?"); none → numbers on everything, or the grid when there is nothing at all."""
        uia, self._texts, elements = self._snapshot(win)
        hits = marks.rank(name, uia, role=role, near=near)
        if len(hits) == 1:
            return Found(hits[0], win.hwnd, self.dialog_text())
        everything: list[Target] = []
        if not hits:
            everything = self.gather(win, uia=uia)
            hits = marks.rank(name, everything, role=role, near=near)
            if len(hits) == 1:
                return Found(hits[0], win.hwnd, self.dialog_text())
        ocr = [t for t in everything if t.source == "ocr"]
        by_meaning = self._by_meaning(win, name, elements, ocr, hits)
        if by_meaning is not None:
            return by_meaning
        if hits:
            return self.show(win, targets=hits, note="Which one? Say its number")
        if everything:
            return self.show(win, targets=everything, note=f"I don't see “{_cut(name)}” — say a number")
        return self.grid(win, "here", note=f"I don't see “{_cut(name)}” — say a square")

    def _by_meaning(self, win: Win, name: str, elements: Sequence[uitree.Element], ocr: Sequence[Target],
                    hits: Sequence[Target]) -> Found | Outcome | None:
        """Ask the model which row ``name`` means. None: no model, no rows, no usable answer (the old way runs)."""
        meaning = self.meaning
        if meaning is None:
            return None
        found = rows_.build(elements, generation=getattr(self._focus, "generation", 0),
                            first=rows_.focus_area(elements), ocr=ocr)
        if not found:
            return None
        try:
            answer = meaning(name, rows_.lines(found))
        except Exception:  # noqa: BLE001 - a model failure is never worse than click-by-name without it
            _log.warning("click by meaning failed", exc_info=True)
            return None
        row = rows_.by_number(found, answer[0]) if answer else None
        if row is None:
            return None
        tied = [h for h in hits if h.box == row.target.box]
        if answer[1] >= SURE and (not hits or tied):
            return Found(tied[0] if tied else row.target, win.hwnd, self.dialog_text())
        if hits and not tied:
            return None  # it picked none of the tied ones: "Which one?" on those
        others = [h for h in hits if h.box != row.target.box]
        return self.show(win, targets=[row.target, *others][:MEANING_ASK],
                         note=f"“{_cut(row.target.name)}”? Say its number")

    def has_tabs(self, win: Win) -> bool:
        """``win`` shows document tabs of its own (Notepad, Explorer, Terminal): tab keys belong to it, not the
        browser. Ribbon tabs don't count (``marks.doc_tabs``)."""
        if self._focus is None:
            return False
        names = [e.name for e in uitree.warm_snapshot(self._focus, win, self._cold_wait) if e.role == "tab item"]
        if names and win.exe_name.lower() == "windowsterminal.exe":
            return True  # every tab is a shell; a program in it (Claude Code) retitles the window, not the tab
        return marks.doc_tabs(win.title, names)

    def button_named(self, win: Win, said: str) -> str | None:
        """A button of ``win`` named exactly what he said (``marks.button_named``), from a UIA snapshot."""
        if not 0 < len(marks.normalize(said).split()) <= marks.BUTTON_WORDS:
            return None  # can't be a button's name: no snapshot
        uia, _ = self._elements(win)
        return marks.button_named(said, uia)

    def dialog_text(self) -> str:
        return " ".join(" ".join(self._texts).split())[:DIALOG_TEXT]

    # --- hiding ----------------------------------------------------------------------------------------------

    def hide(self, why: str = "hidden") -> bool:
        with self._lock:
            was = bool(self._marks) or self._grid is not None
            self._marks, self._targets, self._grid, self._level = [], [], None, 0
            self._showing += 1
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        if was:
            self._publish(MarksHidden(why))
        return was

    def _announce(self, note: str) -> Outcome:
        with self._lock:
            assert self._monitor is not None
            self._showing += 1
            showing = self._showing
            cells: tuple[tuple[int, native.Rect], ...] = ()
            if self._grid is not None:
                cells = tuple((i + 1, _rect(c)) for i, c in enumerate(marks.grid_cells(self._grid)))
            tags = tuple((m.number, _rect(m.target.box)) for m in self._marks)
            shown = MarksShown(self._monitor.device, self._monitor.rect, tags, cells, note)
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self._hide_after, self._expire, args=(showing,))
            self._timer.daemon = True
            self._timer.start()
        self._publish(shown)
        what = f"{len(tags)} numbers" if tags else "Grid"
        return Outcome(f"{what} · {note}" if note else what, "grid" if cells else "numbers", hold=SHOW_HOLD_S)

    def _expire(self, showing: int) -> None:
        with self._lock:
            if showing != self._showing:
                return
        self.hide("timed out")


def _rect(box: Box) -> native.Rect:
    return (box.left, box.top, box.right, box.bottom)


def _around(box: Box, frame: native.Rect | None, pad: int = 24) -> native.Rect:
    """Where a click's effect shows: the window when we know it, else the target and a margin."""
    if frame is not None:
        return frame
    return (box.left - pad, box.top - pad, box.right + pad, box.bottom + pad)


def _said(target: Target, number: int | None) -> str:
    if target.name:
        return f"“{_cut(target.name)}”"
    return f"number {number}" if number is not None else "it"


def _cut(text: str, limit: int = 28) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"

