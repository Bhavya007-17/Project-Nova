"""The hands run a task (Stage 07d): steered while it runs, guide mode's line on the bus, the ring cleaned up."""

import pytest

from nova.actions import hands as hands_
from nova.actions.hands import WindowsHands
from nova.brain import task as task_
from nova.events import Guiding, TaskRunning

DONE = task_.Result("done", "Done")


class FakeWorld:
    made: list = []

    def __init__(self, pointer, front, scene, run, *, publish):
        self.publish = publish
        self.hidden = 0
        FakeWorld.made.append(self)

    def hide(self):
        self.hidden += 1


class FakeLoop:
    body = staticmethod(lambda loop: DONE)

    def __init__(self, world, picker, confirm, *, stopped, progress, note, steer):
        self.world, self.progress, self.note, self.steer = world, progress, note, steer

    def run(self, goal, mode="do", point=False):
        self.steer.mode = "guide" if point else mode
        self.ran = (goal, mode, point)
        return FakeLoop.body(self)


class FakeTimer:
    made: list = []

    def __init__(self, delay, function):
        self.delay, self.function = delay, function
        self.started = self.cancelled = False
        FakeTimer.made.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True


@pytest.fixture
def hands(monkeypatch):
    FakeWorld.made, FakeTimer.made = [], []
    monkeypatch.setattr(hands_, "WindowWorld", FakeWorld)
    monkeypatch.setattr(task_, "Loop", FakeLoop)
    monkeypatch.setattr(hands_.native, "com_init", lambda: None)
    monkeypatch.setattr(hands_.threading, "Timer", FakeTimer)
    published = []
    h = WindowsHands(lambda: None, focus=object(), nova=object(), pointer=object(), publish=published.append)
    h.picker = lambda *args: None
    return h, published


def task(h, mode="do", point=False, progressed=None):
    return h.task("turn on word wrap", mode, point=point, confirm=lambda q: True, stopped=lambda: False,
                  progress=lambda label, n: (progressed if progressed is not None else []).append((label, n)))


def test_a_guided_task_is_steered_while_it_runs(hands):
    h, published = hands
    seen = {}

    def body(loop):
        loop.progress("Click “View”", 1)
        h.steer_to("next")
        h.steer_to("where")
        seen["poke"] = loop.steer.poke
        h.steer_to("do")
        loop.progress("Click “Word wrap”", 2)
        return DONE

    FakeLoop.body = staticmethod(body)
    progressed = []
    assert task(h, "guide", progressed=progressed) == DONE
    assert seen["poke"] == "where"
    assert progressed == [("Click “View”", 1), ("Click “Word wrap”", 2)]
    assert published == [TaskRunning("guide"), Guiding(True, "1 · Click “View”"), TaskRunning("do"),
                         Guiding(False), TaskRunning("")]
    assert FakeWorld.made[0].publish == h._publish and FakeWorld.made[0].hidden == 1


def test_let_me_do_it_turns_a_running_task_into_a_guide(hands):
    h, published = hands

    def body(loop):
        h.steer_to("guide")
        loop.progress("Click “View”", 1)
        return DONE

    FakeLoop.body = staticmethod(body)
    task(h)
    assert published[:3] == [TaskRunning("do"), TaskRunning("guide"), Guiding(True, "1 · Click “View”")]


def test_a_steer_with_no_task_running_does_nothing(hands):
    h, published = hands
    FakeLoop.body = staticmethod(lambda loop: DONE)
    task(h)
    for what in ("do", "guide", "next", "where"):
        h.steer_to(what)
    assert published == [TaskRunning("do"), TaskRunning("")]


def test_the_task_ends_cleanly_when_the_loop_raises(hands):
    h, published = hands

    def body(loop):
        loop.progress("Click “View”", 1)
        raise RuntimeError("bug")

    FakeLoop.body = staticmethod(body)
    with pytest.raises(RuntimeError):
        task(h, "guide")
    assert published[-2:] == [Guiding(False), TaskRunning("")] and FakeWorld.made[0].hidden == 1
    h.steer_to("do")  # nothing left to steer


def test_where_is_leaves_the_ring_up_a_while(hands):
    h, published = hands
    FakeLoop.body = staticmethod(lambda loop: task_.Result("done", "There: “Word wrap”"))
    task(h, point=True)
    world, timer = FakeWorld.made[0], FakeTimer.made[0]
    assert published[0] == TaskRunning("guide") and world.hidden == 0
    assert timer.started and timer.delay == hands_.POINT_S and timer.function == world.hide


def test_the_next_task_takes_a_pointed_ring_down_first(hands):
    h, _ = hands
    FakeLoop.body = staticmethod(lambda loop: DONE)
    task(h, point=True)
    task(h)
    first, timer = FakeWorld.made[0], FakeTimer.made[0]
    assert timer.cancelled and first.hidden == 1 and len(FakeTimer.made) == 1


def test_tasks_need_the_model(hands):
    h, _ = hands
    h.picker = None
    with pytest.raises(hands_.ActionError):
        task(h, "guide")
