"""Nova's state machine (SPEC §5): key gestures + speech + finals -> states and effects."""

from dataclasses import replace

import pytest

from catalog_fixture import CATALOG

from nova.brain.tier0 import Tier0Resolver
from nova.gate import Env, WakeName
from nova.keys import Cancel, DoubleTap, Press, Release
from nova.state import (
    CLAIM_S,
    DICTATION_SILENCE_S,
    DONE_S,
    FOLLOW_UP_S,
    HEARD_WAIT_S,
    CONFIRM_S,
    ERROR_S,
    QUEUE_MAX,
    QUEUE_MAX_AGE_S,
    TALK_IDLE_S,
    TALK_QUEUE_MAX,
    TALK_QUEUE_MAX_AGE_S,
    TALK_WARN_S,
    WARM_WAIT_S,
    Answer,
    Cancelled,
    Command,
    Dictated,
    Dictation,
    Done,
    Dropped,
    DropUtterance,
    EndTalk,
    EndUtterance,
    Heard,
    Ignored,
    Machine,
    PassthroughEnded,
    Power,
    Preload,
    Progress,
    Queued,
    Skipped,
    Sleep,
    SpeechEnd,
    SpeechStart,
    State,
    TalkEvent,
    Tick,
    Transition,
    WakeHeard,
    Warming,
    Woke,
    next_deadline,
    outputs,
    spoken,
    step,
)

ENV = Env(WakeName.of("Nova"), True, Tier0Resolver(CATALOG))


def spoken_talk(run, utterance):
    said = spoken(run.m, utterance)
    return said is not None and said.talk


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


# Stage 07b: a tap opens a talk session (the session tests below); these keep the 07a intent of each tap test.


def test_tap_talk_pause_is_accepted_at_the_endpoint():
    run = Run()(Press("copilot", 1.0), Release("copilot", 1.1, 0.1, hold=False))
    assert run.state is State.IDLE and run.m.talk is not None
    assert not outputs(run.m).held and outputs(run.m).attentive
    run(SpeechStart(2, 1.5), SpeechEnd(2, "silence", 2.4), heard(2, "open spotify", 2.7))
    command = run.has(Command)[0]
    assert command.decision.text == "open spotify" and command.decision.via == "talk"


def test_tap_then_silence_times_out():
    run = Run()(Press("copilot", 1.0), Release("copilot", 1.1, 0.1, hold=False), Tick(1.1 + TALK_IDLE_S))
    assert run.state is State.IDLE and run.m.talk is None


def test_tap_while_talking_finishes_now():
    run = Run()(Press("chord", 1.0), Release("chord", 1.1, 0.1, hold=False), SpeechStart(3, 1.3))
    run(Press("chord", 3.0))
    assert not run.effects or not run.has(EndUtterance)  # tap or hold is known only at the release
    run(Release("chord", 3.1, 0.1, hold=False))
    assert run.has(EndUtterance) and run.m.talk is None
    run(SpeechEnd(3, "key", 3.1), heard(3, "open spotify", 3.3))
    assert run.has(Command)[0].decision.talk  # the sentence under way still counts


def test_tap_while_listening_to_nothing_stops():
    run = Run()(Press("copilot", 1.0), Release("copilot", 1.1, 0.1, hold=False), Press("copilot", 2.0),
                Release("copilot", 2.1, 0.1, hold=False))
    assert run.state is State.IDLE and run.m.talk is None and not outputs(run.m).attentive


def test_new_press_while_thinking_starts_over():
    run = Run()(heard(1, "open spotify", 1.0))  # outside a session
    assert run.state is State.THINKING and run.m.talk is None
    run(Press("copilot", 1.2))
    assert run.state is State.LISTENING and run.has(Cancelled)[0].why == "superseded"


# --- double-tap, Esc -----------------------------------------------------------------------------------


def test_double_tap_locks_dictation_until_the_next_tap():
    run = Run()(Press("copilot", 1.0), Release("copilot", 1.1, 0.1, hold=False), DoubleTap("copilot", 1.3))
    assert run.state is State.DICTATING and run.has(Dictation)[0].on and run.has(DropUtterance)
    assert run.has(TalkEvent)[0].what == "closed" and run.m.talk is None  # the first tap's session closes
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
    assert run.state is State.THINKING and run.m.talk is not None  # and a talk session opens (07b)


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


# Stage 07b: the name alone opens a talk session; what he says next is a clause of it (no name needed).


def test_name_alone_opens_a_session_for_the_next_utterance():
    run = Run()(SpeechStart(1, 1.0), SpeechEnd(1, "silence", 1.4), heard(1, "Nova.", 1.6))
    assert run.state is State.IDLE and run.m.talk is not None
    assert next_deadline(run.m) == 1.6 + TALK_IDLE_S - TALK_WARN_S
    run(SpeechStart(2, 2.0), SpeechEnd(2, "silence", 3.0), heard(2, "I'm gonna open a restaurant", 3.2))
    decision = run.has(Command)[0].decision
    assert decision.via == "talk" and decision.text == "I'm gonna open a restaurant"


def test_name_alone_claims_the_command_he_already_started():
    # Logged shape (~7 % of name-alone sessions): he starts the command 400 ms after the name; the name's final
    # lands 50 ms later. Nothing claimed that utterance and it was dropped as "busy".
    run = Run()(SpeechStart(1, 0.0), SpeechEnd(1, "silence", 0.45), SpeechStart(2, 0.85), heard(1, "Nova.", 0.9))
    assert run.state is State.IDLE and run.m.talk is not None
    assert [s.talk for s in run.m.spoken] == [True, True]  # begun after the name: a clause of the session
    run(SpeechEnd(2, "silence", 1.8), heard(2, "the blue folder please", 2.0))
    decision = run.has(Command)[0].decision
    assert decision.via == "talk" and decision.text == "the blue folder"


def test_name_alone_claims_a_short_command_that_already_ended():
    run = Run()(SpeechStart(1, 0.0), SpeechEnd(1, "silence", 0.45), SpeechStart(2, 0.7), SpeechEnd(2, "silence", 1.2),
                heard(1, "Nova.", 1.3))
    assert run.state is State.IDLE and spoken_talk(run, 2)
    run(heard(2, "pause", 1.5))
    assert run.has(Command)[0].decision.via == "talk" and not run.has(Ignored)


def test_name_seen_in_live_words_then_a_pause_still_claims_the_command():
    run = Run()(SpeechStart(1, 0.0), WakeHeard(1, 0.3), SpeechEnd(1, "silence", 0.45), SpeechStart(2, 0.85),
                heard(1, "Nova.", 0.9))
    assert run.state is State.IDLE and run.m.session is None and run.m.talk is not None
    run(SpeechEnd(2, "silence", 1.8), heard(2, "new tab", 2.0))
    assert run.has(Command)[0].decision.via == "talk"


def test_name_alone_claims_only_fresh_speech():
    from nova.state import Spoken

    stale = Machine(spoken=(Spoken(9, 1.0, 2.0),))  # an utterance from long ago whose final never came
    run = Run(stale)(SpeechStart(10, 30.0), SpeechEnd(10, "silence", 30.4), heard(10, "Nova.", 30.6))
    assert run.m.talk is not None and not spoken_talk(run, 9) and spoken_talk(run, 10)


def test_name_alone_then_nothing_times_out():
    run = Run()(heard(1, "Nova", 1.0), Tick(1.0 + TALK_IDLE_S))
    assert run.state is State.IDLE and run.m.talk is None


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
    run = Run()(heard(1, "open spotify", 1.0), Done(True, 1.2))  # a command outside a session (07b)
    run(heard(2, "now close Spotify", 5.0))
    assert run.has(Command)[0].decision.via == "follow-up"
    run(Done(True, 5.2), Tick(5.2 + FOLLOW_UP_S + 0.1), heard(3, "close Spotify", 14.0))
    assert run.has(Ignored)[0].decision.via == "needs-name"


# --- never drop him: what passes the gate while Nova is busy runs next (SPEC §5) ---------------------------


def keyed(text, at=1.0, utterance=1, run=None):
    """A held-key command (one command, no talk session): its final arrives at ``at``."""
    run = run or Run()
    return run(Press("copilot", at - 0.8), SpeechStart(utterance, at - 0.7), Release("copilot", at - 0.3, 0.5, hold=True),
               SpeechEnd(utterance, "key", at - 0.3), heard(utterance, text, at))


def busy(at=1.0):
    """ "open Chrome" was held-key accepted (no session) and the brain is acting on it."""
    return keyed("open Chrome", at)(Progress(State.ACTING, at + 0.1))


def test_a_command_said_while_nova_is_busy_runs_next():
    run = busy()(SpeechStart(2, 1.2), SpeechEnd(2, "silence", 1.9), heard(2, "maximize it", 2.1))
    assert run.has(Queued)[0].decision.text == "maximize it"
    assert not run.has(Command) and not run.has(Cancelled) and not run.has(Ignored)
    assert run.state is State.ACTING  # the running command is not disturbed
    run(Done(True, 2.5, "Opened Chrome"))
    command = run.has(Command)[0]
    assert run.state is State.THINKING and command.utterance == 2 and command.waited == pytest.approx(0.6)
    run(Progress(State.ACTING, 2.6), Done(True, 2.9, "Maximized"))
    assert run.state is State.DONE and run.m.queue == ()


def test_queued_commands_run_in_order():
    run = busy()(heard(2, "maximize it", 1.5), heard(3, "open spotify", 1.8))
    assert [w.utterance for w in run.m.queue] == [2, 3]
    run(Done(True, 2.0))
    assert run.has(Command)[0].utterance == 2
    run(Done(True, 2.5))
    assert run.has(Command)[0].utterance == 3
    run(Done(True, 3.0))
    assert run.state is State.DONE and not run.has(Command)


def test_a_queued_command_older_than_10_s_is_dropped_with_a_note():
    run = busy()(SpeechStart(2, 1.2), SpeechEnd(2, "silence", 1.9), heard(2, "maximize it", 2.1))
    run(Done(True, 1.9 + QUEUE_MAX_AGE_S + 0.5, "Opened Chrome"))
    dropped = run.has(Dropped)[0]
    assert dropped.utterance == 2 and dropped.stale and not run.has(Command)
    assert run.state is State.DONE and run.m.queue == ()


def test_a_stale_command_is_skipped_and_a_fresh_one_behind_it_runs():
    run = busy()(heard(2, "maximize it", 1.5), heard(3, "open spotify", 9.0))
    run(Done(True, 12.0))
    assert [d.utterance for d in run.has(Dropped)] == [2] and run.has(Command)[0].utterance == 3


@pytest.mark.parametrize("said", ["stop", "Stop.", "Cancel.", "cancel that", "um, stop it", "Nova, stop", "never mind", "Wait."])
def test_stop_while_busy_aborts_the_action_and_clears_the_queue(said):
    run = busy()(heard(2, "maximize it", 1.5), heard(3, said, 1.8))
    assert run.state is State.IDLE and run.has(Cancelled)[0].why == "stop"
    assert [d.utterance for d in run.has(Dropped)] == [2] and run.m.queue == ()
    assert not run.has(Command)
    run(Done(True, 2.0))  # a late result of the stopped command changes nothing
    assert run.state is State.IDLE and not run.has(Command)


def test_a_sentence_with_stop_in_it_is_not_a_stop():
    run = busy()(heard(2, "we should stop by the store later", 1.5))
    assert run.state is State.ACTING and not run.has(Cancelled)


def test_the_name_still_interrupts_and_replaces_what_was_waiting():
    run = busy()(heard(2, "maximize it", 1.5), heard(3, "Nova, pause", 1.8))
    assert run.has(Cancelled)[0].why == "superseded" and run.has(Command)[0].utterance == 3
    assert [d.utterance for d in run.has(Dropped)] == [2] and run.m.queue == ()
    run(Done(True, 2.2))
    assert run.state is State.DONE and not run.has(Command)


def test_the_key_still_interrupts_and_replaces_what_was_waiting():
    run = busy()(heard(2, "maximize it", 1.5), Press("copilot", 1.8))
    assert run.state is State.LISTENING and run.has(Cancelled)[0].why == "superseded"
    assert [d.utterance for d in run.has(Dropped)] == [2] and run.m.queue == ()


def test_a_failed_command_stops_the_queue():
    run = busy()(heard(2, "maximize it", 1.5), Done(False, 2.0, "Chrome didn't open"))
    assert run.state is State.ERROR and [d.utterance for d in run.has(Dropped)] == [2] and not run.has(Command)


def test_esc_clears_the_queue():
    run = busy()(heard(2, "maximize it", 1.5), Cancel(1.8))
    assert run.state is State.IDLE and run.has(Dropped) and run.m.queue == ()


def test_a_command_that_was_not_one_after_all_lets_the_next_run():
    run = keyed("do the thing", 1.0)(heard(2, "open spotify", 1.5))
    run(Progress(State.IDLE, 1.8, "not a command"))
    assert run.state is State.THINKING and run.has(Command)[0].utterance == 2


def test_a_question_keeps_the_queue_and_a_no_drops_it():
    run = busy()(heard(2, "maximize it", 1.5), Progress(State.CONFIRM, 1.7))
    assert [w.utterance for w in run.m.queue] == [2]
    run(Progress(State.IDLE, 3.0, "Cancelled"))  # he said no
    assert run.state is State.IDLE and run.has(Dropped) and run.m.queue == ()


def test_the_queue_is_small():
    run = busy()
    for n in range(2, 2 + QUEUE_MAX):
        run(heard(n, "open spotify", 1.5))
    run(heard(99, "open spotify", 1.6))
    assert len(run.m.queue) == QUEUE_MAX and run.has(Ignored)[0].decision.via == "busy"


def test_chatter_while_busy_is_still_ignored():
    run = busy()(heard(2, "did you close the door", 1.5))
    assert run.has(Ignored)[0].decision.via == "not-command" and run.m.queue == ()


def test_chatter_during_a_key_session_cannot_take_it_over():
    run = Run()(Press("copilot", 1.0), heard(9, "open spotify", 1.5))  # the key is held: a 07a key session
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
    run(SpeechStart(2, 2.2), SpeechEnd(2, "silence", 2.8), heard(2, "yes", 3.0))
    assert run.has(Answer)[0].text == "yes" and run.state is State.CONFIRM
    run(Tick(1.4 + 15.0))
    assert run.state is State.IDLE and run.has(Cancelled)[0].why == "no answer"


# --- a yes must be a real yes (SPEC §5, §14) -----------------------------------------------------------


def asking(kind=State.CONFIRM, at=10.0):
    return keyed("send it", at - 0.5)(Progress(kind, at))  # outside a session: a press interrupts at once


def test_a_tiny_clip_that_began_before_the_question_never_answers_it():
    # The logged case: the Confirm appeared, and 262 ms later the final of a 224 ms clip said "Yeah."
    run = asking(at=10.0)
    run(SpeechStart(2, 9.95), SpeechEnd(2, "silence", 10.174), heard(2, "Yeah.", 10.262))
    assert not run.has(Answer) and run.state is State.CONFIRM
    assert run.has(Ignored)[0].decision.via == "answer-early"


def test_speech_under_way_when_the_question_appears_never_answers_it():
    run = Run()(heard(1, "Nova, send it", 9.5), SpeechStart(2, 9.8), Progress(State.CONFIRM, 10.0))
    run(SpeechEnd(2, "silence", 11.5), heard(2, "yes that one is fine", 11.7))
    assert not run.has(Answer) and run.has(Ignored)[0].decision.via == "answer-early"
    assert run.state is State.CONFIRM and outputs(run.m).asking == "confirm"  # Enter / the buttons still answer


def test_an_answer_shorter_than_400_ms_never_counts():
    run = asking(at=10.0)
    run(SpeechStart(2, 10.6), SpeechEnd(2, "silence", 10.9), heard(2, "Yeah.", 11.0))
    assert not run.has(Answer) and run.has(Ignored)[0].decision.via == "answer-short"
    assert run.state is State.CONFIRM


def test_a_600_ms_yes_that_began_after_the_question_answers_it():
    run = asking(at=10.0)
    run(SpeechStart(2, 10.5), SpeechEnd(2, "silence", 11.1), heard(2, "Yes.", 11.3))
    assert run.has(Answer)[0].text == "Yes." and not run.has(Ignored)


def test_clarify_answers_follow_the_same_rule():
    run = asking(State.CLARIFY, at=10.0)
    run(SpeechStart(2, 9.9), SpeechEnd(2, "silence", 10.7), heard(2, "the second one", 10.9))
    assert not run.has(Answer)
    run(SpeechStart(3, 11.0), SpeechEnd(3, "silence", 11.8), heard(3, "the second one", 12.0))
    assert run.has(Answer)[0].utterance == 3


def test_an_utterance_nobody_saw_start_is_not_an_answer():
    run = asking(at=10.0)(heard(7, "yes", 12.0))
    assert not run.has(Answer) and run.has(Ignored)[0].decision.via == "answer-early"


def test_each_question_has_its_own_clock():
    # He answers the first question; a second one appears while his next words are already under way.
    run = asking(State.CLARIFY, at=10.0)
    run(SpeechStart(2, 10.5), SpeechEnd(2, "silence", 11.2), heard(2, "the first one", 11.4))
    assert run.has(Answer)
    run(Progress(State.ACTING, 11.5), SpeechStart(3, 11.6), Progress(State.CONFIRM, 12.0))
    run(SpeechEnd(3, "silence", 12.4), heard(3, "okay", 12.6))
    assert not run.has(Answer) and run.has(Ignored)[0].decision.via == "answer-early"


def test_a_key_press_still_interrupts_a_question():
    run = asking(at=10.0)(Press("copilot", 10.3))
    assert run.state is State.LISTENING and run.has(Cancelled)[0].why == "superseded"


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


def test_thinking_can_go_quietly_back_to_idle():
    run = Run()(heard(1, "Nova, open the thing", 1.0), Progress(State.IDLE, 1.6, "not a command"))
    assert run.state is State.IDLE and run.has(Transition)[-1].why == "not a command"
    assert not run.has(Done)


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


def test_an_answer_to_read_stays_up_longer():
    run = Run()(Press("copilot", 1.0), SpeechStart(1, 1.2), Release("copilot", 2.0, 1.0, hold=True), SpeechEnd(1, "key", 2.0),
                heard(1, "what's due today", 2.2), Done(True, 2.5, "Nothing due today — next: HW 6", 6.0))
    assert run.state is State.DONE
    run(Tick(2.5 + DONE_S))
    assert run.state is State.DONE  # still reading
    run(Tick(2.5 + 6.0))
    assert run.state is State.IDLE
    run = Run()(Press("copilot", 1.0), SpeechStart(1, 1.2), Release("copilot", 2.0, 1.0, hold=True), SpeechEnd(1, "key", 2.0),
                heard(1, "what's due today", 2.2), Done(True, 2.5, "x", 600.0))
    run(Tick(2.5 + 10.0))
    assert run.state is State.IDLE  # capped


# --- dictation (Stage 07) -------------------------------------------------------------------------------


def test_dictation_ends_after_ten_seconds_of_silence():
    run = Run()(DoubleTap("copilot", 1.0))
    assert run.m.deadline == 1.0 + DICTATION_SILENCE_S
    run(SpeechStart(1, 3.0))
    assert run.m.deadline is None
    run(SpeechEnd(1, "silence", 4.0), Tick(13.9))
    assert run.state is State.DICTATING
    run(Tick(14.0))
    assert run.state is State.IDLE and run.has(Dictation)[0].why == "silence"


def test_a_tap_while_talking_finishes_the_sentence_first():
    run = Run()(DoubleTap("copilot", 1.0), SpeechStart(4, 2.0), Press("copilot", 3.0))
    assert run.state is State.DICTATING and run.has(EndUtterance)
    run(Release("copilot", 3.1, 0.1, hold=False), SpeechEnd(4, "key", 3.0), SpeechStart(5, 3.2))
    assert run.state is State.DICTATING
    run(heard(4, "the last words", 3.3))
    assert run.has(Dictated)[0].text == "the last words"
    assert run.state is State.IDLE and run.has(Dictation)[0].why == "tap"


def test_finishing_gives_up_when_no_text_comes():
    run = Run()(DoubleTap("copilot", 1.0), SpeechStart(4, 2.0), Press("copilot", 3.0), Tick(3.0 + HEARD_WAIT_S))
    assert run.state is State.IDLE and run.has(Dictation)[0].why == "tap"


def test_stop_dictation_ends_it_at_once_after_typing_what_came_first():
    run = Run()(DoubleTap("copilot", 1.0), SpeechStart(2, 2.0), SpeechEnd(2, "silence", 3.0),
                heard(2, "That's all. Stop dictation.", 3.6))
    assert run.has(Dictated)[0].text == "That's all."
    assert run.state is State.IDLE and run.has(Dictation)[0].why == "stop dictation"
    run = Run()(DoubleTap("copilot", 1.0), heard(3, "Nova, stop dictation", 2.0))
    assert not run.has(Dictated) and run.state is State.IDLE


def test_esc_while_dictating_drops_what_he_is_saying():
    run = Run()(DoubleTap("copilot", 1.0), SpeechStart(5, 2.0), Cancel(2.5))
    assert run.state is State.IDLE and run.has(DropUtterance) and run.has(Dictation)[0].why == "Esc"


def test_a_plan_starting_dictation_sets_the_silence_clock_and_its_failure_ends_it():
    run = Run()(heard(1, "Nova, open a google doc and type this", 1.0), Progress(State.ACTING, 1.1),
                Progress(State.DICTATING, 1.2, "Google Doc"))
    assert run.state is State.DICTATING and run.m.deadline == 1.2 + DICTATION_SILENCE_S
    assert run.has(Dictation)[0].on
    run(Done(True, 1.5, "Dictating"))
    assert run.state is State.DICTATING
    run(Done(False, 4.0, "Google Docs didn't open"))
    assert run.state is State.ERROR and not run.has(Dictation)[0].on


def test_outputs_say_when_dictating():
    assert outputs(Run()(DoubleTap("copilot", 1.0)).m).dictating
    assert not outputs(Machine()).dictating


def test_silence_clock_waits_for_a_sentence_already_under_way():
    run = Run()(SpeechStart(1, 0.5), heard(9, "Nova, type this", 0.6), Progress(State.DICTATING, 0.7, "voice"),
                Tick(11.0))
    assert run.state is State.DICTATING
    run(SpeechEnd(1, "silence", 12.0), Tick(21.9))
    assert run.state is State.DICTATING
    run(Tick(22.0))
    assert run.state is State.IDLE


def test_the_follow_up_window_is_judged_by_when_he_started_speaking():
    run = Run()(heard(1, "open spotify", 1.0), Done(True, 1.2))  # outside a session (07b)
    closes = 1.2 + FOLLOW_UP_S
    run(SpeechStart(2, closes - 0.3), Tick(closes + 0.1), SpeechEnd(2, "silence", closes + 1.4),
        heard(2, "now close Spotify", closes + 1.9))  # the text arrives 1.9 s after the window closed
    assert run.has(Command)[0].decision.via == "follow-up"


def test_speech_that_began_before_the_window_opened_is_not_a_follow_up():
    run = Run()(SpeechStart(1, 0.2), heard(2, "open spotify", 1.0), Done(True, 1.2),
                SpeechEnd(1, "silence", 2.0), heard(1, "close Spotify", 2.5))  # chatter from before the command
    assert run.has(Ignored)[0].decision.via == "needs-name"


def test_speech_that_began_while_nova_worked_is_a_follow_up():
    run = Run()(heard(1, "open spotify", 1.0), SpeechStart(2, 1.1), Done(True, 1.2),
                SpeechEnd(2, "silence", 1.9), heard(2, "now close Spotify", 2.1))
    assert run.has(Command)[0].decision.via == "follow-up"


# --- a cold model: the load outlives the thinking deadline (Stage 07a) -------------------------------------------------


def thinking(at=1.0):
    return keyed("open the thing", at)  # outside a session: the key interrupts at the press


def test_a_request_warming_up_is_not_timed_out_at_ten_seconds():
    run = thinking(1.0)(Warming(1.5))
    assert run.state is State.THINKING and run.m.deadline == 1.5 + WARM_WAIT_S and not run.effects
    run(Tick(1.0 + HEARD_WAIT_S + 0.1))
    assert run.state is State.THINKING and not run.has(Cancelled)
    run(Tick(1.5 + WARM_WAIT_S - 0.1))
    assert run.state is State.THINKING
    run(Tick(1.5 + WARM_WAIT_S))
    assert run.state is State.IDLE and run.has(Cancelled)[0].why == "timed out"  # a load that never finishes


def test_the_wait_for_a_load_is_longer_than_the_load_timeout():
    from nova.brain.llm import LOAD_TIMEOUT_S

    assert WARM_WAIT_S > LOAD_TIMEOUT_S


def test_once_loaded_the_ten_second_deadline_counts_from_then():
    run = thinking(1.0)(Warming(1.5), Progress(State.THINKING, 30.0))
    assert run.state is State.THINKING and run.m.deadline == 30.0 + HEARD_WAIT_S and not run.effects
    run(Tick(30.0 + HEARD_WAIT_S - 0.1))
    assert run.state is State.THINKING
    run(Tick(30.0 + HEARD_WAIT_S))
    assert run.state is State.IDLE and run.has(Cancelled)[0].why == "timed out"


def test_esc_while_warming_cancels_at_once():
    run = thinking(1.0)(Warming(1.5), Cancel(3.0, "Esc"))
    assert run.state is State.IDLE and run.has(Cancelled)[0].why == "Esc" and run.m.deadline is None


def test_the_key_and_the_name_replace_a_warming_request_at_once():
    run = thinking(1.0)(Warming(1.5), Press("copilot", 3.0))
    assert run.state is State.LISTENING and run.has(Cancelled)[0].why == "superseded"
    run = thinking(1.0)(Warming(1.5), heard(2, "Nova, open notepad", 3.0))
    assert run.state is State.THINKING and run.has(Cancelled)[0].why == "superseded"
    assert run.m.deadline == 3.0 + HEARD_WAIT_S  # the new command's own deadline


def test_warming_outside_thinking_changes_nothing():
    listening = Run()(Press("copilot", 1.0)).m
    done = Run()(heard(1, "Nova, open Chrome", 1.0), Done(True, 1.2)).m
    confirm = Run()(heard(1, "Nova, open Chrome", 1.0), Progress(State.CONFIRM, 1.2)).m
    for machine in (Machine(), listening, done, confirm):
        assert step(machine, Warming(2.0)).machine == machine and not step(machine, Warming(2.0)).effects


def test_warming_while_a_session_waits_for_its_own_words_changes_nothing():
    run = Run()(Press("copilot", 1.0), SpeechStart(1, 1.1), Release("copilot", 2.0, 1.0, hold=True),
                SpeechEnd(1, "key", 2.0))
    assert run.state is State.THINKING and run.m.session is not None  # the final hasn't come yet
    before = run.m
    assert step(before, Warming(2.1)).machine == before


def test_thinking_without_a_deadline_keeps_none_while_warming():
    run = Run()(heard(1, "Nova, send it", 1.0), Progress(State.CONFIRM, 1.2), Progress(State.THINKING, 3.0))
    assert run.state is State.THINKING and run.m.deadline is None  # his answer went back to the model
    run(Warming(3.1))
    assert run.m.deadline is None
    run(Progress(State.THINKING, 20.0))
    assert run.state is State.THINKING and run.m.deadline is None


# --- talk sessions (Stage 07b, SPEC §5): the mic stays open across commands -------------------------------------


def tap(run, at, source="copilot"):
    return run(Press(source, at), Release(source, at + 0.1, 0.1, hold=False))


def talking(at=1.0, machine=None):
    """A talk session opened by a Copilot tap whose release came at ``at + 0.1`` (clock: 30 s from then)."""
    return tap(Run(machine), at)


def say(run, utterance, text, start, end=None, final=None):
    end = start + 0.8 if end is None else end
    final = end + 0.2 if final is None else final
    return run(SpeechStart(utterance, start), SpeechEnd(utterance, "silence", end), heard(utterance, text, final))


def talk_events(effects, what=None):
    return [e for e in effects if isinstance(e, TalkEvent) and (what is None or e.what == what)]


def test_a_tap_opens_a_talk_session():
    run = Run()(Press("copilot", 1.0))
    assert run.state is State.LISTENING  # tap or hold is known only at the release
    run(Release("copilot", 1.15, 0.15, hold=False))
    assert run.state is State.IDLE and run.m.session is None and run.m.talk.via == "copilot"
    opened, moved = run.effects
    assert isinstance(opened, TalkEvent) and isinstance(moved, Transition)  # the pill knows before the state moves
    assert (opened.what, opened.why, opened.closes_at) == ("opened", "copilot", pytest.approx(1.15 + TALK_IDLE_S))
    assert (moved.old, moved.new, moved.why) == (State.LISTENING, State.IDLE, "talk session")
    o = outputs(run.m)
    assert o.attentive and o.talking and o.esc and not o.held and not o.dictating
    assert next_deadline(run.m) == pytest.approx(1.15 + TALK_IDLE_S - TALK_WARN_S)


def test_a_hold_is_one_command_without_a_session():
    run = keyed("open notepad", 2.0)
    assert run.has(Command)[0].decision.via == "key" and run.m.talk is None
    run(Done(True, 2.5))
    assert run.state is State.DONE and run.m.follow_up_until == 2.5 + FOLLOW_UP_S
    assert not talk_events(run.all) and not outputs(run.m).talking


def test_the_name_alone_opens_a_session():
    run = say(Run(), 1, "Nova.", 1.0)
    assert run.state is State.IDLE and run.m.talk.via == "wake"
    assert run.has(Woke) and run.has(Preload)[0].reason == "wake"
    assert talk_events(run.effects, "opened")[0].closes_at == pytest.approx(2.0 + TALK_IDLE_S)
    say(run, 2, "open notepad", 3.0)
    decision = run.has(Command)[0].decision
    assert decision.via == "talk" and decision.text == "open notepad" and decision.talk
    # The name in the live words showed Listening first: the talk takes over from that turn.
    run = Run()(SpeechStart(1, 1.0), WakeHeard(1, 1.2), SpeechEnd(1, "silence", 1.5), heard(1, "Nova", 1.7))
    assert run.state is State.IDLE and run.m.session is None and run.m.talk is not None


def test_nova_command_runs_and_opens_a_paused_session():
    run = say(Run(), 1, "Nova, open Chrome", 1.0)
    command = run.has(Command)[0]
    assert command.decision.via == "wake" and command.decision.text == "open Chrome"
    assert run.state is State.THINKING and run.m.talk.via == "named" and run.m.talk.since is None
    assert run.m.talk_command and talk_events(run.effects, "opened")[0].closes_at is None
    run(Progress(State.ACTING, 2.2), Done(True, 3.0, "Opened Chrome"))
    assert run.state is State.DONE and run.m.follow_up_until == 0.0  # the session, not the 8 s follow-up
    resumed = talk_events(run.effects, "resumed")[0]
    assert resumed.why == "done" and resumed.closes_at == pytest.approx(3.0 + TALK_IDLE_S)


def test_clauses_of_one_final_run_in_order():
    run = talking(1.0)
    say(run, 1, "Open Notepad and then create a new note.", 2.0)  # ends 2.8, final 3.0
    first = run.has(Command)[0]
    assert first.decision.text == "Open Notepad" and first.decision.clause == 0 and first.decision.talk
    assert first.decision.request and first.decision.via == "talk"
    assert run.has(Queued)[0].decision.text == "create a new note" and run.m.queue[0].talk
    assert isinstance(run.effects[0], TalkEvent) and run.effects[0].what == "paused"
    assert run.effects[0].why == "running" and run.m.talk.since is None
    run(Progress(State.ACTING, 3.1), Done(True, 4.0, "Opened Notepad"))
    second = run.has(Command)[0]
    assert second.decision.clause == 1 and second.waited == pytest.approx(4.0 - 2.8)
    assert run.state is State.THINKING and not talk_events(run.effects)  # still busy: the clock stays paused
    run(Progress(State.ACTING, 4.1), Done(True, 5.0, "New note"))
    assert run.state is State.DONE and talk_events(run.effects, "resumed")[0].closes_at == pytest.approx(35.0)


def test_small_talk_is_dropped_and_never_touches_the_clock():
    run = talking(1.0)
    before = run.m.talk
    for n, said in enumerate(["Great, okay.", "um", "Nice.", "Cool, thank you."], start=1):
        say(run, n, said, 5.0 + n)
        assert not run.has(Command) and not talk_events(run.effects) and run.state is State.IDLE
        assert run.has(Ignored) and all(i.decision.via == "small-talk" and i.decision.talk for i in run.has(Ignored))
    assert run.m.talk == before


def test_not_a_command_after_all_resumes_the_clock_where_it_stopped():
    run = talking(1.0)  # closes at 31.1
    say(run, 1, "the next video is so good", 9.0)  # final at 10.0
    assert run.state is State.THINKING and not run.has(Command)[0].decision.request
    assert run.m.talk.left == pytest.approx(21.1)
    run(Progress(State.IDLE, 11.0, "not a command"))
    resumed = talk_events(run.effects, "resumed")[0]
    assert resumed.why == "ignored" and resumed.closes_at == pytest.approx(11.0 + 21.1)


def test_a_finished_command_restarts_the_clock():
    run = talking(1.0)
    say(run, 1, "open notepad", 9.0)
    run(Done(True, 11.0, "Opened Notepad"))
    resumed = talk_events(run.effects, "resumed")[0]
    assert resumed.why == "done" and resumed.closes_at == pytest.approx(11.0 + TALK_IDLE_S)


def test_idle_close_warns_five_seconds_before():
    run = talking(1.0)
    closes = 1.1 + TALK_IDLE_S
    run(Tick(closes - TALK_WARN_S - 0.1))
    assert not run.effects
    run(Tick(closes - TALK_WARN_S))
    closing = talk_events(run.effects, "closing")[0]
    assert closing.why == "idle" and closing.closes_at == pytest.approx(closes)
    assert next_deadline(run.m) == pytest.approx(closes)
    run(Tick(closes - 1.0))
    assert not run.effects  # warned once
    run(Tick(closes))
    assert talk_events(run.effects, "closed")[0].why == "idle" and run.m.talk is None
    assert not run.has(Transition) and run.state is State.IDLE
    o = outputs(run.m)
    assert not o.attentive and not o.talking and not o.esc


def test_idle_close_waits_for_a_sentence_under_way():
    closes = 1.1 + TALK_IDLE_S
    run = talking(1.0)(SpeechStart(1, closes - 2.0), Tick(closes))
    assert run.m.talk is not None  # he is talking
    run(SpeechEnd(1, "silence", closes + 0.5), Tick(closes + 0.6))
    assert run.m.talk is not None  # his final is on its way
    assert next_deadline(run.m) == pytest.approx(closes + 0.5 + HEARD_WAIT_S)
    run(heard(1, "Great.", closes + 0.8), Tick(closes + 0.8))
    assert talk_events(run.all, "closed") and run.m.talk is None  # small talk: closes at once
    # A command said at the edge runs and gets a fresh clock.
    run = talking(1.0)(SpeechStart(1, closes - 2.0), Tick(closes), SpeechEnd(1, "silence", closes + 0.5))
    run(heard(1, "open notepad", closes + 0.8), Tick(closes + 0.8), Done(True, closes + 2.0))
    assert run.m.talk is not None and run.m.talk.closes_at() == pytest.approx(closes + 2.0 + TALK_IDLE_S)
    # A final that never comes is given up on.
    run = talking(1.0)(SpeechStart(1, closes - 2.0), SpeechEnd(1, "silence", closes + 0.5), Tick(closes + 1.0))
    assert run.m.talk is not None
    run(Tick(closes + 0.5 + HEARD_WAIT_S))
    assert run.m.talk is None


def test_a_tap_closes_and_the_running_step_and_queue_go_on():
    run = talking(1.0)
    say(run, 1, "open notepad and then create a new note", 2.0)
    tap(run, 4.0)
    closed = talk_events(run.all, "closed")[0]
    assert closed.why == "tap" and run.m.talk is None
    assert run.state is State.THINKING and len(run.m.queue) == 1 and not run.has(Cancelled)
    run(Done(True, 5.0))
    assert run.has(Command)[0].decision.text == "create a new note"
    run(Done(True, 6.0))
    assert run.state is State.DONE and run.m.follow_up_until == 0.0  # it came from the session


def test_a_tap_mid_sentence_finishes_it_and_its_clauses_run():
    run = talking(1.0)(SpeechStart(1, 2.0), Press("copilot", 3.0))
    assert run.state is State.IDLE and outputs(run.m).held and not run.has(EndUtterance)
    run(Release("copilot", 3.1, 0.1, hold=False))
    assert run.has(EndUtterance) and talk_events(run.effects, "closed")[0].why == "tap"
    run(SpeechEnd(1, "key", 3.1), heard(1, "open notepad and create a new note", 3.3))
    assert run.has(Command)[0].decision.text == "open notepad" and run.has(Queued)[0].decision.talk


def test_a_hold_inside_a_session_interrupts_on_release():
    run = talking(1.0)
    say(run, 1, "open notepad and then create a new note", 2.0)
    run(Progress(State.ACTING, 3.1), Press("copilot", 5.0), SpeechStart(2, 5.1))
    assert run.state is State.ACTING and not run.has(Cancelled)  # nothing is cancelled at the press
    run(Release("copilot", 6.0, 1.0, hold=True))
    assert run.has(Cancelled)[0].why == "superseded" and run.has(EndUtterance)
    assert run.has(Dropped)[0].why == "replaced by a new command" and run.has(Skipped)[0].texts == ("create a new note",)
    assert run.state is State.LISTENING and run.m.session.utterance == 2 and run.m.queue == ()
    run(SpeechEnd(2, "key", 6.0))
    assert run.state is State.THINKING
    run(heard(2, "close spotify", 6.2))
    decision = run.has(Command)[0].decision
    assert decision.via == "key" and decision.text == "close spotify" and run.m.talk is not None
    # A hold with nothing said changes nothing.
    run = talking(1.0)(Press("copilot", 5.0), Release("copilot", 6.0, 1.0, hold=True))
    assert run.state is State.IDLE and run.m.talk is not None and not run.effects


@pytest.mark.parametrize("why", ["Esc", "stop button"])
def test_esc_and_the_stop_button_close_and_cancel(why):
    run = talking(1.0)
    say(run, 1, "open notepad and then create a new note", 2.0)
    run(Cancel(5.0, why))
    assert talk_events(run.effects, "closed")[0].why == why and run.m.talk is None
    assert run.has(Cancelled)[0].why == why and run.state is State.IDLE
    assert run.has(Skipped)[0].texts == ("create a new note",) and run.m.queue == ()
    assert not outputs(run.m).esc
    # Nothing running, he is mid-sentence: that sentence is thrown away.
    run = talking(1.0)(SpeechStart(1, 2.0), Cancel(2.5, why))
    assert run.has(DropUtterance) and run.m.talk is None and not run.has(Transition)


def test_stop_clears_the_queue_and_keeps_the_session():
    run = talking(1.0)
    say(run, 1, "open notepad and then create a new note", 2.0)
    run(Progress(State.ACTING, 3.1))
    say(run, 2, "stop", 4.0)
    assert run.state is State.IDLE and run.has(Cancelled)[0].why == "stop"
    skipped = run.has(Skipped)[0]
    assert skipped.texts == ("create a new note",) and skipped.why == "stopped"
    resumed = talk_events(run.effects, "resumed")[0]
    assert resumed.why == "done" and run.m.talk is not None


@pytest.mark.parametrize("said", ["stop", "Wait.", "never mind", "hold on", "Cancel that.", "um, hold on"])
def test_a_stop_word_with_nothing_to_stop_is_dropped_like_small_talk(said):
    run = talking(1.0)
    before = run.m.talk
    say(run, 1, said, 5.0)
    assert not run.has(Command) and not run.has(Cancelled) and not talk_events(run.effects)
    (ignored,) = run.has(Ignored)
    assert ignored.decision.via == "stop" and ignored.decision.talk and not ignored.decision.accepted
    assert run.state is State.IDLE and run.m.talk == before  # the idle clock is untouched


def test_a_stop_word_over_the_speakers_is_an_ordinary_clause():
    run = talking(1.0)(SpeechStart(1, 5.0), SpeechEnd(1, "silence", 5.8))
    run(Heard(1, "stop", 6.0, replace(ENV, speakers=True)))
    assert not any(i.decision.via == "stop" for i in run.has(Ignored))
    decision = run.has(Command)[0].decision  # "stop" can still reach the player (strict Tier 0: pause)
    assert decision.accepted and decision.heard == "stop" and decision.talk and decision.safe_only


def test_a_failed_step_stops_the_queue_and_says_what_was_skipped():
    run = talking(1.0)
    say(run, 1, "open the notes app and then create a new note and then make the title say hello", 2.0)
    assert len(run.m.queue) == 2
    run(Progress(State.ACTING, 3.1), Done(False, 4.0, "Couldn't find the notes app"))
    assert run.state is State.ERROR
    skipped = run.has(Skipped)[0]
    assert skipped.texts == ("create a new note", "make the title say hello")
    assert skipped.why == "the step before it didn't finish"
    assert talk_events(run.effects, "resumed")[0].why == "done" and run.m.talk is not None
    run(Tick(4.0 + ERROR_S))
    assert run.state is State.IDLE and run.m.talk is not None


def test_a_declined_or_timed_out_confirm_stops_the_queue():
    run = talking(1.0)
    say(run, 1, "close notepad and then open chrome", 2.0)
    run(Progress(State.CONFIRM, 3.5), Progress(State.IDLE, 5.0, "Cancelled"))
    assert run.has(Skipped)[0].texts == ("open chrome",) and run.m.talk is not None
    assert talk_events(run.effects, "resumed")[0].closes_at == pytest.approx(5.0 + TALK_IDLE_S)
    run = talking(1.0)
    say(run, 1, "close notepad and then open chrome", 2.0)
    run(Progress(State.CONFIRM, 3.5), Tick(3.5 + CONFIRM_S))
    assert run.has(Cancelled)[0].why == "no answer" and run.has(Skipped)[0].texts == ("open chrome",)
    assert run.m.talk is not None and run.state is State.IDLE
    run = talking(1.0)  # the island's No: the same, and the session stays
    say(run, 1, "close notepad and then open chrome", 2.0)
    run(Progress(State.CONFIRM, 3.5), Cancel(4.0, "No"))
    assert run.has(Skipped) and run.m.talk is not None


def test_confirm_inside_a_session_needs_a_fresh_answer():
    run = talking(1.0)
    say(run, 1, "close notepad", 2.0)
    run(SpeechStart(2, 3.2), Progress(State.CONFIRM, 3.5))
    run(SpeechEnd(2, "silence", 4.0), heard(2, "yes", 4.1))
    assert not run.has(Answer) and run.has(Ignored)[0].decision.via == "answer-early"
    say(run, 3, "yeah", 4.5, 4.7, 4.8)
    assert not run.has(Answer) and run.has(Ignored)[0].decision.via == "answer-short"
    say(run, 4, "yes, close it and open chrome", 5.0, 5.7, 5.9)
    assert run.has(Answer)[0].text == "yes, close it and open chrome"  # an answer is never split
    assert not run.has(Command) and not run.has(Queued) and run.state is State.CONFIRM


def test_session_queue_limits():
    run = talking(1.0)
    say(run, 1, "open notepad", 2.0)
    for n in range(2, 2 + TALK_QUEUE_MAX):
        say(run, n, "open chrome", 3.0)
    assert len(run.m.queue) == TALK_QUEUE_MAX > QUEUE_MAX
    say(run, 99, "open chrome", 3.5)
    assert run.has(Ignored)[0].decision.via == "busy" and len(run.m.queue) == TALK_QUEUE_MAX
    # Older than the 07a limit but within the session's: it still runs.
    run(Done(True, 3.8 + QUEUE_MAX_AGE_S + 5.0))
    assert run.has(Command) and not run.has(Dropped)
    # The head waited too long: everything behind it goes too (later clauses build on it).
    run(Done(True, 3.8 + TALK_QUEUE_MAX_AGE_S + 0.5))
    dropped = run.has(Dropped)
    assert len(dropped) == TALK_QUEUE_MAX - 1 and all(d.stale for d in dropped) and not run.has(Command)
    assert run.has(Skipped)[0].texts == ("open chrome",) * (TALK_QUEUE_MAX - 1)
    assert run.state is State.DONE and run.m.queue == ()


def test_outside_a_session_the_07a_queue_is_unchanged():
    run = busy()
    for n in range(2, 2 + QUEUE_MAX):
        run(heard(n, "open spotify", 1.5))
    run(heard(99, "open spotify", 1.6))
    assert len(run.m.queue) == QUEUE_MAX and run.has(Ignored)[0].decision.via == "busy"
    assert not any(w.talk for w in run.m.queue)
    run(Done(True, 1.5 + QUEUE_MAX_AGE_S + 0.5))
    assert len(run.has(Dropped)) == QUEUE_MAX and not run.has(Command)
    run = busy()(heard(2, "maximize it", 1.5), Done(False, 2.0, "Chrome didn't open"))
    assert [d.utterance for d in run.has(Dropped)] == [2] and not run.has(Skipped)
    assert run.m.follow_up_until == 2.0 + FOLLOW_UP_S
    assert not talk_events(run.all) and not any(isinstance(e, Skipped) for e in run.all)


def test_type_this_pauses_the_clock_and_a_tap_ends_only_dictation():
    run = talking(1.0)
    say(run, 1, "type this hello world", 2.0)
    assert run.has(Command)[0].decision.text == "type this hello world"
    run(Progress(State.ACTING, 3.1), Progress(State.DICTATING, 3.2, "voice"))
    assert talk_events(run.effects, "paused")[0].why == "dictating"
    o = outputs(run.m)
    assert o.dictating and o.attentive and not o.talking
    run(Done(True, 3.5, "Dictating…"))
    assert run.state is State.DICTATING and not run.effects
    run(Press("copilot", 10.0))
    assert run.state is State.IDLE and run.has(Dictation)[0].why == "tap"
    resumed = talk_events(run.effects, "resumed")[0]
    assert resumed.why == "done" and resumed.closes_at == pytest.approx(10.0 + TALK_IDLE_S)
    assert outputs(run.m).talking
    run(Release("copilot", 10.1, 0.1, hold=False))
    assert not run.effects and run.m.talk is not None


def test_dictation_end_gives_a_fresh_clock():
    run = talking(1.0)
    say(run, 1, "type this", 2.0)
    run(Progress(State.DICTATING, 3.1))
    run(Tick(3.1 + DICTATION_SILENCE_S))
    assert run.has(Dictation)[0].why == "silence"
    assert talk_events(run.effects, "resumed")[0].closes_at == pytest.approx(3.1 + DICTATION_SILENCE_S + TALK_IDLE_S)
    run = talking(1.0)
    say(run, 1, "type this", 2.0)
    run(Progress(State.DICTATING, 3.1), Progress(State.IDLE, 8.0, "stopped"))
    assert talk_events(run.effects, "resumed")[0].why == "done"


def test_the_closing_phrase_closes_and_drops_what_waits():
    run = talking(1.0)
    say(run, 1, "That's all.", 2.0)
    say(run, 2, "open chrome", 3.0)
    run(Progress(State.ACTING, 4.0), EndTalk(4.1))
    assert talk_events(run.effects, "closed")[0].why == "phrase" and run.m.talk is None
    assert run.has(Skipped)[0].texts == ("open chrome",) and run.m.queue == ()
    assert run.state is State.ACTING and not run.has(Cancelled)
    run(Done(True, 4.5, "Talk session ended"))
    assert run.state is State.DONE and run.m.follow_up_until == 0.0 and not talk_events(run.effects)
    assert not outputs(run.m).attentive


def test_no_follow_up_after_a_session_command():
    run = talking(1.0)
    say(run, 1, "open chrome", 2.0)
    run(Done(True, 3.5))
    assert run.m.follow_up_until == 0.0
    tap(run, 4.0)
    say(run, 2, "open spotify", 5.0)  # outside the session now: always-listening
    assert run.has(Command)[0].decision.via == "always"
    run(Done(True, 6.5))
    assert run.m.follow_up_until == 6.5 + FOLLOW_UP_S


def test_the_name_interrupts_inside_a_session():
    run = talking(1.0)
    say(run, 1, "open notepad and then create a new note", 2.0)
    run(Progress(State.ACTING, 3.1))
    say(run, 2, "Nova, open chrome", 4.0)
    assert run.has(Cancelled)[0].why == "superseded" and run.has(Skipped)[0].texts == ("create a new note",)
    command = run.has(Command)[0]
    assert command.decision.text == "open chrome" and command.utterance == 2 and run.state is State.THINKING
    assert run.m.queue == () and run.m.talk is not None
    # The name alone: nothing runs -> a fresh clock; something runs -> only a preload.
    run = talking(1.0)
    say(run, 1, "Nova", 20.0)
    assert run.has(Ignored)[0].decision.via == "wake-only"
    assert talk_events(run.effects, "resumed")[0].why == "name"
    assert run.m.talk.closes_at() == pytest.approx(21.0 + TALK_IDLE_S)
    say(run, 2, "open notepad", 22.0)
    say(run, 3, "Nova.", 24.0)
    assert [type(e) for e in run.effects] == [Preload] and run.state is State.THINKING


def test_passthrough_teaching_sleep_and_off_close_the_session():
    run = talking(1.0)
    say(run, 1, "turn on claude voice", 2.0)
    run(Progress(State.PASSTHROUGH, 3.5))
    assert talk_events(run.effects, "closed")[0].why == "voice mode" and run.m.talk is None
    run = talking(1.0)
    say(run, 1, "watch me do this", 2.0)
    run(Progress(State.TEACHING, 3.5))
    assert talk_events(run.effects, "closed")[0].why == "teaching"
    run = talking(1.0)(Sleep(True, 3.0))
    assert talk_events(run.effects, "closed")[0].why == "asleep" and run.m.asleep and run.m.talk is None
    run = talking(1.0)(Power(False, 3.0))
    assert talk_events(run.effects, "closed")[0].why == "off" and run.m.talk is None
    run(Power(True, 4.0))
    assert run.m.talk is None and run.state is State.IDLE


def test_a_final_begun_in_the_session_counts_after_a_tap_close():
    run = talking(1.0)(SpeechStart(1, 2.0), SpeechEnd(1, "silence", 2.8))
    tap(run, 3.0)
    run(heard(1, "type hello", 3.2))
    decision = run.has(Command)[0].decision
    assert decision.talk and decision.via == "talk"
    # Esc forgets what was said in the session: the same words now need the name.
    run = talking(1.0)(SpeechStart(1, 2.0), SpeechEnd(1, "silence", 2.8), Cancel(3.0))
    run(heard(1, "type hello", 3.2))
    assert run.has(Ignored)[0].decision.via == "needs-name"


@pytest.mark.parametrize(("env", "refused"), [(replace(ENV, speakers=True), "speakers"),
                                              (replace(ENV, mic_busy="Teams"), "mic-busy")])
def test_the_name_keeps_its_authority_inside_a_session(env, refused):
    run = talking(1.0)(SpeechStart(1, 2.0), SpeechEnd(1, "silence", 2.8))
    run(Heard(1, "Nova, close Notepad and then type hello", 3.0, env))
    first, waiting = run.has(Command)[0].decision, run.has(Queued)[0].decision
    assert (first.accepted, first.via, first.text, first.safe_only, first.talk) == (
        True, "wake", "close Notepad", False, True)
    assert (waiting.accepted, waiting.via, waiting.text, waiting.safe_only, waiting.talk) == (
        True, "wake", "type hello", False, True)
    assert run.state is State.THINKING and run.m.talk is not None
    # Without the name the same clause keeps the safe-only rule.
    run = talking(1.0)(SpeechStart(1, 2.0), SpeechEnd(1, "silence", 2.8), Heard(1, "close Notepad", 3.0, env))
    assert not run.has(Command) and run.has(Ignored)[0].decision.via == refused


def test_the_command_that_opens_a_session_is_a_talk_command():
    run = say(Run(), 1, "Nova, open Notepad", 1.0)
    decision = run.has(Command)[0].decision
    assert (decision.via, decision.text, decision.talk, decision.safe_only) == ("wake", "open Notepad", True, False)
    assert run.m.talk.via == "named"
    # The opening is announced before the command it opens (the brain clears the old context first).
    kinds = [type(e) for e in run.effects]
    assert kinds.index(TalkEvent) < kinds.index(Command)


def test_a_final_given_up_on_is_no_longer_a_talk_clause():
    closes = 1.1 + TALK_IDLE_S
    run = talking(1.0)(SpeechStart(1, closes - 2.0), SpeechEnd(1, "silence", closes - 1.0), Tick(closes + 15.0))
    assert run.m.talk is None and not spoken_talk(run, 1)
    run(Heard(1, "type hello world", closes + 16.0, replace(ENV, always_listening=False)))
    assert not run.has(Command) and run.has(Ignored)[0].decision.via == "off"
    assert run.state is State.IDLE


def test_idle_s_comes_from_the_machine():
    run = talking(1.0, Machine(idle_s=12.0))
    assert run.m.talk.closes_at() == pytest.approx(1.1 + 12.0)
    say(run, 1, "open notepad", 2.0)
    run(Done(True, 4.0))
    assert talk_events(run.effects, "resumed")[0].closes_at == pytest.approx(16.0)
    assert Machine().idle_s == TALK_IDLE_S


def test_flow_1_tap_two_clauses_small_talk_then_thats_all():
    run = Run()(Press("copilot", 0.0), Release("copilot", 0.15, 0.15, hold=False))
    assert talk_events(run.effects, "opened")[0].closes_at == pytest.approx(30.15)
    say(run, 1, "Open Notepad and then create a new note.", 1.0, 2.6, 2.85)
    assert run.has(Command)[0].decision.text == "Open Notepad" and run.has(Queued)
    run(Progress(State.ACTING, 3.0), Done(True, 3.5, "Opened Notepad"))
    assert run.has(Command)[0].decision.text == "create a new note"
    run(Progress(State.ACTING, 4.5), Done(True, 5.0, "New note"))
    assert run.state is State.DONE and talk_events(run.effects, "resumed")[0].closes_at == pytest.approx(35.0)
    run(Tick(6.5))
    assert run.state is State.IDLE and run.m.talk is not None  # the pill stays between clauses
    say(run, 2, "Great, okay.", 8.0)
    assert run.has(Ignored)[0].decision.via == "small-talk" and run.m.talk.closes_at() == pytest.approx(35.0)
    say(run, 3, "That's all.", 10.0)
    assert run.has(Command)[0].decision.text == "That's all"
    run(Progress(State.ACTING, 11.1), EndTalk(11.2))
    assert talk_events(run.effects, "closed")[0].why == "phrase" and not outputs(run.m).talking
    run(Done(True, 11.3, "Talk session ended"))
    assert run.state is State.DONE and run.m.follow_up_until == 0.0
    run(Tick(11.3 + DONE_S))
    assert run.state is State.IDLE and not outputs(run.m).attentive


def test_flow_2_three_clauses_in_one_breath_while_the_first_acts():
    run = talking(0.0)
    say(run, 1, "open Fusion", 1.0)
    run(Progress(State.ACTING, 2.1))
    say(run, 2, "snap it to the left, open Claude and put it on the right", 5.0, 7.9, 8.1)
    assert [q.decision.text for q in run.has(Queued)] == ["snap it to the left", "open Claude", "put it on the right"]
    assert run.state is State.ACTING and not run.has(Command)
    waited = []
    for at in (12.0, 13.0, 14.0):
        run(Done(True, at))
        waited.append(run.has(Command)[0].waited)
    assert waited == [pytest.approx(4.1), pytest.approx(5.1), pytest.approx(6.1)]
    run(Done(True, 15.0))
    assert run.state is State.DONE and run.m.queue == ()
