"""The one QObject QML sees as ``island``: the current ``View`` as properties, and slots for clicks.

Lives on the Qt GUI thread. ``apply(view)`` emits only the notify signals of properties
that changed, so QML re-evaluates only the bindings that depend on them (a list
property re-read would rebuild its Repeater). Mic level has its own property: it
changes ~30x a second while listening and nothing else should re-evaluate for it.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Property, QObject, QRectF, Signal, Slot

from .view import View

_GETTERS: dict[str, Callable[[View], object]] = {
    "state": lambda v: v.state,
    "accent": lambda v: v.accent,
    "open": lambda v: v.open,
    "expanded": lambda v: v.expanded,
    "interactive": lambda v: v.interactive,
    "words": lambda v: v.words,
    "final": lambda v: v.final,
    "fresh": lambda v: v.fresh,
    "windowed": lambda v: v.windowed,
    "placeholder": lambda v: v.placeholder,
    "note": lambda v: v.note,
    "steps": lambda v: list(v.steps),
    "step": lambda v: v.step,
    "result": lambda v: v.result,
    "icon": lambda v: v.icon,
    "question": lambda v: v.question,
    "choices": lambda v: list(v.choices),
    "buttons": lambda v: list(v.buttons),
    "chips": lambda v: [{"id": c.id, "text": c.text, "status": c.status} for c in v.chips],
    "pinned": lambda v: v.pinned,
    "always": lambda v: v.always,
}


class IslandBridge(QObject):
    stateChanged = Signal()
    accentChanged = Signal()
    openChanged = Signal()
    expandedChanged = Signal()
    interactiveChanged = Signal()
    wordsChanged = Signal()
    finalChanged = Signal()
    freshChanged = Signal()
    windowedChanged = Signal()
    placeholderChanged = Signal()
    noteChanged = Signal()
    stepsChanged = Signal()
    stepChanged = Signal()
    resultChanged = Signal()
    iconChanged = Signal()
    questionChanged = Signal()
    choicesChanged = Signal()
    buttonsChanged = Signal()
    chipsChanged = Signal()
    pinnedChanged = Signal()
    alwaysChanged = Signal()
    levelChanged = Signal()

    def __init__(
        self,
        on_act: Callable[[str, int, str], None] = lambda what, index, chip: None,
        on_collapsed: Callable[[], None] = lambda: None,
        on_hit_area: Callable[[list[QRectF], bool], None] = lambda rects, settled: None,
        on_dropped: Callable[[], None] = lambda: None,
        on_reset_home: Callable[[], None] = lambda: None,
    ) -> None:
        super().__init__()
        self._view = View()
        self._level = 0.0
        self._on_act = on_act
        self._on_collapsed = on_collapsed
        self._on_hit_area = on_hit_area
        self._on_dropped = on_dropped
        self._on_reset_home = on_reset_home

    @property
    def view(self) -> View:
        return self._view

    def apply(self, view: View) -> None:
        old, self._view = self._view, view
        if old == view:
            return
        for name, get in _GETTERS.items():
            if get(old) != get(view):
                getattr(self, f"{name}Changed").emit()

    def set_level(self, level: float) -> None:
        if level != self._level:
            self._level = level
            self.levelChanged.emit()

    # --- from QML ------------------------------------------------------------------------------

    @Slot(str, int, str)
    def act(self, what: str, index: int, chip: str) -> None:
        """A click: yes | no | choice (index) | retry | show-me | chip (chip id)."""
        self._on_act(what, index, chip)

    @Slot()
    def collapsed(self) -> None:
        """The pill finished closing and nothing else is showing: the window can hide."""
        self._on_collapsed()

    @Slot()
    def dropped(self) -> None:
        """He let go after dragging the pill: that spot is the island's home now."""
        self._on_dropped()

    @Slot()
    def resetHome(self) -> None:  # noqa: N802 - called from QML
        """Double-click: back to the top centre."""
        self._on_reset_home()

    @Slot("QVariantList", bool)
    def setHitArea(self, rects: list, settled: bool) -> None:  # noqa: N802 - called from QML
        """Where the visible content is (window coordinates) and whether it is still moving."""
        self._on_hit_area([QRectF(rect) for rect in rects], settled)

    # --- properties --------------------------------------------------------------------------

    state = Property(str, lambda self: self._view.state, notify=stateChanged)
    accent = Property(str, lambda self: self._view.accent, notify=accentChanged)
    open = Property(bool, lambda self: self._view.open, notify=openChanged)
    expanded = Property(bool, lambda self: self._view.expanded, notify=expandedChanged)
    interactive = Property(bool, lambda self: self._view.interactive, notify=interactiveChanged)
    words = Property(str, lambda self: self._view.words, notify=wordsChanged)
    final = Property(bool, lambda self: self._view.final, notify=finalChanged)
    fresh = Property(int, lambda self: self._view.fresh, notify=freshChanged)
    windowed = Property(bool, lambda self: self._view.windowed, notify=windowedChanged)
    placeholder = Property(str, lambda self: self._view.placeholder, notify=placeholderChanged)
    note = Property(str, lambda self: self._view.note, notify=noteChanged)
    steps = Property(list, lambda self: list(self._view.steps), notify=stepsChanged)
    step = Property(int, lambda self: self._view.step, notify=stepChanged)
    result = Property(str, lambda self: self._view.result, notify=resultChanged)
    icon = Property(str, lambda self: self._view.icon, notify=iconChanged)
    question = Property(str, lambda self: self._view.question, notify=questionChanged)
    choices = Property(list, lambda self: list(self._view.choices), notify=choicesChanged)
    buttons = Property(list, lambda self: list(self._view.buttons), notify=buttonsChanged)
    chips = Property(list, lambda self: _GETTERS["chips"](self._view), notify=chipsChanged)
    pinned = Property(bool, lambda self: self._view.pinned, notify=pinnedChanged)
    always = Property(bool, lambda self: self._view.always, notify=alwaysChanged)
    level = Property(float, lambda self: self._level, notify=levelChanged)
