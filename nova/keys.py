"""Keyboard chord parsing ("ctrl+alt+n") into RegisterHotKey modifiers + virtual-key code."""

from __future__ import annotations

from dataclasses import dataclass

# RegisterHotKey modifier flags (winuser.h).
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

_MODIFIERS = {
    "ctrl": MOD_CONTROL,
    "control": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN,
}
# Label order for display: Ctrl+Alt+Shift+Win+Key.
_MODIFIER_LABELS = ((MOD_CONTROL, "Ctrl"), (MOD_ALT, "Alt"), (MOD_SHIFT, "Shift"), (MOD_WIN, "Win"))
_NAMED_KEYS = {"space": (0x20, "Space")}


@dataclass(frozen=True)
class Chord:
    modifiers: int
    vk: int
    label: str


def _key(name: str) -> tuple[int, str]:
    if len(name) == 1 and name.isascii() and name.isalnum():
        return ord(name.upper()), name.upper()
    if name in _NAMED_KEYS:
        return _NAMED_KEYS[name]
    if name.startswith("f") and name[1:].isdigit() and 1 <= int(name[1:]) <= 24:
        number = int(name[1:])
        return 0x70 + number - 1, f"F{number}"
    raise ValueError(f"unknown key {name!r} (use a letter, digit, F1-F24 or space)")


def parse_chord(text: object) -> Chord:
    """Parse "ctrl+alt+n" style text. Needs at least one modifier and exactly one key."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("a chord looks like ctrl+alt+n")
    parts = [part.strip().lower() for part in text.split("+")]
    if any(not part for part in parts):
        raise ValueError(f"chord {text!r} has an empty part")
    modifiers = 0
    keys = []
    for part in parts:
        flag = _MODIFIERS.get(part)
        if flag is None:
            keys.append(part)
        elif modifiers & flag:
            raise ValueError(f"chord {text!r} repeats {part!r}")
        else:
            modifiers |= flag
    if not modifiers:
        raise ValueError(f"chord {text!r} needs ctrl, alt, shift or win")
    if len(keys) != 1:
        raise ValueError(f"chord {text!r} needs exactly one non-modifier key")
    vk, key_label = _key(keys[0])
    label = "+".join([name for flag, name in _MODIFIER_LABELS if modifiers & flag] + [key_label])
    return Chord(modifiers, vk, label)
