"""Dictation rules (SPEC §11): spoken commands, fillers, self-corrections, stop phrases, joins at the cursor."""

import pytest

from nova.dictation.rules import Joined, Parsed, join, landed, parse, split_stop


def text(said: str) -> str:
    return parse(said).text


# --- fillers -------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("said, want", [
    ("Um, so I wanted to check in about the, uh, robotics project.",
     "So I wanted to check in about the robotics project."),
    ("Uh I think so.", "I think so."),
    ("It works, erm, most of the time.", "It works, most of the time."),
    ("We need apples, um, oranges and pears.", "We need apples, oranges and pears."),
    ("Hmm. Let me think.", "Let me think."),
    ("We, um, uh, finished it.", "We finished it."),
    ("Call the ER now.", "Call the ER now."),  # an acronym, not a filler
])
def test_fillers_go(said, want):
    assert text(said) == want


@pytest.mark.parametrize("said, want", [
    ("I like it.", "I like it."),
    ("It looks like rain.", "It looks like rain."),
    ("It was, like, really big.", "It was really big."),
    ("Like, I don't know.", "I don't know."),
    ("Do you know the answer?", "Do you know the answer?"),
    ("You know what I mean?", "You know what I mean?"),
    ("It's, you know, complicated.", "It's complicated."),
    ("You know, I think we should go.", "I think we should go."),
    ("I'd like you to know that.", "I'd like you to know that."),
])
def test_like_and_you_know_only_when_clearly_filler(said, want):
    assert text(said) == want


def test_stutters_on_small_words_collapse():
    assert text("I I think the the plan works.") == "I think the plan works."
    assert text("I know that that is true.") == "I know that that is true."  # grammatical


# --- spoken commands -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("said, want", [
    ("First line. New line. Second line.", "First line.\nSecond line."),
    ("End of the intro. New paragraph. Next part.", "End of the intro.\n\nNext part."),
    ("New paragraph.", "\n\n"),
    ("new line and then we left", "\nAnd then we left"),
    ("Dear team, new paragraph. We met today.", "Dear team,\n\nWe met today."),
    ("Let's start a new paragraph here.", "Let's start a new paragraph here."),  # talking about one
])
def test_new_line_and_new_paragraph(said, want):
    assert text(said) == want


@pytest.mark.parametrize("said, want", [
    ("Hello comma how are you question mark", "Hello, how are you?"),
    ("Hello comma, how are you question mark?", "Hello, how are you?"),
    ("That's it period", "That's it."),
    ("That's it full stop.", "That's it."),
    ("Dear team colon", "Dear team:"),
    ("Wow exclamation point", "Wow!"),
    ("Wow exclamation mark.", "Wow!"),
    ("First semicolon second.", "First; second."),
    ("Wait for the period of the wave.", "Wait for the period of the wave."),
    ("Add a comma there.", "Add a comma there."),
])
def test_punctuation_words(said, want):
    assert text(said) == want


# --- scratch that ------------------------------------------------------------------------------------------------


def test_scratch_that_deletes_the_sentence_before_it_in_the_segment():
    assert parse("I'll be there at five. Scratch that. I'll be there at six.") == Parsed("I'll be there at six.")
    assert parse("One. Two. Scratch that.") == Parsed("One.")
    assert parse("We should go, scratch that, we should stay.") == Parsed("We should stay.")


def test_scratch_that_at_the_start_deletes_the_last_segment_typed():
    assert parse("Scratch that.") == Parsed("", scratch=1)
    assert parse("Scratch that. Let's go.") == Parsed("Let's go.", scratch=1)
    assert parse("Delete that.") == Parsed("", scratch=1)
    assert parse("Scratch that. Scratch that.") == Parsed("", scratch=2)


def test_scratch_that_as_words_in_a_sentence_still_scratches():
    # in dictation "scratch that" is always a command (SPEC §11)
    assert parse("Fine, scratch that.") == Parsed("")


# --- self-corrections --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("said, want", [
    ("Let's meet Tuesday, no wait, Wednesday at noon.", "Let's meet Wednesday at noon."),
    ("Let's meet Tuesday. No, wait, Wednesday at noon.", "Let's meet Wednesday at noon."),
    ("Send it to John, no wait, send it to Mary.", "Send it to Mary."),
    ("Send it to John, no wait, to Mary.", "Send it to Mary."),
    ("We need five, no wait, six chairs.", "We need six chairs."),
    ("Call me at 5, no wait, 6.", "Call me at 6."),
    ("I'll bring cookies, no wait, brownies.", "I'll bring brownies."),
    ("We should go to the park, no wait, the beach.", "We should go to the beach."),
    ("It's big, or rather, huge.", "It's huge."),
    ("The red one, no, I mean the blue one.", "The blue one."),
    ("It's on March 3rd, wait, no, March 4th.", "It's on March 4th."),
])
def test_self_corrections_keep_the_correction(said, want):
    assert text(said) == want


@pytest.mark.parametrize("said", [
    "No, wait for me.",
    "I said no, wait until tomorrow.",
    "I'm fine, I mean, it's okay.",
    "No wait times today.",
])
def test_not_corrections(said):
    assert text(said) == said


# --- tidy ------------------------------------------------------------------------------------------------------------


def test_spacing_and_doubled_punctuation():
    assert text("Hello , world .") == "Hello, world."
    assert text("Done.. Next") == "Done. Next"
    assert text("We went, um.") == "We went."


def test_sentence_starts_are_capitalized_but_odd_casing_is_kept():
    assert text("um the plan works.") == "The plan works."
    assert text("iPhone sales grew.") == "iPhone sales grew."


def test_nothing_said():
    assert parse("") == Parsed("")
    assert parse("Um.") == Parsed("")


# --- stop ---------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("said, want", [
    ("Stop dictation.", ("", True)),
    ("stop dictating", ("", True)),
    ("Nova, stop dictation.", ("", True)),
    ("End dictation.", ("", True)),
    ("Dictation off.", ("", True)),
    ("Turn off dictation please.", ("", True)),
    ("That's all for today. Stop dictation.", ("That's all for today.", True)),
    ("That's it, stop dictation.", ("That's it", True)),
    ("And dictation.", ("", True)),  # "end dictation", as speech-to-text heard it
    ("Nova, stop.", ("", True)),
    ("We tested speech, and dictation.", ("We tested speech, and dictation.", False)),
    ("I want to stop the dictation", ("I want to stop the dictation", False)),
    ("Please don't stop dictation software.", ("Please don't stop dictation software.", False)),
])
def test_stop_phrase_at_the_end(said, want):
    assert split_stop(said) == want


# --- joins at the cursor --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("before, new, want", [
    (None, "Hello world.", "Hello world."),
    ("", "hello world.", "Hello world."),
    ("Dear team,\n", "We met.", "We met."),
    ("I went home.", "Then I slept.", " Then I slept."),
    ("I went home. ", "Then I slept.", "Then I slept."),
    ("The deadline is", "Friday at noon.", " Friday at noon."),
    ("I wanted to check in", "About the project.", " about the project."),
    ("We tested it with", "I think three robots.", " I think three robots."),
    ("We tested it with", "Claude yesterday.", " Claude yesterday."),
    ("Hello", "\n\nNext part.", "\n\nNext part."),
    ("Time:", "Noon.", " Noon."),
    ("He said (", "Maybe.", "Maybe."),
    ("Is it done?", "yes.", " Yes."),
])
def test_join(before, new, want):
    assert join(before, new) == Joined(want)


def test_join_drops_a_period_that_cut_a_sentence_in_half():
    assert join("I want to go to.", "The store.") == Joined(" the store.", erase=1)
    assert join("I went to the store.", "Then home.") == Joined(" Then home.")


def test_landed_compares_the_end_of_the_field():
    assert landed("Hi there. We met today.", " We met today.")
    assert landed("Hi\r\n\r\nNext", "\n\nNext")
    assert landed("x a b", " a b")  # editors turn spaces into no-break spaces
    assert not landed("Hi there.", " We met today.")
    assert not landed(None, "x")


@pytest.mark.parametrize("said, want", [
    ("No wait.", "only"),
    ("No, wait.", "only"),
    ("Sorry, I mean.", "only"),
    ("No wait, Wednesday at noon.", "lead"),
    ("No, wait. Wednesday at noon.", "lead"),
    ("No wait times today.", ""),
    ("Wednesday at noon.", ""),
])
def test_a_segment_that_corrects_the_one_before(said, want):
    from nova.dictation.rules import correction_lead

    assert correction_lead(said) == want


def test_a_correction_split_across_segments_reads_as_one():
    assert text("Let's meet on Tuesday. No wait. Wednesday at noon to plan it.") == \
        "Let's meet on Wednesday at noon to plan it."
