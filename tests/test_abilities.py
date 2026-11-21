"""What Nova can do (prompt 07c §8): the help card is generated from the action list."""

from nova.brain import abilities


def test_card_has_a_row_per_category_within_the_island():
    question, choices, topics = abilities.card()
    assert question and topics == abilities.CATEGORIES and len(choices) == 4
    assert choices[0].startswith("Apps & windows: open, switch, close")
    assert all(len(c) <= 48 for c in choices)


def test_every_action_has_a_category_and_every_category_examples():
    assert all(a.category in abilities.CATEGORIES for a in abilities.ACTIONS.values())
    for category in abilities.CATEGORIES:
        said = abilities.examples(category)
        assert len(said) >= 4 and len(set(said)) == len(said)


def test_topic_quotes_a_few_phrases():
    text = abilities.topic("screen")
    assert text.startswith("“scroll down”") and text.count("“") == abilities.MAX_EXAMPLES
    assert len(text) <= 110  # two lines in the pill
    assert "what can I say" in abilities.topic("nonsense")
