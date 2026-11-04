"""The Chrome API over the bridge: tab reuse, the Canvas tab, Canvas JSON (pages, login, DOM fallback). A fake bridge
answers like the extension; window activation is stubbed (no real windows)."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from catalog_fixture import CATALOG

from nova.actions.chrome import Chrome
from nova.actions.native import ActionError
from nova.chrome.bridge import ChromeError

EST = timezone(timedelta(hours=-5))
NOW = datetime(2026, 11, 4, 15, 0, tzinfo=EST)
TABS = [
    {"id": 1, "windowId": 1, "url": "https://www.youtube.com/watch?v=1", "title": "lofi - YouTube", "lastAccessed": 10,
     "status": "complete"},
    {"id": 2, "windowId": 1, "url": "https://canvas.vt.edu/courses/11", "title": "ME 3124", "lastAccessed": 5,
     "status": "complete"},
]


def row(pid, title, due):
    return {"plannable_type": "assignment", "plannable_id": pid, "course_id": 11, "context_name": "ME 3124",
            "html_url": f"/courses/11/assignments/{pid}", "plannable": {"title": title, "due_at": due},
            "submissions": {"submitted": False}}


class FakeBridge:
    def __init__(self, fetches=None, active=1):
        self.connected = True
        self.sent = []
        self.fetches = list(fetches or [])
        self.active = active

    def active_tab(self):
        from nova.chrome.bridge import TabInfo

        return TabInfo.of(next(t for t in TABS if t["id"] == self.active))

    def request(self, cmd, args=None, timeout=3.0):
        self.sent.append((cmd, dict(args or {})))
        if cmd == "tabs.list":
            return {"tabs": TABS}
        if cmd == "tabs.activate":
            return {"tab": next(t for t in TABS if t["id"] == args["tabId"])}
        if cmd == "tabs.open":
            return {"tab": {"id": 9, "windowId": 1, "url": args["url"], "title": "", "status": "loading"}}
        if cmd == "tabs.wait":
            return {"tab": {"id": args["tabId"], "windowId": 1, "url": "https://canvas.vt.edu/", "title": "Dashboard",
                            "status": "complete"}}
        if cmd == "canvas.fetch":
            answer = self.fetches.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        if cmd == "canvas.dom":
            return {"items": [{"title": "HW 5", "url": "https://canvas.vt.edu/courses/11/assignments/1",
                               "row": "HW 5 Due Nov 4 at 11:59pm"}]}
        raise AssertionError(cmd)


@pytest.fixture
def chrome(monkeypatch):
    monkeypatch.setattr(Chrome, "front", lambda self, tab=None: "stub")

    def make(**kw):
        return Chrome(FakeBridge(**kw), lambda: CATALOG)

    return make


def ok(rows, link=""):
    return {"status": 200, "type": "application/json; charset=utf-8", "url": "https://canvas.vt.edu/api/v1/planner/items",
            "link": link, "text": "while(1);" + json.dumps(rows)}


def test_opening_a_site_again_switches_to_its_tab(chrome):
    c = chrome()
    tab, reused = c.open("https://canvas.vt.edu")
    assert reused and tab.id == 2 and ("tabs.activate", {"tabId": 2}) in c._bridge.sent
    tab, reused = c.open("https://github.com")
    assert not reused and tab.id == 9
    tab, reused = c.open("https://www.youtube.com/results?search_query=x", reuse=False)
    assert not reused


def test_the_canvas_origin_comes_from_the_alias(chrome):
    assert chrome().canvas_origin() == "https://canvas.vt.edu"


def test_planner_pages_are_followed_and_cached(chrome):
    page2 = "<https://canvas.vt.edu/api/v1/planner/items?page=2>; rel=\"next\""
    c = chrome(fetches=[ok([row(1, "HW 5", "2026-11-05T04:59:00Z")], page2), ok([row(2, "HW 6", "2026-11-13T04:59:00Z")])])
    items = c.canvas_items("week", NOW)
    assert [i.title for i in items] == ["HW 5", "HW 6"]
    fetches = [args["path"] for cmd, args in c._bridge.sent if cmd == "canvas.fetch"]
    assert fetches[0].startswith("/api/v1/planner/items?start_date=") and fetches[1] == "/api/v1/planner/items?page=2"
    assert c.canvas_items("week", NOW) == items  # cached: no third fetch
    assert len([1 for cmd, _ in c._bridge.sent if cmd == "canvas.fetch"]) == 2


def test_logged_out_canvas_says_so_and_shows_the_tab(chrome):
    login = {"status": 200, "type": "text/html", "url": "https://login.vt.edu/profile/cas/login", "link": "",
             "text": "<html>"}
    c = chrome(fetches=[login])
    with pytest.raises(ActionError, match="log in"):
        c.canvas_items("today", NOW)
    assert ("tabs.activate", {"tabId": 2}) in c._bridge.sent
    with pytest.raises(ActionError, match="log in"):
        chrome(fetches=[{"status": 401, "type": "application/json", "text": "{}"}]).canvas_items("today", NOW)


def test_a_broken_planner_falls_back_to_the_page(chrome):
    c = chrome(fetches=[{"status": 500, "type": "text/html", "text": "oops"}])
    items = c.canvas_items("today", NOW)
    assert [i.title for i in items] == ["HW 5"] and ("canvas.dom", {"tabId": 2}) in c._bridge.sent


def test_no_canvas_tab_opens_one_in_the_background(chrome):
    c = chrome(fetches=[ok([])])
    c._bridge.active = 1
    TABS_BACKUP = list(TABS)
    TABS[:] = [t for t in TABS if "canvas" not in t["url"]]
    try:
        assert c.canvas_items("today", NOW) == ()
        opened = [args for cmd, args in c._bridge.sent if cmd == "tabs.open"]
        assert opened == [{"url": "https://canvas.vt.edu/", "active": False}]
    finally:
        TABS[:] = TABS_BACKUP


def test_bridge_errors_become_action_errors(chrome):
    c = chrome(fetches=[ChromeError("Chrome didn't answer in time.")])
    with pytest.raises(ActionError, match="in time"):
        c.canvas_items("today", NOW)


def test_assignment_text(chrome):
    from nova.chrome.canvas import Item

    c = chrome(fetches=[{"status": 200, "type": "application/json", "url": "https://canvas.vt.edu/api/v1/x",
                         "text": json.dumps({"description": "<p>Read chapter 5</p>"})}])
    item = Item("HW 5", "ME 3124", NOW, "https://canvas.vt.edu/courses/11/assignments/1", "assignment", item_id="1",
                course_id="11")
    assert c.canvas_text(item) == "Read chapter 5"
    with pytest.raises(ActionError, match="doesn't share"):
        c.canvas_text(Item("Note", "", NOW, "https://x", "page"))
