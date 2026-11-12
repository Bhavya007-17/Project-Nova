"""Nova's state machine (SPEC §5): key gestures + speech + finals -> states and effects."""

from nova.gate import Env, StubResolver, WakeName
from nova.keys import Cancel, DoubleTap, Press, Release
from nova.state import (
    CLAIM_S,
    DICTATION_SILENCE_S,
    DONE_S,
    FOLLOW_UP_S,
    HEARD_WAIT_S,
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
    run(SpeechStart(2, 2.2), SpeechEnd(2, "silence", 2.8), heard(2, "yes", 3.0))
    assert run.has(Answer)[0].text == "yes" and run.state is State.CONFIRM
    run(Tick(1.4 + 15.0))
    assert run.state is State.IDLE and run.has(Cancelled)[0].why == "no answer"


# --- a yes must be a real yes (SPEC §5, §14) -----------------------------------------------------------


def asking(kind=State.CONFIRM, at=10.0):
    return Run()(heard(1, "Nova, send it", at - 0.5), Progress(kind, at))


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
