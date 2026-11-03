"""Speech-to-text backends behind one small interface (SPEC §4: transcribe(pcm) -> text)."""

from .base import Transcriber

__all__ = ["Transcriber"]
