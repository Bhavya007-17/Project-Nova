"""The window in front as a task's world (Stage 07d): rows + pixels to look, the pointer / hands to act."""

import pytest

from nova.actions.marks import Box, Target
from nova.actions.native import ActionError, Monitor, Win
from nova.actions.rows import Row
from nova.actions.taskworld import WindowWorld
from nova.brain.risk import Scene
from nova.brain.task import Choice
from nova.events import GuideHidden, GuideRing

WIN = Win(42, 1, 1, "notes.txt - Notepad", "Notepad", "C:/Windows/notepad.exe", ())
SCREEN = Monitor(7, (0, 0, 2880, 1800), (0, 0, 2880, 1740), True, r"\\.\DISPLAY1", 192)
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

    def monitor(self, hwnd):
        return SCREEN

    def act(self, target, verb="click", *, number=None, hwnd=None):
        self.acted.append((target.name, verb, hwnd))
        return "clicked"


def world(pointer=None, scenes=None, front=lambda: WIN, *, published=None, frames=None, cursor=lambda: (5, 5)):
    ran, asked = [], []
    scenes = list(scenes or [])
    frames = frames if frames is not None else [(100, 100, 900, 700)]

    def scene(s):
        asked.append(s)
        return scenes.pop(0) if scenes else Scene(app="Notepad")

    def frame(hwnd):
        if not frames:
            raise ActionError("gone")
        return frames.pop(0) if len(frames) > 1 else frames[0]

    w = WindowWorld(pointer or FakePointer(), front, scene, lambda s: ran.append(s) or "ran",
                    signature=lambda rect: (rect[2], 7), publish=(published if published is not None else []).append,
                    frame=frame, cursor=cursor)
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


def test_the_ring_goes_on_the_row_and_follows_the_window():
    published = []
    w, _, _ = world(published=published, frames=[(100, 100, 900, 700), (100, 100, 900, 700), (130, 90, 930, 690)])
    w.look()
    assert w.show(VIEW, "Click “View”") == VIEW.target.box
    assert w.show(VIEW, "Click “View”") == Box(30, -10, 40, 0)  # he dragged the window 30 right, 10 up
    assert w.show(VIEW, "Click “View”") == Box(30, -10, 40, 0)
    assert published == [GuideRing(SCREEN.device, SCREEN.rect, (0, 0, 10, 10), "Click “View”"),
                         GuideRing(SCREEN.device, SCREEN.rect, (30, -10, 40, 0), "Click “View”")]  # once per move


def test_hide_takes_the_ring_away_once():
    published = []
    w, _, _ = world(published=published)
    w.hide()
    assert published == []
    w.look()
    w.show(VIEW, "Click “View”")
    w.hide()
    w.hide()
    assert published[1:] == [GuideHidden("guide")]


def test_a_step_without_a_row_has_no_ring():
    published = []
    w, _, _ = world(published=published)
    w.look()
    w.show(VIEW, "Click “View”")
    assert w.show(None, "Press Ctrl + S") is None
    assert published[-1] == GuideHidden("guide")


def test_the_ring_stays_put_when_the_window_cant_be_read():
    w, _, _ = world(frames=[])
    w.look()
    assert w.show(NAME, "Click “File name”") == NAME.target.box


def test_the_mouse_or_none():
    assert world(cursor=lambda: (3, 4))[0].cursor() == (3, 4)

    def broken():
        raise OSError("no desktop")

    assert world(cursor=broken)[0].cursor() is None
