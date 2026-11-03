"""Detectors that feed the gate (SPEC §5): another app recording (a call), speakers playing.

Both ask the Windows audio engine through the MMDevice API (plain ctypes COM):
- mic busy: an active capture session, on any active input device, owned by a process
  other than Nova. (SPEC suggested the ConsentStore LastUsedTimeStop registry values;
  on this laptop they stopped updating on 2026-10-28, for every app.)
- speakers: the peak meter of the default output device, sampled while he talks.

Use an ``AudioDetector`` from one thread only; COM is initialized on that thread.
"""

from __future__ import annotations

import ctypes
import logging
import os
from collections.abc import Sequence
from ctypes import POINTER, byref, c_float, c_int, c_uint, c_void_p, wintypes

_log = logging.getLogger(__name__)

PEAK_THRESHOLD = 0.01  # -40 dBFS: anything audible
PLAYING_SHARE = 0.25  # this share of the samples taken while he talked were above it


def speakers_playing(peaks: Sequence[float], threshold: float = PEAK_THRESHOLD, share: float = PLAYING_SHARE) -> bool:
    """Were the speakers playing while he talked? ``peaks`` = meter samples taken during the utterance."""
    if not peaks:
        return False
    loud = sum(1 for peak in peaks if peak > threshold)
    return loud / len(peaks) >= share


# --- COM plumbing ----------------------------------------------------------------------------------------

_ole32 = ctypes.OleDLL("ole32")
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, POINTER(wintypes.DWORD)]
_kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

_CLSCTX_ALL = 0x17
_COINIT_MULTITHREADED = 0x0
_E_RENDER, _E_CAPTURE = 0, 1
_E_MULTIMEDIA = 1
_DEVICE_STATE_ACTIVE = 0x1
_SESSION_ACTIVE = 1
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


def _guid(text: str) -> _GUID:
    guid = _GUID()
    _ole32.CLSIDFromString(ctypes.c_wchar_p(text), byref(guid))
    return guid


_CLSID_MMDeviceEnumerator = _guid("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
_IID_IMMDeviceEnumerator = _guid("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
_IID_IAudioSessionManager2 = _guid("{77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F}")
_IID_IAudioSessionControl2 = _guid("{BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D}")
_IID_IAudioMeterInformation = _guid("{C02216F6-8C67-4B5B-9D00-D008E73E0064}")


def _call(pointer: c_void_p, index: int, *args: object, types: Sequence[object] = ()) -> None:
    """Call vtable slot ``index`` of a COM interface; a failing HRESULT raises OSError."""
    vtable = ctypes.cast(pointer, POINTER(POINTER(c_void_p)))[0]
    method = ctypes.WINFUNCTYPE(ctypes.HRESULT, c_void_p, *types)(vtable[index])
    method(pointer, *args)


def _release(pointer: c_void_p) -> None:
    if pointer:
        vtable = ctypes.cast(pointer, POINTER(POINTER(c_void_p)))[0]
        ctypes.WINFUNCTYPE(ctypes.c_ulong, c_void_p)(vtable[2])(pointer)


def _activate(device: c_void_p, iid: _GUID) -> c_void_p:
    out = c_void_p()
    _call(device, 3, byref(iid), _CLSCTX_ALL, None, byref(out),
          types=(POINTER(_GUID), wintypes.DWORD, c_void_p, POINTER(c_void_p)))
    return out


def _process_name(pid: int) -> str:
    handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return f"process {pid}"
    try:
        size = wintypes.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not _kernel32.QueryFullProcessImageNameW(handle, 0, buffer, byref(size)):
            return f"process {pid}"
        return os.path.basename(buffer.value)
    finally:
        _kernel32.CloseHandle(handle)


class AudioDetector:
    def __init__(self, own_pid: int | None = None) -> None:
        _ole32.CoInitializeEx(None, _COINIT_MULTITHREADED)
        self._own = os.getpid() if own_pid is None else own_pid
        self._enumerator = c_void_p()
        _ole32.CoCreateInstance(byref(_CLSID_MMDeviceEnumerator), None, _CLSCTX_ALL, byref(_IID_IMMDeviceEnumerator),
                                byref(self._enumerator))
        self._meter: c_void_p | None = None
        self._names: dict[int, str] = {}
        self._warned: set[str] = set()

    def _warn(self, what: str, error: Exception) -> None:
        if what not in self._warned:
            self._warned.add(what)
            _log.warning("audio detector: %s failed (%s); treating it as no", what, error)

    def recording_apps(self) -> list[str]:
        """Names of other processes recording from any input device right now (empty if none / unknown)."""
        names: list[str] = []
        devices = c_void_p()
        try:
            _call(self._enumerator, 3, _E_CAPTURE, _DEVICE_STATE_ACTIVE, byref(devices),
                  types=(c_int, wintypes.DWORD, POINTER(c_void_p)))
            count = c_uint()
            _call(devices, 3, byref(count), types=(POINTER(c_uint),))
            for index in range(count.value):
                device = c_void_p()
                _call(devices, 4, index, byref(device), types=(c_uint, POINTER(c_void_p)))
                try:
                    names.extend(self._recording_on(device))
                finally:
                    _release(device)
        except OSError as error:
            self._warn("listing recording apps", error)
            return []
        finally:
            _release(devices)
        return names

    def _recording_on(self, device: c_void_p) -> list[str]:
        manager = _activate(device, _IID_IAudioSessionManager2)
        sessions = c_void_p()
        found: list[str] = []
        try:
            _call(manager, 5, byref(sessions), types=(POINTER(c_void_p),))
            count = c_int()
            _call(sessions, 3, byref(count), types=(POINTER(c_int),))
            for index in range(count.value):
                control, control2 = c_void_p(), c_void_p()
                try:
                    _call(sessions, 4, index, byref(control), types=(c_int, POINTER(c_void_p)))
                    state = c_int()
                    _call(control, 3, byref(state), types=(POINTER(c_int),))
                    if state.value != _SESSION_ACTIVE:
                        continue
                    _call(control, 0, byref(_IID_IAudioSessionControl2), byref(control2),
                          types=(POINTER(_GUID), POINTER(c_void_p)))
                    pid = wintypes.DWORD()
                    _call(control2, 14, byref(pid), types=(POINTER(wintypes.DWORD),))
                    if pid.value and pid.value != self._own:
                        if pid.value not in self._names:
                            self._names[pid.value] = _process_name(pid.value)
                        found.append(self._names[pid.value])
                finally:
                    _release(control2)
                    _release(control)
        finally:
            _release(sessions)
            _release(manager)
        return found

    def refresh_speakers(self) -> None:
        """Re-pick the default output device (call at the start of each utterance)."""
        if self._meter is not None:
            _release(self._meter)
            self._meter = None
        device = c_void_p()
        try:
            _call(self._enumerator, 4, _E_RENDER, _E_MULTIMEDIA, byref(device), types=(c_int, c_int, POINTER(c_void_p)))
            self._meter = _activate(device, _IID_IAudioMeterInformation)
        except OSError as error:
            self._warn("opening the speaker meter", error)  # e.g. no output device at all
        finally:
            _release(device)

    def speaker_peak(self) -> float | None:
        if self._meter is None:
            return None
        peak = c_float()
        try:
            _call(self._meter, 3, byref(peak), types=(POINTER(c_float),))
        except OSError as error:
            self._warn("reading the speaker meter", error)
            return None
        return float(peak.value)

    def close(self) -> None:
        if self._meter is not None:
            _release(self._meter)
            self._meter = None
        _release(self._enumerator)
        self._enumerator = c_void_p()
