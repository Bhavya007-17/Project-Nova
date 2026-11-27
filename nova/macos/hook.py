"""macOS stand-in for ``nova.hook``: a Quartz event tap feeding the same ``TriggerKeys`` logic.

Key codes are translated to Windows VKs (``keymap``), so the chord "ctrl+alt+n" is Control + Option + N.
Modifier changes are always passed through (swallowing a Control release would leave apps thinking it's held);
only the trigger key, Esc while armed, and Enter / 1-4 while the island asks are swallowed.
The tap needs Accessibility permission for the app running Nova. macOS disables a tap whose callback is slow
(or after a timeout); the callback re-enables it and resets the trigger state like the Windows re-hook.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Sequence

import Quartz

from ..keys import NOVA_MARK, Key
from . import keymap

_log = logging.getLogger(__name__)

LATE_MS = 150


class HookStats:
    def __init__(self) -> None:
        self.calls = 0
        self.total_ms = 0.0
        self.max_ms = 0.0
        self.over_5ms = 0
        self.late = 0
        self.raw = 0
        self.reinstalls = 0
        self.errors = 0

    @property
    def mean_ms(self) -> float:
        return self.total_ms / self.calls if self.calls else 0.0


def send_keys(strokes: Sequence[tuple[int, bool]]) -> int:
    from . import native

    return native.send_keys(strokes)


class KeyHook:
    """Installs the tap on a "nova-keys" thread with its own CFRunLoop. ``on_key(Key) -> bool`` (True = swallow)."""

    def __init__(self, on_key: Callable[[Key], bool], *, on_reinstall: Callable[[], None] | None = None,
                 name: str = "nova-keys") -> None:
        self._on_key = on_key
        self._on_reinstall = on_reinstall
        self._tap = None
        self._loop = None
        self._flags = 0
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._callback_ref = self._callback  # keep it alive
        self.stats = HookStats()
        self.error: str | None = None

    def start(self, timeout: float = 5.0) -> bool:
        self._thread.start()
        self._ready.wait(timeout)
        return self._tap is not None

    def close(self) -> None:
        if self._loop is not None:
            Quartz.CFRunLoopStop(self._loop)
        if self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(2)

    @property
    def alive(self) -> bool:
        return self._tap is not None and self._thread.is_alive()

    def post(self, strokes: Sequence[tuple[int, bool]]) -> None:
        """The Windows hook replays masked Win / Alt releases; macOS needs none of that (modifiers pass through)."""

    def _run(self) -> None:
        mask = (Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown) | Quartz.CGEventMaskBit(Quartz.kCGEventKeyUp)
                | Quartz.CGEventMaskBit(Quartz.kCGEventFlagsChanged))
        tap = Quartz.CGEventTapCreate(Quartz.kCGSessionEventTap, Quartz.kCGHeadInsertEventTap,
                                      Quartz.kCGEventTapOptionDefault, mask, self._callback_ref, None)
        if tap is None:
            self.error = ("macOS refused the keyboard tap — give the app running Nova Accessibility permission "
                          "(System Settings > Privacy & Security > Accessibility), then restart Nova")
            self._ready.set()
            return
        source = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
        self._loop = Quartz.CFRunLoopGetCurrent()
        Quartz.CFRunLoopAddSource(self._loop, source, Quartz.kCFRunLoopCommonModes)
        Quartz.CGEventTapEnable(tap, True)
        self._tap = tap
        self._ready.set()
        Quartz.CFRunLoopRun()
        self._tap = None

    def _callback(self, proxy, kind, event, refcon):  # noqa: ANN001
        if kind in (Quartz.kCGEventTapDisabledByTimeout, Quartz.kCGEventTapDisabledByUserInput):
            if self._tap is not None:
                Quartz.CGEventTapEnable(self._tap, True)
            self.stats.reinstalls += 1
            if self._on_reinstall is not None:
                self._on_reinstall()
            return event
        began = time.perf_counter()
        try:
            code = int(Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode))
            extra = int(Quartz.CGEventGetIntegerValueField(event, Quartz.kCGEventSourceUserData))
            vk = keymap.MAC_TO_VK.get(code)
            if kind == Quartz.kCGEventFlagsChanged:
                flag = keymap.FLAG_FOR_CODE.get(code)
                if flag is None or vk is None:
                    return event
                down = bool(Quartz.CGEventGetFlags(event) & flag)
                self._on_key(Key(vk, down, began, code, 0, 0, extra))
                return event  # never swallow modifiers
            if vk is None:
                return event
            down = kind == Quartz.kCGEventKeyDown
            if down and Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventAutorepeat):
                pass  # repeats go through TriggerKeys too: it swallows them while a trigger is held
            swallow = self._on_key(Key(vk, down, began, code, 0, 0, extra))
            return None if swallow and extra != NOVA_MARK else event
        except Exception:  # noqa: BLE001 - never break the user's keyboard
            self.stats.errors += 1
            _log.exception("key tap callback failed")
            return event
        finally:
            ms = (time.perf_counter() - began) * 1000
            stats = self.stats
            stats.calls += 1
            stats.total_ms += ms
            stats.max_ms = max(stats.max_ms, ms)
            if ms > 5:
                stats.over_5ms += 1


class TriggerHook:
    """``KeyHook`` + ``TriggerKeys`` (same contract as the Windows one)."""

    def __init__(self, keys, sink: Callable[[object], None]) -> None:
        self.keys = keys
        self._sink = sink
        self.hook = KeyHook(self._on_key, on_reinstall=self._reinstalled)

    def start(self) -> bool:
        return self.hook.start()

    def close(self) -> None:
        self.hook.close()

    @property
    def error(self) -> str | None:
        return self.hook.error

    def _on_key(self, key: Key) -> bool:
        verdict = self.keys.on_key(key)
        if verdict.event is not None:
            self._sink(verdict.event)
        return verdict.swallow

    def _reinstalled(self) -> None:
        for event in self.keys.reset(time.perf_counter()):
            self._sink(event)
