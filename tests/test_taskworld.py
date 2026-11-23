"""The window in front as a task's world (Stage 07d): rows + pixels to look, the pointer / hands to act."""

import pytest

from nova.actions.marks import Box, Target
from nova.actions.native import ActionError, Win
from nova.actions.rows import Row
from nova.actions.taskworld import WindowWorld
from nova.brain.risk import Scene
from nova.brain.task import Choice

WIN = Win(42, 1, 1, "notes.txt - Notepad", "Notepad", "C:/Windows/notepad.exe", ())
VIEW = Row(1, Target(Box(0, 0, 10, 10), "View", "menu item"))
NAME = Row(2, Target(Box(0, 20, 50, 30), "File name", "field"))


class FakePointer:
    def __init__(self, fail=False):
        self.acted = []
        self._fail = fail

    def rows(self, win):
        return [VIEW, NAME]

    def window_area(self, hwnd):
        if self._fail:
            raise ActionError("gone")
        return (0, 0, 100, 100)

    def act(self, target, verb="click", *, number=None, hwnd=None):
        self.acted.append((target.name, verb, hwnd))
        return "clicked"


def world(pointer=None, scenes=None, front=lambda: WIN):
    ran, asked = [], []
    scenes = list(scenes or [])

    def scene(s):
        asked.append(s)
        return scenes.pop(0) if scenes else Scene(app="Notepad")

    w = WindowWorld(pointer or FakePointer(), front, scene, lambda s: ran.append(s) or "ran",
                    signature=lambda rect: (rect[2], 7))
    return w, ran, asked


def test_look_is_rows_and_pixels():
    w, _, _ = world()
    view = w.look()
    assert view.lines == ('1 menu item "View"', '2 field "File name"') and view.signature == (100, 7)
    assert w.signature() == (100, 7)


def test_no_window_or_no_frame_means_no_pixels():
    def gone():
        raise ActionError("No window is in front.")

    assert world(front=gone)[0].signature() == ()
    assert world(FakePointer(fail=True))[0].signature() == ()


@pytest.mark.parametrize("verb", ["click", "right", "double"])
def test_clicks_go_through_the_pointer_on_the_front_window(verb):
    pointer = FakePointer()
    w, ran, _ = world(pointer)
    w.act(VIEW, Choice(row=1, verb=verb))
    assert pointer.acted == [("View", verb, 42)] and ran == []


def test_a_click_needs_its_row():
    with pytest.raises(ActionError):
        world()[0].act(None, Choice(row=1, verb="click"))


def test_typing_clicks_the_row_then_types():
    pointer = FakePointer()
    w, ran, asked = world(pointer)
    w.act(NAME, Choice(row=2, verb="type", text="report"))
    assert pointer.acted == [("File name", "click", 42)]
    assert [(s.action, s.get("text")) for s in ran] == [("type_text", "report")]
    assert asked[-1].action == "type_text"  # the focus is read again after the click


def test_never_types_where_the_click_landed_on_a_password():
    pointer = FakePointer()
    w, ran, _ = world(pointer, scenes=[Scene(password=True)])
    with pytest.raises(ActionError, match="password"):
        w.act(NAME, Choice(row=2, verb="type", text="hunter2"))
    assert ran == []


def test_keys_and_scrolling_go_through_the_hands():
    w, ran, _ = world()
    w.act(None, Choice(verb="press", keys="tab"))
    w.act(None, Choice(verb="scroll", text="up"))
    w.act(None, Choice(verb="scroll"))
    assert [(s.action, dict(s.args)) for s in ran] == [("press", {"keys": "tab"}), ("scroll", {"dir": "up"}),
                                                        ("scroll", {"dir": "down"})]
