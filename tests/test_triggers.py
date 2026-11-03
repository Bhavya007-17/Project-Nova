"""The controller thread with fakes: no hook, no mic, no COM."""

import queue
import time

import numpy as np
import pytest

from nova.bus import Bus
from nova.config import Settings, Triggers as TriggerSettings
from nova.events import (
    AlwaysListeningChanged,
    ConfigChanged,
    DictationChanged,
    FinalTranscript,
    Gated,
    PartialTranscript,
    PowerChanged,
    PreloadHint,
    SpeechEnded,
    SpeechStarted,
    StateChanged,
)
from nova.keys import TriggerEvent, TriggerKeys
from nova.triggers import Triggers, stub_brain


class FakeSpeech:
    def __init__(self):
        self.calls = queue.Queue()

    def end_now(self):
        self.calls.put(("end_now",))

    def cancel(self):
        self.calls.put(("cancel",))

    def set_held(self, held):
        self.calls.put(("held", held))

    def set_attentive(self, on):
        self.calls.put(("attentive", on))

    def drain(self, wait=0.2):
        out, deadline = [], time.monotonic() + wait
        while time.monotonic() < deadline:
            try:
                out.append(self.calls.get(timeout=0.02))
            except queue.Empty:
                pass
        return out


class FakeDetector:
    def __init__(self, recording=(), peak=0.0):
        self.recording, self.peak = list(recording), peak

    def recording_apps(self):
        return list(self.recording)

    def refresh_speakers(self):
        pass

    def speaker_peak(self):
        return self.peak

    def close(self):
        pass


class Rig:
    def __init__(self, settings=None, detector=None, names=("Spotify", "Google Chrome")):
        self.bus = Bus()
        self.events = queue.Queue()
        self.bus.subscribe(object, self.events.put)
        self.speech = FakeSpeech()
        self.keys = TriggerKeys()
        self.detector = detector or FakeDetector()
        self.triggers = Triggers(self.bus, self.speech, settings or Settings(), keys=self.keys,
                                 detector=lambda: self.detector, names=names)
        self.triggers.start()
        self.utterance = 0

    def next(self, kind, timeout=2.0, where=lambda e: True):
        deadline = time.monotonic() + timeout
        while True:
            event = self.events.get(timeout=max(0.01, deadline - time.monotonic()))
            if isinstance(event, kind) and where(event):
                return event

    def key(self, down, source="copilot"):
        self.triggers.key_event(TriggerEvent(source, down, time.perf_counter()))

    def say(self, text, reason="silence", talk_s=0.15):
        self.utterance += 1
        n = self.utterance
        self.bus.publish(SpeechStarted(n, time.perf_counter()))
        time.sleep(talk_s)
        self.bus.publish(SpeechEnded(n, reason, np.zeros(1600, np.float32), 0, 1600, 1600, time.perf_counter()))
        self.bus.publish(FinalTranscript(n, text, text, reason, 100.0, 200.0, 1000.0))
        return self.next(Gated, where=lambda e: e.utterance == n)

    def close(self):
        self.triggers.close()


@pytest.fixture
def rig():
    made = []

    def factory(**kw):
        made.append(Rig(**kw))
        return made[-1]

    yield factory
    for r in made:
        r.close()


def test_hold_drives_the_speech_worker_and_the_key_session_is_accepted(rig):
    r = rig()
    r.key(True)
    assert r.next(StateChanged).new == "listening"
    assert r.next(PreloadHint).reason == "key"
    assert {("held", True), ("attentive", True)} <= set(r.speech.drain())
    assert r.keys.esc_armed
    r.bus.publish(SpeechStarted(1, time.perf_counter()))
    time.sleep(0.3)
    r.key(False)
    assert ("end_now",) in r.speech.drain()
    r.utterance = 1
    r.bus.publish(SpeechEnded(1, "key", np.zeros(16, np.float32), 0, 16, 16, time.perf_counter()))
    r.bus.publish(FinalTranscript(1, "I'm gonna open a restaurant someday", "x", "key", 1.0, 1.0, 1.0))
    gated = r.next(Gated)
    assert gated.accepted and gated.via == "key"


def test_another_app_on_the_mic_needs_the_name(rig):
    r = rig(detector=FakeDetector(recording=["Teams.exe"]))
    gated = r.say("open spotify")
    assert not gated.accepted and gated.via == "mic-busy" and "Teams.exe" in gated.why
    assert r.say("Nova, open spotify").accepted


def test_speakers_playing_need_the_name(rig):
    r = rig(detector=FakeDetector(peak=0.3))
    gated = r.say("open spotify", talk_s=0.35)
    assert not gated.accepted and gated.via == "speakers"


def test_always_listening_command_and_chatter(rig):
    r = rig()
    assert r.say("open spotify").accepted
    assert not r.say("we should switch to the other lab").accepted


def test_wake_name_in_live_words_starts_listening(rig):
    r = rig()
    r.bus.publish(SpeechStarted(1, time.perf_counter()))
    r.bus.publish(PartialTranscript(1, "Nova, open"))
    assert r.next(StateChanged, where=lambda e: e.new == "listening").trigger == "named"
    assert r.next(PreloadHint, where=lambda e: e.reason == "wake")


def test_power_off_gives_the_keys_back_to_windows(rig):
    r = rig()
    r.bus.publish(PowerChanged(False))
    assert r.next(StateChanged).new == "off"
    time.sleep(0.05)
    assert r.keys._config.copilot is False and r.keys._config.chord is None
    r.bus.publish(PowerChanged(True))
    r.next(StateChanged, where=lambda e: e.new == "idle")
    time.sleep(0.05)
    assert r.keys._config.copilot is True and r.keys._config.chord is not None


def test_tray_toggle_holds_until_the_file_changes_that_setting(rig):
    r = rig()
    r.bus.publish(AlwaysListeningChanged(False))
    assert r.say("open spotify").via == "off"
    r.bus.publish(ConfigChanged(Settings(wake_name="Nova")))  # unrelated save: the toggle stays
    assert r.say("open spotify").via == "off"
    r.bus.publish(ConfigChanged(Settings(triggers=TriggerSettings(always_listening=False))))
    r.bus.publish(ConfigChanged(Settings()))  # the file turned it back on
    assert r.say("open spotify").accepted


def test_stub_brain_finishes_commands_and_opens_the_follow_up_window(rig):
    r = rig()
    stub_brain(r.bus)
    assert r.say("Nova, open Chrome").accepted
    assert r.next(StateChanged, where=lambda e: e.new == "done")
    assert r.say("now close Spotify").via == "follow-up"


def test_double_tap_requests_dictation(rig):
    r = rig()
    for down in (True, False, True):
        r.key(down)
        time.sleep(0.05)
    changed = r.next(DictationChanged)
    assert changed.on and changed.why == "double-tap"


def test_names_arrive_later(rig):
    r = rig(names=())
    assert r.say("open spotify").via == "unresolved"
    r.triggers.set_names(["Spotify"])
    assert r.say("open spotify").accepted
