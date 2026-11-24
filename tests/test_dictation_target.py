"""Target.erase hands back the spot as it is after the backspaces, so the next insert's landing check can pass."""

from dataclasses import dataclass

import pytest

from nova.dictation import target as target_module
from nova.dictation.target import Spot, Target


@dataclass(frozen=True)
class FakeWin:
    hwnd: int = 1


class Field:
    """A Win32 Edit / RichEdit's length as Nova reads it: one reading per call, the last one repeats."""

    def __init__(self, *readings):
        self.readings = list(readings)
        self.erased = []

    def length(self, hwnd):
        return self.readings.pop(0) if len(self.readings) > 1 else self.readings[0]

    def erase(self, count, win):
        self.erased.append(count)


@pytest.fixture
def field(monkeypatch):
    def make(*readings):
        f = Field(*readings)
        monkeypatch.setattr(target_module.keyboard, "_text_length", f.length)
        monkeypatch.setattr(target_module.keyboard, "erase", f.erase)
        monkeypatch.setattr(target_module, "ERASED_WAIT_S", 0.1)
        return f
    return make


def _target():
    return Target(lambda: None, focus=object())


def _edit_spot(length=20):
    return Spot(FakeWin(), "Notepad", "native", "x" * length, edit_hwnd=7, length=length)


def test_erase_then_insert_check_counts_from_the_new_length(field):
    f = field(20, 17, 15)  # the backspaces land over a few readings
    spot = _target().erase(_edit_spot(20), 5)
    assert f.erased == [5]
    assert spot.length == 15
    f.readings = [15]
    check = _target()._check(spot, "hello!")
    assert check() is False  # the paste hasn't landed: his clipboard must not go back yet
    f.readings = [21]
    assert check() is True


def test_the_old_spot_could_never_pass(field):
    """What the live test found: 20 - 5 + 6 = 21 is never ≥ 20 + 6, so insert waited 3 s and said it didn't land."""
    field(21)
    check = _target()._check(_edit_spot(20), "hello!")
    assert check() is False


def test_waits_for_the_last_backspaces(field):
    """A plain Edit drops two characters for a line break, so the count passes 20 - 5 before the last ones land."""
    field(20, 14, 13)
    assert _target().erase(_edit_spot(20), 5).length == 13


def test_backspaces_that_never_land_drop_the_length_check(field):
    field(20)
    spot = _target().erase(_edit_spot(20), 5)
    assert spot.length is None
    assert spot.before is not None  # the caret check still applies


def test_nothing_to_erase_keeps_the_spot(field):
    f = field(20)
    spot = _edit_spot(20)
    assert _target().erase(spot, 0) is spot
    assert f.erased == [0]


def test_docs_cursor_is_dropped(field):
    """Docs' check is "the cursor moved": after backspaces it already has, so it would pass before the paste."""
    field(None)
    spot = Spot(FakeWin(), "Google Docs", "docs", cursor=(3, 9))
    after = _target().erase(spot, 2)
    assert after.cursor is None
    assert _target()._check(after, "hi") is None


def test_other_fields_keep_the_spot(field):
    field(None)
    spot = Spot(FakeWin(), "Chrome", "chrome", "some text")
    assert _target().erase(spot, 3) is spot
