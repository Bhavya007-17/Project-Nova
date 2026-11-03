"""Keys, pure parts (SPEC §5): chord parsing, trigger recognition on the raw key stream, gestures.

``TriggerKeys.on_key`` runs inside the low-level hook callback (``nova.hook``), so it
only looks at a few integers and returns a ``Verdict``: swallow the key or not, keys
to replay right after the callback, and maybe one ``TriggerEvent`` for the controller.
``Gestures`` runs on the controller thread and turns trigger down/up events into
press / release (tap or hold) / double-tap / cancel.
"""

from __future__ import annotations

from dataclasses import dataclass, field

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


# --- raw keys -> trigger events (runs inside the hook callback) ---------------------------------------

VK_SHIFT, VK_CONTROL, VK_MENU = 0x10, 0x11, 0x12  # generic codes (some injectors send these)
VK_LSHIFT, VK_RSHIFT = 0xA0, 0xA1
VK_LCONTROL, VK_RCONTROL = 0xA2, 0xA3
VK_LMENU, VK_RMENU = 0xA4, 0xA5
VK_LWIN, VK_RWIN = 0x5B, 0x5C
VK_ESCAPE = 0x1B
VK_RETURN = 0x0D  # also the numpad Enter
VK_F23 = 0x86  # the Copilot key sends LWin + LShift + F23
VK_MASK = 0xE8  # unassigned: replayed before a Win/Alt release so Start / the menu bar don't open
NOVA_MARK = 0x4E4F5641  # dwExtraInfo on every key Nova injects ("NOVA"); the hook ignores those

_MODIFIER_FLAGS = {
    VK_SHIFT: MOD_SHIFT, VK_LSHIFT: MOD_SHIFT, VK_RSHIFT: MOD_SHIFT,
    VK_CONTROL: MOD_CONTROL, VK_LCONTROL: MOD_CONTROL, VK_RCONTROL: MOD_CONTROL,
    VK_MENU: MOD_ALT, VK_LMENU: MOD_ALT, VK_RMENU: MOD_ALT,
    VK_LWIN: MOD_WIN, VK_RWIN: MOD_WIN,
}
_COPILOT_MODIFIERS = MOD_WIN | MOD_SHIFT
_MASKED_MODIFIERS = MOD_WIN | MOD_ALT  # released alone, these open Start / a menu bar
# Keys that answer the island while it asks (it never takes focus, so the hook reads them).
_ANSWER_KEYS = {
    "confirm": {VK_RETURN: "enter"},
    "clarify": {**{0x31 + i: str(i + 1) for i in range(4)}, **{0x61 + i: str(i + 1) for i in range(4)}},  # 1-4, numpad 1-4
}


@dataclass(frozen=True, slots=True)
class Key:
    """One event from the low-level hook. ``at`` = perf_counter when the hook saw it."""

    vk: int
    down: bool
    at: float
    scan: int = 0
    flags: int = 0  # KBDLLHOOKSTRUCT.flags (0x10 = injected, 0x01 = extended)
    time: int = 0  # the event's own GetTickCount stamp (ms)
    extra: int = 0  # dwExtraInfo

    @property
    def injected(self) -> bool:
        return bool(self.flags & 0x10)

    @property
    def ours(self) -> bool:
        return self.extra == NOVA_MARK


@dataclass(frozen=True, slots=True)
class TriggerEvent:
    source: str  # "copilot" | "chord" | "esc" | "answer" | "pin" (Ctrl + Copilot)
    down: bool
    at: float
    key: str = ""  # answer: "enter" (Confirm) or "1".."4" (Clarify)


@dataclass(frozen=True, slots=True)
class Verdict:
    swallow: bool = False
    replay: tuple[tuple[int, bool], ...] = ()  # (vk, down) to inject right after the callback returns
    event: TriggerEvent | None = None


PASS = Verdict()
_SWALLOW = Verdict(swallow=True)


@dataclass(frozen=True)
class _Config:
    chord: Chord | None
    copilot: bool


class TriggerKeys:
    """Finds the Copilot key and the fallback chord in the raw key stream.

    Owned by the hook thread. Other threads only replace whole values:
    ``configure(...)`` swaps an immutable config, ``esc_armed`` is a bool, ``asking``
    is "confirm" / "clarify" / None (the island is asking: Enter / 1-4 answer it,
    with no modifier held, so Ctrl+1 and friends still reach his app).
    Keys Nova injected itself are never looked at; other injected keys count
    (some laptops synthesize the Copilot chord in a vendor driver).

    Swallowing the trigger key leaves Windows seeing Win (or Alt) pressed and
    released alone, which opens Start (or a menu bar). So the release of a Win /
    Alt key that was down when a trigger fired is swallowed too and replayed as
    mask key down, mask key up, release, after the callback returns. Injecting
    inside the callback would cost 4-20 ms (SendInput runs every hook in the system).
    """

    def __init__(self, chord: Chord | None = None, copilot: bool = True) -> None:
        self._config = _Config(chord, copilot)
        self.esc_armed = False  # the controller arms Esc while a session can be cancelled
        self.asking: str | None = None  # the controller arms answer keys while the island asks
        self._modifiers: frozenset[int] = frozenset()
        self._masked: frozenset[int] = frozenset()  # Win / Alt keys whose release gets the mask
        self._copilot_down = False
        self._pin_down = False  # Ctrl + Copilot is held (pin / unpin the island)
        self._chord_down: int | None = None  # vk of the chord key while it is held
        self._esc_down = False
        self._answer_down: int | None = None  # vk of a swallowed answer key while it is held

    def configure(self, chord: Chord | None, copilot: bool) -> None:
        self._config = _Config(chord, copilot)

    def _flags(self) -> int:
        flags = 0
        for vk in self._modifiers:
            flags |= _MODIFIER_FLAGS[vk]
        return flags

    def on_key(self, key: Key) -> Verdict:
        if key.ours:
            return PASS
        vk = key.vk
        if vk in _MODIFIER_FLAGS:
            if key.down:
                self._modifiers = self._modifiers | {vk}
                return PASS
            self._modifiers = self._modifiers - {vk}
            if vk in self._masked:
                self._masked = self._masked - {vk}
                return Verdict(swallow=True, replay=((VK_MASK, True), (VK_MASK, False), (vk, False)))
            return PASS
        config = self._config
        if vk == VK_F23 and (config.copilot or self._copilot_down or self._pin_down):
            return self._copilot(key)
        if self._chord_down == vk or (config.chord is not None and vk == config.chord.vk):
            verdict = self._chord(key, config.chord)
            if verdict is not PASS:
                return verdict  # (a chord key pressed without its modifiers may still answer the island)
        if vk == VK_ESCAPE:
            return self._esc(key)
        if self._answer_down == vk or self.asking is not None:
            return self._answer(key)
        return PASS

    def _copilot(self, key: Key) -> Verdict:
        if key.down:
            if self._copilot_down or self._pin_down:
                return _SWALLOW  # auto-repeat while held
            flags = self._flags()
            if flags & _COPILOT_MODIFIERS != _COPILOT_MODIFIERS:
                return PASS  # a plain F23 from some other tool
            self._mask_held_modifiers()
            if flags & MOD_CONTROL:  # Ctrl + Copilot: keep the island on screen, or stop
                self._pin_down = True
                return Verdict(swallow=True, event=TriggerEvent("pin", True, key.at))
            self._copilot_down = True
            return Verdict(swallow=True, event=TriggerEvent("copilot", True, key.at))
        if self._pin_down:
            self._pin_down = False
            return _SWALLOW
        if not self._copilot_down:
            return PASS
        self._copilot_down = False
        return Verdict(swallow=True, event=TriggerEvent("copilot", False, key.at))

    def _chord(self, key: Key, chord: Chord | None) -> Verdict:
        if key.down:
            if self._chord_down == key.vk:
                return _SWALLOW
            if chord is None or self._flags() != chord.modifiers:
                return PASS
            self._chord_down = key.vk
            self._mask_held_modifiers()
            return Verdict(swallow=True, event=TriggerEvent("chord", True, key.at))
        if self._chord_down != key.vk:
            return PASS
        self._chord_down = None
        return Verdict(swallow=True, event=TriggerEvent("chord", False, key.at))

    def _mask_held_modifiers(self) -> None:
        self._masked = self._masked | {vk for vk in self._modifiers if _MODIFIER_FLAGS[vk] & _MASKED_MODIFIERS}

    def _esc(self, key: Key) -> Verdict:
        if key.down:
            if self._esc_down:
                return _SWALLOW
            if not self.esc_armed:
                return PASS
            self._esc_down = True
            return Verdict(swallow=True, event=TriggerEvent("esc", True, key.at))
        if not self._esc_down:
            return PASS
        self._esc_down = False
        return _SWALLOW

    def _answer(self, key: Key) -> Verdict:
        if not key.down:
            if self._answer_down != key.vk:
                return PASS
            self._answer_down = None
            return _SWALLOW  # its press was swallowed: so is the release, even if no longer asking
        if self._answer_down == key.vk:
            return _SWALLOW  # auto-repeat while held
        name = _ANSWER_KEYS.get(self.asking or "", {}).get(key.vk)
        if name is None or self._flags():
            return PASS
        self._answer_down = key.vk
        return Verdict(swallow=True, event=TriggerEvent("answer", True, key.at, name))

    def reset(self, at: float) -> list[TriggerEvent]:
        """The hook was reinstalled and may have missed key-ups: release whatever was held."""
        released = []
        if self._copilot_down:
            released.append(TriggerEvent("copilot", False, at))
        if self._chord_down is not None:
            released.append(TriggerEvent("chord", False, at))
        self._modifiers = frozenset()
        self._masked = frozenset()
        self._copilot_down = False
        self._pin_down = False
        self._chord_down = None
        self._esc_down = False
        self._answer_down = None
        return released

    @property
    def holding(self) -> bool:
        """A trigger key is down right now (the hook should not be swapped under it)."""
        return self._copilot_down or self._pin_down or self._chord_down is not None


# --- trigger events -> gestures (runs on the controller thread) ---------------------------------------

HOLD_S = 0.25  # held longer than this = push-to-talk
DOUBLE_TAP_S = 0.4  # second press this soon after a tap's release = double-tap


@dataclass(frozen=True)
class Press:
    source: str
    at: float


@dataclass(frozen=True)
class Release:
    source: str
    at: float
    held: float  # seconds the key was down
    hold: bool  # longer than HOLD_S: push-to-talk ends now


@dataclass(frozen=True)
class DoubleTap:
    source: str
    at: float


@dataclass(frozen=True)
class Cancel:
    at: float
    why: str = "Esc"  # Esc | No (the island's No button)


Gesture = Press | Release | DoubleTap | Cancel


@dataclass
class Gestures:
    """Trigger down/up -> Press / Release(tap or hold) / DoubleTap; Esc -> Cancel."""

    _down: TriggerEvent | None = None
    _last_tap: TriggerEvent | None = None  # release of the last tap, for double-tap
    _double: bool = field(default=False)  # the key now down is the second tap of a double-tap

    def feed(self, event: TriggerEvent) -> tuple[Gesture, ...]:
        if event.source in ("answer", "pin"):
            return ()  # not gestures: the controller handles these itself
        if event.source == "esc":
            if not event.down:
                return ()
            self._last_tap = None
            return (Cancel(event.at),)
        if event.down:
            if self._down is not None:
                return ()  # another trigger is already down
            self._down = event
            last = self._last_tap
            self._last_tap = None
            if last is not None and last.source == event.source and event.at - last.at <= DOUBLE_TAP_S:
                self._double = True
                return (DoubleTap(event.source, event.at),)
            self._double = False
            return (Press(event.source, event.at),)
        began = self._down
        if began is None or began.source != event.source:
            return ()
        self._down = None
        held = event.at - began.at
        hold = held > HOLD_S
        self._last_tap = event if not hold and not self._double else None
        self._double = False
        return (Release(event.source, event.at, held, hold),)
