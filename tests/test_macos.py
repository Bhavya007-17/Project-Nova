"""The macOS backend's pure parts: key translation, key-code maps, the Unix-socket bridge address."""

import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS backend")


def test_windows_shortcuts_become_mac_ones():
    from nova.macos.input import mac_keys

    assert mac_keys("ctrl+t") == "cmd+t"
    assert mac_keys("ctrl+shift+t") == "cmd+shift+t"
    assert mac_keys("ctrl+tab") == "ctrl+tab"  # tab switching is Ctrl+Tab on a Mac too
    assert mac_keys("ctrl+shift+tab") == "ctrl+shift+tab"
    assert mac_keys("alt+left") == "cmd+["
    assert mac_keys("f5") == "cmd+r"
    assert mac_keys("win+v") == "cmd+v"
    assert mac_keys("enter") == "enter"


def test_trigger_keys_round_trip_through_the_key_maps():
    from nova.keys import VK_ESCAPE, VK_LCONTROL, VK_LMENU, VK_RETURN
    from nova.macos.keymap import MAC_TO_VK, VK_TO_MAC

    assert MAC_TO_VK[VK_TO_MAC[ord("N")]] == ord("N")
    assert MAC_TO_VK[59] == VK_LCONTROL and MAC_TO_VK[58] == VK_LMENU  # Control, Option
    assert MAC_TO_VK[53] == VK_ESCAPE and MAC_TO_VK[36] == VK_RETURN and MAC_TO_VK[76] == VK_RETURN
    assert [MAC_TO_VK[VK_TO_MAC[0x31 + i]] for i in range(4)] == [0x31, 0x32, 0x33, 0x34]  # clarify answers


def test_chord_on_a_mac_is_control_option_n():
    from nova.keys import Key, TriggerKeys, parse_chord
    from nova.macos.keymap import MAC_TO_VK

    keys = TriggerKeys(parse_chord("ctrl+alt+n"), copilot=False)
    for code in (59, 58):
        assert not keys.on_key(Key(MAC_TO_VK[code], True, 0.0)).swallow
    verdict = keys.on_key(Key(MAC_TO_VK[45], True, 0.1))
    assert verdict.swallow and verdict.event is not None and verdict.event.source == "chord"


def test_bridge_family_follows_the_address():
    from nova.chrome.bridge import family

    assert family(r"\\.\pipe\nova-chrome-abc") == "AF_PIPE"
    assert family("/tmp/nova-chrome-abc.sock") == "AF_UNIX"
