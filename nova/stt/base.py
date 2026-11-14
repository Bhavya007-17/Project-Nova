"""The speech-to-text interface. Swap Parakeet for another model by implementing it."""

from __future__ import annotations

from typing import Protocol

import numpy as np


class Transcriber(Protocol):
    name: str  # short label for logs and traces

    def transcribe(self, pcm: np.ndarray, *, retry_empty: bool = True) -> str:
        """16 kHz mono float32 in [-1, 1] -> text. Must be safe to call from two threads at once.
        ``retry_empty``: decode once more (padded) when nothing came out — for finals, not for live words."""
        ...
