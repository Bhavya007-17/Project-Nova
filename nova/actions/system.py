"""Volume and media (SPEC §8): the default speakers' volume through Core Audio, media keys for players.

Volume is set exactly (and read back) rather than by pressing volume keys, so "volume 30"
works and the result can say what it is now. "pause" / "play" first look at the speakers'
peak meter: the media key toggles, so pausing when nothing plays would start the music.
"""

from __future__ import annotations

import ctypes
import time
from ctypes import POINTER, byref, c_float, c_void_p, wintypes

from . import native
from .native import ActionError

_CLSID_MMDeviceEnumerator = native.guid("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
_IID_IMMDeviceEnumerator = native.guid("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
_IID_IAudioEndpointVolume = native.guid("{5CDF2C82-841E-4546-9722-0CF74078229A}")
_IID_IAudioMeterInformation = native.guid("{C02216F6-8C67-4B5B-9D00-D008E73E0064}")
_CLSCTX_ALL = 0x17
_E_RENDER, _E_MULTIMEDIA = 0, 1
DEFAULT_STEP = 10
_AUDIBLE = 0.01  # peak meter: -40 dBFS
_MEDIA_KEYS = {"play_pause": 0xB3, "play": 0xB3, "pause": 0xB3, "next": 0xB0, "prev": 0xB1, "stop": 0xB2}


def _endpoint(iid: native.GUID) -> c_void_p:
    native.com_init()
    enumerator, device, out = c_void_p(), c_void_p(), c_void_p()
    hr = native.ole32.CoCreateInstance(byref(_CLSID_MMDeviceEnumerator), None, _CLSCTX_ALL,
                                       byref(_IID_IMMDeviceEnumerator), byref(enumerator))
    if hr < 0:
        raise ActionError("Couldn't reach Windows audio.")
    try:
        if native.com_call(enumerator, 4, _E_RENDER, _E_MULTIMEDIA, byref(device),
                           types=(ctypes.c_int, ctypes.c_int, POINTER(c_void_p))) < 0 or not device:
            raise ActionError("No speakers or headphones are set as the default output.")
        try:
            if native.com_call(device, 3, byref(iid), _CLSCTX_ALL, None, byref(out),
                               types=(POINTER(native.GUID), wintypes.DWORD, c_void_p, POINTER(c_void_p))) < 0:
                raise ActionError("Couldn't reach the speakers' controls.")
        finally:
            native.com_release(device)
    finally:
        native.com_release(enumerator)
    return out


def _level(volume: c_void_p) -> tuple[int, bool]:
    level, muted = c_float(), wintypes.BOOL()
    native.com_call(volume, 9, byref(level), types=(POINTER(c_float),))
    native.com_call(volume, 15, byref(muted), types=(POINTER(wintypes.BOOL),))
    return round(level.value * 100), bool(muted.value)


def volume(op: str, *, step: int | None = None, level: int | None = None) -> str:
    endpoint = _endpoint(_IID_IAudioEndpointVolume)
    try:
        now, muted = _level(endpoint)
        if op in ("mute", "unmute"):
            want = op == "mute"
            if muted == want:
                return "Already muted" if want else "Sound is on"
            native.com_call(endpoint, 14, want, None, types=(wintypes.BOOL, c_void_p))
            return "Muted" if want else f"Unmuted · {now}%"
        if op == "set":
            target = max(0, min(100, int(level if level is not None else now)))
        else:
            change = step or DEFAULT_STEP
            target = max(0, min(100, now + (change if op == "up" else -change)))
        native.com_call(endpoint, 7, c_float(target / 100.0), None, types=(c_float, c_void_p))
        if muted and op in ("up", "set") and target > 0:
            native.com_call(endpoint, 14, False, None, types=(wintypes.BOOL, c_void_p))
        after, _ = _level(endpoint)
        return f"Volume {after}%"
    finally:
        native.com_release(endpoint)


def playing(samples: int = 4, every: float = 0.03) -> bool:
    """Is anything audible on the speakers right now?"""
    meter = _endpoint(_IID_IAudioMeterInformation)
    try:
        peak = c_float()
        for i in range(samples):
            if native.com_call(meter, 3, byref(peak), types=(POINTER(c_float),)) >= 0 and peak.value > _AUDIBLE:
                return True
            if i + 1 < samples:
                time.sleep(every)
        return False
    finally:
        native.com_release(meter)


def media(op: str) -> str:
    if op == "pause" and not playing():
        return "Nothing's playing"
    if op == "play" and playing():
        return "Already playing"
    vk = _MEDIA_KEYS[op]
    native.send_keys([(vk, True), (vk, False)])
    return {"play_pause": "Play / pause", "play": "Playing", "pause": "Paused", "next": "Next track",
            "prev": "Previous track", "stop": "Stopped"}[op]
