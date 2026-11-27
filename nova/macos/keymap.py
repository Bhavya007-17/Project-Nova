"""Windows virtual-key codes <-> macOS key codes (ANSI layout).

The trigger logic (``nova.keys``) and the hands speak Windows VKs; the event tap and CGEvent posting speak mac key
codes. Control = VK_CONTROL, Option = VK_MENU (Alt), Command = VK_LWIN, so the chord "ctrl+alt+n" is
Control + Option + N on a Mac.
"""

from __future__ import annotations

_LETTERS = {"a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9, "b": 11, "q": 12,
            "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "o": 31, "u": 32, "i": 34, "p": 35, "l": 37, "j": 38,
            "k": 40, "n": 45, "m": 46}
_DIGITS = {"1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "9": 25, "7": 26, "8": 28, "0": 29}
_FKEYS = {1: 122, 2: 120, 3: 99, 4: 118, 5: 96, 6: 97, 7: 98, 8: 100, 9: 101, 10: 109, 11: 103, 12: 111,
          13: 105, 14: 107, 15: 113, 16: 106, 17: 64, 18: 79, 19: 80, 20: 90}

VK_TO_MAC: dict[int, int] = {
    **{ord(ch.upper()): code for ch, code in _LETTERS.items()},
    **{ord(ch): code for ch, code in _DIGITS.items()},
    **{0x6F + n: code for n, code in _FKEYS.items()},
    0x0D: 36,  # enter
    0x1B: 53,  # esc
    0x20: 49,  # space
    0x09: 48,  # tab
    0x08: 51,  # backspace
    0x2E: 117,  # forward delete
    0x2D: 114,  # insert -> help
    0x24: 115, 0x23: 119, 0x21: 116, 0x22: 121,  # home end pgup pgdn
    0x25: 123, 0x27: 124, 0x28: 125, 0x26: 126,  # left right down up
    0x10: 56, 0xA0: 56, 0xA1: 60,  # shift
    0x11: 59, 0xA2: 59, 0xA3: 62,  # control
    0x12: 58, 0xA4: 58, 0xA5: 61,  # option
    0x5B: 55, 0x5C: 54,  # command
    0xBA: 41, 0xBB: 24, 0xBC: 43, 0xBD: 27, 0xBE: 47, 0xBF: 44, 0xC0: 50, 0xDB: 33, 0xDC: 42, 0xDD: 30, 0xDE: 39,
    **{0x60 + n: code for n, code in enumerate((82, 83, 84, 85, 86, 87, 88, 89, 91, 92))},  # numpad 0-9
}

# What the event tap reports, as the VK the trigger logic expects (left / right modifiers kept apart).
MAC_TO_VK: dict[int, int] = {}
for _vk, _code in VK_TO_MAC.items():
    MAC_TO_VK.setdefault(_code, _vk)
MAC_TO_VK.update({56: 0xA0, 60: 0xA1, 59: 0xA2, 62: 0xA3, 58: 0xA4, 61: 0xA5, 55: 0x5B, 54: 0x5C, 76: 0x0D})

MODIFIER_CODES = frozenset({54, 55, 56, 58, 59, 60, 61, 62, 63, 57})

# CGEventFlags
FLAG_SHIFT = 1 << 17
FLAG_CONTROL = 1 << 18
FLAG_OPTION = 1 << 19
FLAG_COMMAND = 1 << 20
FLAG_FOR_VK = {0x10: FLAG_SHIFT, 0xA0: FLAG_SHIFT, 0xA1: FLAG_SHIFT, 0x11: FLAG_CONTROL, 0xA2: FLAG_CONTROL,
               0xA3: FLAG_CONTROL, 0x12: FLAG_OPTION, 0xA4: FLAG_OPTION, 0xA5: FLAG_OPTION, 0x5B: FLAG_COMMAND,
               0x5C: FLAG_COMMAND}
FLAG_FOR_CODE = {56: FLAG_SHIFT, 60: FLAG_SHIFT, 59: FLAG_CONTROL, 62: FLAG_CONTROL, 58: FLAG_OPTION,
                 61: FLAG_OPTION, 55: FLAG_COMMAND, 54: FLAG_COMMAND}
