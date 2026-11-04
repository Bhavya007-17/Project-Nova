"""Chrome hands (SPEC §8-§10): steps that act inside Chrome through Nova's extension, and the API they use.

``Chrome`` is the API (tabs, the page map, clicks, fills, scrolling, Canvas's own JSON from the logged-in tab).
``ChromeHands`` gives the executor what it needs for Chrome steps:
- ``resolve`` turns words into targets before the risk check: "the blue button" -> an element of the page map
  (ranked in ``nova.chrome.pagemap``), "my assignment for today" -> the item's address (``nova.chrome.canvas``);
  several candidates -> a ``Pick`` for the island; "nothing due today" is an answer of its own.
- ``run`` does it and says what happened; ``preview`` outlines an element while the island asks about it.
- ``page_rows`` is the router's page source: the top elements for Tier 1 when he points at the page.
Runs on the brain thread; every call has a deadline (``nova.chrome.bridge``).
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from ..brain.executor import Pick
from ..brain.plan import Step, step
from ..chrome import canvas, pagemap
from ..chrome.bridge import Bridge, ChromeError, TabInfo
from ..chrome.pagemap import PageView, Scored
from ..chrome.tabs import best_tab, same_site, window_title_matches
from . import native
from .apps import Outcome
from .catalog import Catalog
from .native import ActionError

_log = logging.getLogger(__name__)

CANVAS_DEFAULT = "https://canvas.vt.edu"
LLM_ROWS = 40  # page elements Tier 1 sees (SPEC §9)
MAP_S = 2.0
CLICK_S = 4.0  # 300 ms highlight + the click + watching what it did
CANVAS_S = 8.0
CANVAS_PAGES = 5  # planner pages of 100 items
CANVAS_CACHE_S = 60.0
READ_HOLD_S = 6.0  # "Nothing due today — next: …" stays up this long
CHROME_EXE = "chrome.exe"
DIRECT_AT = 85.0  # a click his words name this plainly needs no LLM ("hit that sign in thing")
_CLICK_WORDS = re.compile(r"\b(click|tap|press|hit|push|smash|select|choose)\b")
STEPS = frozenset({"page_click", "page_fill", "page_read", "scroll_to", "canvas_due", "canvas_open_item",
                   "canvas_item_text", "canvas_open"})


def _cut(text: str, limit: int = 32) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


@dataclass(frozen=True)
class PageRows:
    """What Tier 1 is shown of the page: ranked elements, numbered from 1 — and, when his words name one element
    plainly ("hit that sign in thing"), the click itself, so no LLM is needed."""

    scored: tuple[Scored, ...]
    ms: float
    direct: Step | None = None

    def rows(self) -> list[str]:
        return pagemap.llm_rows(self.scored, LLM_ROWS)


class Chrome:
    def __init__(self, bridge: Bridge, catalog: Callable[[], Catalog], *, clock: Callable[[], float] = time.monotonic) -> None:
        self._bridge = bridge
        self._catalog = catalog
        self._clock = clock
        self._planner: dict[tuple[str, str], tuple[float, tuple[canvas.Item, ...]]] = {}
        self.last_map: PageView | None = None
        self.last_text = ""  # the last page / assignment text read (Stage 09 attaches it to handoffs)

    # --- state ----------------------------------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._bridge.connected

    def catalog(self) -> Catalog:
        return self._catalog()

    def active_tab(self) -> TabInfo | None:
        return self._bridge.active_tab()

    def _request(self, cmd: str, args: Mapping[str, Any] | None = None, timeout: float = 3.0) -> Any:
        try:
            return self._bridge.request(cmd, args, timeout)
        except ChromeError as error:
            raise ActionError(str(error)) from None

    # --- windows and tabs ---------------------------------------------------------------------------------------

    @staticmethod
    def chrome_in_front() -> bool:
        win = native.describe(native.foreground())
        return win is not None and win.exe_name.lower() == CHROME_EXE

    def front(self, tab: TabInfo | None = None) -> str:
        """Bring the Chrome window that shows ``tab`` (default: the active one) to the front. Returns how."""
        tab = tab or self.active_tab()
        fg = native.describe(native.foreground())
        if fg is not None and fg.exe_name.lower() == CHROME_EXE and (tab is None or window_title_matches(fg.title, tab.title)):
            return "already"
        wins = [w for w in native.app_windows() if w.exe_name.lower() == CHROME_EXE]
        target = next((w for w in wins if tab is not None and window_title_matches(w.title, tab.title)), None)
        target = target or (wins[0] if wins else None)
        if target is None:
            return "none"
        try:
            return native.activate(target.hwnd)
        except ActionError:
            return "refused"

    def tabs(self) -> list[TabInfo]:
        data = self._request("tabs.list") or {}
        return [t for t in (TabInfo.of(raw) for raw in data.get("tabs") or ()) if t is not None]

    def activate(self, tab: TabInfo) -> TabInfo:
        data = self._request("tabs.activate", {"tabId": tab.id}) or {}
        shown = TabInfo.of(data.get("tab")) or tab
        self.front(shown)
        return shown

    def open(self, url: str, *, reuse: bool = True, active: bool = True) -> tuple[TabInfo, bool]:
        """A tab on ``url``: one already on that site (made active), else a new one. (tab, reused)."""
        if reuse:
            found = [t for t in self.tabs() if same_site(t.url, url)]
            if found:
                current = self.active_tab()
                tab = next((t for t in found if current is not None and t.id == current.id), None)
                tab = tab or max(found, key=lambda t: t.last)
                return (self.activate(tab) if active else tab), True
        data = self._request("tabs.open", {"url": url, "active": active}) or {}
        tab = TabInfo.of(data.get("tab"))
        if tab is None:
            raise ActionError("Chrome didn't open the tab.")
        if active:
            self.front(None)
        return tab, False

    def navigate(self, tab: TabInfo, url: str) -> TabInfo:
        data = self._request("tabs.navigate", {"tabId": tab.id, "url": url}) or {}
        shown = TabInfo.of(data.get("tab")) or tab
        self.front(None)
        return shown

    def wait(self, tab: TabInfo, *, timeout: float = 8.0, url_prefix: str = "") -> TabInfo:
        data = self._request("tabs.wait", {"tabId": tab.id, "status": "complete", "urlPrefix": url_prefix,
                                           "timeout": int(timeout * 1000)}, timeout + 1.0) or {}
        return TabInfo.of(data.get("tab")) or tab

    # --- pages --------------------------------------------------------------------------------------------------

    def page_map(self, *, words: tuple[str, ...] = (), sections: bool = False, tab: TabInfo | None = None) -> PageView:
        began = time.perf_counter()
        args: dict[str, Any] = {"words": list(words), "sections": sections}
        if tab is not None:
            args["tabId"] = tab.id
        data = self._request("page.map", args, MAP_S) or {}
        view = PageView.of(data, (time.perf_counter() - began) * 1000)
        self.last_map = view
        return view

    def click(self, element: str, label: str = "") -> dict[str, Any]:
        return self._request("page.click", {"id": element, "label": label}, CLICK_S) or {}

    def call(self, fn: str, *args: Any, element: str | None = None, timeout: float = 3.0) -> dict[str, Any]:
        request: dict[str, Any] = {"fn": fn, "args": list(args)}
        if element is not None:
            request["id"] = element
        return self._request("page.call", request, timeout) or {}

    def scroll(self, direction: str, amount: str | None) -> dict[str, Any]:
        return self.call("scroll", direction, amount)

    # --- Canvas -----------------------------------------------------------------------------------------------

    def canvas_origin(self) -> str:
        target = dict(self.catalog().aliases).get("canvas")
        url = target.url if target is not None and target.url else CANVAS_DEFAULT
        parts = urlsplit(url)
        return f"{parts.scheme}://{parts.netloc}"

    def canvas_tab(self, *, open_if_missing: bool, active: bool) -> TabInfo:
        origin = self.canvas_origin()
        host = _host(origin)
        mine = [t for t in self.tabs() if _host(t.url) == host]
        current = self.active_tab()
        tab = next((t for t in mine if current is not None and t.id == current.id), None)
        tab = tab or (max(mine, key=lambda t: t.last) if mine else None)
        if tab is None:
            if not open_if_missing:
                raise ActionError("Canvas isn't open in Chrome.")
            tab, _ = self.open(origin + "/", reuse=False, active=active)
        if tab.status != "complete":
            tab = self.wait(tab, timeout=CANVAS_S)
        return tab

    def _canvas_json(self, tab: TabInfo, path: str) -> tuple[Any, str]:
        """Canvas's JSON for ``path`` from inside his tab, and the next page's path ("" if none)."""
        origin = self.canvas_origin()
        data = self._request("canvas.fetch", {"tabId": tab.id, "path": path}, CANVAS_S) or {}
        status = int(data.get("status") or 0)
        kind = str(data.get("type") or "")
        final = str(data.get("url") or "")
        if status in (401, 403) or (status == 200 and ("json" not in kind or (final and _host(final) != _host(origin)))):
            raise _LoggedOut()
        if status != 200:
            raise _CanvasDown(f"HTTP {status or data.get('error') or 'error'}")
        try:
            parsed = json.loads(canvas.strip_guard(str(data.get("text") or "")))
        except ValueError as error:
            raise _CanvasDown("not JSON") from error
        return parsed, canvas.next_page(str(data.get("link") or ""), origin) or ""

    def canvas_items(self, name: str | None, now: datetime) -> tuple[canvas.Item, ...]:
        """Coursework around ``name``'s range (planner JSON; the page's own To Do list if that fails)."""
        key = (name or "", now.date().isoformat())
        cached = self._planner.get(key)
        if cached is not None and self._clock() - cached[0] < CANVAS_CACHE_S:
            return cached[1]
        tab = self.canvas_tab(open_if_missing=True, active=False)
        start, end = canvas.fetch_window(name, now)
        path, rows = canvas.planner_path(start, end), []
        try:
            for _ in range(CANVAS_PAGES):
                data, path = self._canvas_json(tab, path)
                rows.extend(data if isinstance(data, list) else [])
                if not path:
                    break
            items = canvas.parse_planner(rows, self.canvas_origin())
        except _LoggedOut:
            self.activate(tab)
            raise ActionError("Canvas wants you to log in — log in, then ask again.") from None
        except _CanvasDown as error:
            _log.warning("canvas planner failed (%s); reading the page instead", error)
            dom = self._request("canvas.dom", {"tabId": tab.id}, CANVAS_S) or {}
            items = canvas.dom_items(dom.get("items"), now)
            if not items:
                raise ActionError(f"Couldn't read Canvas's planner ({error}).") from None
        self._planner[key] = (self._clock(), items)
        return items

    def canvas_text(self, item: canvas.Item) -> str:
        path = canvas.item_api(item)
        if path is None:
            raise ActionError(f"Canvas doesn't share the text of “{item.title}”.")
        tab = self.canvas_tab(open_if_missing=True, active=False)
        try:
            data, _ = self._canvas_json(tab, path)
        except _LoggedOut:
            raise ActionError("Canvas wants you to log in — log in, then ask again.") from None
        except _CanvasDown as error:
            raise ActionError(f"Couldn't read “{item.title}” ({error}).") from None
        return canvas.description(data)


class _LoggedOut(Exception):
    pass


class _CanvasDown(Exception):
    pass


# --- the hands for Chrome steps ---------------------------------------------------------------------------------------


_KIND = {"page_click": "click", "page_fill": "fill", "scroll_to": "section"}
_ASK = {"page_click": "Click which one?", "page_fill": "Type into which one?", "scroll_to": "Scroll to which one?"}
_VERB = {"page_click": "Click", "page_fill": "Type into", "scroll_to": "Scroll to"}


class ChromeHands:
    def __init__(self, chrome: Chrome, *, now: Callable[[], datetime] = lambda: datetime.now().astimezone()) -> None:
        self.chrome = chrome
        self._now = now

    @staticmethod
    def handles(s: Step) -> bool:
        return s.action in STEPS or (s.action == "tab" and s.get("op") == "goto")

    def _need(self) -> None:
        if not self.chrome.connected:
            raise ActionError("Nova's Chrome extension isn't connected — is Chrome open? (nova doctor checks the setup.)")

    # --- what the router shows Tier 1 ----------------------------------------------------------------------------

    def page_rows(self, text: str, ctx: Any) -> PageRows | None:
        """The page, ranked for what he said — only when he points at it and Chrome is in front (SPEC §7.1)."""
        if not self.chrome.connected or not pagemap.points_at_page(text):
            return None
        fg = getattr(ctx, "foreground", None)
        if fg is not None and fg.win.exe_name.lower() != CHROME_EXE and "chrome" not in text.lower():
            return None
        sections = pagemap.wants_sections(text)
        try:
            view = self.chrome.page_map(words=tuple(pagemap.words(text)), sections=sections)
        except ActionError as error:
            _log.info("no page map for Tier 1: %s", error)
            return None
        now = self._now().replace(tzinfo=None)
        top = pagemap.top_for_llm(view.elements, text, now=now, limit=LLM_ROWS, viewport=view.viewport)
        direct = None
        if not sections and _CLICK_WORDS.search(text.lower()):
            decision = pagemap.decide(top)
            if decision.kind == "one" and decision.best is not None and top and top[0].score >= DIRECT_AT:
                direct = _target(step("page_click", description=" ".join(text.split())), decision.best)
        return PageRows(tuple(top), view.ms, direct) if top else None

    # --- resolve: words -> a target ---------------------------------------------------------------------------------

    def resolve(self, s: Step, ctx: Any) -> Step | Pick | Outcome:
        action = s.action
        if action in _KIND:
            return self._resolve_element(s)
        if action == "canvas_due":
            return self._canvas_due(str(s.get("range") or "upcoming"), str(s.get("item") or ""))
        if action in ("canvas_open_item", "canvas_item_text") and not s.get("url"):
            return self._canvas_item(s)
        if action == "canvas_item_text":
            return self._canvas_read(s)
        if action == "tab" and s.get("op") == "goto" and s.get("tab") is None:
            return self._tab(s)
        return s

    def _resolve_element(self, s: Step) -> Step | Pick:
        self._need()
        element = s.get("element")
        last = self.chrome.last_map
        if element and last is not None and last.find(element) is not None:
            return s  # Tier 1 pointed at an element of the map it was shown
        description = str(s.get("description") or "")
        self.chrome.front()
        view = self.chrome.page_map(words=tuple(pagemap.words(description)), sections=s.action == "scroll_to")
        scored = pagemap.rank(view.elements, description, now=self._now().replace(tzinfo=None), kind=_KIND[s.action],
                              viewport=view.viewport)
        decision = pagemap.decide(scored)
        if decision.kind == "none":
            if not view.elements:
                raise ActionError("I can't see anything to click on this page.")
            if s.action == "page_fill":  # the only box like that is a password / card field: say why, plainly
                secrets = [e for e in view.elements if e.secret]
                if pagemap.decide(pagemap.rank(secrets, description, kind="click")).kind != "none":
                    raise ActionError("That's a password field — I never type there.")
            raise ActionError(f"Couldn't find “{_cut(description)}” on this page.")
        if decision.kind == "one" and decision.best is not None:
            return _target(s, decision.best)
        options = tuple(_target(s, e) for e in decision.options)
        if len(options) == 1:
            return Pick(f"{_VERB[s.action]} “{_cut(decision.options[0].name)}”?", (pagemap.label(decision.options[0]),),
                        options)
        return Pick(_ASK[s.action], tuple(pagemap.label(e) for e in decision.options), options)

    def _canvas_due(self, name: str, asked: str = "") -> Pick | Outcome:
        self._need()
        now = self._now()
        items = self.chrome.canvas_items(name, now)
        found = canvas.due(items, name, now)
        named = canvas.parse_ref(asked, now).words if asked else ()
        what = f" for {' '.join(named)}" if named else ""
        if named:  # "anything for physics due this week": only those
            found = canvas.only(found, named)
            if not found:
                return Outcome(f"Nothing{what} due {canvas.range_words(name)}", "canvas", hold=READ_HOLD_S)
        if not found:
            return Outcome(canvas.nothing_due(name, canvas.next_due(items, now), now), "canvas", hold=READ_HOLD_S)
        words = canvas.range_words(name)
        said = f"{len(found)} overdue{what}" if name == "overdue" else f"{len(found)} due {words}{what}"
        shown = found[:4]
        question = said + (" — the first 4" if len(found) > 4 else "") + " · say a number to open one"
        return Pick(question, tuple(canvas.label(i, now) for i in shown), tuple(_open(i) for i in shown),
                    optional=True, detail=said, hold=READ_HOLD_S)

    def _canvas_item(self, s: Step) -> Step | Pick | Outcome:
        self._need()
        now = self._now()
        said = str(s.get("item") or "")
        ref = canvas.parse_ref(said, now)
        name = ref.range or (_weekday(ref.day) if ref.day is not None else None)
        items = self.chrome.canvas_items(name, now)
        found = canvas.select(items, ref, now)
        reading = s.action == "canvas_item_text"
        if not found:
            if ref.words:
                raise ActionError(f"Couldn't find “{_cut(said)}” on Canvas.")
            return Outcome(canvas.nothing_due(name or "upcoming", canvas.next_due(items, now), now), "canvas",
                           hold=READ_HOLD_S)
        make = _read if reading else _open
        if len(found) == 1:
            return make(found[0])
        shown = found[:4]
        when = f" due {canvas.range_words(name)}" if name else ""
        return Pick(f"{len(found)}{when} — which one?", tuple(canvas.label(i, now) for i in shown),
                    tuple(make(i) for i in shown))

    def _canvas_read(self, s: Step) -> Outcome:
        item = canvas.Item(str(s.get("title") or ""), "", self._now(), str(s.get("url")), str(s.get("kind") or ""),
                           item_id=str(s.get("item_id") or ""), course_id=str(s.get("course_id") or ""))
        text = self.chrome.canvas_text(item)
        self.chrome.last_text = text
        first = text.split("\n", 1)[0] if text else "(no instructions)"
        return Outcome(f"{item.title}: {_cut(first, 70)}", "canvas", hold=8.0)

    def _tab(self, s: Step) -> Step | Pick:
        self._need()
        said = str(s.get("match") or "")
        found = best_tab(self.chrome.tabs(), said)
        if not found:
            target = self.chrome.catalog().resolve(said).best  # "the canvas tab" with no Canvas tab: open Canvas
            if target is not None and target.target.kind == "site":
                return step("open_url", url=target.target.url)
            raise ActionError(f"No tab called “{_cut(said)}”.")
        if len(found) == 1 or _same_host(found):
            return step("tab", op="goto", match=said, tab=found[0].id, title=found[0].title)
        shown = found[:4]
        return Pick("Which tab?", tuple(_cut(t.title, 30) for t in shown),
                    tuple(step("tab", op="goto", match=said, tab=t.id, title=t.title) for t in shown))

    # --- run ------------------------------------------------------------------------------------------------------

    def run(self, s: Step) -> Outcome:
        self._need()
        a = s.args
        match s.action:
            case "page_click":
                return self._click(s)
            case "page_fill":
                result = self.chrome.call("fill", "$n", a["text"], {"submit": bool(a.get("submit"))}, element=a["element"])
                if not result.get("ok"):
                    raise ActionError(_page_problem(result, a.get("label") or a["description"]))
                where = a.get("label") or a["description"]
                return Outcome(f"Typed “{_cut(a['text'], 24)}” in {_cut(where, 20)}" + (" ⏎" if a.get("submit") else ""),
                               "page")
            case "scroll_to":
                result = self.chrome.call("scrollTo", "$n", element=a["element"])
                if not result.get("ok"):
                    raise ActionError(_page_problem(result, a.get("label") or a["description"]))
                return Outcome(f"Scrolled to {_cut(a.get('label') or a['description'])}", "page")
            case "page_read":
                result = self.chrome.call("read", a.get("what", "page"), timeout=4.0)
                text = str(result.get("text") or "")
                self.chrome.last_text = text
                return Outcome(f"Read {len(text.split())} words" if text else "Nothing to read here", "page")
            case "canvas_open":
                tab = self.chrome.canvas_tab(open_if_missing=True, active=True)
                self.chrome.navigate(tab, a["url"])
                return Outcome(f"Opened {_cut(a['title'], 40)}", "canvas")
            case "tab":
                tab = TabInfo(int(a["tab"]), 0, "", str(a.get("title") or ""))
                shown = self.chrome.activate(tab)
                return Outcome(f"Switched to {_cut(shown.title or a.get('match', 'the tab'))}", "tab")
        raise ActionError(f"I can't do {s.action.replace('_', ' ')} yet.")

    def _click(self, s: Step) -> Outcome:
        a = s.args
        label = str(a.get("label") or a.get("description") or "")
        result = self.chrome.click(a["element"], label)
        if not result.get("ok") and result.get("error") == "stale":
            again = self._resolve_element(step("page_click", description=str(a.get("description") or label)))
            if not isinstance(again, Step) or str(again.get("label")) != label:
                raise ActionError("The page changed — say it again?")
            result = self.chrome.click(str(again.get("element")), label)
        if not result.get("ok"):
            raise ActionError(_page_problem(result, label))
        return Outcome(f"Clicked “{_cut(label)}”", str(result.get("effect") or ""))

    # --- for the executor's other questions --------------------------------------------------------------------------

    def preview(self, s: Step, on: bool) -> None:
        if s.action == "page_click" and s.get("element"):
            if on:
                self.chrome.call("highlight", "$n", {"on": True, "ms": 16000}, element=s.get("element"), timeout=1.5)
            else:
                self.chrome.call("highlight", None, {"on": False}, element=s.get("element"), timeout=1.5)

    def label(self, s: Step) -> str | None:
        a = s.args
        match s.action:
            case "page_click":
                return f"Click “{_cut(a.get('label') or a.get('description', ''), 24)}”"
            case "page_fill":
                return f"Type “{_cut(a.get('text', ''), 18)}”"
            case "scroll_to":
                return f"Scroll to {_cut(a.get('label') or a.get('description', ''), 24)}"
            case "page_read":
                return "Read the page"
            case "canvas_due":
                return f"Canvas · due {canvas.range_words(str(a.get('range', 'upcoming')))}"
            case "canvas_open_item" | "canvas_item_text":
                return f"Canvas · {_cut(a.get('item', ''), 24)}"
            case "canvas_open":
                return f"Canvas → {_cut(a.get('title', ''), 24)}"
            case "tab" if a.get("op") == "goto":
                return f"Tab “{_cut(a.get('match', ''), 20)}”"
        return None


def _target(s: Step, e: pagemap.Element) -> Step:
    """The step, pointed at one element (its name is what the risk check reads)."""
    args = {k: v for k, v in s.args.items() if k not in ("element", "label", "submits", "secret", "search")}
    extra: dict[str, Any] = {"element": e.id, "label": e.name}
    for flag in ("submits", "secret", "search"):
        if getattr(e, flag):
            extra[flag] = True
    return Step(s.action, {**args, **extra})


def _open(item: canvas.Item) -> Step:
    return step("canvas_open", url=item.url, title=item.title)


def _read(item: canvas.Item) -> Step:
    return step("canvas_item_text", item=item.title, url=item.url, title=item.title, kind=item.kind,
                item_id=item.item_id, course_id=item.course_id)


def _weekday(day: Any) -> str:
    return ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")[day.weekday()]


def _same_host(tabs: list[TabInfo]) -> bool:
    return len({(_host(t.url), t.title) for t in tabs}) == 1


def _page_problem(result: Mapping[str, Any], label: str) -> str:
    error = str(result.get("error") or "")
    if error == "disabled":
        return f"“{_cut(label)}” is greyed out right now."
    if error == "secret":
        return "That's a password field — I never type there."
    if error == "stale":
        return "The page changed — say it again?"
    if error == "not a field":
        return f"“{_cut(label)}” isn't something I can type into."
    return f"Couldn't do it on the page ({error or 'no answer'})."
