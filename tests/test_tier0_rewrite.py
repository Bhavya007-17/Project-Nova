"""Tier 0 rewrite patterns (Stage 07e): "make this …", "rewrite this …", "fix the grammar", "translate this to …",
"turn this into …", "shorten / expand this" -> rewrite{instruction}, the instruction cut from what he said."""

import pytest
from catalog_fixture import CATALOG

from nova.brain.plan import step
from nova.brain.tier0 import describe, parse, rewrite_that


@pytest.mark.parametrize(
    ("text", "instruction"),
    [
        ("make this more formal", "more formal"),
        ("Make this more formal.", "more formal"),
        ("can you make that a bit friendlier", "a bit friendlier"),
        ("make it shorter", "shorter"),
        ("make it sound more confident", "sound more confident"),
        ("make this more formal and friendlier", "more formal and friendlier"),
        ("make the text less casual please", "less casual"),
        ("make this paragraph clearer", "clearer"),
        ("make it a bulleted list", "a bulleted list"),
        ("make that Spanish", "Spanish"),
        ("rewrite this to sound friendlier", "to sound friendlier"),
        ("rewrite this", "rewrite"),
        ("rephrase that", "rephrase"),
        ("reword the selection so it's more polite", "so it's more polite"),
        ("rewrite more formally", "more formally"),
        ("fix the grammar", "fix the grammar"),
        ("fix the spelling in this", "fix the spelling in this"),
        ("correct my typos", "correct my typos"),
        ("proofread this", "proofread"),
        ("translate this to Spanish", "translate this to Spanish"),
        ("translate to French", "translate to French"),
        ("turn this into bullet points", "into bullet points"),
        ("turn that into a short email", "into a short email"),
        ("put that in French", "in French"),
        ("put this paragraph into Hindi", "into Hindi"),
        ("shorten this", "shorten"),
        ("shorten this a bit", "shorten this a bit"),
        ("shorten", "shorten"),
        ("simplify that", "simplify"),
        ("expand this", "expand"),
        ("elaborate on this", "elaborate"),
    ],
)
def test_rewrite_phrases(text, instruction):
    plan = parse(text, CATALOG)
    assert plan is not None and plan.kind == "act", (text, plan)
    assert list(plan.steps) == [step("rewrite", instruction=instruction)]
    assert instruction in text  # cut from what he said, his casing


@pytest.mark.parametrize(
    "text",
    [
        "summarize this",
        "explain this",
        "fix this",
        "make it louder",
        "make it full screen",
        "make the text bigger",
        "make the font bigger",
        "turn it up",
        "turn it off",
        "translate this page",
        "translate the page to Spanish",
        "make a new doc",
        "make it bold",
        "expand",
        "translate",
        "make this window smaller",
        "make it more formal and then send it",
        "put this on the left",
        "put it in the downloads folder",
        "put that in my notes",
        "put French",
    ],
)
def test_not_rewrites(text):
    plan = parse(text, CATALOG)
    assert plan is None or all(s.action != "rewrite" for s in plan.steps), (text, plan)


def test_volume_still_works():
    plan = parse("make it louder", CATALOG)
    assert plan is not None and plan.steps[0].action == "volume"


def test_describe():
    plan = parse("make this more formal", CATALOG)
    assert describe(plan) == "Rewrite it: “more formal”"


@pytest.mark.parametrize(
    ("text", "instruction"),
    [
        ("Make that more formal.", "more formal"),
        ("make it shorter", "shorter"),
        ("Can you make that shorter?", "shorter"),
        ("Translate that to Spanish.", "Translate that to Spanish"),
        ("Fix the grammar in that.", "Fix the grammar in that"),
        ("Rewrite this as bullet points.", "as bullet points"),
    ],
)
def test_dictated_rewrite_of_the_last_segment(text, instruction):
    assert rewrite_that(text) == instruction


@pytest.mark.parametrize(
    "text",
    [
        "shorten",  # nothing points at the text
        "More formal.",
        "I want to make that more formal next time.",
        "We should make it more formal for the client meeting tomorrow afternoon.",  # a long sentence is dictation
        "make that more formal and send it",
        "make the last paragraph shorter",
    ],
)
def test_dictated_words_that_are_not_a_rewrite(text):
    assert rewrite_that(text) is None
