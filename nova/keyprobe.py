"""`nova keys --probe`: what does the Copilot key really send? (SPEC §5, §20)

Logs raw events from a WH_KEYBOARD_LL hook with timestamps, then says whether a
real key-up arrives on release (hold = push-to-talk) or only right after the
key-down (tap behaviour only). Character keys (letters, digits, punctuation,
space) print as "(other key)", so nothing typed meanwhile is shown. The Copilot
key and the fallback chord are swallowed and masked exactly as Nova does it
(so Copilot and Start stay shut), unless --raw.
"""

from __future__ import annotations

import queue
import time

from . import paths
from .config import ConfigStore
from .console import enable_vt
from .hook import KeyHook
from .keys import PASS, VK_ESCAPE, VK_F23, VK_MASK, Key, TriggerKeys, Verdict, parse_chord

_NAMES = {
    0x08: "Backspace", 0x09: "Tab", 0x0D: "Enter", 0x10: "Shift", 0x11: "Ctrl", 0x12: "Alt", 0x13: "Pause",
    0x14: "CapsLock", 0x1B: "Esc", 0x21: "PageUp", 0x22: "PageDown", 0x23: "End", 0x24: "Home",
    0x25: "Left", 0x26: "Up", 0x27: "Right", 0x28: "Down", 0x2C: "PrintScreen", 0x2D: "Insert", 0x2E: "Delete",
    0x5B: "LWin", 0x5C: "RWin", 0x5D: "Menu", 0x5F: "Sleep", 0x90: "NumLock", 0x91: "ScrollLock",
    0xA0: "LShift", 0xA1: "RShift", 0xA2: "LCtrl", 0xA3: "RCtrl", 0xA4: "LAlt", 0xA5: "RAlt",
    0xAD: "VolumeMute", 0xAE: "VolumeDown", 0xAF: "VolumeUp", 0xB0: "NextTrack", 0xB1: "PrevTrack",
    0xB2: "MediaStop", 0xB3: "PlayPause", VK_MASK: "mask key",
}
_NAMES.update({0x70 + i: f"F{i + 1}" for i in range(24)})
# Keys that type characters: never named.
_CHARACTER = (
    {0x20, 0xE2}
    | set(range(0x30, 0x3A)) | set(range(0x41, 0x5B)) | set(range(0x60, 0x70))
    | set(range(0xBA, 0xC1)) | set(range(0xDB, 0xE0))
)


def key_name(vk: int) -> str | None:
    """Printable name of a non-character key; None for keys that type characters."""
    if vk in _CHARACTER:
        return None
    return _NAMES.get(vk, f"vk 0x{vk:02X}")


def describe(key: Key, verdict: Verdict, repeat: bool, since_first: float, since_last: float) -> str:
    name = key_name(key.vk)
    label = name if name is not None else "(other key)"
    notes = []
    if repeat:
        notes.append("repeat")
    if key.ours:
        notes.append("injected by Nova")
    elif key.injected:
        notes.append("injected")
    if verdict.swallow:
        notes.append("swallowed")
    if verdict.replay:
        notes.append("replayed after the mask key")
    scan = f"scan {key.scan:02X}{' ext' if key.flags & 0x01 else ''}" if name is not None else ""
    return (
        f"  +{since_first:7.3f} s  (+{since_last * 1000:5.0f} ms)  {label:<12} {'down' if key.down else 'up  '}"
        f"  {scan:<11} {', '.join(notes)}"
    ).rstrip()


def verdict_text(durations_ms: list[float], repeats: int) -> str:
    if not durations_ms:
        return (
            "No Copilot key seen (no F23 key event). If you pressed it, it sends something else: "
            "the lines above show what. Paste them to Claude Code."
        )
    longest = max(durations_ms)
    shown = ", ".join(f"{d:.0f}" for d in durations_ms)
    if longest > 400:
        line = (
            f"Real key-up on release: YES. {len(durations_ms)} presses, down->up {shown} ms. "
            "Hold = push-to-talk works."
        )
    elif longest < 40:
        line = (
            f"Key-up arrives right after key-down (at most {longest:.0f} ms, {len(durations_ms)} presses), even when held: "
            "no real key-up. Nova uses tap behaviour only (tap to start, tap or pause to finish)."
        )
    else:
        line = f"Unclear: down->up {shown} ms. Run it again and hold the key for a full 2 seconds."
    if repeats:
        line += f" The key auto-repeats while held ({repeats} repeats); Nova ignores repeats."
    return line


def main(seconds: float = 20.0, raw: bool = False) -> int:
    enable_vt()
    store = ConfigStore(paths.SETTINGS)
    store.load()
    try:
        chord = parse_chord(store.settings.chord)
    except ValueError:
        chord = None
    triggers = TriggerKeys(chord=chord, copilot=True)
    events: queue.SimpleQueue[tuple[Key, Verdict]] = queue.SimpleQueue()

    def on_key(key: Key) -> bool:
        verdict = PASS if raw else triggers.on_key(key)
        if verdict.replay:
            hook.post(verdict.replay)
        events.put((key, verdict))
        return verdict.swallow

    hook = KeyHook(on_key, name="nova-keys-probe")
    print(
        f"Nova keys probe ({seconds:.0f} s, Esc stops early). Tap the Copilot key 3 times, then hold it "
        "about 2 seconds, 3 times."
    )
    print(
        "Copilot and the fallback chord are swallowed like Nova does it; say if Copilot or Start still opens."
        if not raw else "--raw: nothing is swallowed; Copilot will open."
    )
    print("Letters, digits and punctuation show as (other key).\n")
    if not hook.start():
        print(hook.error)
        return 1
    first = last = None
    down: set[int] = set()
    copilot_down: float | None = None
    durations: list[float] = []
    repeats = 0
    deadline = time.monotonic() + seconds
    try:
        while time.monotonic() < deadline:
            try:
                key, verdict = events.get(timeout=0.1)
            except queue.Empty:
                continue
            first = key.at if first is None else first
            repeat = key.down and key.vk in down
            if key.down:
                down.add(key.vk)
            else:
                down.discard(key.vk)
            print(describe(key, verdict, repeat, key.at - first, key.at - (last if last is not None else key.at)))
            last = key.at
            if key.vk == VK_F23 and not key.ours:
                if key.down and copilot_down is None:
                    copilot_down = key.at
                elif key.down:
                    repeats += 1
                elif copilot_down is not None:
                    durations.append((key.at - copilot_down) * 1000)
                    copilot_down = None
            if key.vk == VK_ESCAPE and key.down and not key.ours and not verdict.swallow:
                break
    except KeyboardInterrupt:
        pass
    finally:
        hook.close()
    stats = hook.stats
    print()
    print(verdict_text(durations, repeats))
    print(
        f"Hook: {stats.calls} callbacks, mean {stats.mean_ms * 1000:.0f} us, max {stats.max_ms:.2f} ms "
        f"({stats.over_5ms} over 5 ms, {stats.late} late); raw input saw {stats.raw} keys; "
        f"reinstalled {stats.reinstalls} times."
    )
    return 0
