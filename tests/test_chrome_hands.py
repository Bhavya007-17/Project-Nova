"""Chrome hands (SPEC §8-§10): words -> a page element / Canvas item / tab, questions when unsure, plain answers.
A fake Chrome API stands in for the extension (no browser, no windows)."""

from datetime import datetime, timedelta, timezone

import pytest
from catalog_fixture import CATALOG

from nova.actions.apps import Outcome
from nova.actions.chrome import ChromeHands, PageRows
from nova.actions.native import ActionError
from nova.brain.executor import Executor, Pick
from nova.brain.plan import Plan, Step, step
from nova.brain.risk import Scene
from nova.chrome import canvas
from nova.chrome.bridge import TabInfo
from nova.chrome.pagemap import Element, PageView

EST = timezone(timedelta(hours=-5))
NOW = datetime(2026, 11, 4, 15, 0, tzinfo=EST)


def items():
    def item(title, days, hour, course="ME 3124 Thermodynamics", done=False, n=1):
        due = (NOW + timedelta(days=days)).replace(hour=hour, minute=0)
        return canvas.Item(title, course, due, f"https://canvas.vt.edu/courses/11/assignments/{n}", "assignment",
                           done=done, item_id=str(n), course_id="11")

    return (item("HW 5", 0, 23, n=1), item("Pre-lab", 0, 18, "PHYS 2306", n=2), item("Lab 3", 2, 17, "PHYS 2306", n=3),
            item("HW 6", 8, 23, n=4))


class FakeChrome:
    def __init__(self, elements=(), planner=None, tabs=()):
        self.connected = True
        self.view = PageView("https://example.com/", "Example", tuple(elements))
        self.planner = planner if planner is not None else items()
        self._tabs = list(tabs)
        self.last_map = None
        self.last_text = ""
        self.clicks = []
        self.calls = []
        self.fronted = 0
        self.stale_once = False

    def catalog(self):
        return CATALOG

    def active_tab(self):
        return TabInfo(1, 1, "https://example.com/", "Example")

    def front(self, tab=None):
        self.fronted += 1
        return "already"

    def page_map(self, *, words=(), sections=False, tab=None):
        self.last_map = self.view
        return self.view

    def tabs(self):
        return list(self._tabs)

    def canvas_items(self, name, now):
        return self.planner

    def canvas_text(self, item):
        return "Read chapter 5.\nDo problems 1-4."

    def click(self, element, label=""):
        self.clicks.append((element, label))
        if self.stale_once:
            self.stale_once = False
            return {"ok": False, "error": "stale"}
        return {"ok": True, "effect": "navigated"}

    def call(self, fn, *args, element=None, timeout=3.0):
        self.calls.append((fn, args, element))
        if fn == "fill":
            return {"ok": True, "value": args[1]}
        if fn == "read":
            return {"ok": True, "text": "one two three"}
        return {"ok": True}

    def navigate(self, tab, url):
        self.calls.append(("navigate", (url,), None))
        return tab

    def canvas_tab(self, *, open_if_missing, active):
        return TabInfo(9, 1, "https://canvas.vt.edu/", "Dashboard")

    def activate(self, tab):
        return TabInfo(tab.id, 1, "https://www.youtube.com/", "lofi - YouTube")


def hands(**kw):
    return ChromeHands(FakeChrome(**kw), now=lambda: NOW)


PAGE = (Element("0:1", "button", "Sign in", section="header"), Element("0:2", "button", "Sign in", section="Log in"),
        Element("0:3", "link", "Thermodynamics HW 5"), Element("0:4", "link", "Thermodynamics HW 6"),
        Element("0:5", "button", "Submit Assignment", submits=True),
        Element("0:6", "textbox", "Password", editable=True, secret=True),
        Element("0:7", "searchbox", "Search", editable=True, search=True))


def test_resolving_never_brings_chrome_forward():
    # Resolve runs before the risk check and any Confirm: nothing may take focus yet (SPEC §2, §14).
    h = hands(elements=PAGE)
    assert h.resolve(step("page_click", description="submit assignment"), None).get("label") == "Submit Assignment"
    h.resolve(step("page_click", description="sign in button"), None)  # a question with two candidates
    h.resolve(step("page_fill", description="search", text="thermo"), None)
    assert h.chrome.fronted == 0


def test_chrome_comes_forward_only_when_the_page_step_runs():
    h = hands(elements=PAGE)
    h.run(step("page_click", description="thermodynamics hw 5", element="0:3", label="Thermodynamics HW 5"))
    assert h.chrome.fronted == 1 and h.chrome.clicks == [("0:3", "Thermodynamics HW 5")]
    h.run(step("page_fill", description="search", text="thermo", element="0:7", label="Search"))
    assert h.chrome.fronted == 2
    h.run(step("page_read", what="page"))  # reading needs no window in front
    assert h.chrome.fronted == 2


class _PageHands:
    """ChromeHands as the executor sees them, on a Canvas page."""

    def __init__(self, chrome_hands):
        self.chrome_hands = chrome_hands

    def resolve(self, s, ctx):
        return self.chrome_hands.resolve(s, ctx)

    def scene(self, s, ctx):
        return Scene(app="Google Chrome", exe="chrome.exe", url="https://canvas.vt.edu/courses/11/assignments/1")

    def label(self, s):
        return self.chrome_hands.label(s) or s.action

    def run(self, s, ctx):
        return self.chrome_hands.run(s)


class _Asker:
    def __init__(self, answer, log):
        self.answer, self.log = answer, log

    def confirm(self, question):
        self.log.append(("confirm", question))
        return self.answer

    def choose(self, question, choices):
        return None


@pytest.mark.parametrize("answer", [True, False])
def test_a_risky_click_is_confirmed_before_chrome_takes_focus(answer):
    h = hands(elements=PAGE)
    log = []
    front = h.chrome.front
    h.chrome.front = lambda tab=None: (log.append(("front", "")), front(tab))[1]
    plan = Plan("act", (step("page_click", description="the submit assignment button"),))
    result = Executor(_PageHands(h), _Asker(answer, log)).run(plan, None)
    if answer:
        assert [kind for kind, _ in log] == ["confirm", "front"] and result.ok
    else:
        assert [kind for kind, _ in log] == ["confirm"] and result.cancelled and not h.chrome.clicks
    assert log[0][1] == "Click “Submit Assignment” on Canvas?"


def test_a_clear_match_becomes_that_element():
    h = hands(elements=PAGE)
    resolved = h.resolve(step("page_click", description="sign in"), None)
    assert resolved == step("page_click", description="sign in", element="0:1", label="Sign in")
    resolved = h.resolve(step("page_click", description="the blue button at the bottom submit"), None)
    assert resolved.get("label") == "Submit Assignment" and resolved.get("submits") is True


def test_close_matches_are_a_question():
    pick = hands(elements=PAGE).resolve(step("page_click", description="thermodynamics homework"), None)
    assert isinstance(pick, Pick) and pick.question == "Click which one?" and len(pick.steps) == 2
    assert {s.get("element") for s in pick.steps} == {"0:3", "0:4"}


def test_nothing_like_it():
    with pytest.raises(ActionError, match="Couldn't find “purple elephant”"):
        hands(elements=PAGE).resolve(step("page_click", description="purple elephant"), None)
    with pytest.raises(ActionError, match="anything to click"):
        hands(elements=()).resolve(step("page_click", description="sign in"), None)
    with pytest.raises(ActionError, match="password field"):
        hands(elements=PAGE).resolve(step("page_fill", description="the password", text="x"), None)


def test_an_element_tier_1_pointed_at_is_kept():
    h = hands(elements=PAGE)
    h.chrome.last_map = h.chrome.view
    s = step("page_click", description="that", element="0:5", label="Submit Assignment")
    assert h.resolve(s, None) is s


def test_fill_and_scroll_run():
    h = hands(elements=PAGE)
    fill = h.resolve(step("page_fill", description="search", text="thermo", submit=True), None)
    assert fill.get("element") == "0:7" and fill.get("search") is True
    out = h.run(fill)
    assert out.detail == "Typed “thermo” in Search ⏎" and h.chrome.calls[-1][0] == "fill"
    out = h.run(step("scroll_to", description="comments", element="0:3", label="Comments"))
    assert out.detail == "Scrolled to Comments"
    out = h.run(step("page_read", what="page"))
    assert out.detail == "Read 3 words" and h.chrome.last_text == "one two three"


def test_a_stale_click_is_found_again_once():
    h = hands(elements=PAGE)
    h.chrome.stale_once = True
    out = h.run(step("page_click", description="sign in", element="0:1", label="Sign in"))
    assert out.detail == "Clicked “Sign in”" and len(h.chrome.clicks) == 2


def test_whats_due_is_a_list_to_read():
    pick = hands().resolve(step("canvas_due", range="today"), None)
    assert isinstance(pick, Pick) and pick.optional and pick.detail == "2 due today"
    assert pick.choices[0].startswith("Pre-lab") and pick.steps[1] == step(
        "canvas_open", url="https://canvas.vt.edu/courses/11/assignments/1", title="HW 5")
    answer = hands().resolve(step("canvas_due", range="weekend"), None)
    assert isinstance(answer, Outcome) and answer.hold > 0 and answer.detail.startswith("Nothing due this weekend — next: Pre-lab (PHYS 2306), today 6 PM")


def test_click_on_my_assignment_for_today():
    planner = tuple(i for i in items() if i.title != "Pre-lab")
    one = ChromeHands(FakeChrome(planner=planner), now=lambda: NOW).resolve(
        step("canvas_open_item", item="my assignment for today"), None)
    assert one == step("canvas_open", url="https://canvas.vt.edu/courses/11/assignments/1", title="HW 5")
    several = hands().resolve(step("canvas_open_item", item="my assignment for today"), None)
    assert isinstance(several, Pick) and several.question == "2 due today — which one?" and not several.optional
    none = hands().resolve(step("canvas_open_item", item="my assignment for saturday"), None)
    assert isinstance(none, Outcome) and none.detail.startswith("Nothing due on Saturday — next: Pre-lab")
    with pytest.raises(ActionError, match="Couldn't find"):
        hands().resolve(step("canvas_open_item", item="the chemistry essay"), None)


def test_canvas_open_runs_in_the_canvas_tab():
    h = hands()
    out = h.run(step("canvas_open", url="https://canvas.vt.edu/courses/11/assignments/1", title="HW 5"))
    assert out.detail == "Opened HW 5" and h.chrome.calls[-1] == ("navigate", ("https://canvas.vt.edu/courses/11/assignments/1",), None)


def test_reading_an_assignment():
    h = hands(planner=tuple(i for i in items() if i.title == "HW 6"))
    resolved = h.resolve(step("canvas_item_text", item="the thermo homework"), None)
    assert resolved.action == "canvas_item_text" and resolved.get("url")
    out = h.resolve(resolved, None)
    assert out.detail == "HW 6: Read chapter 5." and h.chrome.last_text.startswith("Read chapter 5.")


def test_named_tabs():
    youtube = TabInfo(4, 1, "https://www.youtube.com/watch?v=1", "lofi - YouTube", last=5.0)
    gmail = TabInfo(5, 1, "https://mail.google.com/", "Inbox - Gmail", last=9.0)
    h = hands(tabs=(youtube, gmail))
    resolved = h.resolve(step("tab", op="goto", match="youtube"), None)
    assert resolved == step("tab", op="goto", match="youtube", tab=4, title="lofi - YouTube")
    assert h.run(resolved).detail == "Switched to lofi - YouTube"
    assert h.resolve(step("tab", op="goto", match="canvas"), None) == step("open_url", url="https://canvas.vt.edu")
    with pytest.raises(ActionError, match="No tab called"):
        h.resolve(step("tab", op="goto", match="spotify"), None)


def test_page_rows_only_when_he_points_at_the_page():
    class Front:
        def __init__(self, exe):
            self.win = type("W", (), {"exe_name": exe})()

    ctx = type("Ctx", (), {"foreground": Front("chrome.exe")})()
    h = hands(elements=PAGE)
    rows = h.page_rows("hit that sign in thing", ctx)
    assert isinstance(rows, PageRows) and rows.scored[0].element.name == "Sign in"
    assert rows.rows()[0].startswith("1 button: Sign in")
    assert h.page_rows("open spotify", ctx) is None
    notepad = type("Ctx", (), {"foreground": Front("notepad.exe")})()
    assert h.page_rows("click sign in", notepad) is None


def test_labels_and_preview():
    h = hands()
    assert h.label(step("page_click", description="x", label="Submit Assignment")) == "Click “Submit Assignment”"
    assert h.label(step("canvas_due", range="week")) == "Canvas · due this week"
    assert h.label(step("tab", op="new")) is None
    h.preview(step("page_click", description="x", element="0:5"), True)
    h.preview(step("page_click", description="x", element="0:5"), False)
    assert [c[0] for c in h.chrome.calls] == ["highlight", "highlight"]
    assert h.chrome.calls[0][1][1] == {"on": True, "ms": 16000}


def test_not_connected_says_so():
    h = hands()
    h.chrome.connected = False
    with pytest.raises(ActionError, match="isn't connected"):
        h.resolve(step("page_click", description="sign in"), None)
    assert isinstance(step("open_url", url="https://x.org"), Step)


def test_a_plain_click_comes_with_the_page_rows():
    class Front:
        def __init__(self, exe):
            self.win = type("W", (), {"exe_name": exe})()

    ctx = type("Ctx", (), {"foreground": Front("chrome.exe")})()
    h = hands(elements=PAGE)
    rows = h.page_rows("hit that sign in thing", ctx)
    assert rows.direct == step("page_click", description="hit that sign in thing", element="0:1", label="Sign in")
    assert h.page_rows("press the thermodynamics homework link", ctx).direct is None  # two of them: Tier 1 / ask
    assert h.page_rows("scroll to the thermodynamics part", ctx) is None or \
        h.page_rows("scroll to the thermodynamics part", ctx).direct is None


def test_whats_due_for_one_course():
    pick = hands().resolve(step("canvas_due", range="week", item="do I have anything for physics due this week"), None)
    assert isinstance(pick, Pick) and pick.detail == "2 due this week for physics" and len(pick.steps) == 2
    none = hands().resolve(step("canvas_due", range="week", item="anything for chemistry due this week"), None)
    assert isinstance(none, Outcome) and none.detail == "Nothing for chemistry due this week"
