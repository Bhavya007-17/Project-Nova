"""`nova keys --probe` helpers: nothing typed is ever named; the verdict reads the durations."""

import pytest

from nova.keyprobe import describe, key_name, verdict_text
from nova.keys import PASS, Key, Verdict


@pytest.mark.parametrize("vk", [0x41, 0x5A, 0x30, 0x39, 0x20, 0xBA, 0xDE, 0x60, 0x6F, 0xE2])
def test_character_keys_are_never_named(vk):
    assert key_name(vk) is None
    assert "(other key)" in describe(Key(vk, True, 1.0), PASS, False, 0.0, 0.0)


def test_special_keys_are_named():
    assert key_name(0x86) == "F23" and key_name(0x5B) == "LWin" and key_name(0xA0) == "LShift"
    assert key_name(0xC3) == "vk 0xC3"


def test_describe_marks_swallowed_and_replayed_keys():
    line = describe(Key(0x5B, False, 1.0, scan=0x5B, flags=0x01), Verdict(swallow=True, replay=((0xE8, True),)), False, 1.0, 0.004)
    assert "LWin" in line and "up" in line and "swallowed" in line and "replayed" in line and "ext" in line


def test_verdicts():
    assert "YES" in verdict_text([112, 149, 1059, 1409], 65) and "65 repeats" in verdict_text([1059], 65)
    assert "no real key-up" in verdict_text([3, 4, 5], 0)
    assert "Unclear" in verdict_text([100, 200], 0)
    assert "No Copilot key seen" in verdict_text([], 0)
