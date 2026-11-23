"""The controller thread with fakes: no hook, no mic, no COM."""

import queue
import time

import numpy as np
import pytest

from nova.bus import Bus
from nova.config import Settings, Triggers as TriggerSettings
from nova.events import (
    AlwaysListeningChanged,
    AnswerHeard,
    BrainProgress,
    CommandFinished,
    ConfigChanged,
    DictationChanged,
    FinalTranscript,
    Gated,
    IslandAction,
    PartialTranscript,
    PinToggled,
    PowerChanged,
    PreloadHint,
    SessionCancelled,
    SpeechEnded,
    SpeechStarted,
    StateChanged,
)
from catalog_fixture import CATALOG

from nova.brain.tier0 import Tier0Resolver
from nova.gate import Resolved
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

    def set_dictating(self, on):
        self.calls.put(("dictating", on))

    def set_talking(self, on):
        self.calls.put(("talking", on))

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


RESOLVER = Tier0Resolver(CATALOG)


class Rig:
    def __init__(self, settings=None, detector=None, resolver=RESOLVER, clock=time.perf_counter, running=None):
        self.bus = Bus()
        self.events = queue.Queue()
        self.bus.subscribe(object, self.events.put)
        self.speech = FakeSpeech()
        self.keys = TriggerKeys()
        self.detector = detector or FakeDetector()
        self.triggers = Triggers(self.bus, self.speech, settings or Settings(), keys=self.keys,
                                 detector=lambda: self.detector, clock=clock, running=running)
        if resolver is not None:
            self.triggers.set_resolver(resolver)  # queued: handled before anything said
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


def test_pause_works_over_playing_speakers(rig):
    r = rig(detector=FakeDetector(peak=0.3))
    gated = r.say("pause the music", talk_s=0.35)
    assert gated.accepted and gated.via == "always" and gated.safe_only


def test_a_bare_next_over_playing_speakers_skips_the_track(rig):
    r = rig(detector=FakeDetector(peak=0.3))
    gated = r.say("next", talk_s=0.35)
    assert gated.accepted and gated.via == "always" and gated.text == "next track" and gated.heard == "next"


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
    finished = []
    r.bus.subscribe(CommandFinished, finished.append)
    assert r.say("open spotify").accepted  # outside a session ("Nova, ..." opens one: no follow-up window)
    assert [f.detail for f in finished] == ["open spotify"]  # the island shows ✓ + the command
    assert r.next(StateChanged, where=lambda e: e.new == "done")
    assert r.say("now close Spotify").via == "follow-up"


def test_double_tap_requests_dictation(rig):
    r = rig()
    for down in (True, False, True):
        r.key(down)
        time.sleep(0.05)
    changed = r.next(DictationChanged)
    assert changed.on and changed.why == "double-tap"
    assert ("dictating", True) in r.speech.drain()  # speech is cut on dictation pauses now


def test_the_catalog_arrives_later(rig):
    r = rig(resolver=None)  # before the catalog is read nothing resolves
    assert r.triggers.known_names == 0
    assert r.say("open spotify").via == "unresolved"
    r.triggers.set_resolver(RESOLVER)
    assert r.say("open spotify").via == "always"
    assert r.triggers.known_names == RESOLVER.count


class RunningResolver:
    """Resolves "open <app>" only while that app is in the snapshot it was handed."""

    count = 1

    def __init__(self, running=frozenset()):
        self.running = frozenset(running)

    def seeing(self, running):
        return RunningResolver(running)

    def resolve(self, words, *, media=False):
        app = " ".join(words[1:])
        return Resolved("open", app, safe=True) if words[:1] == ("open",) and app in self.running else None


def test_each_final_is_judged_against_a_fresh_running_apps_snapshot(rig):
    snapshots = [frozenset(), frozenset({"zorblax"})]
    calls = []

    def running():
        calls.append(1)
        return snapshots[min(len(calls), len(snapshots)) - 1]

    r = rig(resolver=RunningResolver(), running=running)
    assert r.say("open zorblax").via == "unresolved"
    assert r.say("open zorblax").accepted
    assert len(calls) == 2  # one snapshot per final


def test_no_snapshot_is_taken_for_a_resolver_that_cannot_use_one(rig):
    calls = []
    r = rig(resolver=None, running=lambda: calls.append(1) or ())
    assert r.say("open spotify").via == "unresolved"
    assert calls == []


def test_a_running_apps_provider_that_raises_never_breaks_the_gate(rig, caplog):
    def broken():
        raise OSError("no windows today")

    r = rig(running=broken)
    with caplog.at_level("WARNING", logger="nova.triggers"):
        assert not r.say("we should switch to the other lab").accepted
        assert r.say("open zorblax").via == "unresolved"
        assert r.say("open spotify").via == "always"  # resolved against an empty snapshot
    warnings = [rec for rec in caplog.records if rec.name == "nova.triggers" and rec.levelname == "WARNING"]
    assert len(warnings) == 1  # logged once, not per final


def asking(r, kind):
    assert r.say("open spotify").accepted  # outside a session: after a No, Esc is disarmed again
    r.bus.publish(BrainProgress(kind))
    r.next(StateChanged, where=lambda e: e.new == kind)
    deadline = time.monotonic() + 1.0
    while r.keys.asking != kind and time.monotonic() < deadline:
        time.sleep(0.01)
    assert r.keys.asking == kind


def test_enter_confirms_through_the_hook_while_the_island_asks(rig):
    r = rig()
    asking(r, "confirm")
    r.triggers.key_event(TriggerEvent("answer", True, time.perf_counter(), "enter"))
    action = r.next(IslandAction)
    assert (action.what, action.via) == ("yes", "key")


def speak(r, n, text, talk_s):
    r.bus.publish(SpeechStarted(n, time.perf_counter()))
    time.sleep(talk_s)
    r.bus.publish(SpeechEnded(n, "silence", np.zeros(1600, np.float32), 0, 1600, 1600, time.perf_counter()))
    r.bus.publish(FinalTranscript(n, text, text, "silence", 100.0, 200.0, 1000.0))


def test_a_tiny_yes_does_not_answer_but_enter_and_a_real_yes_do(rig):
    r = rig()
    asking(r, "confirm")
    speak(r, 2, "Yeah.", talk_s=0.1)  # a 100 ms clip: speech-to-text invents these
    gated = r.next(Gated, where=lambda e: e.utterance == 2)
    assert not gated.accepted and gated.via == "answer-short"
    assert r.keys.asking == "confirm"  # still asking
    r.triggers.key_event(TriggerEvent("answer", True, time.perf_counter(), "enter"))
    assert r.next(IslandAction).what == "yes"
    speak(r, 3, "Yes.", talk_s=0.5)
    assert r.next(AnswerHeard).text == "Yes."


def test_a_command_said_while_busy_reaches_the_brain_when_the_first_is_done(rig):
    r = rig()
    assert r.say("Nova, open Chrome").accepted
    speak(r, 2, "open spotify", talk_s=0.2)
    time.sleep(0.2)
    waiting = [e for e in r.events.queue if isinstance(e, Gated) and e.utterance == 2]
    assert waiting == [] and r.triggers.machine.queue  # held back: the brain is busy with the first one
    r.bus.publish(CommandFinished(True, "Opened Chrome"))
    gated = r.next(Gated, where=lambda e: e.utterance == 2)
    assert gated.accepted and gated.text == "open spotify"
    assert r.triggers.machine.state.value == "thinking"


def test_stop_while_busy_cancels_the_running_command(rig):
    r = rig()
    assert r.say("Nova, open Chrome").accepted
    speak(r, 2, "open spotify", talk_s=0.2)
    speak(r, 3, "Stop.", talk_s=0.2)
    assert r.next(StateChanged, where=lambda e: e.new == "idle").why == "stopped"
    assert r.next(SessionCancelled).why == "stop"  # the brain aborts on this
    r.bus.publish(CommandFinished(True, "Opened Chrome"))
    time.sleep(0.2)
    assert not [e for e in r.events.queue if isinstance(e, Gated) and e.utterance == 2 and e.accepted]


def test_a_command_that_waited_too_long_is_skipped_with_a_note(rig):
    now = [100.0]
    r = rig(clock=lambda: now[0])
    r.bus.publish(FinalTranscript(1, "open notepad", "x", "silence", 1.0, 1.0, 1.0))  # outside a session (07b)
    assert r.next(Gated).accepted
    r.bus.publish(BrainProgress("acting"))
    r.bus.publish(FinalTranscript(2, "open spotify", "x", "silence", 1.0, 1.0, 1.0))
    time.sleep(0.2)
    now[0] += 11.0  # the first command took 11 s
    r.bus.publish(CommandFinished(True, "Opened Chrome"))
    note = r.next(CommandFinished, where=lambda e: not e.ok)
    assert "open spotify" in note.detail and "waited too long" in note.detail
    time.sleep(0.2)
    assert r.triggers.machine.state.value == "done"  # the note is for the island: it is no result
    assert not [e for e in r.events.queue if isinstance(e, Gated) and e.utterance == 2 and e.accepted]


def test_number_key_picks_a_choice(rig):
    r = rig()
    asking(r, "clarify")
    r.triggers.key_event(TriggerEvent("answer", True, time.perf_counter(), "2"))
    action = r.next(IslandAction)
    assert (action.what, action.index) == ("choice", 1)


def test_no_click_cancels_and_disarms(rig):
    r = rig()
    asking(r, "confirm")
    r.bus.publish(IslandAction("no", time.perf_counter()))
    assert r.next(StateChanged, where=lambda e: e.new == "idle")
    assert r.next(SessionCancelled).why == "No"
    deadline = time.monotonic() + 1.0
    while r.keys.asking is not None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert r.keys.asking is None and not r.keys.esc_armed


def test_brain_progress_to_an_unknown_state_is_ignored(rig):
    r = rig()
    assert r.say("Nova, do the thing").accepted
    r.bus.publish(BrainProgress("dancing"))
    r.bus.publish(BrainProgress("acting"))
    assert r.next(StateChanged, where=lambda e: e.new != "thinking").new == "acting"


def test_ctrl_copilot_toggles_the_pin_and_leaves_the_state_alone(rig):
    r = rig()
    r.triggers.key_event(TriggerEvent("pin", True, time.perf_counter()))
    assert isinstance(r.next(PinToggled), PinToggled)
    r.triggers.key_event(TriggerEvent("pin", False, time.perf_counter()))
    assert r.triggers.machine.state.value == "idle"


def test_warming_moves_the_thinking_deadline_and_thinking_brings_it_back(rig, caplog):
    from nova.events import WARMING
    from nova.state import HEARD_WAIT_S, WARM_WAIT_S

    now = [100.0]
    r = rig(clock=lambda: now[0])
    r.bus.publish(FinalTranscript(1, "Nova, open Chrome", "x", "silence", 1.0, 1.0, 1.0))
    assert r.next(Gated).accepted

    def deadline_is(value):
        limit = time.monotonic() + 2.0
        while r.triggers.machine.deadline != value and time.monotonic() < limit:
            time.sleep(0.01)
        return r.triggers.machine.deadline == value

    assert deadline_is(100.0 + HEARD_WAIT_S)
    now[0] = 101.0
    with caplog.at_level("WARNING", logger="nova.triggers"):
        r.bus.publish(BrainProgress("warming", WARMING))  # the model is cold: the load comes first
        assert deadline_is(101.0 + WARM_WAIT_S)
    assert not [rec for rec in caplog.records if rec.name == "nova.triggers" and rec.levelname == "WARNING"]
    now[0] = 100.0 + HEARD_WAIT_S + 5.0  # past the old deadline: still thinking
    r.bus.publish(BrainProgress("thinking"))  # loaded: the answer has the usual 10 s
    assert deadline_is(now[0] + HEARD_WAIT_S)
    assert r.triggers.machine.state.value == "thinking"
    assert not [e for e in r.events.queue if isinstance(e, SessionCancelled)]


# --- talk sessions (Stage 07b) ---------------------------------------------------------------------------------

from nova.config import SessionSettings  # noqa: E402
from nova.events import ClausesSkipped, TalkChanged, TalkEndRequested, TaskRunning, TaskSteered  # noqa: E402


def tap(r):
    r.key(True)
    r.key(False)  # well under HOLD_S: a tap
    return r.next(TalkChanged, where=lambda e: e.what == "opened")


def eventually(check, timeout=1.0):
    deadline = time.monotonic() + timeout
    while not check() and time.monotonic() < deadline:
        time.sleep(0.01)
    return check()


def test_a_tap_switches_the_speech_worker_to_talk_rules(rig):
    r = rig()
    tap(r)
    calls = r.speech.drain()
    assert ("talking", True) in calls and ("attentive", True) in calls
    assert eventually(lambda: r.keys.esc_armed)
    r.triggers.key_event(TriggerEvent("esc", True, time.perf_counter()))
    assert r.next(TalkChanged, where=lambda e: e.what == "closed").why == "Esc"
    assert ("talking", False) in r.speech.drain()
    assert eventually(lambda: not r.keys.esc_armed)


def test_talk_changes_reach_the_bus(rig):
    r = rig()
    opened = tap(r)
    assert opened.why == "copilot" and opened.closes_at is not None and opened.closes_at > opened.at
    speak(r, 1, "open notepad", talk_s=0.15)
    assert r.next(TalkChanged, where=lambda e: e.what == "paused").why == "running"  # before the clause's Gated
    gated = r.next(Gated, where=lambda e: e.utterance == 1)
    assert gated.accepted and gated.talk and gated.via == "talk"
    r.bus.publish(CommandFinished(True, "Opened Notepad"))
    resumed = r.next(TalkChanged, where=lambda e: e.what == "resumed")
    assert resumed.why == "done" and resumed.closes_at is not None


def test_talk_end_requested_closes_the_session(rig):
    r = rig()
    tap(r)
    r.bus.publish(TalkEndRequested())
    assert r.next(TalkChanged, where=lambda e: e.what == "closed").why == "phrase"
    assert eventually(lambda: r.triggers.machine.talk is None)


def test_stop_button_closes_the_session(rig):
    r = rig()
    tap(r)
    r.bus.publish(IslandAction("stop-session", time.perf_counter()))
    assert r.next(TalkChanged, where=lambda e: e.what == "closed").why == "stop button"


def test_the_guides_stop_button_cancels_the_command(rig):
    r = rig()
    assert r.say("Nova, show me how to turn on word wrap").accepted
    r.bus.publish(BrainProgress("acting"))
    r.next(StateChanged, where=lambda e: e.new == "acting")
    r.bus.publish(IslandAction("stop", time.perf_counter()))
    assert r.next(StateChanged, where=lambda e: e.new == "idle")
    assert r.next(SessionCancelled).why == "Stop"


def test_a_steer_reaches_the_running_task_and_the_command_keeps_going(rig):
    r = rig()
    assert r.say("Nova, show me how to turn on word wrap").accepted
    r.bus.publish(BrainProgress("acting"))
    r.next(StateChanged, where=lambda e: e.new == "acting")
    r.bus.publish(TaskRunning("guide"))
    steer = r.say("Nova, next")
    assert not steer.accepted and steer.via == "steer"
    assert r.next(TaskSteered).what == "next"
    assert r.triggers.machine.state.value == "acting"
    r.bus.publish(TaskRunning(""))
    assert r.say("Nova, where is it").via != "steer"  # the task ended: no steering


def test_session_idle_s_follows_settings(rig):
    r = rig(settings=Settings(session=SessionSettings(idle_s=12.0)))
    assert r.triggers.machine.idle_s == 12.0
    opened = tap(r)
    assert opened.closes_at - opened.at == pytest.approx(12.0, abs=0.5)
    r.bus.publish(ConfigChanged(Settings(session=SessionSettings(idle_s=45.0))))
    assert eventually(lambda: r.triggers.machine.idle_s == 45.0)


def test_skipped_clauses_reach_the_bus(rig):
    r = rig()
    tap(r)
    assert r.say("open notepad and then create a new note").accepted
    r.bus.publish(CommandFinished(False, "Couldn't find Notepad"))
    skipped = r.next(ClausesSkipped)
    assert skipped.texts == ("create a new note",) and skipped.why == "the step before it didn't finish"


def test_gated_carries_clause_and_wait(rig):
    r = rig()
    tap(r)
    first = r.say("open notepad and then create a new note")
    assert (first.talk, first.clause, first.request, first.waited) == (True, 0, True, None)
    assert first.text == "open notepad" and first.heard == "open notepad and then create a new note"
    r.bus.publish(CommandFinished(True, "Opened Notepad"))
    second = r.next(Gated, where=lambda e: e.clause == 1)
    assert second.accepted and second.text == "create a new note" and second.waited is not None
    assert second.waited >= 0.0
    small = r.say("great, okay")
    assert not small.accepted and small.via == "small-talk" and small.talk


def test_no_preload_hint_on_speech_inside_a_session(rig):
    r = rig()
    tap(r)
    r.bus.publish(CommandFinished(True, "x"))  # nothing runs: no effect
    time.sleep(0.05)
    while not r.events.empty():
        r.events.get()
    r.say("great")
    time.sleep(0.1)
    assert not [e for e in r.events.queue if isinstance(e, PreloadHint)]


def test_esc_hides_the_numbers_before_anything_else(rig):
    from nova.events import MarksHidden, MarksHide, MarksShown

    r = rig()
    r.bus.publish(MarksShown("DISPLAY1", (0, 0, 100, 100), ((1, (0, 0, 10, 10)),)))
    assert eventually(lambda: r.keys.marks_up)
    r.triggers.key_event(TriggerEvent("esc", True, time.perf_counter()))
    assert r.next(MarksHide).why == "esc"
    r.bus.publish(MarksHidden("esc"))
    assert eventually(lambda: not r.keys.marks_up)
