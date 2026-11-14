"""Talk-session clauses (Stage 07b, SPEC §5): a final -> clauses in order, small talk marked, text cut out."""

import pytest

from nova import clauses, gate
from nova.clauses import Clause, split, strip

# The reference recording (docs/prompts/07b-sessions.md), one piece per line as he pauses; the camera left out (07c).
RECORDING = [
    "Alright, can you open up the notes app for me",
    "and once you're there can you create a new note",
    "and inside this new note let's make the title say hello",
    "Great, okay.",
    "Let's move on and can you open up the browser",
    "google search Norbert Wiener",
    "now open x.com",
    "Nice.",
    "Cool, thank you.",
]
# The same, said in one breath: one final, the way speech-to-text punctuates a run-on.
RUN_ON = (
    "Alright, can you open up the notes app for me and once you're there can you create a new note and inside this "
    "new note let's make the title say hello. Great, okay. Let's move on and can you open up the browser. Google "
    "search Norbert Wiener. Now open x.com. Nice. Cool, thank you."
)
WANTED = [
    "open up the notes app",
    "create a new note",
    "inside this new note let's make the title say hello",
    "open up the browser",
    "google search norbert wiener",
    "open x.com",
]


def commands(found):
    return [c.text.lower() for c in found if c.kind == "command"]


def small_talk(found):
    return [c for c in found if c.kind != "command"]


def test_split_at_continuations_that_start_a_request():
    found = split("open notepad and then create a new note")
    assert [c.text for c in found] == ["open notepad", "create a new note"]
    assert all(c.kind == "command" and c.request for c in found)


@pytest.mark.parametrize("final", ["open chrome and fusion", "put fusion left and claude right"])
def test_and_without_a_verb_stays_one_clause(final):
    found = split(final)
    assert len(found) == 1 and found[0].text == final


def test_sentences_always_split():
    assert [c.text for c in split("Open Chrome. Notepad.")] == ["Open Chrome", "Notepad"]
    assert [c.text for c in split("type hello! open chrome; close it?")] == ["type hello", "open chrome", "close it"]


def test_the_recording_splits_into_its_actions_and_small_talk():
    by_line = [c for line in RECORDING for c in split(line)]
    whole = split(RUN_ON)
    for found in (by_line, whole):
        assert commands(found) == WANTED
        assert len(small_talk(found)) == 3
        assert all(c.kind == "ack" for c in small_talk(found))
    # Nothing from the small talk leaks into a command, and the camera clause (07c) isn't there.
    for c in by_line + list(whole):
        if c.kind == "command":
            assert not any(word in c.text.lower() for word in ("great", "okay", "nice", "cool", "thank"))
    assert not any("photo" in c.text.lower() for c in whole)


def test_the_recording_with_the_closing_phrase_has_seven_commands():
    # Case 1 of docs/eval/sessions.yaml ends "Cool, thank you. That's all." -> a 7th command, the closing phrase.
    by_line = [c for line in [*RECORDING[:-1], "Cool, thank you. That's all."] for c in split(line)]
    whole = split(RUN_ON + " That's all.")
    for found in (by_line, whole):
        assert commands(found) == [*WANTED, "that's all"]
        assert len(small_talk(found)) == 3


@pytest.mark.parametrize(
    ("said", "text"),
    [
        ("alright, can you open up the notes app for me", "open up the notes app"),
        ("and once you're there can you create a new note", "create a new note"),
        ("let's move on and can you open up the browser", "open up the browser"),
        ("now open x.com", "open x.com"),
        ("Um, could you please open Chrome real quick", "open Chrome"),
        ("great, now open notepad", "open notepad"),
        ("let's make the title say hello", "make the title say hello"),
    ],
)
def test_discourse_and_polite_frames_are_stripped(said, text):
    assert strip(said) == text


@pytest.mark.parametrize(
    "final",
    [RUN_ON, *RECORDING, "Open Notepad, and then create a new note.", "okay so um type See you at 5 PM, okay?",
     "Hey, it's fine. Google search “Norbert Wiener” please!", "  , . ", "next tab and then reload"],
)
def test_text_is_always_a_substring_of_the_final(final):
    for c in split(final):
        assert c.said and c.said in final
        assert c.text in c.said
        assert c.said == c.said.strip(" ,.;!?")


@pytest.mark.parametrize("final", ["great", "nice, okay", "thank you", "cool, thank you", "Great great. Okay.", "um"])
def test_acknowledgements_drop(final):
    found = split(final)
    assert found and all(c.kind in ("ack", "empty") and c.text == "" for c in found)
    assert clauses.is_acknowledgement(final) is (final != "um")  # only filler: nothing left, not an acknowledgement


def test_thanks_thats_all_is_a_command():
    (found,) = split("Thanks, that's all.")
    assert found.kind == "command" and found.text == "that's all"
    assert [c.kind for c in split("Cool, thank you. That's all.")] == ["ack", "command"]
    assert strip("that'll be all, thanks") == "that'll be all"


@pytest.mark.parametrize(
    ("text", "wanted"),
    [
        ("create a new note", True),
        ("can you believe it", True),
        ("inside this new note let's make the title say hello", True),
        ("and then open chrome", True),
        ("the next video is so good", False),
        ("clothes notepad", False),
        ("Norbert Wiener", False),
    ],
)
def test_request_wording(text, wanted):
    assert clauses.is_request(text) is wanted


def test_starts_like_request():
    assert clauses.starts_like_request("and once you're there can you create a new note")
    assert clauses.starts_like_request("so open chrome")
    assert not clauses.starts_like_request("fusion")
    assert not clauses.starts_like_request("inside this new note let's make the title say hello")


def test_free_text_verbs_keep_their_sentence():
    (typed,) = split("type hello and then open chrome")
    assert typed.text == "type hello and then open chrome"  # Tier 0 cuts the payload; the clause module never does
    (searched,) = split("can you google cats and dogs, then open the first one")
    assert searched.text == "google cats and dogs, then open the first one"
    assert [c.text for c in split("type hello. Open chrome.")] == ["type hello", "Open chrome"]
    assert strip("type thank you") == "type thank you"  # his words to type, not a trailing "thank you"


def test_next_is_only_small_talk_before_a_pause():
    assert strip("next tab") == "next tab"
    assert strip("Next, open chrome") == "open chrome"
    assert [c.text for c in split("open chrome and next tab")] == ["open chrome", "next tab"]
    assert [c.text for c in split("open the next video")] == ["open the next video"]


def test_right_is_only_small_talk_before_a_pause():
    assert strip("right click on the file") == "right click on the file"
    assert strip("Right, open Chrome") == "open Chrome"
    assert [c.text for c in split("right click on the file")] == ["right click on the file"]
    assert clauses.is_acknowledgement("yeah right")  # still an acknowledgement on its own


_PRAISE = ["great job", "great work", "nice work", "good work", "well done", "nicely done", "nice job"]


@pytest.mark.parametrize("final", _PRAISE + [p.capitalize() + "." for p in _PRAISE])
def test_praise_is_an_acknowledgement(final):
    found = split(final)
    assert [(c.kind, c.text) for c in found] == [("ack", "")]
    assert clauses.is_acknowledgement(final)


@pytest.mark.parametrize("final", _PRAISE + ["Great job. Nice work!"])
def test_tier0_makes_no_plan_from_praise(final):
    from catalog_fixture import CATALOG

    from nova.brain import tier0

    assert tier0.parse(final, CATALOG) is None


def test_lexicon_covers_the_gate_verbs():
    assert gate._VERBS <= clauses.IMPERATIVE_VERBS
    assert clauses.FREE_TEXT_VERBS <= clauses.IMPERATIVE_VERBS


def test_empty_final():
    assert split("") == ()
    assert split("  . , ") == ()
    assert strip("") == ""
    assert not clauses.is_acknowledgement("") and not clauses.is_request("")
    assert split("um.") == (Clause("um", "", "empty", False),)
