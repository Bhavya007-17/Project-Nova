"""Scrolling that says what moved (SPEC §8, Stage 07c).

One scroll tries, in order: the page through the extension (Chrome in front), UI Automation's ScrollPattern on the
content, PageDown / arrow / Ctrl+Home keys, the wheel at the content's centre. The first that moves wins. Every try
returns a receipt (the extension's ``moved``, the scroll percent, or the pixels under the content), and "Already at
the bottom" needs two tries that agree — one method saying "nothing moved" is not proof (a Chrome PDF ignores the
page script; an app's own scroller ignores the wheel).

Also "keep scrolling" (steady wheel ticks until "stop", the end, or another window in front) and document moves:
go to page N, next / previous page, zoom, fit to width.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from . import input as keyboard
from . import native, screen, uitree
from .apps import Outcome
from .native import ActionError, Win

_log = logging.getLogger(__name__)

MOVED, AT_END, UNMOVED, UNAVAILABLE = "moved", "at_end", "unmoved", "unavailable"
WATCH_S = 0.4  # how long a key / wheel scroll gets to show on screen (smooth scrolling animates)
SPEEDS = (0.8, 0.5, 0.3, 0.18, 0.1)  # seconds per wheel notch, slow -> fast
KEEP_SPEED, SLOW_SPEED = 2, 1
STILL_CHECKS = 3  # steady scrolling stops after this many looks at the screen without a change
WHEEL_NOTCHES = {"bit": 2, "small": 2, "page": 5, "large": 15}

Method = Callable[[str, str], str]  # (direction, amount) -> MOVED / AT_END / UNMOVED / UNAVAILABLE


@dataclass(frozen=True)
class Scrolled:
    moved: bool
    method: str  # the try that decided it
    text: str  # what the island says
    tries: tuple[tuple[str, str], ...]


def edge(direction: str) -> str:
    if direction in ("up", "top"):
        return "top"
    if direction in ("down", "bottom"):
        return "bottom"
    return f"{direction} edge"


def moved_text(direction: str, amount: str) -> str:
    if direction in ("top", "bottom"):
        return f"Scrolled to the {direction}"
    if amount == "half":
        return "Scrolled halfway"
    if amount in ("bit", "small"):
        return f"Scrolled {direction} a bit"
    return f"Scrolled {direction}"


def cascade(methods: Sequence[tuple[str, Method]], direction: str, amount: str) -> Scrolled:
    """Try each method until one moves. Two that say "nothing moved" = already at the end. None that could tell =
    an error (nothing scrolled, and Nova can't say it's at the end)."""
    tries: list[tuple[str, str]] = []
    votes = 0
    for name, method in methods:
        try:
            result = method(direction, amount)
        except ActionError as error:
            _log.info("scroll by %s: %s", name, error)
            result = UNAVAILABLE
        tries.append((name, result))
        if result == MOVED:
            return Scrolled(True, name, moved_text(direction, amount), tuple(tries))
        if result in (AT_END, UNMOVED):
            votes += 1
            if votes >= 2:
                return Scrolled(False, name, f"Already at the {edge(direction)}", tuple(tries))
    _log.info("scroll %s %s: %s", direction, amount, tries)
    raise ActionError("Nothing scrolled — is there something to scroll in front?")


def key_presses(direction: str, amount: str) -> list[str]:
    """The keys one scroll presses (empty = keys can't do it, e.g. halfway)."""
    if direction == "top":
        return ["ctrl+home"]
    if direction == "bottom":
        return ["ctrl+end"]
    if amount == "half":
        return []
    vertical = direction in ("up", "down")
    if vertical and amount in ("page", "large"):
        return ["pageup" if direction == "up" else "pagedown"] * (3 if amount == "large" else 1)
    return [direction] * (3 if amount in ("bit", "small") or not vertical else 6)


def wheel_notches(direction: str, amount: str) -> int:
    """Signed notches (positive = down / right); 0 = the wheel can't do it."""
    if direction in ("top", "bottom") or amount == "half":
        return 0
    notches = WHEEL_NOTCHES.get(amount, WHEEL_NOTCHES["page"])
    return notches if direction in ("down", "right") else -notches


@dataclass(frozen=True)
class Front:
    """What one scroll works on: the window in front, its content's middle, and whether it's Chrome's PDF viewer."""

    win: Win
    rect: native.Rect
    centre: tuple[int, int]
    chrome: bool  # Chrome in front with the extension connected

    @classmethod
    def of(cls, win: Win, chrome: bool) -> Front:
        rect = native.frame_rect(win.hwnd)
        inner = screen.inset(rect)
        return cls(win, rect, ((inner[0] + inner[2]) // 2, (inner[1] + inner[3]) // 2), chrome)

    @property
    def watched(self) -> native.Rect:
        return screen.inset(self.rect)


def watch(rect: native.Rect, act: Callable[[], None], timeout: float = WATCH_S) -> bool:
    """Run ``act`` and say whether the pixels in ``rect`` changed within ``timeout``."""
    before = screen.signature(rect)
    act()
    deadline = time.monotonic() + timeout
    while True:
        time.sleep(0.05)
        if screen.changed(before, screen.signature(rect)):
            return True
        if time.monotonic() >= deadline:
            return False


class Scroller:
    """The scroll cascade, steady scrolling and document moves for the window in front."""

    def __init__(self, focus: object | None, chrome: Callable[[], object | None],
                 notify: Callable[[str], None] = lambda text: None,
                 on_change: Callable[[bool], None] = lambda on: None) -> None:
        self._focus = focus  # uia.FocusReader (ScrollPattern, SetFocus)
        self._chrome = chrome  # -> actions.chrome.Chrome, or None
        self._notify = notify
        self._on_change = on_change  # steady scrolling started (True) / stopped (False)
        self._steady: Steady | None = None
        self._lock = threading.Lock()

    # --- one scroll -------------------------------------------------------------------------------------------

    def _chrome_front(self) -> object | None:
        chrome = self._chrome()
        if chrome is None or not chrome.connected or not chrome.chrome_in_front():
            return None
        return chrome

    def scroll(self, win: Win, direction: str, amount: str) -> Outcome:
        front = Front.of(win, self._chrome_front() is not None)
        state = {"pdf": False}
        done = cascade(self.methods(front, state), direction, amount)
        return Outcome(done.text, done.method)

    def methods(self, front: Front, state: dict[str, bool]) -> list[tuple[str, Method]]:
        found: list[tuple[str, Method]] = []
        if front.chrome:
            found.append(("page", lambda d, a: self._page(d, a, state)))
        if self._focus is not None:
            found.append(("pattern", lambda d, a: self._pattern(front, d, a)))
        found.append(("keys", lambda d, a: self._keys(front, d, a, state)))
        found.append(("wheel", lambda d, a: self._wheel(front, d, a)))
        return found

    def _page(self, direction: str, amount: str, state: dict[str, bool]) -> str:
        chrome = self._chrome()
        result = chrome.scroll(direction, amount)
        if result.get("pdf"):
            state["pdf"] = True
            return UNAVAILABLE
        if not result.get("ok"):
            return UNAVAILABLE
        return MOVED if result.get("moved") else AT_END

    def _pattern(self, front: Front, direction: str, amount: str) -> str:
        receipt = self._focus.scroll(direction, amount, *front.centre)
        if receipt is None:
            return UNAVAILABLE
        if receipt.moved:
            return MOVED
        towards_end = direction in ("down", "right", "bottom")
        at_limit = receipt.after >= 99.9 if towards_end else receipt.after <= 0.1
        return AT_END if at_limit else UNMOVED

    def _keys(self, front: Front, direction: str, amount: str, state: dict[str, bool]) -> str:
        presses = key_presses(direction, amount)
        if not presses:
            return UNAVAILABLE
        if state["pdf"] and self._focus is not None:
            self._focus.focus_at(*front.centre)  # the viewer takes keys only once its content has focus

        def press() -> None:
            for keys in presses:
                keyboard.press(keys, front.win)

        return MOVED if watch(front.watched, press) else UNMOVED

    @staticmethod
    def _wheel(front: Front, direction: str, amount: str) -> str:
        notches = wheel_notches(direction, amount)
        if not notches:
            return UNAVAILABLE
        horizontal = direction in ("left", "right")
        return MOVED if watch(front.watched, lambda: native.wheel_at(*front.centre, notches, horizontal)) else UNMOVED

    # --- steady scrolling ------------------------------------------------------------------------------------------

    def steady(self, win: Win, direction: str, speed: str = "keep") -> Outcome:
        if direction not in ("up", "down", "left", "right"):
            raise ActionError("I can keep scrolling up, down, left or right.")
        self.stop()
        front = Front.of(win, False)
        level = SLOW_SPEED if speed == "slow" else len(SPEEDS) - 1 if speed == "fast" else KEEP_SPEED
        steady = Steady(front, direction, level, self._ended)
        with self._lock:
            self._steady = steady
        steady.start()
        self._on_change(True)
        return Outcome(f"Scrolling {direction} — say “stop”", "steady")

    def change_speed(self, faster: bool) -> Outcome:
        steady = self._steady
        if steady is None or not steady.running:
            raise ActionError("I'm not scrolling right now.")
        level = steady.change(1 if faster else -1)
        top = level == (len(SPEEDS) - 1 if faster else 0)
        word = "Faster" if faster else "Slower"
        return Outcome(f"{word} (that's the {'fastest' if faster else 'slowest'})" if top else word, "steady")

    def stop(self) -> bool:
        """Stop steady scrolling. True = it was running."""
        with self._lock:
            steady, self._steady = self._steady, None
        if steady is None or not steady.running:
            return False
        steady.stop()
        self._on_change(False)
        return True

    @property
    def scrolling(self) -> bool:
        steady = self._steady
        return steady is not None and steady.running

    def _ended(self, steady: Steady, why: str) -> None:
        with self._lock:
            current = self._steady is steady
            if current:
                self._steady = None
        if current:  # a run that was replaced or stopped has said so already
            self._on_change(False)
        if why == "end":
            self._notify(f"Reached the {edge(steady.direction)}")

    # --- documents ---------------------------------------------------------------------------------------------

    def _pdf(self) -> object | None:
        chrome = self._chrome_front()
        if chrome is None:
            return None
        try:
            return chrome if chrome.kind().get("pdf") else None
        except ActionError:
            return None

    def doc(self, win: Win, op: str, page: int | None = None) -> Outcome:
        front = Front.of(win, False)
        chrome = self._pdf()
        if chrome is not None and self._focus is not None and op != "goto_page":
            self._focus.focus_at(*front.centre)
        match op:
            case "goto_page":
                return self._goto(front, chrome, page)
            case "next_page" | "prev_page":
                forward = op == "next_page"
                keys = ("right" if forward else "left") if chrome is not None else ("pagedown" if forward else "pageup")
                if not watch(front.watched, lambda: keyboard.press(keys, win)):
                    raise ActionError("The page didn't change — is it the last one?" if forward else
                                      "The page didn't change — is it the first one?")
                return Outcome("Next page" if forward else "Previous page", "keys")
            case "zoom_in" | "zoom_out" | "zoom_reset":
                keys = {"zoom_in": "ctrl+=", "zoom_out": "ctrl+-", "zoom_reset": "ctrl+0"}[op]
                text = {"zoom_in": "Zoomed in", "zoom_out": "Zoomed out", "zoom_reset": "Zoom reset"}[op]
                if not watch(screen.inset(front.rect, 0.05, 0.15, 0.05, 0.05), lambda: keyboard.press(keys, win)):
                    raise ActionError("Nothing changed — it may not zoom any further.")
                return Outcome(text, "keys")
            case "fit_width":
                return self._fit_width(front, chrome)
        raise ActionError(f"I can't do {op.replace('_', ' ')} yet.")

    def _goto(self, front: Front, chrome: object | None, page: int | None) -> Outcome:
        if page is None or page < 1:
            raise ActionError("Which page?")
        if chrome is None:
            raise ActionError("Going to a page works in PDFs open in Chrome for now.")
        tab = chrome.active_tab()
        if tab is None:
            raise ActionError("I couldn't see Chrome's tab.")
        url = tab.url.split("#", 1)[0] + f"#page={page}"
        if watch(front.watched, lambda: chrome.navigate(tab, url), timeout=1.0):
            return Outcome(f"Page {page}", "url")
        return self._page_box(front, page)

    def _page_box(self, front: Front, page: int) -> Outcome:
        """The viewer's page-number box: click it, type the number, Enter."""
        box = self._find(front.win, ("page number", "page"), role="edit")
        if box is None:
            raise ActionError(f"I couldn't get to page {page}.")
        left, top, right, bottom = box.rect
        native.click_at((left + right) // 2, (top + bottom) // 2)
        keyboard.press("ctrl+a", front.win)
        if not watch(front.watched, lambda: keyboard.type_text(str(page), True, front.win), timeout=1.0):
            raise ActionError(f"Page {page} didn't show — does the document have that many?")
        return Outcome(f"Page {page}", "page-box")

    def _fit_width(self, front: Front, chrome: object | None) -> Outcome:
        if chrome is None:
            raise ActionError("Fit to width works in PDFs open in Chrome for now.")
        button = self._find(front.win, ("fit to width",), role="button")
        if button is None:
            if self._find(front.win, ("fit to page",), role="button") is not None:
                return Outcome("Already fit to width", "uia")
            raise ActionError("I couldn't find the viewer's fit button.")
        if self._focus.act(button.index, button.patterns) is None:
            raise ActionError("The fit button didn't answer.")
        return Outcome("Fit to width", "uia")

    def _find(self, win: Win, names: tuple[str, ...], role: str) -> object | None:
        if self._focus is None:
            return None
        elements = uitree.warm_snapshot(self._focus, win)  # a cold Chrome PDF tree has only its caption buttons
        for wanted in names:
            for element in elements:
                if element.role == role and element.name.strip().lower() == wanted:
                    return element
        return next((e for e in elements if e.role == role and names[-1] in e.name.lower()), None)


class Steady:
    """Wheel ticks at the content's middle until stopped, the content stops moving, or another window comes up."""

    def __init__(self, front: Front, direction: str, level: int, ended: Callable[[Steady, str], None]) -> None:
        self.front = front
        self.direction = direction
        self._level = level
        self._ended = ended
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="nova-steady-scroll", daemon=True)

    @property
    def running(self) -> bool:
        return self._thread.is_alive() and not self._stop.is_set()

    @property
    def level(self) -> int:
        return self._level

    def start(self) -> None:
        self._thread.start()

    def change(self, step: int) -> int:
        self._level = max(0, min(len(SPEEDS) - 1, self._level + step))
        return self._level

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        sign = 1 if self.direction in ("down", "right") else -1
        horizontal = self.direction in ("left", "right")
        why = "stopped"
        try:
            last = screen.signature(self.front.watched)
            ticks = still = 0
            while not self._stop.wait(SPEEDS[self._level]):
                fg = native.foreground()
                if fg != self.front.win.hwnd and native.root(fg) != self.front.win.hwnd:
                    why = "focus"
                    break
                native.wheel_at(*self.front.centre, sign, horizontal)
                ticks += 1
                if ticks % 2:
                    continue
                now = screen.signature(self.front.watched)
                still = 0 if screen.changed(last, now) else still + 1
                last = now
                if still >= STILL_CHECKS:
                    why = "end"
                    break
        except ActionError as error:
            _log.info("steady scrolling stopped: %s", error)
            why = "error"
        finally:
            self._stop.set()
            _log.info("steady scrolling %s ended: %s", self.direction, why)
            self._ended(self, why)
