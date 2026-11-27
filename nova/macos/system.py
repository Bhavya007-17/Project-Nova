"""macOS stand-in for ``nova.actions.system``: volume (osascript) and media keys (NX system-defined events)."""

from __future__ import annotations

import subprocess

from AppKit import NSEvent

from .native import ActionError

DEFAULT_STEP = 10
_NX_KEYS = {"play_pause": 16, "play": 16, "pause": 16, "next": 17, "prev": 18, "stop": 16}


def _osascript(script: str) -> str:
    result = subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True, text=True, timeout=5,
                            check=False)
    if result.returncode != 0:
        raise ActionError("macOS wouldn't change the volume.")
    return result.stdout.strip()


def _level() -> tuple[int, bool]:
    raw = _osascript("get volume settings")  # output volume:50, input volume:..., alert volume:..., output muted:false
    values = dict(part.strip().split(":", 1) for part in raw.split(",") if ":" in part)
    level = values.get("output volume", "0")
    return (int(level) if level.isdigit() else 0), values.get("output muted", "false") == "true"


def volume(op: str, *, step: int | None = None, level: int | None = None) -> str:
    now, muted = _level()
    if op in ("mute", "unmute"):
        want = op == "mute"
        if muted == want:
            return "Already muted" if want else "Sound is on"
        _osascript(f"set volume output muted {'true' if want else 'false'}")
        return "Muted" if want else f"Unmuted · {now}%"
    if op == "set":
        target = max(0, min(100, int(level if level is not None else now)))
    else:
        change = step or DEFAULT_STEP
        target = max(0, min(100, now + (change if op == "up" else -change)))
    script = f"set volume output volume {target}"
    if muted and op in ("up", "set") and target > 0:
        script += " without output muted"
    _osascript(script)
    return f"Volume {_level()[0]}%"


def playing(samples: int = 4, every: float = 0.03) -> bool:
    """Not measured on a Mac (no public output meter): assume we can't tell."""
    return False


def _media_key(code: int) -> None:
    import Quartz

    for down in (True, False):
        flags = 0xA00 if down else 0xB00
        event = NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
            14, (0, 0), flags, 0, 0, None, 8, (code << 16) | (flags), -1)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event.CGEvent())


def media(op: str) -> str:
    _media_key(_NX_KEYS[op])
    return {"play_pause": "Play / pause", "play": "Play / pause", "pause": "Play / pause", "next": "Next track",
            "prev": "Previous track", "stop": "Play / pause"}[op]
