"""What the island shows (SPEC §6): the pure reducer from bus events to the view."""

from nova.config import Settings, Triggers
from nova.events import (
    AlwaysListeningChanged,
    Asking,
    ConfigChanged,
    CommandFinished,
    DictatedText,
    FinalTranscript,
    Gated,
    JobChip,
    MicChanged,
    PartialTranscript,
    PinToggled,
    SpeechReady,
    StateChanged,
    StepsChanged,
)
from nova.island.view import ACCENTS, PASSTHROUGH_END, TEACHING_NOTE, View, fresh_index, reduce

READY = reduce(View(), SpeechReady(True, "parakeet"))


def feed(view, *events):
    for event in events:
        view = reduce(view, event)
    return view


def go(old, new, why=""):
    return StateChanged(old, new, why or new, "copilot", 0.0)


def partial(utterance, text, windowed=False):
    return PartialTranscript(utterance, text, windowed)


def final(utterance, text):
    return FinalTranscript(utterance, text, text, "key", 100.0, 200.0, 1000.0)


def gated(utterance, text, accepted=True, wake_only=False):
    return Gated(utterance, accepted, "key", text, text, "why", False, wake_only, 0.0)


# --- fresh words -------------------------------------------------------------------------------------


def test_fresh_index_marks_where_new_words_start():
    assert fresh_index("open", "open chrome") == 4
    assert fresh_index("open chr", "open chrome") == 4  # a word that grew counts as new
    assert fresh_index("open chrome", "open chrome") == len("open chrome")
    assert fresh_index("", "open") == 0
    assert fresh_index("close", "open chrome") == 0
    assert fresh_index("open  my", "open  my thermo") == len("open  my")


# --- a key session ------------------------------------------------------------------------------------


def test_hold_talk_release_done_collapse():
    view = feed(READY, go("idle", "listening", "Copilot key"))
    assert view.state == "listening" and view.open and view.visible and not view.expanded
    assert view.placeholder == "Listening…" and view.accent == ACCENTS["listening"]
    view = feed(view, partial(1, "open"))
    assert (view.words, view.final, view.fresh) == ("open", False, 0)
    view = feed(view, partial(1, "open chrome"))
    assert view.fresh == 4
    view = feed(view, go("listening", "thinking", "heard you"))
    assert view.state == "thinking" and view.words == "open chrome"  # live words stay until the final
    view = feed(view, final(1, "Open Chrome."), gated(1, "Open Chrome."))
    assert view.words == "Open Chrome." and view.final
    view = feed(view, CommandFinished(True, "Opened Chrome"), go("thinking", "done", "Opened Chrome"))
    assert (view.state, view.icon, view.result) == ("done", "ok", "Opened Chrome")
    view = feed(view, go("done", "idle"))
    assert view.state == "hidden" and not view.visible
    assert view.result == "Opened Chrome"  # still on the pill while it collapses


def test_placeholder_says_what_is_wrong_first():
    view = reduce(View(), go("idle", "listening"))
    assert view.placeholder == "Loading speech…"
    assert reduce(view, SpeechReady(False, "no model")).placeholder == "Speech-to-text isn't working"
    view = feed(view, SpeechReady(True, "ok"), MicChanged(False, "Mic", "no mic found"))
    assert view.placeholder == "No mic — no mic found"
    assert reduce(view, MicChanged(True, "Mic", "listening")).placeholder == "Listening…"


def test_a_newer_utterance_takes_over_and_older_ones_are_ignored():
    view = feed(READY, go("idle", "listening"), partial(3, "hello there"))
    view = reduce(view, partial(2, "late words"))
    assert view.words == "hello there"
    view = reduce(view, partial(4, "open"))
    assert (view.words, view.utterance, view.fresh) == ("open", 4, 0)


def test_live_words_only_while_a_session_listens():
    assert reduce(READY, partial(1, "chatter in the room")).words == ""  # hidden
    view = feed(READY, go("idle", "listening"), partial(1, "open"), go("listening", "thinking"))
    assert reduce(view, partial(1, "open chrome and")).words == "open"
    assert reduce(view, final(2, "someone else")).words == "open"  # not this session's utterance


def test_windowed_partials_show_the_tail_without_fading():
    view = feed(READY, go("idle", "listening"), partial(1, "the end of a long sentence", windowed=True))
    assert view.windowed and view.fresh == len(view.words)
    view = reduce(view, final(1, "The whole long sentence."))
    assert not view.windowed and view.final


def test_name_alone_clears_the_words_for_the_command():
    view = feed(READY, go("idle", "listening"), partial(1, "Nova"), final(1, "Nova"), gated(1, "", wake_only=True))
    assert (view.words, view.utterance) == ("", None)
    assert reduce(view, partial(2, "open spotify")).words == "open spotify"


def test_always_listening_command_starts_fresh_and_shows_the_command():
    old = feed(READY, go("idle", "listening"), partial(1, "old"), StepsChanged(("Chrome",)), go("listening", "acting"),
               CommandFinished(True, "Opened Chrome"), go("acting", "done"), go("done", "idle"))
    view = feed(old, go("idle", "thinking", "always"))
    assert (view.words, view.steps, view.result) == ("", (), "")
    view = reduce(view, gated(7, "open spotify"))
    assert view.words == "open spotify" and view.final


def test_follow_up_command_from_done_starts_fresh():
    view = feed(READY, go("idle", "thinking"), gated(1, "open chrome"), StepsChanged(("Chrome",)),
                CommandFinished(True, "Opened Chrome"), go("thinking", "done"))
    view = feed(view, go("done", "thinking", "follow-up"))
    assert (view.steps, view.result, view.icon) == ((), "", "")


# --- acting, results ------------------------------------------------------------------------------------


def test_steps_and_the_running_one():
    view = feed(READY, go("idle", "thinking"), StepsChanged(("Chrome", "Canvas", "Today"), 1), go("thinking", "acting"))
    assert view.steps == ("Chrome", "Canvas", "Today") and view.step == 1
    assert reduce(view, StepsChanged(("Chrome",), 5)).step == 0  # clamped


def test_learned_skill_shows_lightning():
    view = feed(READY, go("idle", "thinking"), CommandFinished(True, "Canvas · Dashboard", skill=True), go("thinking", "done"))
    assert view.icon == "skill" and view.accent == ACCENTS["done"]


def test_error_has_buttons_and_falls_back_to_the_why():
    view = feed(READY, go("idle", "thinking"), CommandFinished(False, "Chrome didn't open"), go("thinking", "error"))
    assert (view.icon, view.result, view.buttons) == ("fail", "Chrome didn't open", ("Try again", "Show me"))
    assert view.expanded and view.interactive and view.accent == ACCENTS["error"]
    view = feed(READY, go("idle", "thinking"), go("thinking", "error", "timed out"))
    assert (view.icon, view.result) == ("fail", "timed out")


# --- asking -----------------------------------------------------------------------------------------------


def test_confirm_shows_the_exact_action_with_no_and_yes():
    view = feed(READY, go("idle", "thinking"), Asking("confirm", "Submit 'HW 5' on Canvas?", ("ignored",)),
                go("thinking", "confirm"))
    assert (view.question, view.choices, view.buttons) == ("Submit 'HW 5' on Canvas?", (), ("No", "Yes"))
    assert view.expanded and view.interactive and view.accent == ACCENTS["confirm"]


def test_clarify_keeps_up_to_four_choices():
    choices = ("Google Chrome", "Chrome Canary", "Chrome Beta", "Chrome Dev", "Chromium")
    view = feed(READY, go("idle", "thinking"), Asking("clarify", "Which Chrome?", choices), go("thinking", "clarify"))
    assert view.choices == choices[:4] and view.interactive and view.buttons == ()
    assert reduce(reduce(READY, go("idle", "thinking")), go("thinking", "clarify", "Which one?")).question == "Which one?"


# --- teaching, dictating, passthrough ----------------------------------------------------------------------


def test_teaching_and_passthrough_notes():
    assert reduce(READY, go("thinking", "teaching")).note == TEACHING_NOTE
    assert reduce(READY, go("thinking", "passthrough", "Claude voice")).note == f"Claude voice · {PASSTHROUGH_END}"
    view = reduce(READY, StateChanged("thinking", "passthrough", "", None, 0.0))
    assert view.note.startswith("Voice mode")


def test_dictating_shows_live_words_then_the_sentence_typed():
    view = feed(READY, go("idle", "dictating", "double-tap"))
    assert view.placeholder == "Dictating…" and view.accent == ACCENTS["dictating"]
    view = reduce(view, partial(5, "so the first law"))
    assert view.words == "so the first law" and not view.final
    view = reduce(view, final(5, "raw final"))
    assert view.words == "so the first law"  # the typed sentence comes as DictatedText
    view = reduce(view, DictatedText(5, "So the first law.", 0.0))
    assert view.words == "So the first law." and view.final


# --- chips ---------------------------------------------------------------------------------------------------


def test_chips_update_in_place_and_keep_the_window_up():
    view = feed(READY, JobChip("a", "Claude · HW 5"), JobChip("b", "ChatGPT · Carnot"))
    assert [c.id for c in view.chips] == ["a", "b"]
    assert view.visible and not view.open and view.interactive  # chips alone: clickable, pill closed
    view = reduce(view, JobChip("a", "Claude · HW 5", "done"))
    assert [(c.id, c.status) for c in view.chips] == [("a", "done"), ("b", "working")]
    view = feed(view, JobChip("a", "", "gone"), JobChip("b", "", "gone"))
    assert view.chips == () and not view.visible


def test_off_and_idle_hide_and_unrelated_events_change_nothing():
    view = feed(READY, go("idle", "listening"))
    assert reduce(view, go("listening", "off")).state == "hidden"
    assert reduce(view, object()) is view


# --- pinned (Ctrl + Copilot) -----------------------------------------------------------------------------


def test_pin_keeps_a_status_pill_between_commands():
    view = reduce(READY, PinToggled(1.0))
    assert view.pinned and view.state == "idle" and view.open and view.interactive  # draggable
    view = feed(view, go("idle", "listening"), partial(1, "open chrome"), go("listening", "thinking"),
                CommandFinished(True, "Opened Chrome"), go("thinking", "done"))
    assert view.state == "done" and view.interactive  # pinned: the whole pill takes clicks (drag)
    view = reduce(view, go("done", "idle"))
    assert view.state == "idle" and view.visible  # back to the status pill, not hidden
    view = reduce(view, PinToggled(2.0))
    assert not view.pinned and view.state == "hidden" and not view.visible


def test_unpinning_mid_command_lets_it_hide_afterwards():
    view = feed(READY, PinToggled(1.0), go("idle", "listening"), PinToggled(2.0))
    assert view.state == "listening" and not view.pinned
    assert reduce(view, go("listening", "idle")).state == "hidden"


def test_off_hides_the_pinned_pill_and_on_brings_it_back():
    view = feed(READY, PinToggled(1.0), go("idle", "off"))
    assert view.state == "hidden" and view.off and view.pinned
    view = reduce(view, PinToggled(2.0))  # (the hotkey is Windows' again while off; nothing breaks if it arrives)
    view = reduce(view, PinToggled(3.0))
    assert view.state == "hidden"
    assert reduce(view, go("off", "idle")).state == "idle"


def test_a_command_from_the_pinned_pill_starts_fresh():
    view = feed(READY, PinToggled(1.0), StepsChanged(("Chrome",)), CommandFinished(True, "old"))
    view = reduce(view, go("idle", "thinking", "always"))
    assert (view.steps, view.result) == ((), "")


def test_status_dot_follows_always_listening_like_the_controller():
    view = View(always=True, file_always=True)
    view = reduce(view, AlwaysListeningChanged(False))  # tray toggle
    assert not view.always
    view = reduce(view, ConfigChanged(Settings()))  # unrelated save (file still says on): the toggle stays
    assert not view.always
    off = Settings(triggers=Triggers(always_listening=False))
    view = feed(view, ConfigChanged(off), ConfigChanged(Settings()))  # the file itself turned it back on
    assert view.always and view.file_always


def test_dictating_shows_what_was_typed_and_what_was_saved_instead():
    from nova.events import DictationTyped

    view = feed(READY, go("idle", "dictating", "double-tap"), DictatedText(5, "um so the first law", 0.0))
    view = reduce(view, DictationTyped(5, "So the first law."))
    assert view.words == "So the first law." and view.final
    view = reduce(view, DictationTyped(6, "secret", False, "That's a password field · Saved"))
    assert view.words == "That's a password field · Saved"
    assert reduce(feed(READY), DictationTyped(1, "x")).words == ""  # not dictating: nothing to show
