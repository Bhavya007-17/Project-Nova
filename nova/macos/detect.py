"""macOS stand-in for ``nova.detect``. macOS has no public API for "which app has the mic" or the output peak meter,
so these report nothing; the gate then decides from speech alone (as when the Windows meters fail)."""

from __future__ import annotations

from collections.abc import Sequence

PEAK_THRESHOLD = 0.01
PLAYING_SHARE = 0.25


def speakers_playing(peaks: Sequence[float], threshold: float = PEAK_THRESHOLD, share: float = PLAYING_SHARE) -> bool:
    if not peaks:
        return False
    loud = sum(1 for peak in peaks if peak > threshold)
    return loud / len(peaks) >= share


class AudioDetector:
    def __init__(self, own_pid: int | None = None) -> None:
        pass

    def recording_apps(self) -> list[str]:
        return []

    def refresh_speakers(self) -> None:
        pass

    def speaker_peak(self) -> float | None:
        return None

    def close(self) -> None:
        pass
