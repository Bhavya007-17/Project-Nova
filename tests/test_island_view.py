"""What the island shows (SPEC §6): the pure reducer from bus events to the view."""

from nova.config import Settings, Triggers
from nova.events import (
    WARMING,
    AlwaysListeningChanged,
    BrainProgress,
    Asking,
    ClausesSkipped,
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
    TalkChanged,
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


# --- a cold model (Stage 07a) -------------------------------------------------------------------------------


def test_a_cold_model_says_warming_up_instead_of_his_words():
    view = feed(READY, go("idle", "listening"), final(1, "make this bigger"), go("listening", "thinking"),
                gated(1, "make this bigger"))
    assert view.words == "make this bigger"
    view = reduce(view, BrainProgress("warming", WARMING))
    assert view.state == "thinking" and view.words == WARMING and view.final and view.warming
    view = reduce(view, gated(1, "make this bigger"))  # late events don't hide it
    assert view.words == WARMING
    view = feed(view, go("thinking", "acting"))
    assert not view.warming


def test_warming_that_arrives_before_thinking_still_shows():
    view = feed(READY, BrainProgress("warming", WARMING), go("idle", "thinking"), gated(4, "open the thing"))
    assert view.state == "thinking" and view.words == WARMING


def test_the_next_command_doesnt_inherit_warming():
    view = feed(READY, go("idle", "thinking"), BrainProgress("warming", WARMING), go("thinking", "done"),
                go("done", "thinking"), gated(2, "open notepad"))
    assert view.words == "open notepad" and not view.warming


def test_once_loaded_the_island_goes_back_to_his_words():
    view = feed(READY, go("idle", "listening"), final(1, "make this bigger"), go("listening", "thinking"),
                gated(1, "make this bigger"), BrainProgress("warming", WARMING))
    assert view.words == WARMING
    view = reduce(view, BrainProgress("thinking"))  # loaded: the model is answering now
    assert view.state == "thinking" and view.words == "make this bigger" and view.final and not view.warming


def test_once_loaded_the_island_shows_the_command_that_arrived_while_warming():
    view = feed(READY, BrainProgress("warming", WARMING), go("idle", "thinking"), gated(4, "open the thing"))
    assert view.words == WARMING
    view = reduce(view, BrainProgress("thinking"))
    assert view.state == "thinking" and view.words == "open the thing" and not view.warming


def test_thinking_progress_without_warming_leaves_the_view_alone():
    view = feed(READY, go("idle", "thinking"), gated(1, "x y"))
    assert reduce(view, BrainProgress("thinking")) == view  # his answer going back to the model (router)


def test_other_brain_progress_leaves_the_view_alone():
    view = feed(READY, go("idle", "thinking"), gated(1, "x y"))
    assert reduce(view, BrainProgress("acting")) == view


# --- talk sessions (Stage 07b): the session pill ---------------------------------------------------------------


def talk(what, why="", closes_at=30.0):
    return TalkChanged(what, why or what, None if what in ("paused", "closed") else closes_at, 0.0)


def clause(utterance, text, index=0, accepted=True, via="talk"):
    return Gated(utterance, accepted, via, text, text, "talk session", False, False, 0.0, True, True, index)


def small_talk(utterance, text):
    return Gated(utterance, False, "small-talk", text, text, "small talk", False, False, 0.0, True)


def opened(view=READY):
    """A Copilot tap: listening while the key is down, then the talk opens and the machine goes idle."""
    return feed(view, go("idle", "listening", "Copilot key"), talk("opened", "copilot"), go("listening", "idle", "talk session"))


def running(view, utterance, text, steps, index=0):
    """One clause from thinking to acting (his words are already on the pill)."""
    return feed(view, go(view.machine, "thinking"), clause(utterance, text, index), StepsChanged(steps),
                go("thinking", "acting"))


def test_talk_opened_shows_the_session_pill():
    view = feed(READY, go("idle", "listening", "Copilot key"), partial(1, "um"))
    view = reduce(view, talk("opened", "copilot"))
    assert view.state == "session" and view.talk and view.open and view.visible and not view.expanded
    assert view.words == "" and view.placeholder == "Listening…"  # fresh words for the session
    assert view.accent == ACCENTS["listening"] and view.beneath == "" and not view.closing
    view = reduce(view, go("listening", "idle", "talk session"))
    assert view.state == "session" and view.visible


def test_idle_inside_a_session_keeps_the_pill():
    view = feed(opened(), final(1, "Open Notepad."))
    view = running(view, 1, "Open Notepad", ("Notepad",))
    view = feed(view, CommandFinished(True, "Opened Notepad"), go("acting", "done"), talk("resumed", "done"))
    view = reduce(view, go("done", "idle"))
    assert view.state == "session" and view.visible and view.beneath == ""  # the pill never collapses between clauses
    assert view.words == "Open Notepad"  # row 1 keeps his last words
    stopped = feed(opened(), final(2, "Open Fusion."))
    stopped = reduce(running(stopped, 2, "Open Fusion", ("Fusion",)), go("acting", "idle", "stopped"))
    assert stopped.state == "session" and stopped.beneath == ""


def test_done_inside_a_session_shows_the_result_under_the_words():
    view = feed(opened(), final(1, "Open Notepad and then create a new note."), talk("paused", "running"),
                go("idle", "thinking"), clause(1, "Open Notepad"))
    assert (view.state, view.beneath, view.words) == ("session", "thinking", "Open Notepad")
    view = feed(view, StepsChanged(("Notepad",)), go("thinking", "acting"))
    assert view.beneath == "acting" and view.steps == ("Notepad",)
    view = feed(view, CommandFinished(True, "Opened Notepad"), go("acting", "thinking"), clause(1, "create a new note", 1))
    assert (view.beneath, view.steps, view.result, view.icon) == ("thinking", (), "", "")  # the next clause starts clean
    assert view.words == "create a new note"
    view = feed(view, StepsChanged(("Notepad", "new note"), 1), go("thinking", "acting"),
                CommandFinished(True, "New note in Notepad"), go("acting", "done"), talk("resumed", "done"))
    assert (view.state, view.beneath, view.icon, view.result) == ("session", "done", "ok", "New note in Notepad")
    assert view.words == "create a new note" and view.accent == ACCENTS["listening"]  # still the session's blue pill
    assert reduce(feed(opened(), go("idle", "thinking")), go("thinking", "done", "Opened it")).result == "Opened it"


def test_live_words_roll_in_every_state_while_talking():
    view = running(feed(opened(), final(1, "Open Fusion.")), 1, "Open Fusion", ("Fusion",))
    view = reduce(view, partial(2, "snap it"))
    assert view.words == "snap it" and not view.final  # acting, and his next words still roll in
    view = reduce(view, partial(2, "snap it to the left"))
    assert view.fresh == len("snap it")
    view = reduce(view, final(2, "Snap it to the left."))
    assert view.words == "Snap it to the left." and view.final
    assert reduce(view, clause(1, "an older clause", 1)).words == "Snap it to the left."  # what he says now stays
    assert reduce(view, partial(1, "late words")).words == "Snap it to the left."
    asking = feed(view, Asking("confirm", "Close Fusion without saving?"), go("acting", "confirm"), partial(3, "yes close it"))
    assert asking.state == "confirm" and asking.words == "yes close it"
    heard = reduce(opened(), final(4, "Great, okay."))
    assert heard.words == "Great, okay."
    assert reduce(heard, small_talk(4, "Great, okay.")) == heard  # dropped small talk changes nothing on screen
    assert reduce(heard, clause(4, "", accepted=False, via="stop")) == heard


def test_closing_shrinks_and_closed_hides():
    view = reduce(opened(), talk("closing", "idle"))
    assert view.closing and view.state == "session"
    view = reduce(view, talk("closed", "idle"))
    assert view.state == "hidden" and not view.talk and not view.closing and not view.visible
    assert view.beneath == "" and view.skipped == ""
    pinned = reduce(opened(reduce(READY, PinToggled(1.0))), talk("closed", "Esc"))
    assert pinned.state == "idle" and pinned.visible  # back to the pinned status pill
    tapped = running(feed(opened(), final(1, "Open Fusion.")), 1, "Open Fusion", ("Fusion",))
    tapped = reduce(tapped, talk("closed", "tap"))  # the running step goes on: its own face
    assert tapped.state == "acting" and tapped.steps == ("Fusion",) and not tapped.talk
    assert reduce(tapped, go("acting", "idle")).state == "hidden"
    assert reduce(READY, talk("closing", "idle")) == READY  # no session: nothing to shrink


def test_paused_cancels_the_shrink():
    view = feed(opened(), talk("closing", "idle"), talk("paused", "running"))
    assert not view.closing and view.state == "session"
    view = feed(view, talk("resumed", "ignored"), talk("closing", "idle"))
    assert view.closing
    assert not reduce(view, talk("resumed", "name")).closing


def test_skipped_clauses_show_under_the_error():
    view = feed(opened(), final(1, "Open the notes app, then create a new note and make the title say hello."),
                go("idle", "thinking"), clause(1, "open the notes app"), CommandFinished(False, "Couldn't find the notes app"),
                go("thinking", "error"),
                ClausesSkipped(("create a new note", "make the title say hello"), "the step before it didn't finish", 0.0),
                talk("resumed", "done"))
    assert (view.state, view.beneath, view.icon, view.result) == ("session", "error", "fail", "Couldn't find the notes app")
    assert view.skipped == "Skipped: create a new note, make the title…"  # cut to fit
    assert not view.expanded and view.buttons == () and view.accent == ACCENTS["listening"]
    view = reduce(view, go("error", "idle"))
    assert view.state == "session" and view.skipped  # still says what didn't run until the next command
    assert reduce(view, small_talk(2, "great")).skipped
    view = feed(view, go("idle", "thinking"), clause(3, "open chrome"))
    assert view.skipped == ""
    stopped = reduce(opened(), ClausesSkipped(("open x.com",), "stopped", 0.0))
    assert stopped.skipped == "Skipped: open x.com" and stopped.beneath == ""
    assert reduce(stopped, ClausesSkipped((), "stopped", 0.0)) == stopped


def test_confirm_inside_a_session_still_expands():
    view = running(feed(opened(), final(1, "Close Notepad.")), 1, "Close Notepad", ("Notepad",))
    view = feed(view, Asking("confirm", "Close Notepad without saving?"), go("acting", "confirm"))
    assert view.state == "confirm" and view.expanded and view.buttons == ("No", "Yes") and view.talk
    assert view.question == "Close Notepad without saving?" and view.accent == ACCENTS["confirm"]
    view = reduce(view, go("confirm", "acting"))
    assert (view.state, view.beneath, view.steps) == ("session", "acting", ("Notepad",))  # the same command goes on
    view = feed(view, CommandFinished(True, "Closed Notepad"), go("acting", "done"))
    assert view.result == "Closed Notepad" and view.beneath == "done"
    asked = feed(opened(), go("idle", "thinking"), Asking("clarify", "Which Chrome?", ("A", "B")), go("thinking", "clarify"))
    assert asked.state == "clarify" and asked.choices == ("A", "B")
    assert reduce(asked, go("clarify", "thinking")).question == "Which Chrome?"  # his answer goes back: same command


def test_dictating_inside_a_session_then_back_to_the_pill():
    view = feed(opened(), final(1, "Type this hello world."), talk("paused", "running"), go("idle", "thinking"),
                clause(1, "type this hello world"), talk("paused", "dictating"), go("thinking", "dictating"))
    assert view.state == "dictating" and view.accent == ACCENTS["dictating"] and view.placeholder == "Dictating…"
    view = feed(view, final(2, "raw words"), DictatedText(2, "Hello world.", 0.0))
    assert view.words == "Hello world."
    view = feed(view, go("dictating", "idle", "tap"), talk("resumed", "done"))
    assert (view.state, view.beneath, view.words) == ("session", "", "") and view.placeholder == "Listening…"
    assert reduce(view, partial(1, "older words")).words == ""
    assert reduce(view, partial(3, "open chrome")).words == "open chrome"


def test_the_session_pill_takes_clicks():
    view = opened()
    assert view.interactive and view.buttons == ()  # the stop button
    assert view.interactive == reduce(view, go("idle", "thinking")).interactive
    assert not reduce(view, talk("closed", "stop button")).interactive


def test_warming_inside_a_session_keeps_his_words():
    view = feed(opened(), final(1, "make this bigger"), go("idle", "thinking"), clause(1, "make this bigger"),
                BrainProgress("warming", WARMING))
    assert view.warming and view.words == "make this bigger" and view.beneath == "thinking"
    view = reduce(view, BrainProgress("thinking"))
    assert not view.warming and view.words == "make this bigger"
    closed = feed(opened(), go("idle", "thinking"), clause(2, "make it red"), BrainProgress("warming", WARMING),
                  talk("closed", "tap"))
    assert closed.state == "thinking" and closed.words == WARMING  # its own face again: says why it waits


def test_a_command_that_opens_the_session_keeps_its_words():
    view = feed(READY, final(1, "Nova, open Chrome."), talk("opened", "named"), go("idle", "thinking", "wake"),
                gated(1, "open chrome"))
    assert (view.state, view.beneath, view.words) == ("session", "thinking", "open chrome")
    late = feed(READY, go("idle", "thinking", "wake"), gated(1, "open chrome"), talk("opened", "named"))
    assert (late.state, late.beneath, late.words) == ("session", "thinking", "open chrome")  # either order
