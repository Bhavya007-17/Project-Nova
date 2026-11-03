"""The pure key layer: what the hook swallows, and tap / hold / double-tap from raw trigger events."""

import pytest

from nova.keys import (
    NOVA_MARK,
    PASS,
    VK_ESCAPE,
    VK_F23,
    VK_LCONTROL,
    VK_LMENU,
    VK_LSHIFT,
    VK_LWIN,
    VK_MASK,
    VK_RCONTROL,
    VK_RETURN,
    VK_RMENU,
    Cancel,
    DoubleTap,
    Gestures,
    Key,
    Press,
    Release,
    TriggerEvent,
    TriggerKeys,
    parse_chord,
)

VK_N = 0x4E
INJECTED = 0x10


def down(vk, at=0.0, **kw):
    return Key(vk, True, at, **kw)


def up(vk, at=0.0, **kw):
    return Key(vk, False, at, **kw)


def keys(chord="ctrl+alt+n", **kw):
    return TriggerKeys(chord=parse_chord(chord) if chord else None, **kw)


def run(trigger, events):
    return [trigger.on_key(event) for event in events]


# --- Copilot key: LWin + LShift + F23 ------------------------------------------------------------


MASKED_WIN_UP = ((VK_MASK, True), (VK_MASK, False), (VK_LWIN, False))


def test_copilot_press_is_swallowed_and_reported():
    t = keys()
    lwin, lshift, f23 = run(t, [down(VK_LWIN), down(VK_LSHIFT), down(VK_F23, 0.01)])
    assert lwin == PASS and lshift == PASS  # modifier presses always reach Windows
    assert f23.swallow and f23.event == TriggerEvent("copilot", True, 0.01) and not f23.replay


def test_win_release_after_copilot_is_replayed_behind_the_mask_key():
    """Start opens on a lone Win release: swallow that release and replay it after the unassigned mask key."""
    t = keys()
    run(t, [down(VK_LWIN), down(VK_LSHIFT), down(VK_F23), up(VK_F23)])
    lshift, lwin = run(t, [up(VK_LSHIFT), up(VK_LWIN)])
    assert lshift == PASS
    assert lwin.swallow and lwin.replay == MASKED_WIN_UP and lwin.event is None
    # Only that one release: a later plain Win tap must still open Start.
    assert run(t, [down(VK_LWIN), up(VK_LWIN)]) == [PASS, PASS]


def test_win_released_before_f23_is_still_masked():
    t = keys()
    run(t, [down(VK_LWIN), down(VK_LSHIFT), down(VK_F23)])
    assert t.on_key(up(VK_LWIN)).replay == MASKED_WIN_UP
    assert t.on_key(up(VK_F23, 1.0)).event == TriggerEvent("copilot", False, 1.0)


def test_copilot_repeats_are_swallowed_silently_and_release_is_reported():
    t = keys()
    run(t, [down(VK_LWIN), down(VK_LSHIFT), down(VK_F23)])
    repeat, release = run(t, [down(VK_F23, 0.5), up(VK_F23, 1.2)])
    assert repeat.swallow and repeat.event is None and not repeat.replay
    assert release.swallow and release.event == TriggerEvent("copilot", False, 1.2)


def test_f23_without_win_shift_is_left_alone():
    t = keys()
    verdicts = run(t, [down(VK_F23), up(VK_F23)])
    assert all(not v.swallow and v.event is None for v in verdicts)


def test_copilot_off_passes_everything():
    t = keys(copilot=False)
    verdicts = run(t, [down(VK_LWIN), down(VK_LSHIFT), down(VK_F23), up(VK_F23)])
    assert all(not v.swallow and v.event is None for v in verdicts)


def test_injected_copilot_chord_counts_but_novas_own_keys_never_do():
    t = keys()
    f23 = run(t, [down(VK_LWIN, flags=INJECTED), down(VK_LSHIFT, flags=INJECTED), down(VK_F23, flags=INJECTED)])[-1]
    assert f23.event is not None and f23.event.down
    ours = keys()
    mark = {"flags": INJECTED, "extra": NOVA_MARK}
    verdicts = run(ours, [down(VK_LWIN, **mark), down(VK_LSHIFT, **mark), down(VK_F23, **mark), down(VK_MASK, **mark)])
    assert all(not v.swallow and v.event is None for v in verdicts)


# --- fallback chord ----------------------------------------------------------------------------------


def test_chord_press_repeat_release():
    t = keys()
    run(t, [down(VK_LCONTROL), down(VK_LMENU)])
    press, repeat, release = run(t, [down(VK_N, 1.0), down(VK_N, 1.5), up(VK_N, 2.0)])
    assert press.swallow and press.event == TriggerEvent("chord", True, 1.0)
    assert repeat.swallow and repeat.event is None
    assert release.swallow and release.event == TriggerEvent("chord", False, 2.0)
    # Alt released alone would open the menu bar: masked like Win; Ctrl passes.
    alt, ctrl = run(t, [up(VK_LMENU), up(VK_LCONTROL)])
    assert alt.swallow and alt.replay == ((VK_MASK, True), (VK_MASK, False), (VK_LMENU, False))
    assert ctrl == PASS


def test_chord_needs_exactly_its_modifiers():
    t = keys()
    run(t, [down(VK_LCONTROL), down(VK_LMENU), down(VK_LSHIFT)])
    assert not t.on_key(down(VK_N)).swallow  # Ctrl+Alt+Shift+N is someone else's shortcut
    assert not keys().on_key(down(VK_N)).swallow  # plain N is typing


def test_right_hand_modifiers_count():
    t = keys()
    run(t, [down(VK_RCONTROL), down(VK_RMENU)])
    assert t.on_key(down(VK_N)).event == TriggerEvent("chord", True, 0.0)


def test_chord_ends_on_its_key_even_if_modifiers_go_first():
    t = keys()
    run(t, [down(VK_LCONTROL), down(VK_LMENU), down(VK_N)])
    run(t, [up(VK_LMENU), up(VK_LCONTROL)])
    assert t.on_key(up(VK_N, 3.0)).event == TriggerEvent("chord", False, 3.0)


def test_no_chord_configured():
    t = keys(chord=None)
    run(t, [down(VK_LCONTROL), down(VK_LMENU)])
    assert not t.on_key(down(VK_N)).swallow


def test_configure_swaps_chord_and_copilot():
    t = keys()
    t.configure(chord=parse_chord("ctrl+alt+space"), copilot=False)
    run(t, [down(VK_LCONTROL), down(VK_LMENU)])
    assert not t.on_key(down(VK_N)).swallow
    assert t.on_key(down(0x20)).event == TriggerEvent("chord", True, 0.0)


# --- Esc ------------------------------------------------------------------------------------------------


def test_esc_passes_unless_armed():
    t = keys()
    assert not t.on_key(down(VK_ESCAPE)).swallow
    assert not t.on_key(up(VK_ESCAPE)).swallow


def test_armed_esc_is_swallowed_with_its_repeats_and_release():
    t = keys()
    t.esc_armed = True
    press, repeat = run(t, [down(VK_ESCAPE, 1.0), down(VK_ESCAPE, 1.1)])
    t.esc_armed = False  # Nova went idle meanwhile: the release still belongs to the swallowed press
    release = t.on_key(up(VK_ESCAPE, 1.2))
    assert press.swallow and press.event == TriggerEvent("esc", True, 1.0)
    assert repeat.swallow and repeat.event is None
    assert release.swallow and release.event is None


def test_esc_pressed_before_arming_is_not_swallowed_on_release():
    t = keys()
    t.on_key(down(VK_ESCAPE))
    t.esc_armed = True
    assert not t.on_key(up(VK_ESCAPE)).swallow


# --- answer keys (the island asks; it never takes focus) ------------------------------------------------


def test_enter_answers_confirm_only_while_asking():
    trigger = keys()
    assert trigger.on_key(down(VK_RETURN)) == PASS
    trigger.on_key(up(VK_RETURN))
    trigger.asking = "confirm"
    verdict = trigger.on_key(down(VK_RETURN, at=1.0))
    assert verdict.swallow and verdict.event == TriggerEvent("answer", True, 1.0, "enter")
    assert trigger.on_key(down(VK_RETURN)).swallow and trigger.on_key(down(VK_RETURN)).event is None  # repeats
    assert trigger.on_key(up(VK_RETURN)).swallow
    assert trigger.on_key(down(0x31)) == PASS  # numbers don't answer a confirm


def test_number_keys_pick_a_clarify_choice():
    trigger = keys()
    trigger.asking = "clarify"
    assert trigger.on_key(down(0x32)).event.key == "2"
    trigger.on_key(up(0x32))
    assert trigger.on_key(down(0x63)).event.key == "3"  # numpad 3
    trigger.on_key(up(0x63))
    assert trigger.on_key(down(0x35)) == PASS  # only 1-4
    assert trigger.on_key(down(VK_RETURN)) == PASS


def test_answer_keys_with_a_modifier_reach_the_app():
    trigger = keys()
    trigger.asking = "clarify"
    trigger.on_key(down(VK_LCONTROL))
    assert trigger.on_key(down(0x31)) == PASS  # Ctrl+1 is his app's shortcut


def test_answer_release_is_swallowed_even_after_the_question_ends():
    trigger = keys()
    trigger.asking = "confirm"
    assert trigger.on_key(down(VK_RETURN)).swallow
    trigger.asking = None
    assert trigger.on_key(up(VK_RETURN)).swallow  # its press never reached the app
    assert trigger.on_key(down(VK_RETURN)) == PASS


def test_a_digit_chord_does_not_hide_the_answer_keys():
    trigger = keys("ctrl+alt+1")
    trigger.asking = "clarify"
    assert trigger.on_key(down(0x31)).event.key == "1"
    trigger.on_key(up(0x31))
    trigger.on_key(down(VK_LCONTROL))
    trigger.on_key(down(VK_LMENU))
    assert trigger.on_key(down(0x31)).event.source == "chord"


def test_gestures_ignore_answer_keys():
    assert Gestures().feed(TriggerEvent("answer", True, 1.0, "enter")) == ()


# --- reset (hook reinstalled) ------------------------------------------------------------------------


def test_reset_releases_held_triggers():
    t = keys()
    run(t, [down(VK_LWIN), down(VK_LSHIFT), down(VK_F23)])
    assert t.holding
    assert t.reset(5.0) == [TriggerEvent("copilot", False, 5.0)]
    assert t.reset(6.0) == [] and not t.holding
    assert t.on_key(up(VK_F23)) == PASS
    assert t.on_key(up(VK_LWIN)) == PASS  # nothing pending after a reset


# --- gestures -----------------------------------------------------------------------------------------


def g(source, is_down, at):
    return TriggerEvent(source, is_down, at)


def feed(gestures, events):
    out = []
    for event in events:
        out.extend(gestures.feed(event))
    return out


def test_tap():
    assert feed(Gestures(), [g("copilot", True, 0.0), g("copilot", False, 0.1)]) == [
        Press("copilot", 0.0),
        Release("copilot", 0.1, pytest.approx(0.1), hold=False),
    ]


def test_hold_is_longer_than_250_ms():
    out = feed(Gestures(), [g("copilot", True, 0.0), g("copilot", False, 0.4)])
    assert out[-1] == Release("copilot", 0.4, pytest.approx(0.4), hold=True)
    assert feed(Gestures(), [g("chord", True, 0.0), g("chord", False, 0.25)])[-1].hold is False


def test_double_tap():
    out = feed(Gestures(), [g("copilot", True, 0.0), g("copilot", False, 0.1), g("copilot", True, 0.45)])
    assert out[-1] == DoubleTap("copilot", 0.45)


def test_slow_second_tap_is_a_press():
    out = feed(Gestures(), [g("copilot", True, 0.0), g("copilot", False, 0.1), g("copilot", True, 0.6)])
    assert out[-1] == Press("copilot", 0.6)


def test_tap_after_a_hold_is_not_a_double_tap():
    out = feed(Gestures(), [g("copilot", True, 0.0), g("copilot", False, 1.0), g("copilot", True, 1.1)])
    assert out[-1] == Press("copilot", 1.1)


def test_third_quick_tap_starts_over():
    events = [g("copilot", True, 0.0), g("copilot", False, 0.1), g("copilot", True, 0.2), g("copilot", False, 0.3),
              g("copilot", True, 0.4)]
    assert feed(Gestures(), events)[-1] == Press("copilot", 0.4)


def test_different_triggers_never_double_tap():
    out = feed(Gestures(), [g("copilot", True, 0.0), g("copilot", False, 0.1), g("chord", True, 0.2)])
    assert out[-1] == Press("chord", 0.2)


def test_esc_cancels_and_forgets_the_last_tap():
    gestures = Gestures()
    out = feed(gestures, [g("copilot", True, 0.0), g("copilot", False, 0.1), g("esc", True, 0.2), g("esc", False, 0.25),
                          g("copilot", True, 0.3)])
    assert out[-3:] == [Release("copilot", 0.1, pytest.approx(0.1), hold=False), Cancel(0.2), Press("copilot", 0.3)]


def test_stray_events_are_ignored():
    gestures = Gestures()
    assert feed(gestures, [g("copilot", False, 0.0)]) == []
    assert feed(gestures, [g("copilot", True, 1.0), g("chord", True, 1.1), g("chord", False, 1.2)]) == [Press("copilot", 1.0)]
    assert feed(gestures, [g("copilot", False, 1.3)])[0].source == "copilot"


# --- Ctrl + Copilot: pin / unpin the island ----------------------------------------------------------


def test_ctrl_copilot_pins_instead_of_listening():
    trigger = keys()
    out = run(trigger, [down(VK_LCONTROL), down(VK_LWIN), down(VK_LSHIFT), down(VK_F23, at=2.0)])
    assert out[-1].swallow and out[-1].event == TriggerEvent("pin", True, 2.0)
    assert trigger.on_key(down(VK_F23)).swallow  # auto-repeat
    released = run(trigger, [up(VK_F23), up(VK_LSHIFT), up(VK_LWIN), up(VK_LCONTROL)])
    assert released[0].swallow and released[0].event is None
    assert released[2].replay == ((VK_MASK, True), (VK_MASK, False), (VK_LWIN, False))  # Start stays shut
    assert released[3] == PASS
    out = run(trigger, [down(VK_LWIN), down(VK_LSHIFT), down(VK_F23)])
    assert out[-1].event.source == "copilot"  # without Ctrl: talk as usual


def test_pin_needs_the_copilot_trigger_on():
    trigger = keys(copilot=False)
    assert run(trigger, [down(VK_RCONTROL), down(VK_LWIN), down(VK_LSHIFT), down(VK_F23)])[-1] == PASS


def test_gestures_ignore_pin():
    assert Gestures().feed(TriggerEvent("pin", True, 1.0)) == ()
