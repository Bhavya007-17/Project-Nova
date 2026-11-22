"""Scroll cascade (order, receipts, two-vote "at the end"), key / wheel amounts, pixel receipts, ScrollPattern
targets. Fake hands only: nothing here scrolls a real window."""

from __future__ import annotations

import pytest

from nova.actions import screen, scrolling
from nova.actions.native import ActionError, Win
from nova.actions.scrolling import AT_END, MOVED, UNAVAILABLE, UNMOVED, Front, Scroller, cascade
from nova.actions.uitree import Element, ScrollReceipt, scroll_target


def fixed(result: str, calls: list[str], name: str):
    def method(direction: str, amount: str) -> str:
        calls.append(name)
        return result
    return name, method


def failing(calls: list[str], name: str):
    def method(direction: str, amount: str) -> str:
        calls.append(name)
        raise ActionError("no")
    return name, method


def test_first_method_that_moves_wins_and_later_ones_never_run():
    calls: list[str] = []
    done = cascade([fixed(UNAVAILABLE, calls, "page"), fixed(MOVED, calls, "pattern"), fixed(MOVED, calls, "keys")],
                   "down", "page")
    assert done.moved and done.method == "pattern" and done.text == "Scrolled down"
    assert calls == ["page", "pattern"]


def test_pdf_page_saying_nothing_moved_is_not_the_end_on_its_own():
    """The audit bug: Chrome's PDF viewer ignores the page script, so one "didn't move" must not stop the cascade."""
    calls: list[str] = []
    done = cascade([fixed(AT_END, calls, "page"), fixed(UNAVAILABLE, calls, "pattern"), fixed(MOVED, calls, "keys")],
                   "down", "page")
    assert done.moved and done.method == "keys"


def test_two_methods_agreeing_means_already_at_the_end():
    calls: list[str] = []
    done = cascade([fixed(AT_END, calls, "page"), fixed(UNMOVED, calls, "pattern"), fixed(MOVED, calls, "keys")],
                   "down", "page")
    assert not done.moved and done.text == "Already at the bottom"
    assert calls == ["page", "pattern"]


def test_up_and_top_edges():
    calls: list[str] = []
    assert cascade([fixed(UNMOVED, calls, "keys"), fixed(UNMOVED, calls, "wheel")], "up", "page").text == \
        "Already at the top"
    assert cascade([fixed(MOVED, calls, "keys")], "top", "page").text == "Scrolled to the top"
    assert cascade([fixed(MOVED, calls, "keys")], "down", "half").text == "Scrolled halfway"
    assert cascade([fixed(MOVED, calls, "keys")], "up", "bit").text == "Scrolled up a bit"


def test_errors_count_as_unavailable_and_nothing_that_could_tell_is_an_error():
    calls: list[str] = []
    with pytest.raises(ActionError, match="Nothing scrolled"):
        cascade([failing(calls, "page"), fixed(UNAVAILABLE, calls, "pattern"), fixed(AT_END, calls, "keys")],
                "down", "page")
    assert calls == ["page", "pattern", "keys"]


@pytest.mark.parametrize(("direction", "amount", "keys"), [
    ("down", "page", ["pagedown"]),
    ("up", "large", ["pageup"] * 3),
    ("down", "small", ["down"] * 3),
    ("down", "bit", ["down"] * 3),
    ("right", "page", ["right"] * 3),  # sideways is always short: arrows move a column, not a line
    ("top", "page", ["ctrl+home"]),
    ("bottom", "small", ["ctrl+end"]),
    ("down", "half", []),
])
def test_key_presses(direction, amount, keys):
    assert scrolling.key_presses(direction, amount) == keys


def test_wheel_notches_sign_and_size():
    assert scrolling.wheel_notches("down", "page") == 5
    assert scrolling.wheel_notches("up", "small") == -2
    assert scrolling.wheel_notches("up", "large") == -15
    assert scrolling.wheel_notches("right", "page") == 5
    assert scrolling.wheel_notches("bottom", "page") == 0
    assert scrolling.wheel_notches("down", "half") == 0


def test_screen_changed_ignores_a_caret_blink_but_not_a_scroll():
    before = tuple(range(100))
    blink = before[:50] + (-1, -2, -3) + before[53:]
    scrolled = tuple(range(10, 110))
    assert not screen.changed(before, before)
    assert not screen.changed(before, blink)
    assert screen.changed(before, scrolled)
    assert screen.changed(before, before[:90])  # a different size is a change
    assert not screen.changed((), ())


def test_screen_rows_hash_every_other_row():
    shot = screen.Shot(0, 0, 2, 4, bytes([1] * 8 + [2] * 8 + [3] * 8 + [4] * 8))
    assert len(screen.rows(shot)) == 2
    assert screen.rows(shot)[0] != screen.rows(shot)[1]


def test_inset_keeps_the_middle():
    assert screen.inset((0, 0, 1000, 1000)) == (200, 250, 800, 900)


@pytest.mark.parametrize(("before", "view", "direction", "amount", "target"), [
    (40.0, 20.0, "down", "half", 50.0),
    (40.0, 20.0, "top", "page", 0.0),
    (40.0, 20.0, "bottom", "page", 100.0),
    (40.0, 20.0, "down", "page", None),  # the control's own page step
    (40.0, 20.0, "down", "small", 46.25),  # a quarter of a page: 20 / 80 of the range * 25
    (5.0, 20.0, "up", "large", 0.0),  # clamped
])
def test_scroll_pattern_targets(before, view, direction, amount, target):
    assert scroll_target(before, view, direction, amount) == target


# --- Scroller against fakes -----------------------------------------------------------------------------------------

WIN = Win(hwnd=1, pid=2, tid=3, title="t", cls="c", exe="x.exe", aumids=())


class FakeChrome:
    connected = True

    def __init__(self, result: dict) -> None:
        self.result = result

    @staticmethod
    def chrome_in_front() -> bool:
        return True

    def scroll(self, direction, amount):
        return self.result


class FakeFocus:
    def __init__(self, receipt: ScrollReceipt | None) -> None:
        self.receipt = receipt
        self.focused: list[tuple[int, int]] = []

    def scroll(self, direction, amount, x, y):
        return self.receipt

    def focus_at(self, x, y):
        self.focused.append((x, y))
        return True


def front(chrome: bool) -> Front:
    return Front(WIN, (0, 0, 1000, 1000), (500, 575), chrome)


def test_method_order_chrome_in_front():
    scroller = Scroller(FakeFocus(None), lambda: FakeChrome({}))
    assert [name for name, _ in scroller.methods(front(True), {"pdf": False})] == ["page", "pattern", "keys", "wheel"]
    assert [name for name, _ in scroller.methods(front(False), {"pdf": False})] == ["pattern", "keys", "wheel"]
    assert [name for name, _ in Scroller(None, lambda: None).methods(front(False), {"pdf": False})] == ["keys", "wheel"]


def test_page_method_reads_the_extensions_receipt():
    state = {"pdf": False}
    page = dict(Scroller(None, lambda: FakeChrome({"ok": True, "moved": False, "pdf": True})).methods(front(True), state))
    assert page["page"]("down", "page") == UNAVAILABLE and state["pdf"]
    page = dict(Scroller(None, lambda: FakeChrome({"ok": True, "moved": True})).methods(front(True), {"pdf": False}))
    assert page["page"]("down", "page") == MOVED
    page = dict(Scroller(None, lambda: FakeChrome({"ok": True, "moved": False})).methods(front(True), {"pdf": False}))
    assert page["page"]("down", "page") == AT_END
    page = dict(Scroller(None, lambda: FakeChrome({"ok": False})).methods(front(True), {"pdf": False}))
    assert page["page"]("down", "page") == UNAVAILABLE


@pytest.mark.parametrize(("receipt", "direction", "result"), [
    (None, "down", UNAVAILABLE),
    (ScrollReceipt(10.0, 30.0, 20.0), "down", MOVED),
    (ScrollReceipt(100.0, 100.0, 20.0), "down", AT_END),
    (ScrollReceipt(0.0, 0.0, 20.0), "up", AT_END),
    (ScrollReceipt(40.0, 40.0, 20.0), "down", UNMOVED),  # stuck mid-way: no proof of the end
])
def test_pattern_method(receipt, direction, result):
    methods = dict(Scroller(FakeFocus(receipt), lambda: None).methods(front(False), {"pdf": False}))
    assert methods["pattern"](direction, "page") == result


def test_keys_in_a_chrome_pdf_focus_the_content_first(monkeypatch):
    focus = FakeFocus(None)
    pressed: list[str] = []
    monkeypatch.setattr(scrolling.keyboard, "press", lambda keys, target: pressed.append(keys))
    monkeypatch.setattr(scrolling, "watch", lambda rect, act, timeout=0.4: (act(), True)[1])
    methods = dict(Scroller(focus, lambda: None).methods(front(True), {"pdf": True}))
    assert methods["keys"]("down", "page") == MOVED
    assert focus.focused == [(500, 575)] and pressed == ["pagedown"]


def test_keys_that_change_nothing_vote_unmoved(monkeypatch):
    monkeypatch.setattr(scrolling.keyboard, "press", lambda keys, target: None)
    monkeypatch.setattr(scrolling, "watch", lambda rect, act, timeout=0.4: (act(), False)[1])
    methods = dict(Scroller(None, lambda: None).methods(front(False), {"pdf": False}))
    assert methods["keys"]("down", "page") == UNMOVED
    assert methods["keys"]("down", "half") == UNAVAILABLE
    assert methods["wheel"]("bottom", "page") == UNAVAILABLE


def test_speed_changes_without_steady_scrolling_say_so():
    with pytest.raises(ActionError, match="not scrolling"):
        Scroller(None, lambda: None).change_speed(True)
    assert Scroller(None, lambda: None).stop() is False


def test_steady_speed_levels_clamp():
    steady = scrolling.Steady(front(False), "down", scrolling.KEEP_SPEED, lambda s, why: None)
    for _ in range(10):
        steady.change(1)
    assert steady.level == len(scrolling.SPEEDS) - 1
    for _ in range(10):
        steady.change(-1)
    assert steady.level == 0


# --- a cold Chrome PDF tree (Chromium builds it only once asked) ----------------------------------------------------

def _button(index: int, name: str) -> Element:
    return Element(index, name, 50000, (index * 40, 0, index * 40 + 30, 30), frozenset({"invoke"}))


class ColdPdfFocus:
    def __init__(self) -> None:
        self.asked = 0
        self.acted: list[int] = []

    def snapshot(self, hwnd):
        self.asked += 1
        caption = [_button(1, "Minimize"), _button(2, "Maximize"), _button(3, "Close")]
        return caption if self.asked == 1 else caption + [_button(4, "Fit to width")]

    def act(self, index, patterns):
        self.acted.append(index)
        return "invoke"


def test_fit_width_asks_a_cold_chrome_tree_again():
    focus = ColdPdfFocus()
    chrome_win = Win(hwnd=1, pid=2, tid=3, title="doc.pdf", cls="Chrome_WidgetWin_1", exe="chrome.exe", aumids=())
    done = Scroller(focus, lambda: None)._fit_width(Front(chrome_win, (0, 0, 1000, 1000), (500, 575), True), object())
    assert done.detail == "Fit to width" and focus.acted == [4] and focus.asked == 2
