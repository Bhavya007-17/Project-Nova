"""The window in front as a task's world (``nova.brain.task.World``, Stage 07d).

Look: the front window's rows (``Pointer.rows``: one UIA snapshot, OCR only when it names few) plus a pixel signature
of its frame. Act: a click on a row through the pointer (Invoke while the snapshot is current, else a real click);
typing clicks the row first and checks the focused control again, so a password field is never typed into even when
the click moved the focus there; keys and scrolling go through the hands like any plan step.

Narrator: the task's step line and notes as guide mode's island line (``Guiding``), and its mode for the triggers
(``TaskRunning``: "just do it" / "next" / "where?" steer it).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..brain.plan import Step
from ..brain.risk import Scene
from ..brain.task import NUDGE, Choice, Steer, View
from ..events import GuideHidden, GuideRing, Guiding, TaskRunning
from . import native, screen
from .marks import Box
from .native import ActionError, Rect, Win
from .rows import Row

_ON_ROW = {"click": "click", "right": "right", "double": "double"}


class WindowWorld:
    def __init__(self, pointer: Any, front: Callable[[], Win], scene: Callable[[Step | None], Scene],
                 run: Callable[[Step], Any], *, signature: Callable[[Rect], tuple[int, ...]] = screen.signature,
                 publish: Callable[[object], None] = lambda event: None,
                 frame: Callable[[int], Rect] = native.frame_rect,
                 cursor: Callable[[], tuple[int, int]] = native.cursor) -> None:
        self._pointer = pointer  # pointing.Pointer: rows / act / window_area / monitor
        self._front = front
        self._scene = scene
        self._run = run
        self._signature = signature
        self._publish = publish
        self._frame = frame
        self._cursor = cursor
        self._looked: tuple[int, Rect | None] | None = None  # the window the rows came from, and its frame then
        self._ring: GuideRing | None = None

    def look(self) -> View:
        win = self._front()
        self._looked = (win.hwnd, self._frame_of(win.hwnd))
        return View(tuple(self._pointer.rows(win)), self._pixels(win))

    def _frame_of(self, hwnd: int) -> Rect | None:
        try:
            return self._frame(hwnd)
        except (ActionError, OSError):
            return None

    def show(self, row: Row | None, caption: str) -> Box | None:
        """Ring the row where it is now: its box from the last look, moved as far as its window moved since."""
        if row is None or self._looked is None:
            self.hide()
            return None
        hwnd, then = self._looked
        now = self._frame_of(hwnd)
        box = row.target.box
        if then is not None and now is not None and (now[0], now[1]) != (then[0], then[1]):
            dx, dy = now[0] - then[0], now[1] - then[1]
            box = Box(box.left + dx, box.top + dy, box.right + dx, box.bottom + dy)
        try:
            monitor = self._pointer.monitor(hwnd)
        except ActionError:
            return box
        ring = GuideRing(monitor.device, monitor.rect, (box.left, box.top, box.right, box.bottom), caption)
        if ring != self._ring:
            self._ring = ring
            self._publish(ring)
        return box

    def hide(self) -> None:
        if self._ring is not None:
            self._ring = None
            self._publish(GuideHidden("guide"))

    def cursor(self) -> tuple[int, int] | None:
        try:
            return self._cursor()
        except (ActionError, OSError):
            return None

    def signature(self) -> tuple[int, ...]:
        try:
            return self._pixels(self._front())
        except ActionError:
            return ()

    def _pixels(self, win: Win) -> tuple[int, ...]:
        try:
            return self._signature(self._pointer.window_area(win.hwnd))
        except ActionError:
            return ()

    def where(self, s: Step | None = None) -> Scene:
        return self._scene(s)

    def act(self, row: Row | None, choice: Choice) -> Any:
        verb = choice.verb
        if verb in _ON_ROW:
            if row is None:
                raise ActionError("Which one? I lost the row.")
            return self._pointer.act(row.target, _ON_ROW[verb], hwnd=self._front().hwnd)
        if verb == "type":
            typing = Step("type_text", {"text": choice.text})
            if row is not None:
                self._pointer.act(row.target, "click", hwnd=self._front().hwnd)
                if self._scene(typing).password:  # the click may have landed in a password field (SPEC §14)
                    raise ActionError("That's a password field — I never type there.")
            return self._run(typing)
        if verb == "press":
            return self._run(Step("press", {"keys": choice.keys}))
        direction = choice.text if choice.text in ("up", "down", "left", "right") else "down"
        return self._run(Step("scroll", {"dir": direction}))


class Narrator:
    """A task's progress and notes, also as guide mode's island line: each ringed step is the caption
    ("2 · Click “Word wrap”"), a note shows under it (a wrong click, the nudge) and the next caption carries it once
    (the nudge excepted: he did something since). Doing a step takes the line down; so does the end.

    The mode is published as each step starts (the loop itself may turn "just do it" back to guiding in settings),
    so a "next" or "where?" only steers while a step is ringed for him."""

    def __init__(self, steer: Steer, publish: Callable[[object], None],
                 progress: Callable[[str, int], None] = lambda label, n: None) -> None:
        self._steer = steer
        self._publish = publish
        self._progress = progress
        self._on = False
        self._carried = ""
        self._running = ""

    def begin(self) -> None:
        self._running = self._steer.mode
        self._publish(TaskRunning(self._running))

    def step(self, label: str, n: int) -> None:
        if self._steer.mode != self._running:
            self._running = self._steer.mode
            self._publish(TaskRunning(self._running))
        if self._steer.mode == "guide":
            self._publish(Guiding(True, f"{n} · {label}", self._carried))
            self._on = True
        elif self._on:
            self._publish(Guiding(False))
            self._on = False
        self._carried = ""
        self._progress(label, n)

    def note(self, text: str) -> None:
        if self._on:
            self._publish(Guiding(True, note=text))
        self._carried = "" if text == NUDGE else text

    def end(self) -> None:
        if self._on:
            self._on = False
            self._publish(Guiding(False))
        if self._running:
            self._running = ""
            self._publish(TaskRunning(""))
