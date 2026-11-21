"""The hands: one plan step -> the Windows action that does it (SPEC §8).

Also what the executor's risk check needs to know about the screen (``scene``) and the
short label the island's step line shows (``label``). Runs on the brain thread.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import quote_plus, urlsplit

from ..brain import abilities
from ..brain.executor import Pick
from ..brain.plan import Step
from ..brain.risk import Scene, needs_focus
from ..events import SteadyScroll
from . import apps, capture, native, places, system, windows
from .scrolling import Scroller
from . import input as keyboard
from .apps import Outcome
from .catalog import App, Catalog, short_name
from .chrome import ChromeHands
from .native import ActionError, Win
from .pointing import Found, Pointer, verb_for
from .uia import FocusReader

_log = logging.getLogger(__name__)

ENGINES = {
    "google": "https://www.google.com/search?q={q}",
    "youtube": "https://www.youtube.com/results?search_query={q}",
    "bing": "https://www.bing.com/search?q={q}",
    "duckduckgo": "https://duckduckgo.com/?q={q}",
    "wikipedia": "https://en.wikipedia.org/w/index.php?search={q}",
    "amazon": "https://www.amazon.com/s?k={q}",
    "github": "https://github.com/search?q={q}",
    "maps": "https://www.google.com/maps/search/{q}",
    "images": "https://www.google.com/search?tbm=isch&q={q}",
}
_TAB_KEYS = {"new": "ctrl+t", "close": "ctrl+w", "next": "ctrl+tab", "prev": "ctrl+shift+tab", "reopen": "ctrl+shift+t"}
_TAB_TEXT = {"new": "New tab", "close": "Closed the tab", "next": "Next tab", "prev": "Previous tab",
             "reopen": "Reopened the tab"}
_NAV_KEYS = {"back": "alt+left", "forward": "alt+right", "reload": "f5", "up": "alt+up"}
_NAV_TEXT = {"back": "Back", "forward": "Forward", "reload": "Reloaded", "up": "Up a folder"}
_EXPLORER = "CabinetWClass"
# what "undo that" undoes: the last of these, with Ctrl+Z in the window it happened in
_UNDOABLE = frozenset({"type_text", "press", "ui_click", "pick", "page_fill"})
_OP_LABEL = {"maximize": "Maximize", "minimize": "Minimize", "restore": "Restore", "close": "Close",
             "fullscreen": "Full screen", "snap_left": "← left", "snap_right": "→ right", "snap_top": "↑ top",
             "snap_bottom": "↓ bottom", "center": "Center", "move_monitor": "Other monitor"}
_VERB_LABEL = {"click": "Click", "right": "Right-click", "double": "Double-click", "middle": "Middle-click",
               "hover": "Point at"}
_NOT_WINDOWS = frozenset({"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"})
_CONTAINERS = frozenset({50033, 50032})  # UIA pane / window: not a field, so the app didn't say what has focus
LAYOUT_WAIT_S = 20.0


@dataclass(frozen=True)
class NovaControls:
    """What steps about Nova itself do (dictation / listening), wired by the brain."""

    dictation: Callable[..., str] = lambda op, text=None: "Dictation runs in the tray app"
    listening: Callable[[str], str] = lambda op: "OK"


class WindowsHands:
    def __init__(self, catalog: Callable[[], Catalog], focus: FocusReader | None = None,
                 nova: NovaControls | None = None, chrome: ChromeHands | None = None,
                 publish: Callable[[object], None] | None = None, pointer: Pointer | None = None) -> None:
        self._catalog = catalog
        self._focus = focus or FocusReader()
        self._nova = nova or NovaControls()
        self._chrome = chrome
        self.stop = threading.Event()
        self.scroller = Scroller(self._focus, lambda: self._chrome.chrome if self._chrome is not None else None,
                                 on_change=lambda on: (publish or (lambda event: None))(SteadyScroll(on)))
        self.pointer = pointer or Pointer(self._focus, publish or (lambda event: None))
        self._found: Found | None = None  # what the last ui_click resolved to (brain thread only)
        self._located = 0
        self._edited = 0  # the window the last undoable step ran in (brain thread only)

    @property
    def chrome(self) -> ChromeHands | None:
        return self._chrome

    # --- for the executor -------------------------------------------------------------------------------------

    def resolve(self, s: Step, ctx: object) -> object:
        """Page / Canvas / named-tab steps are resolved in Chrome first (``nova.actions.chrome``); "click X" on the
        window in front (the page map when Chrome is in front, UI Automation otherwise); a number to its label."""
        if s.action == "ui_click":
            return self._resolve_click(s, ctx)
        if s.action == "pick":
            return self._resolve_pick(s)
        if s.action == "open_path" and not s.get("path"):
            return self._resolve_path(s)
        if s.action == "nova" and s.get("op") == "help":
            return self._help(s.get("topic"))
        if self._chrome is not None and self._chrome.handles(s):
            return self._chrome.resolve(s, ctx)
        return s

    def _chrome_front(self) -> bool:
        return self._chrome is not None and self._chrome.chrome.connected and self._chrome.chrome.chrome_in_front()

    def _resolve_click(self, s: Step, ctx: object) -> object:
        name = str(s.get("name") or "")
        if verb_for(s.get("button"), s.get("count")) == "click" and self._chrome_front():
            try:  # the page first; Chrome's own buttons (and anything the page map misses) through UIA below
                return self._chrome.resolve(Step("page_click", {"description": name}), ctx)
            except ActionError as error:
                _log.info("page map: %s; trying UI Automation", error)
        found = self.pointer.locate(self._front(), name, str(s.get("role") or ""), str(s.get("near") or ""))
        if isinstance(found, Outcome):
            return found  # numbers on the candidates: he answers with one
        self._located += 1
        self._found = found
        return Step("ui_click", {**s.args, "label": found.target.name or name, "dialog": found.dialog,
                                 "located": self._located})

    def _resolve_pick(self, s: Step) -> Step:
        number = s.get("n")
        if number is None or self.pointer.grid_on:
            return s
        target = self.pointer.target(int(number))
        if target is None:
            if self.pointer.showing:
                raise ActionError(f"There's no number {number}.")
            raise ActionError("No numbers are showing — say “show numbers” first.")
        return Step("pick", {**s.args, "label": target.name, "dialog": self.pointer.dialog_text()})

    @staticmethod
    def _resolve_path(s: Step) -> Step | Pick:
        name = str(s.get("name") or "")
        folder = s.get("folder")
        found = places.find(name, folder)
        if not found:
            where = places.folder_title(places.folder_key(folder) or "") if folder else "your usual folders"
            raise ActionError(f"I couldn't find “{_cut(name)}” in {where}.")
        steps = tuple(Step("open_path", {**s.args, "path": e.path, "label": e.name, "dir": e.is_dir}) for e in found)
        if len(steps) == 1:
            return steps[0]
        return Pick(f"Which “{_cut(name)}”?", tuple(e.where for e in found), steps)

    @staticmethod
    def _help(topic: object) -> Pick | Outcome:
        if topic:
            return Outcome(abilities.topic(str(topic)), "help", hold=abilities.HOLD_S)
        question, choices, topics = abilities.card()
        return Pick(question, choices, tuple(Step("nova", {"op": "help", "topic": t}) for t in topics),
                    optional=True, detail="Say “what can I say?” any time", hold=2.0)

    def preview(self, s: Step, on: bool) -> None:
        if self._chrome is not None:
            self._chrome.preview(s, on)

    def label(self, s: Step) -> str:
        a = s.args
        if self._chrome is not None:
            text = self._chrome.label(s)
            if text:
                return text
        match s.action:
            case "open_app" | "switch_app":
                return short_name(a["app"])
            case "close_app":
                return f"Close {short_name(a['app'])}"
            case "window":
                target = a.get("target", "active")
                who = "" if target == "active" else short_name(target) + " "
                return f"{who}{_OP_LABEL.get(a['op'], a['op'])}".strip()
            case "layout":
                return " | ".join(short_name(slot["app"]) for slot in a["slots"])
            case "open_url":
                return _host(a["url"])
            case "web_search":
                return f"Search “{_cut(a['query'])}”"
            case "tab":
                return _TAB_TEXT.get(a["op"], "Switch tab")
            case "nav":
                return _NAV_TEXT[a["op"]]
            case "scroll":
                return f"Scroll {a['dir']}"
            case "autoscroll":
                return {"start": f"Keep scrolling {a.get('dir') or 'down'}", "faster": "Faster",
                        "slower": "Slower", "stop": "Stop scrolling"}.get(a["op"], "Scroll")
            case "doc":
                return f"Page {a.get('page')}" if a["op"] == "goto_page" else a["op"].replace("_", " ").capitalize()
            case "type_text":
                return f"Type “{_cut(a['text'])}”" + (" ⏎" if a.get("submit") else "")
            case "press":
                return a["keys"].replace("+", " + ").title()
            case "volume":
                return f"Volume {a.get('level', a['op'])}"
            case "media":
                return a["op"].replace("_", " / ").capitalize()
            case "dictation":
                return {"paste": "Paste last dictation", "stop": "Stop dictation"}.get(a["op"], "Dictation")
            case "ui_click":
                verb = verb_for(a.get("button"), a.get("count"))
                return f"{_VERB_LABEL[verb]} “{_cut(a.get('label') or a.get('name', ''), 24)}”"
            case "numbers":
                return {"hide": "Hide numbers", "more": "More numbers"}.get(a.get("op", "show"), "Numbers")
            case "grid":
                return "Grid"
            case "pick":
                verb = _VERB_LABEL[a.get("verb") or "click"]
                what = f"“{_cut(a['label'], 24)}”" if a.get("label") else str(a.get("n") or "")
                return f"Drag {what} to {a['to']}" if a.get("to") is not None else f"{verb} {what}".strip()
            case "screenshot":
                return "Window screenshot" if a.get("target") == "window" else "Screenshot"
            case "camera":
                return "Photo"
            case "system":
                return {"lock": "Lock", "sleep": "Sleep"}.get(a["op"], "System")
            case "open_path":
                return f"Open {_cut(a.get('label') or a.get('name') or '')}".strip()
            case "nova":
                return {"help": "What can I say", "again": "Again", "undo": "Undo"}.get(a.get("op"), "Nova")
        return s.action.replace("_", " ").capitalize()

    def scene(self, s: Step, ctx: object) -> Scene:
        win = native.describe(native.foreground())
        catalog = self._catalog()
        app = catalog.app_for(win.exe, win.aumids) if win else None
        password, known = False, True
        if needs_focus(s):  # typing, a paste or a character key: never into a password field (SPEC §14)
            focus = self._focus.focused(0.4)
            if focus is not None and focus.control_type in _CONTAINERS:
                # Chrome / Electron build their accessibility tree on the first UI Automation question and answer
                # "pane" until then (a password field included): ask once more before trusting it.
                time.sleep(0.15)
                focus = self._focus.focused(0.4)
            known = focus is not None and focus.control_type not in _CONTAINERS
            password = bool(focus and focus.password)
        count = 0
        if s.action == "close_app":
            target = catalog.app(s.get("app", ""))
            count = len(apps.windows_of(catalog, target)) if target else 0
        url = ""
        if self._chrome is not None and win is not None and win.exe_name.lower() == "chrome.exe":
            tab = self._chrome.chrome.active_tab()
            url = tab.url if tab is not None else ""
        if s.action == "canvas_open":
            url = str(s.get("url") or url)
        return Scene(
            app=app.name if app else "", exe=win.exe_name if win else "", window_class=win.cls if win else "",
            title=win.title if win else "", url=url, password=password, locked=native.workstation_locked(),
            windows=count, focus_known=known,
        )

    def run(self, s: Step, ctx: object) -> Outcome:
        native.com_init()
        outcome = self._do(s, self._catalog())
        if s.action in _UNDOABLE:
            self._edited = outcome.win.hwnd if outcome.win is not None else native.foreground()
        return outcome

    def _do(self, s: Step, catalog: Catalog) -> Outcome:
        a = s.args
        match s.action:
            case "open_app" | "switch_app":
                return apps.open_app(catalog, self._app(catalog, a["app"]))
            case "close_app":
                return apps.close_app(catalog, self._app(catalog, a["app"]))
            case "window":
                return self._window(catalog, a["op"], a.get("target", "active"), a.get("monitor"))
            case "layout":
                return self._layout(catalog, a["slots"])
            case "open_url":
                tabbed = self._in_tab(a["url"], reuse=not a.get("new_tab"))
                return tabbed or apps.open_url(catalog, a["url"], _host(a["url"]))
            case "web_search":
                engine = a.get("engine") or "google"
                url = ENGINES[engine].format(q=quote_plus(a["query"]))
                outcome = self._in_tab(url, reuse=False) or apps.open_url(catalog, url, f"“{_cut(a['query'])}”")
                return Outcome(f"Searched {engine.capitalize()} for “{_cut(a['query'])}”", outcome.method, outcome.win)
            case "tab" if a["op"] == "goto":
                return self._chrome_run(s)
            case "tab":
                return self._browser_keys(catalog, _TAB_KEYS[a["op"]], _TAB_TEXT[a["op"]], a["op"] != "close")
            case "nav":
                return self._nav(catalog, a["op"])
            case "scroll":
                return self.scroller.scroll(self._front(), a["dir"], a.get("amount") or "page")
            case "autoscroll":
                return self._autoscroll(a["op"], a.get("dir") or "down", a.get("speed") or "keep")
            case "doc":
                return self.scroller.doc(self._front(), a["op"], a.get("page"))
            case "page_click" | "page_fill" | "page_read" | "scroll_to" | "canvas_due" | "canvas_open_item" \
                    | "canvas_item_text" | "canvas_open":
                return self._chrome_run(s)
            case "type_text":
                return Outcome(keyboard.type_text(a["text"], bool(a.get("submit")), self._front()))
            case "press":
                return Outcome(keyboard.press(a["keys"], self._front()))
            case "volume":
                return Outcome(system.volume(a["op"], step=a.get("step"), level=a.get("level")))
            case "media":
                return Outcome(system.media(a["op"]))
            case "dictation":
                return Outcome(self._nova.dictation(a["op"], a.get("text")))
            case "listening":
                return Outcome(self._nova.listening(a["op"]))
            case "ui_click":
                return self._click(s)
            case "numbers":
                op = a.get("op") or "show"
                if op == "hide":
                    return Outcome("Numbers hidden" if self.pointer.hide("hide numbers") else "No numbers are showing",
                                   "numbers")
                if op == "more":
                    return self.pointer.more()
                return self.pointer.show(self._front(), a.get("scope") or "here")
            case "grid":
                return self.pointer.grid(self._front(), a.get("scope") or "everywhere")
            case "pick":
                return self.pointer.pick(a.get("n"), a.get("verb"), a.get("to"))
            case "screenshot":
                front = native.describe(native.foreground())
                return capture.screenshot(a.get("target") or "screen",
                                          front if front is not None and front.cls not in _NOT_WINDOWS else None)
            case "camera":
                return capture.photo(catalog, self._focus, self.stop.is_set)
            case "system":
                return Outcome(system.lock() if a["op"] == "lock" else system.sleep(), "system")
            case "open_path":
                return Outcome(places.open_path(a["path"], a.get("label") or a.get("name") or "it"), "shell")
            case "nova":
                return self._nova_op(catalog, a.get("op"), a.get("topic"))
        raise ActionError(f"I can't do {s.action.replace('_', ' ')} yet.")

    def _click(self, s: Step) -> Outcome:
        if self._found is None or s.get("located") != self._located:
            raise ActionError("I lost track of that — say it again.")  # an older step: leave the newer one alone
        found, self._found = self._found, None
        return self.pointer.act(found.target, verb_for(s.get("button"), s.get("count")), hwnd=found.hwnd)

    # --- Chrome, through the extension when it's connected --------------------------------------------------------

    def _chrome_run(self, s: Step) -> Outcome:
        if self._chrome is None:
            raise ActionError("Page and Canvas steps need Nova's Chrome extension (nova chrome --install).")
        return self._chrome.run(s)

    def _in_tab(self, url: str, *, reuse: bool) -> Outcome | None:
        """Open ``url`` in a Chrome tab through the extension (a tab already on that site is reused). None = do it
        the old way (Chrome not running / extension not connected)."""
        if self._chrome is None or not self._chrome.chrome.connected:
            return None
        try:
            tab, reused = self._chrome.chrome.open(url, reuse=reuse)
        except ActionError as error:
            _log.info("extension couldn't open %s (%s); launching Chrome with it", url, error)
            return None
        host = _host(url)
        return Outcome(f"Switched to {host}" if reused else f"Opened {host}", "tab-reused" if reused else "tab")

    def _nova_op(self, catalog: Catalog, op: object, topic: object) -> Outcome:
        if op == "help":  # resolve() shows the card; this is a topic picked from it
            outcome = self._help(topic or "system")
            if isinstance(outcome, Outcome):
                return outcome
        if op == "undo":
            return self._undo(catalog)
        raise ActionError("Nothing to repeat yet.")  # "again" is the executor's (it remembers the last plan)

    def _undo(self, catalog: Catalog) -> Outcome:
        """Ctrl+Z in the window the last typing / click / key happened in (brought back first), else the front one."""
        win = native.describe(self._edited) if native.is_window(self._edited) else None
        if win is None:
            win = self._front()
        method = native.activate(win.hwnd) if native.root(native.foreground()) != win.hwnd else "front"
        keyboard.press("ctrl+z", win)
        app = catalog.app_for(win.exe, win.aumids)
        return Outcome(f"Undid the last change in {short_name(app.name) if app else 'that window'}", method, win)

    def _nav(self, catalog: Catalog, op: str) -> Outcome:
        """Back / forward / up in File Explorer when it's in front; back / forward / reload in the browser otherwise."""
        front = native.describe(native.foreground())
        if front is not None and front.cls == _EXPLORER and op != "reload":
            keyboard.press(_NAV_KEYS[op], front)
            return Outcome(_NAV_TEXT[op], "keys", front)
        if op == "up":
            raise ActionError("“Up a folder” works in File Explorer.")
        return self._browser_keys(catalog, _NAV_KEYS[op], _NAV_TEXT[op], op != "reload")

    def _autoscroll(self, op: str, direction: str, speed: str) -> Outcome:
        match op:
            case "start":
                return self.scroller.steady(self._front(), direction, speed)
            case "faster" | "slower":
                return self.scroller.change_speed(op == "faster")
            case "stop":
                return Outcome("Stopped" if self.scroller.stop() else "I wasn't scrolling", "steady")
        raise ActionError(f"I can't {op} scrolling.")

    # --- helpers ---------------------------------------------------------------------------------------------

    @staticmethod
    def _app(catalog: Catalog, name: str) -> App:
        app = catalog.app(name)
        if app is None:
            raise ActionError(f"{name} isn't installed (or the app list is still loading).")
        return app

    @staticmethod
    def _front() -> Win:
        win = native.describe(native.foreground())
        if win is None or win.cls in _NOT_WINDOWS:
            raise ActionError("No window is in front.")
        return win

    def _app_window(self, catalog: Catalog, name: str, wait: float) -> tuple[Win, str]:
        app = self._app(catalog, name)
        wins = apps.windows_of(catalog, app)
        if wins:
            return wins[0], short_name(app.name)
        apps.launch(app)
        win = native.wait_for(lambda: self.stop.is_set() or next(iter(apps.windows_of(catalog, app)), None), wait,
                              interval=0.05)
        if not isinstance(win, Win):
            raise ActionError(f"{short_name(app.name)} didn't open in time.")
        return win, short_name(app.name)

    def _window(self, catalog: Catalog, op: str, target: str, monitor: str | None) -> Outcome:
        if target == "active":
            win = self._front()
            app = catalog.app_for(win.exe, win.aumids)
            name = short_name(app.name) if app else "the window"
        else:
            win, name = self._app_window(catalog, target, LAYOUT_WAIT_S)
        if op == "close":
            return apps.close_windows([win], name)
        detail = windows.window_op(win, op, name, monitor)
        method = ""
        if op not in ("minimize", "fullscreen"):
            try:
                method = native.activate(win.hwnd)
            except ActionError:
                method = "refused"
        return Outcome(detail, method, win)

    def _layout(self, catalog: Catalog, slots) -> Outcome:
        placed = []
        for slot in slots:
            if slot["app"] == "active":
                win = self._front()
                app = catalog.app_for(win.exe, win.aumids)
                placed.append((win, slot["area"], short_name(app.name) if app else "this window"))
            else:
                win, name = self._app_window(catalog, slot["app"], LAYOUT_WAIT_S)
                placed.append((win, slot["area"], name))
        handle = native.cursor_monitor() or native.monitor_of(placed[0][0].hwnd)  # where he's looking
        monitor = next((m for m in native.monitors() if m.handle == handle), None)
        if monitor is None:
            raise ActionError("Couldn't find the monitor to arrange on.")
        return Outcome(windows.arrange(placed, monitor))

    def _browser_keys(self, catalog: Catalog, keys: str, text: str, check_title: bool) -> Outcome:
        win, method = apps.browser_window(catalog)
        before = win.title
        keyboard.press(keys, win)
        if check_title and keys in ("alt+left", "alt+right"):
            if not native.wait_for(lambda: native.window_title(win.hwnd) != before, 1.0, interval=0.02):
                where = "back" if keys == "alt+left" else "forward"
                return Outcome(f"Nothing to go {where} to in this tab", method, win)
        return Outcome(text, method, win)


def _host(url: str) -> str:
    host = urlsplit(url).hostname or url
    return host.removeprefix("www.")


def _cut(text: str, limit: int = 28) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
