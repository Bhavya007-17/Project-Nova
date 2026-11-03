"""Mic capture (SPEC §4): 16 kHz mono float32 in 512-sample blocks -> a ring buffer.

The PortAudio callback only copies into the ring and wakes the speech worker.
"default" opens the MME Sound Mapper, which follows the Windows default input
every time it is (re)opened; measured here, MME costs ~1.2 % of a core vs
~1.7 % for WASAPI with auto-convert, at the same 32 ms blocks. A watchdog
reopens the stream when audio stops (unplugged) and when the Windows default
input changes. Open/close/check run on the speech worker thread only.
"""

from __future__ import annotations

import ctypes
import logging
import threading
import time
import wave
from collections.abc import Sequence
from ctypes import wintypes
from pathlib import Path

import numpy as np

_log = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
BLOCK = 512  # samples per callback (32 ms)
STALL_S = 1.5  # no audio for this long while open = the device went away
POLL_DEFAULT_S = 2.0  # how often to check whether the Windows default input changed


LEVEL_FLOOR_DB = -60.0  # the island's waveform: this quiet = flat...
LEVEL_FULL_DB = -12.0  # ...this loud = full height (speech here runs about -35 to -10 dBFS)


def level_of(energy: float) -> float:
    """Mean-square frame energy -> 0..1 for the island's waveform (linear in dB)."""
    if energy <= 0.0:
        return 0.0
    db = 10.0 * float(np.log10(energy))
    return min(1.0, max(0.0, (db - LEVEL_FLOOR_DB) / (LEVEL_FULL_DB - LEVEL_FLOOR_DB)))


class MicError(RuntimeError):
    """The mic can't be opened. The message is shown as-is."""


class Ring:
    """Float32 sample ring addressed by absolute sample position (one writer, one reader).

    The writer sets ``wake`` once ``end`` reaches ``wake_at``, so the reader decides how
    often it is woken (thread wake-ups, not the work, dominate the idle cost).
    """

    def __init__(self, capacity: int = 1 << 17, wake: threading.Event | None = None) -> None:  # ~8 s
        self._buf = np.zeros(capacity, dtype=np.float32)
        self.capacity = capacity
        self._end = 0
        self._lock = threading.Lock()
        self.arrival: tuple[int, float] = (0, 0.0)  # (end position, perf_counter) of the latest write
        self.wake = wake
        self.wake_at = 0

    @property
    def end(self) -> int:
        return self._end

    @property
    def oldest(self) -> int:
        return max(0, self._end - self.capacity)

    def write(self, samples: np.ndarray, at: float) -> None:
        total = len(samples)
        with self._lock:
            start = self._end
            if total > self.capacity:  # only the newest samples fit
                samples = samples[-self.capacity :]
                start += total - self.capacity
            n = len(samples)
            i = start % self.capacity
            first = min(n, self.capacity - i)
            self._buf[i : i + first] = samples[:first]
            if first < n:
                self._buf[: n - first] = samples[first:]
            self._end += total
            self.arrival = (self._end, at)
        if self.wake is not None and self._end >= self.wake_at:
            self.wake.set()

    def write_bytes(self, data: object, at: float) -> None:
        self.write(np.frombuffer(data, dtype=np.float32), at)

    def read(self, start: int, end: int) -> np.ndarray | None:
        """A copy of samples [start, end), or None if they were overwritten or aren't written yet."""
        with self._lock:
            if start < self._end - self.capacity or end > self._end or start > end:
                return None
            i, n = start % self.capacity, end - start
            first = min(n, self.capacity - i)
            out = np.empty(n, dtype=np.float32)
            out[:first] = self._buf[i : i + first]
            if first < n:
                out[first:] = self._buf[: n - first]
            return out


def read_wav(path: Path) -> np.ndarray:
    """16 kHz mono 16-bit PCM WAV -> float32 in [-1, 1]."""
    with wave.open(str(path), "rb") as w:
        if (w.getnchannels(), w.getframerate(), w.getsampwidth()) != (1, SAMPLE_RATE, 2):
            raise ValueError(f"{Path(path).name}: need 16 kHz mono 16-bit WAV")
        data = w.readframes(w.getnframes())
    return np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0


# --- Windows default input (winmm) ---------------------------------------------------

_WAVE_MAPPER = 0xFFFFFFFF
_DRVM_MAPPER_PREFERRED_GET = 0x2015


class _WAVEINCAPSW(ctypes.Structure):
    _fields_ = [
        ("wMid", wintypes.WORD),
        ("wPid", wintypes.WORD),
        ("vDriverVersion", wintypes.UINT),
        ("szPname", wintypes.WCHAR * 32),
        ("dwFormats", wintypes.DWORD),
        ("wChannels", wintypes.WORD),
        ("wReserved1", wintypes.WORD),
    ]


_winmm = ctypes.WinDLL("winmm")
_winmm.waveInMessage.argtypes = [wintypes.HANDLE, wintypes.UINT, ctypes.c_void_p, ctypes.c_void_p]
_winmm.waveInMessage.restype = wintypes.UINT
_winmm.waveInGetDevCapsW.argtypes = [ctypes.c_size_t, ctypes.POINTER(_WAVEINCAPSW), wintypes.UINT]
_winmm.waveInGetDevCapsW.restype = wintypes.UINT


def preferred_input_name() -> str | None:
    """MME name (max 31 chars) of the input the Windows default currently points at."""
    device, flags = wintypes.DWORD(), wintypes.DWORD()
    if _winmm.waveInMessage(wintypes.HANDLE(_WAVE_MAPPER), _DRVM_MAPPER_PREFERRED_GET, ctypes.byref(device), ctypes.byref(flags)):
        return None
    caps = _WAVEINCAPSW()
    if _winmm.waveInGetDevCapsW(device.value, ctypes.byref(caps), ctypes.sizeof(caps)):
        return None
    return caps.szPname


def _ascii(text: str) -> str:
    return text.encode("ascii", "ignore").decode().strip().casefold()


def full_name(mme_name: str, full_names: Sequence[str]) -> str:
    """MME cuts names at 31 characters; find the full (WASAPI) name it starts."""
    short = _ascii(mme_name)
    return next((name for name in full_names if short and _ascii(name).startswith(short)), mme_name.strip())


def mme_match(setting: str, mme_names: dict[int, str], full_names: Sequence[str]) -> int | None:
    """MME input index whose name (or full name) contains ``setting``; the Sound Mapper never matches."""
    wanted = _ascii(setting)
    if not wanted:
        return None
    for index, name in mme_names.items():
        if "sound mapper" in name.casefold():
            continue
        if wanted in _ascii(name) or wanted in _ascii(full_name(name, full_names)):
            return index
    return None


class Mic:
    """The input stream. Not thread-safe: the speech worker calls everything except the callback."""

    def __init__(self, ring: Ring, wake: threading.Event, device: str = "default") -> None:
        self._ring = ring
        self._wake = wake
        self.setting = device
        self.name = device
        self.latency = 0.0
        self._stream = None
        self._opened_at = 0.0
        self._preferred: str | None = None
        self._next_poll = 0.0
        self._refresh = False
        self.overflows = 0

    @property
    def is_open(self) -> bool:
        return self._stream is not None

    def open(self) -> None:
        import sounddevice as sd

        if self._refresh:
            # PortAudio lists devices only when it starts; restart it to see plug/unplug changes.
            self._refresh = False
            try:
                sd._terminate()
                sd._initialize()
            except Exception as error:  # noqa: BLE001 - reported as a mic error below
                raise MicError(f"audio system restart failed ({error})") from error
        try:
            index, self.name = self._resolve(sd)
        except MicError:
            raise
        except Exception as error:  # noqa: BLE001 - PortAudio can fail while listing devices too
            self._refresh = True
            raise MicError(f"cannot list microphones ({error})") from error
        try:
            stream = sd.RawInputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype="float32", blocksize=BLOCK,
                device=index, latency="low", callback=self._callback,
            )
            stream.start()
        except Exception as error:  # noqa: BLE001 - PortAudio raises several types
            self._refresh = True
            raise MicError(f"cannot open {self.name} ({error})") from error
        self._stream = stream
        self.latency = float(stream.latency)
        self._opened_at = time.perf_counter()
        self._next_poll = self._opened_at + POLL_DEFAULT_S
        _log.info("mic open: %s (MME device %d, %.0f ms latency)", self.name, index, self.latency * 1000)

    def close(self) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            stream.abort()
            stream.close(ignore_errors=True)
        except Exception:  # noqa: BLE001 - a vanished device can fail to close; nothing else to do
            _log.exception("closing the mic failed")
        _log.info("mic closed")

    def check(self, now: float) -> str | None:
        """While open: a reason to reopen the stream, or None."""
        if self._stream is None:
            return None
        last = max(self._ring.arrival[1], self._opened_at)
        if now - last > STALL_S:
            self._refresh = True
            return "the mic stopped sending audio"
        if self.setting.casefold() == "default" and now >= self._next_poll:
            self._next_poll = now + POLL_DEFAULT_S
            preferred = preferred_input_name()
            if preferred is not None and self._preferred is not None and preferred != self._preferred:
                self._refresh = True
                return "the Windows default mic changed"
        return None

    def _callback(self, indata, frames, time_info, status) -> None:  # PortAudio thread: copy only
        self._ring.write_bytes(indata, time.perf_counter())  # wakes the worker when it wants more
        if status:
            self.overflows += 1

    def _resolve(self, sd) -> tuple[int, str]:
        apis = sd.query_hostapis()
        mme = next((i for i, api in enumerate(apis) if api["name"] == "MME"), None)
        if mme is None:
            raise MicError("Windows MME audio is missing")
        wasapi = next((i for i, api in enumerate(apis) if api["name"] == "Windows WASAPI"), None)
        devices = sd.query_devices()
        inputs = {int(d["index"]): d["name"] for d in devices if d["hostapi"] == mme and d["max_input_channels"] > 0}
        full = [d["name"] for d in devices if d["hostapi"] == wasapi and d["max_input_channels"] > 0]
        if not inputs:
            self._refresh = True
            raise MicError("no microphone found")
        self._preferred = preferred_input_name()
        if self.setting.casefold() != "default":
            index = mme_match(self.setting, inputs, full)
            if index is not None:
                return index, full_name(inputs[index], full)
            _log.warning("no mic matches %r; using the Windows default", self.setting)
            self._refresh = True  # look again after the next restart of the device list
        mapper = next((i for i, name in inputs.items() if "sound mapper" in name.casefold()), None)
        if mapper is None:
            mapper = int(apis[mme]["default_input_device"])
        shown = full_name(self._preferred, full) if self._preferred else inputs.get(mapper, "default mic")
        return mapper, shown


class WavFeed:
    """Plays 16 kHz WAV files into the ring at real-time pace: the live path without a mic."""

    def __init__(self, ring: Ring, wake: threading.Event, paths: Sequence[Path], *, lead_s: float = 0.5, gap_s: float = 1.2) -> None:
        self._ring = ring
        self._wake = wake
        self._paths = list(paths)
        self._lead = int(lead_s * SAMPLE_RATE)
        self._gap = int(gap_s * SAMPLE_RATE)
        self.name = f"WAV replay ({len(self._paths)} files)"
        self.setting = "wav"
        self.latency = 0.0
        self.starts: list[tuple[Path, int]] = []  # (file, ring position of its first sample)
        self.done = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def is_open(self) -> bool:
        return self._thread is not None and not self._stop.is_set()

    def open(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._play, name="nova-wavfeed", daemon=True)
            self._thread.start()

    def close(self) -> None:
        self._stop.set()

    def check(self, now: float) -> str | None:
        return None

    def _play(self) -> None:
        silence = np.zeros(BLOCK, dtype=np.float32)
        t0 = time.perf_counter()
        written = 0

        def put(block: np.ndarray) -> bool:
            nonlocal written
            target = t0 + (written + len(block)) / SAMPLE_RATE
            delay = target - time.perf_counter()
            if delay > 0 and self._stop.wait(delay):
                return False
            self._ring.write(block, time.perf_counter())
            written += len(block)
            return True

        try:
            for _ in range(self._lead // BLOCK):
                if not put(silence):
                    return
            for path in self._paths:
                audio = read_wav(path)
                pad = (-len(audio)) % BLOCK
                audio = np.concatenate((audio, np.zeros(pad + self._gap, dtype=np.float32)))
                self.starts.append((Path(path), written))
                for i in range(0, len(audio), BLOCK):
                    if not put(audio[i : i + BLOCK]):
                        return
        finally:
            self.done.set()
