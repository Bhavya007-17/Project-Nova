"""The island's Qt side without a real window: placement, the bridge, the demo script, and the QML
loaded offscreen and driven through every demo event (catches broken bindings and names)."""

import os

import pytest

from nova.bus import Bus
from nova.events import JobChip, StateChanged
from nova.island import demo
from nova.island.bridge import IslandBridge
from nova.island.view import View, reduce
from nova.island.window import QML, REACH, clamp_home, top_center


@pytest.fixture(scope="module")
def qt_app():
    from PySide6.QtGui import QGuiApplication

    app = QGuiApplication.instance()
    if app is None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # no real window, ever
        app = QGuiApplication(["nova-tests"])
    return app


def demo_events():
    """Every event the demo publishes, with Go turned into StateChanged as the runner does."""
    state = "idle"
    for scene in demo.SCENES:
        for beat in scene.beats:
            for event in beat.events:
                if isinstance(event, demo.Go):
                    yield StateChanged(state, event.state, event.why or event.state, "copilot", 0.0)
                    state = event.state
                elif not isinstance(event, demo.Talk):
                    yield event


def test_top_center_on_any_monitor():
    assert top_center((0, 0, 1440, 900), (1040, 220)) == (200, 0, 1040, 220)
    assert top_center((-1920, 40, 1920, 1040), (1040, 220)) == (-1480, 40, 1040, 220)  # left monitor, taskbar on top
    assert top_center((0, 0, 800, 600), (1040, 220)) == (0, 0, 800, 220)  # never wider than the screen


def test_demo_shows_every_state_and_ends_hidden():
    events = list(demo_events())
    shown = {event.new for event in events if isinstance(event, StateChanged)}
    assert {"listening", "thinking", "acting", "done", "error", "confirm", "clarify", "teaching", "dictating",
            "passthrough", "idle"} <= shown
    assert any(isinstance(event, JobChip) for event in events)
    view = View()
    for event in events:
        view = reduce(view, event)
    assert view.state == "hidden" and view.chips == ()
    assert demo.NAMES == tuple(scene.name for scene in demo.SCENES)


def test_runner_plays_a_scene_on_the_bus():
    bus = Bus()
    seen = []
    bus.subscribe(object, seen.append)
    scene = demo.Scene("t", "test", (demo.Beat(0.0, (demo.Go("listening"),)), demo.Beat(0.01, (demo.Go("idle"),))))
    runner = demo.Runner(bus, (scene,), loop=False, say=lambda line: None)
    runner.start()
    assert runner.finished.wait(2.0)
    runner.stop()
    moves = [(e.old, e.new) for e in seen if isinstance(e, StateChanged)]
    assert moves == [("idle", "listening"), ("listening", "idle")]


def test_bridge_notifies_only_what_changed(qt_app):
    bridge = IslandBridge()
    fired = []
    for name in ("state", "words", "chips", "accent", "open", "interactive"):
        getattr(bridge, f"{name}Changed").connect(lambda name=name: fired.append(name))
    view = reduce(View(), StateChanged("idle", "listening", "key", "copilot", 0.0))
    bridge.apply(view)
    assert set(fired) == {"state", "open"}  # listening's accent is the hidden one's
    fired.clear()
    bridge.apply(reduce(view, JobChip("a", "Claude · HW 5")))
    assert set(fired) == {"chips", "interactive"}
    assert bridge.chips == [{"id": "a", "text": "Claude · HW 5", "status": "working"}]
    fired.clear()
    bridge.apply(bridge.view)
    assert fired == []


def test_bridge_passes_clicks_and_hit_areas_on(qt_app):
    from PySide6.QtCore import QRectF

    clicks, areas, collapsed = [], [], []
    bridge = IslandBridge(lambda *a: clicks.append(a), lambda: collapsed.append(1), lambda r, s: areas.append((r, s)))
    bridge.act("choice", 1, "")
    bridge.setHitArea([QRectF(1, 2, 3, 4)], True)
    bridge.collapsed()
    assert clicks == [("choice", 1, "")] and areas == [([QRectF(1, 2, 3, 4)], True)] and collapsed == [1]


def test_qml_binds_cleanly_through_every_demo_event(qt_app):
    from PySide6.QtCore import QCoreApplication, QUrl, qInstallMessageHandler
    from PySide6.QtQml import QQmlComponent, QQmlEngine

    messages = []
    previous = qInstallMessageHandler(lambda mode, context, message: messages.append(message))
    try:
        engine = QQmlEngine()
        bridge = IslandBridge()
        component = QQmlComponent(engine, QUrl.fromLocalFile(str(QML)))
        root = component.createWithInitialProperties({"island": bridge})
        assert root is not None, component.errorString()
        view = View()
        for event in demo_events():
            view = reduce(view, event)
            bridge.apply(view)
            bridge.set_level(0.5)
            QCoreApplication.processEvents()
        assert root.property("anything") is False
        del root
    finally:
        qInstallMessageHandler(previous)
    assert [m for m in messages if ".qml" in m] == []  # (the offscreen platform has no fonts: ignore that)


def test_dragged_home_keeps_the_pill_reachable_and_room_to_expand():
    area, size = (0, 0, 1440, 900), (1040, 220)
    assert clamp_home((300, 200), area, size) == (300, 200)  # fine where it is
    assert clamp_home((-900, 200), area, size) == (REACH - 520, 200)  # pill centre kept on screen
    assert clamp_home((1400, 200), area, size) == (1440 - REACH - 520, 200)
    assert clamp_home((300, 850), area, size) == (300, 900 - 220)  # room below for confirm / clarify
    assert clamp_home((300, -50), area, size) == (300, 0)
    assert clamp_home((-1500, 100), (-1920, 0, 1920, 1080), size) == (-1500, 100)  # a left-hand monitor
