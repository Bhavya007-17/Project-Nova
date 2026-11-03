"""Nova's state machine (SPEC §5): key gestures + speech + finals -> states and effects."""

from nova.gate import Env, StubResolver, WakeName
from nova.keys import Cancel, DoubleTap, Press, Release
from nova.state import (
    CLAIM_S,
    DONE_S,
    FOLLOW_UP_S,
    TAP_WAIT_S,
    WAKE_WAIT_S,
    Answer,
    Cancelled,
    Command,
    Dictated,
    Dictation,
    Done,
    DropUtterance,
    EndUtterance,
    Heard,
    Ignored,
    Machine,
    PassthroughEnded,
    Power,
    Preload,
    Progress,
    Sleep,
    SpeechEnd,
    SpeechStart,
    State,
    Tick,
    Transition,
    WakeHeard,
    next_deadline,
    outputs,
    step,
)

ENV = Env(WakeName.of("Nova"), True, StubResolver(["Google Chrome", "Spotify", "Notepad"]))


class Run:
    """Feeds inputs in order and keeps the effects of the last step."""

    def __init__(self, machine=None):
        self.m = machine or Machine()
        self.effects = ()
        self.all = []

    def __call__(self, *events):
        for event in events:
            result = step(self.m, event)
            self.m, self.effects = result.machine, result.effects
            self.all.extend(result.effects)
        return self

    @property
    def state(self):
        return self.m.state

    def has(self, kind):
        return [e for e in self.effects if isinstance(e, kind)]


def heard(utterance, text, at):
    return Heard(utterance, text, at, ENV)


def transitions(run):
    return [(t.old, t.new) for t in run.all if isinstance(t, Transition)]


# --- power -------------------------------------------------------------------------------------------


def test_starts_idle_and_power_toggles():
    run = Run()
    assert run.state is State.IDLE
    run(Power(False, 1.0))
    assert run.state is State.OFF and run.has(Transition)[0].why == "Nova is off"
    run(Press("copilot", 2.0), heard(1, "Nova, open Chrome", 2.5))
    assert run.state is State.OFF and run.effects == ()
    run(Power(True, 3.0))
    assert run.state is State.IDLE


def test_off_while_listening_ends_the_session():
    run = Run()(Press("copilot", 1.0), Power(False, 1.5))
    assert run.state is State.OFF and run.m.session is None
    assert not outputs(run.m).held


# --- Copilot key: hold ---------------------------------------------------------------------------------


def test_hold_talk_release_is_accepted():
    run = Run()(Press("copilot", 1.0))
    assert run.state is State.LISTENING and run.has(Preload)[0].reason == "key"
    assert outputs(run.m).held and outputs(run.m).attentive and outputs(run.m).esc
    run(SpeechStart(1, 1.2), Release("copilot", 2.5, 1.5, hold=True))
    assert run.has(EndUtterance) and not outputs(run.m).held
    run(SpeechEnd(1, "key", 2.5))
    assert run.state is State.THINKING
    run(heard(1, "I'm gonna open a restaurant someday", 2.7))
    command = run.has(Command)[0]
    assert command.decision.accepted and command.decision.via == "key" and command.utterance == 1
    assert run.state is State.THINKING
    run(Done(True, 2.9))
    assert run.state is State.DONE and run.m.follow_up_until == 2.9 + FOLLOW_UP_S
    run(Tick(2.9 + DONE_S))
    assert run.state is State.IDLE


def test_hold_released_before_any_speech_goes_back_to_idle():
    run = Run()(Press("copilot", 1.0), Release("copilot", 1.6, 0.6, hold=True))
    assert run.state is State.IDLE and not run.has(EndUtterance)


def test_speech_that_started_just_before_the_press_belongs_to_the_key():
    run = Run()(SpeechStart(4, 10.0), Press("copilot", 10.0 + CLAIM_S - 0.1))
    assert run.m.session.utterance == 4 and not run.has(DropUtterance)


def test_older_chatter_in_progress_is_dropped_at_the_press():
    run = Run()(SpeechStart(4, 10.0), Press("copilot", 13.0))
    assert run.has(DropUtterance) and run.m.session.utterance is None
    run(SpeechEnd(4, "cancel", 13.0), SpeechStart(5, 13.2))
    assert run.m.session.utterance == 5


# --- Copilot key: tap --------------------------------------------------------------------------------


def test_tap_talk_pause_is_accepted_at_the_endpoint():
    run = Run()(Press("copilot", 1.0), Release("copilot", 1.1, 0.1, hold=False))
    assert run.state is State.LISTENING and not outputs(run.m).held and outputs(run.m).attentive
    assert next_deadline(run.m) == 1.1 + TAP_WAIT_S
    run(SpeechStart(2, 1.5))
    assert run.has(Preload)[0].reason == "speech" and next_deadline(run.m) is None
    run(SpeechEnd(2, "silence", 2.4), heard(2, "open spotify", 2.7))
    assert run.has(Command)[0].decision.text == "open spotify"


def test_tap_then_silence_times_out():
    run = Run()(Press("copilot", 1.0), Release("copilot", 1.1, 0.1, hold=False), Tick(1.1 + TAP_WAIT_S))
    assert run.state is State.IDLE


def test_tap_while_talking_finishes_now():
    run = Run()(Press("chord", 1.0), Release("chord", 1.1, 0.1, hold=False), SpeechStart(3, 1.3))
    run(Press("chord", 3.0))
    assert run.has(EndUtterance) and run.state is State.LISTENING
    run(Release("chord", 3.5, 0.5, hold=True))  # the finishing press's release does nothing more
    assert run.effects == () or not run.has(EndUtterance)
    run(SpeechEnd(3, "key", 3.0))
    assert run.state is State.THINKING


def test_tap_while_listening_to_nothing_stops():
    run = Run()(Press("copilot", 1.0), Release("copilot", 1.1, 0.1, hold=False), Press("copilot", 2.0))
    assert run.state is State.IDLE


def test_new_press_while_thinking_starts_over():
    run = Run()(heard(1, "Nova, open Chrome", 1.0))
    assert run.state is State.THINKING
    run(Press("copilot", 1.2))
    assert run.state is State.LISTENING and run.has(Cancelled)[0].why == "superseded"


# --- double-tap, Esc -----------------------------------------------------------------------------------


def test_double_tap_locks_dictation_until_the_next_tap():
    run = Run()(Press("copilot", 1.0), Release("copilot", 1.1, 0.1, hold=False), DoubleTap("copilot", 1.3))
    assert run.state is State.DICTATING and run.has(Dictation)[0].on and run.has(DropUtterance)
    run(Release("copilot", 1.4, 0.1, hold=False))
    assert run.state is State.DICTATING
    run(SpeechStart(7, 2.0), SpeechEnd(7, "silence", 3.0), heard(7, "dear professor", 3.2))
    assert run.has(Dictated)[0].text == "dear professor"
    run(Press("copilot", 9.0))
    assert run.state is State.IDLE and not run.has(Dictation)[0].on


def test_esc_cancels_listening_and_drops_the_utterance():
    run = Run()(Press("copilot", 1.0), SpeechStart(1, 1.1), Cancel(1.5))
    assert run.state is State.IDLE and run.has(Cancelled)[0].why == "Esc" and run.has(DropUtterance)
    run(SpeechEnd(1, "cancel", 1.5), heard(1, "", 1.6))
    assert run.state is State.IDLE and not run.has(Command)


def test_esc_ends_dictation_and_is_not_armed_when_idle():
    run = Run()(DoubleTap("copilot", 1.0), Cancel(2.0))
    assert run.state is State.IDLE and not run.has(Dictation)[0].on
    assert not outputs(run.m).esc


# --- wake name ----------------------------------------------------------------------------------------


def test_nova_open_chrome_is_accepted_with_the_name_stripped():
    run = Run()(SpeechStart(1, 1.0), SpeechEnd(1, "silence", 2.0), heard(1, "Nova, open Chrome.", 2.3))
    command = run.has(Command)[0]
    assert command.decision.via == "wake" and command.decision.text == "open Chrome."
    assert run.state is State.THINKING


def test_wake_name_in_live_words_shows_listening_early():
    run = Run()(SpeechStart(3, 1.0), WakeHeard(3, 1.4))
    assert run.state is State.LISTENING and run.has(Preload)[0].reason == "wake"
    run(SpeechEnd(3, "silence", 2.0))
    assert run.state is State.THINKING
    run(heard(3, "Nova, open Chrome", 2.2))
    assert run.has(Command)


def test_live_words_wake_that_the_final_does_not_confirm_is_dropped():
    run = Run()(SpeechStart(3, 1.0), WakeHeard(3, 1.4), SpeechEnd(3, "silence", 2.0), heard(3, "Noah, open the door", 2.2))
    assert run.state is State.IDLE and run.has(Ignored)


def test_name_alone_opens_a_session_for_the_next_utterance():
    run = Run()(SpeechStart(1, 1.0), SpeechEnd(1, "silence", 1.4), heard(1, "Nova.", 1.6))
    assert run.state is State.LISTENING and next_deadline(run.m) == 1.6 + WAKE_WAIT_S
    run(SpeechStart(2, 2.0), SpeechEnd(2, "silence", 3.0), heard(2, "I'm gonna open a restaurant", 3.2))
    decision = run.has(Command)[0].decision
    assert decision.via == "wake" and decision.text == "I'm gonna open a restaurant"


def test_name_alone_then_nothing_times_out():
    run = Run()(heard(1, "Nova", 1.0), Tick(1.0 + WAKE_WAIT_S))
    assert run.state is State.IDLE


# --- always-listening, follow-up, busy -------------------------------------------------------------


def test_chatter_is_ignored_and_changes_nothing():
    run = Run()(SpeechStart(1, 1.0), SpeechEnd(1, "silence", 2.0), heard(1, "did you close the door", 2.2))
    assert run.state is State.IDLE and run.has(Ignored)[0].decision.via == "not-command"
    assert transitions(run) == []


def test_always_listening_command():
    run = Run()(heard(1, "open spotify", 1.0))
    decision = run.has(Command)[0].decision
    assert decision.via == "always" and decision.safe_only


def test_follow_up_window_lasts_8_seconds():
    run = Run()(heard(1, "Nova, open Chrome", 1.0), Done(True, 1.2))
    run(heard(2, "now close Spotify", 5.0))
    assert run.has(Command)[0].decision.via == "follow-up"
    run(Done(True, 5.2), Tick(5.2 + FOLLOW_UP_S + 0.1), heard(3, "close Spotify", 14.0))
    assert run.has(Ignored)[0].decision.via == "needs-name"


def test_busy_takes_only_the_name():
    run = Run()(heard(1, "Nova, open Chrome", 1.0))
    run(heard(2, "open spotify", 1.5))
    assert run.has(Ignored)[0].decision.via == "busy"
    run(heard(3, "Nova, pause", 1.8))
    assert run.has(Command) and run.has(Cancelled)[0].why == "superseded"


def test_chatter_during_a_key_session_cannot_take_it_over():
    run = Run()(Press("copilot", 1.0), Release("copilot", 1.1, 0.1, hold=False), heard(9, "open spotify", 1.5))
    assert run.has(Ignored)[0].decision.via == "busy" and run.state is State.LISTENING


def test_empty_final_ends_a_key_session():
    run = Run()(Press("copilot", 1.0), SpeechStart(1, 1.1), Release("copilot", 2.0, 1.0, hold=True),
                SpeechEnd(1, "key", 2.0), heard(1, "", 2.2))
    assert run.state is State.IDLE and run.has(Ignored)[0].decision.via == "empty"


def test_speech_cancelled_under_a_session_ends_it():
    run = Run()(Press("copilot", 1.0), SpeechStart(1, 1.1), SpeechEnd(1, "cancel", 1.5))
    assert run.state is State.IDLE


# --- brain-driven states (later stages) -----------------------------------------------------------


def test_passthrough_ignores_plain_speech_and_ends_on_a_tap():
    run = Run()(heard(1, "Nova, on Claude, turn on voice", 1.0), Progress(State.PASSTHROUGH, 1.5))
    assert run.state is State.PASSTHROUGH and not outputs(run.m).esc
    run(heard(2, "open spotify", 2.0))
    assert run.has(Ignored)[0].decision.via == "passthrough"
    run(heard(3, "Nova, open chrome", 3.0))
    assert run.has(Command)
    run = Run()(heard(1, "Nova, voice", 1.0), Progress(State.PASSTHROUGH, 1.5), Press("copilot", 4.0))
    assert run.state is State.IDLE and run.has(PassthroughEnded)


def test_confirm_takes_answers_and_times_out():
    run = Run()(heard(1, "Nova, submit it", 1.0), Progress(State.CONFIRM, 1.4))
    run(heard(2, "yes", 3.0))
    assert run.has(Answer)[0].text == "yes" and run.state is State.CONFIRM
    run(Tick(1.4 + 15.0))
    assert run.state is State.IDLE and run.has(Cancelled)[0].why == "no answer"


def test_asking_arms_the_answer_keys_and_no_cancels_like_esc():
    run = Run()(heard(1, "Nova, submit it", 1.0))
    assert outputs(run.m).asking is None
    run(Progress(State.CONFIRM, 1.4))
    assert outputs(run.m).asking == "confirm" and outputs(run.m).esc
    run(Cancel(2.0, "No"))
    assert run.state is State.IDLE and run.has(Cancelled)[0].why == "No"
    run = Run()(heard(1, "Nova, open chrome", 1.0), Progress(State.CLARIFY, 1.4))
    assert outputs(run.m).asking == "clarify"


def test_progress_only_moves_along_allowed_paths():
    run = Run()(Progress(State.ACTING, 1.0))
    assert run.state is State.IDLE  # nothing to act on
    run(heard(1, "Nova, open Chrome", 2.0), Progress(State.ACTING, 2.1), Done(False, 2.5, "Chrome didn't open"))
    assert run.state is State.ERROR and run.has(Transition)[0].why == "Chrome didn't open"


def test_sleep_ignores_everything_but_the_name():
    run = Run()(Sleep(True, 1.0), heard(1, "open spotify", 2.0))
    assert run.has(Ignored)[0].decision.via == "asleep"
    run(heard(2, "Nova, wake up", 3.0))
    assert run.has(Command)
    run(Sleep(False, 4.0), Done(True, 4.1), Tick(20.0), heard(3, "open spotify", 21.0))
    assert run.has(Command)


def test_every_change_is_one_transition():
    run = Run()(Press("copilot", 1.0), SpeechStart(1, 1.1), Release("copilot", 2.0, 1.0, hold=True),
                SpeechEnd(1, "key", 2.0), heard(1, "open chrome", 2.2), Done(True, 2.4), Tick(2.4 + DONE_S))
    assert transitions(run) == [
        (State.IDLE, State.LISTENING),
        (State.LISTENING, State.THINKING),
        (State.THINKING, State.DONE),
        (State.DONE, State.IDLE),
    ]
