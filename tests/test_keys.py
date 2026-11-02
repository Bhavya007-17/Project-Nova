import pytest

from nova.keys import MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, Chord, parse_chord


def test_default_chord():
    assert parse_chord("ctrl+alt+n") == Chord(MOD_CONTROL | MOD_ALT, 0x4E, "Ctrl+Alt+N")


def test_case_spaces_and_synonyms():
    assert parse_chord(" Control + ALT + n ") == parse_chord("ctrl+alt+n")


def test_label_uses_canonical_modifier_order():
    assert parse_chord("shift+win+ctrl+alt+k").label == "Ctrl+Alt+Shift+Win+K"
    assert parse_chord("shift+win+ctrl+alt+k").modifiers == MOD_CONTROL | MOD_ALT | MOD_SHIFT | MOD_WIN


@pytest.mark.parametrize(
    ("text", "vk", "label"),
    [
        ("ctrl+alt+7", 0x37, "Ctrl+Alt+7"),
        ("ctrl+f13", 0x7C, "Ctrl+F13"),
        ("alt+f1", 0x70, "Alt+F1"),
        ("ctrl+f24", 0x87, "Ctrl+F24"),
        ("ctrl+alt+space", 0x20, "Ctrl+Alt+Space"),
    ],
)
def test_other_keys(text, vk, label):
    chord = parse_chord(text)
    assert (chord.vk, chord.label) == (vk, label)


@pytest.mark.parametrize(
    "text",
    ["", "n", "ctrl+alt", "ctrl+alt+n+m", "ctrl+ctrl+n", "ctrl+alt+f25", "ctrl+alt+bogus", "ctrl++n", 7],
)
def test_bad_chords(text):
    with pytest.raises(ValueError):
        parse_chord(text)
