"""Numbers, the grid and click-by-name (``nova.actions.pointing``) with a fake UI Automation reader — no windows,
no mouse: ``native.click_at`` / ``hover_at`` / ``drag`` and the click receipt are replaced."""

import threading

import pytest

from nova.actions import native
from nova.actions.hands import WindowsHands
from nova.actions.marks import Box, Target, grid_cells, grid_zoom
from nova.actions.native import ActionError, Monitor, Win
from nova.actions.pointing import Pointer, clip, verb_for
from nova.actions.uitree import Element
from nova.brain.plan import Step
from nova.events import MarksHidden, MarksShown

WIN = Win(hwnd=1, pid=2, tid=3, title="t", cls="c", exe="x.exe", aumids=())
MON = Monitor(handle=9, rect=(0, 0, 2000, 1200), work=(0, 0, 2000, 1150), primary=True, device="\\\\.\\DISPLAY1",
              dpi=96)
FRAME = (100, 100, 900, 700)
BUTTON, TEXT = 50000, 50020


def button(index: int, name: str, rect: tuple[int, int, int, int]) -> Element:
    return Element(index, name, BUTTON, rect, frozenset({"invoke"}))


def text(index: int, name: str, rect: tuple[int, int, int, int] = (120, 120, 400, 140)) -> Element:
    return Element(index, name, TEXT, rect, frozenset())


class FakeFocus:
    def __init__(self, elements=(), result="invoke"):
        self.elements = list(elements)
        self.result = result
        self.generation = 0
        self.acted: list[int] = []

    def snapshot(self, hwnd):
        self.generation += 1
        return list(self.elements)

    def act(self, index, patterns):
        self.acted.append(index)
        return self.result


@pytest.fixture
def mouse(monkeypatch):
    """Records real clicks / hovers / drags; every click 'changes the screen' unless ``moved`` is set False."""
    log: dict = {"clicks": [], "hovers": [], "drags": [], "moved": True}
    monkeypatch.setattr(native, "click_at", lambda x, y, button="left", count=1: log["clicks"].append((x, y, button,
                                                                                                         count)))
    monkeypatch.setattr(native, "hover_at", lambda x, y: log["hovers"].append((x, y)))
    monkeypatch.setattr(native, "drag", lambda start, end, button="left", steps=12: log["drags"].append((start, end)))

    def watch(rect, act):
        act()
        return log["moved"]

    monkeypatch.setattr(Pointer, "_watch", staticmethod(watch))
    return log


def make(elements=(), lines=(), result="invoke", hide_after=60.0):
    events: list = []
    focus = FakeFocus(elements, result)
    pointer = Pointer(focus, events.append, ocr_lines=lambda rect: list(lines), monitor_for=lambda hwnd: MON,
                      frame=lambda hwnd: FRAME, hide_after=hide_after)
    return pointer, focus, events


def shown(events) -> list[MarksShown]:
    return [e for e in events if isinstance(e, MarksShown)]


def hidden(events) -> list[str]:
    return [e.why for e in events if isinstance(e, MarksHidden)]


ROW = [button(0, "Save", (200, 200, 260, 230)), button(1, "Open", (100 + 200, 200, 360, 230)),
       button(2, "Close", (400, 200, 460, 230))]


# --- small helpers ----------------------------------------------------------------------------------------------

def test_verb_for():
    assert verb_for(None, None) == "click"
    assert verb_for("left", 1) == "click"
    assert verb_for("left", 2) == "double"
    assert verb_for(None, 3) == "double"
    assert verb_for("right", 1) == "right"
    assert verb_for("middle", None) == "middle"
    assert verb_for("hover", 2) == "hover"


def test_clip():
    assert clip((50, 50, 500, 500), (0, 0, 300, 300)) == (50, 50, 300, 300)
    assert clip((10, 10, 20, 20), (0, 0, 300, 300)) == (10, 10, 20, 20)
    assert clip((400, 400, 500, 500), (0, 0, 300, 300)) == (0, 0, 300, 300)  # off the monitor: the whole monitor


# --- numbers ----------------------------------------------------------------------------------------------------

def test_show_numbers_in_reading_order_and_publishes_them():
    pointer, _, events = make(ROW)
    outcome = pointer.show(WIN)
    assert pointer.showing and not pointer.grid_on
    assert [pointer.target(n).name for n in (1, 2, 3)] == ["Save", "Open", "Close"]
    event = shown(events)[-1]
    assert event.device == MON.device and event.monitor == MON.rect
    assert [n for n, _ in event.tags] == [1, 2, 3] and event.cells == ()
    assert outcome.method == "numbers" and outcome.hold > 0 and "3 numbers" in outcome.detail
    pointer.hide()


CAPTION = [button(1, "Minimize", (700, 0, 740, 30)), button(2, "Maximize", (740, 0, 780, 30)),
           button(3, "Close", (780, 0, 820, 30))]
PAGE = CAPTION + [button(4, "Send", (100, 100, 200, 140)), button(5, "New chat", (100, 200, 200, 240))]


class ColdFocus(FakeFocus):
    """Chromium's tree arrives only after the first ask."""

    def snapshot(self, hwnd):
        self.generation += 1
        return list(CAPTION if self.generation == 1 else PAGE)


def test_a_cold_chromium_window_is_asked_again():
    focus = ColdFocus()
    pointer = Pointer(focus, ocr_lines=lambda rect: [], monitor_for=lambda hwnd: MON, frame=lambda hwnd: FRAME)
    chromium = Win(hwnd=1, pid=2, tid=3, title="Claude", cls="Chrome_WidgetWin_1", exe="claude.exe", aumids=())
    names = {t.name for t in pointer.gather(chromium)}
    assert {"Send", "New chat"} <= names and focus.generation == 2


def test_other_windows_are_asked_once():
    focus = ColdFocus()
    pointer = Pointer(focus, ocr_lines=lambda rect: [], monitor_for=lambda hwnd: MON, frame=lambda hwnd: FRAME)
    assert {t.name for t in pointer.gather(WIN)} == {"Minimize", "Maximize", "Close"} and focus.generation == 1


def test_a_chromium_window_with_few_buttons_waits_at_most_cold_wait():
    focus = FakeFocus(CAPTION)
    pointer = Pointer(focus, ocr_lines=lambda rect: [], monitor_for=lambda hwnd: MON, frame=lambda hwnd: FRAME,
                      cold_wait=0.2)
    chromium = Win(hwnd=1, pid=2, tid=3, title="x", cls="Chrome_WidgetWin_1", exe="x.exe", aumids=())
    assert len(pointer.gather(chromium)) == 3 and 2 <= focus.generation <= 4


def test_ocr_lines_get_numbers_too():
    pointer, _, _ = make(ROW, lines=[Target(Box(600, 300, 700, 320), "Fit to width", "text", "ocr")])
    pointer.show(WIN)
    assert pointer.target(4).name == "Fit to width"
    pointer.hide()


def test_nothing_to_number_shows_the_grid():
    pointer, _, events = make()
    outcome = pointer.show(WIN)
    assert pointer.grid_on and outcome.method == "grid"
    assert len(shown(events)[-1].cells) == 9
    assert shown(events)[-1].cells[0][1][:2] == FRAME[:2]  # "here": the window, clipped to its monitor
    pointer.hide()


def test_more_pages_through_and_one_page_says_so():
    many = [Target(Box(10 + 30 * (i % 50), 10 + 30 * (i // 50), 30 + 30 * (i % 50), 30 + 30 * (i // 50)), f"t{i}")
            for i in range(120)]
    pointer, _, events = make()
    pointer.show(WIN, targets=many)
    assert len(shown(events)[-1].tags) == 99
    outcome = pointer.more()
    assert len(shown(events)[-1].tags) == 21 and "Page 2 of 2" in outcome.detail
    pointer.more()
    assert len(shown(events)[-1].tags) == 99  # back to the first page

    pointer.show(WIN, targets=many[:5])
    assert pointer.more().detail == "That's all of them"
    pointer.hide()


def test_more_with_nothing_showing_fails():
    pointer, _, _ = make()
    with pytest.raises(ActionError):
        pointer.more()


def test_hide_publishes_once():
    pointer, _, events = make(ROW)
    pointer.show(WIN)
    assert pointer.hide("hide numbers") is True
    assert pointer.hide("hide numbers") is False
    assert hidden(events) == ["hide numbers"]
    assert not pointer.showing and pointer.target(1) is None


def test_numbers_hide_by_themselves():
    pointer, _, events = make(ROW, hide_after=0.05)
    gone = threading.Event()
    pointer._publish = lambda e: (events.append(e), isinstance(e, MarksHidden) and gone.set())
    pointer.show(WIN)
    assert gone.wait(2.0)
    assert hidden(events) == ["timed out"] and not pointer.showing


def test_an_old_timer_never_hides_a_newer_showing():
    pointer, _, events = make(ROW)
    pointer.show(WIN)
    old = pointer._showing
    pointer.show(WIN)
    pointer._expire(old)
    assert pointer.showing and hidden(events) == []
    pointer.hide()


# --- picking a number ---------------------------------------------------------------------------------------------

def test_a_number_invokes_its_element_and_hides_the_tags(mouse):
    pointer, focus, events = make(ROW)
    pointer.show(WIN)
    outcome = pointer.pick(2)
    assert focus.acted == [1] and mouse["clicks"] == []
    assert outcome.method == "invoke" and "Open" in outcome.detail
    assert hidden(events) == ["acted"] and not pointer.showing


def test_right_click_a_number_is_a_real_click(mouse):
    pointer, focus, _ = make(ROW)
    pointer.show(WIN)
    outcome = pointer.pick(1, "right")
    assert focus.acted == [] and mouse["clicks"] == [(230, 215, "right", 1)]
    assert outcome.detail.startswith("Right-clicked")


def test_double_click_and_hover(mouse):
    pointer, _, _ = make(ROW)
    pointer.show(WIN)
    pointer.pick(3, "double")
    assert mouse["clicks"] == [(430, 215, "left", 2)]
    pointer.show(WIN)
    assert pointer.pick(3, "hover").method == "hover" and mouse["hovers"] == [(430, 215)]


def test_drag_one_number_to_another(mouse):
    pointer, _, _ = make(ROW)
    pointer.show(WIN)
    outcome = pointer.pick(1, to=3)
    assert mouse["drags"] == [((230, 215), (430, 215))] and outcome.method == "drag"


def test_bad_numbers(mouse):
    pointer, _, _ = make(ROW)
    with pytest.raises(ActionError, match="show numbers"):
        pointer.pick(1)
    pointer.show(WIN)
    with pytest.raises(ActionError, match="no number 42"):
        pointer.pick(42)
    with pytest.raises(ActionError):
        pointer.pick(1, "lick")
    assert pointer.showing  # a miss leaves the tags up
    pointer.hide()


def test_invoke_that_needs_a_real_click_falls_back(mouse):
    pointer, focus, _ = make(ROW, result="")
    pointer.show(WIN)
    outcome = pointer.pick(1)
    assert focus.acted == [0] and mouse["clicks"] == [(230, 215, "left", 1)] and outcome.method == "click-click"


def test_invoke_still_running_counts_as_clicked(mouse):
    pointer, _, _ = make(ROW, result=None)
    pointer.show(WIN)
    assert pointer.pick(1).method == "invoke" and mouse["clicks"] == []


def test_a_stale_snapshot_never_reaches_invoke(mouse):
    pointer, focus, _ = make(ROW)
    pointer.show(WIN)
    focus.generation += 1  # another snapshot happened since: index 0 may be something else now
    pointer.pick(1)
    assert focus.acted == [] and mouse["clicks"] == [(230, 215, "left", 1)]


def test_a_click_that_changes_nothing_fails(mouse):
    mouse["moved"] = False
    pointer, _, _ = make(ROW)
    pointer.show(WIN)
    with pytest.raises(ActionError, match="nothing changed"):
        pointer.pick(1, "right")


# --- the grid -----------------------------------------------------------------------------------------------------

def test_grid_zooms_three_levels_then_clicks(mouse):
    pointer, _, events = make()
    pointer.grid(WIN)  # "show grid": the whole monitor
    assert pointer.pick(5).method == "grid"
    assert pointer.pick(5).method == "grid"
    assert "click it" in shown(events)[-1].note
    pointer.pick(1)
    expected = grid_cells(grid_zoom(grid_zoom(Box(*MON.rect), 5), 5))[0].centre
    assert mouse["clicks"] == [(*expected, "left", 1)]
    assert hidden(events) == ["acted"] and not pointer.showing


def test_grid_verbs_act_at_once(mouse):
    pointer, _, _ = make()
    pointer.grid(WIN)
    pointer.pick(3, "right")
    assert mouse["clicks"] == [(*grid_cells(Box(*MON.rect))[2].centre, "right", 1)]
    pointer.grid(WIN)
    pointer.pick(1, to=9)
    cells = grid_cells(Box(*MON.rect))
    assert mouse["drags"] == [(cells[0].centre, cells[8].centre)]


def test_grid_click_with_no_square_clicks_its_centre(mouse):
    pointer, _, _ = make()
    pointer.grid(WIN)
    pointer.pick(None, "click")
    assert mouse["clicks"] == [(*Box(*MON.rect).centre, "left", 1)]


def test_grid_square_out_of_range(mouse):
    pointer, _, _ = make()
    pointer.grid(WIN)
    with pytest.raises(ActionError, match="1 to 9"):
        pointer.pick(10)
    with pytest.raises(ActionError, match="1 to 9"):
        pointer.pick(1, to=12)
    pointer.hide()


# --- click by name ------------------------------------------------------------------------------------------------

def test_one_match_is_found_with_the_dialogs_text():
    pointer, _, events = make([text(0, "Delete this file?"), button(1, "OK", (300, 600, 380, 630)),
                               button(2, "Cancel", (400, 600, 480, 630))])
    found = pointer.locate(WIN, "ok")
    assert found.target.name == "OK" and found.hwnd == WIN.hwnd
    assert found.dialog == "Delete this file?"
    assert events == []  # nothing shown


def test_several_matches_put_numbers_on_them():
    pointer, _, events = make([button(0, "Save", (200, 200, 260, 230)), button(1, "Save", (200, 400, 260, 430)),
                               button(2, "Open", (300, 200, 360, 230))])
    outcome = pointer.locate(WIN, "save")
    assert outcome.method == "numbers" and "Which one" in outcome.detail
    assert len(shown(events)[-1].tags) == 2
    pointer.hide()


def test_text_only_on_screen_is_found_through_ocr():
    pointer, _, _ = make(ROW, lines=[Target(Box(600, 300, 700, 320), "Fit to width", "text", "ocr")])
    found = pointer.locate(WIN, "fit to width")
    assert found.target.source == "ocr"


def test_no_match_numbers_everything_or_shows_the_grid():
    pointer, _, events = make(ROW)
    outcome = pointer.locate(WIN, "frobnicate")
    assert outcome.method == "numbers" and "don't see" in outcome.detail
    assert len(shown(events)[-1].tags) == 3

    empty, _, _ = make()
    assert empty.locate(WIN, "frobnicate").method == "grid"
    pointer.hide()
    empty.hide()


# --- through the hands --------------------------------------------------------------------------------------------

def hands_with(pointer, focus, monkeypatch):
    monkeypatch.setattr(WindowsHands, "_front", staticmethod(lambda: WIN))
    return WindowsHands(lambda: None, focus=focus, nova=object(), pointer=pointer)


def test_hands_click_by_name_runs_only_the_step_it_resolved(mouse, monkeypatch):
    pointer, focus, _ = make([text(0, "Delete this file?"), button(1, "OK", (300, 600, 380, 630))])
    hands = hands_with(pointer, focus, monkeypatch)
    first = hands.resolve(Step("ui_click", {"name": "ok"}), None)
    assert first.get("label") == "OK" and first.get("dialog") == "Delete this file?"
    second = hands.resolve(Step("ui_click", {"name": "ok"}), None)
    with pytest.raises(ActionError, match="lost track"):
        hands.run(first, None)  # a newer resolve replaced what it found
    assert hands.run(second, None).method == "invoke"


def test_hands_label_a_number_and_refuse_a_missing_one(mouse, monkeypatch):
    pointer, focus, _ = make(ROW)
    hands = hands_with(pointer, focus, monkeypatch)
    with pytest.raises(ActionError, match="show numbers"):
        hands.resolve(Step("pick", {"n": 2}), None)
    hands.run(Step("numbers", {"op": "show"}), None)
    step = hands.resolve(Step("pick", {"n": 2}), None)
    assert step.get("label") == "Open"
    with pytest.raises(ActionError, match="no number 7"):
        hands.resolve(Step("pick", {"n": 7}), None)
    assert hands.run(step, None).method == "invoke"
    assert hands.run(Step("numbers", {"op": "hide"}), None).detail == "No numbers are showing"


TAB_ITEM = 50019


def tab(index: int, name: str) -> Element:
    return Element(index, name, TAB_ITEM, (120 + 100 * index, 105, 210 + 100 * index, 125), frozenset({"select"}))


@pytest.fixture
def keys(monkeypatch):
    """Records key presses and which window got them; the browser is a separate window."""
    pressed: list[tuple[str, int]] = []
    browser = Win(hwnd=77, pid=8, tid=9, title="Chrome", cls="Chrome_WidgetWin_1", exe="chrome.exe", aumids=())
    monkeypatch.setattr("nova.actions.hands.keyboard.press", lambda combo, win: pressed.append((combo, win.hwnd)))
    monkeypatch.setattr("nova.actions.hands.apps.is_browser", lambda catalog, win: win.hwnd == browser.hwnd)
    monkeypatch.setattr("nova.actions.hands.apps.browser_window", lambda catalog: (browser, "activate"))
    return pressed


def test_tab_keys_go_to_a_tabbed_app_in_front_not_the_browser(keys, monkeypatch):
    pointer, focus, _ = make([tab(0, "notes.txt"), tab(1, "todo.txt"), text(2, "hello")])
    hands = hands_with(pointer, focus, monkeypatch)
    out = hands.run(Step("tab", {"op": "close"}), None)
    assert keys == [("ctrl+w", WIN.hwnd)] and out.detail == "Closed the tab" and out.win == WIN
    hands.run(Step("tab", {"op": "next"}), None)
    assert keys[-1] == ("ctrl+tab", WIN.hwnd)


def test_tab_keys_go_to_the_browser_when_the_front_app_has_no_tabs(keys, monkeypatch):
    pointer, focus, _ = make(ROW)
    hands = hands_with(pointer, focus, monkeypatch)
    hands.run(Step("tab", {"op": "close"}), None)
    assert keys == [("ctrl+w", 77)]


def test_windows_terminal_gets_its_own_tab_keys(keys, monkeypatch):
    term = Win(hwnd=5, pid=2, tid=3, title="PowerShell", cls="CASCADIA_HOSTING_WINDOW_CLASS",
               exe=r"C:\Program Files\WindowsApps\WindowsTerminal.exe", aumids=())
    pointer, focus, _ = make([tab(0, "PowerShell"), tab(1, "cmd")])
    monkeypatch.setattr(WindowsHands, "_front", staticmethod(lambda: term))
    hands = WindowsHands(lambda: None, focus=focus, nova=object(), pointer=pointer)
    hands.run(Step("tab", {"op": "close"}), None)
    hands.run(Step("tab", {"op": "new"}), None)
    assert keys == [("ctrl+shift+w", 5), ("ctrl+shift+t", 5)]  # ctrl+w would delete a word in the shell
