"""Text on screen through Windows' own OCR (Windows.Media.Ocr via winrt, SPEC §8): what the numbers tag where UIA
has nothing (canvas apps, images, custom-drawn UIs). Local, no model to load: the engine ships with Windows.

Measured here (2880 × 1800 at 200 %): grab 50 ms + bitmap 20 ms + recognize 76 ms for a full window; the engine takes
images up to ``OcrEngine.max_image_dimension`` (10 000 px) a side. One grab per showing, of the window in scope only.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Sequence
from dataclasses import dataclass

from . import screen
from .marks import Box, Target

_log = logging.getLogger(__name__)

LINE_GAP = 1.5  # a gap wider than this many line heights splits one OCR line (toolbar items on a row) in two
_engine_lock = threading.Lock()
_engine: object | None = None
_engine_failed = False


@dataclass(frozen=True)
class Word:
    text: str
    box: Box


def segments(words: Sequence[Word], gap: float = LINE_GAP) -> list[Target]:
    """One OCR line's words as targets, split where the space between two words is wide."""
    out: list[Target] = []
    current: list[Word] = []
    for word in sorted(words, key=lambda w: w.box.left):
        if not word.text.strip():
            continue
        if current:
            last = current[-1]
            height = max(last.box.height, word.box.height, 1)
            if word.box.left - last.box.right > gap * height:
                out.append(_join(current))
                current = []
        current.append(word)
    if current:
        out.append(_join(current))
    return out


def _join(words: Sequence[Word]) -> Target:
    box = Box(min(w.box.left for w in words), min(w.box.top for w in words),
              max(w.box.right for w in words), max(w.box.bottom for w in words))
    return Target(box, " ".join(w.text for w in words), "text", "ocr")


def _get_engine() -> object | None:
    global _engine, _engine_failed  # noqa: PLW0603 - one engine per process, made on first use
    with _engine_lock:
        if _engine is None and not _engine_failed:
            try:
                from winrt.windows.media.ocr import OcrEngine  # noqa: PLC0415 - winrt loads ~40 ms; only when used

                _engine = OcrEngine.try_create_from_user_profile_languages()
            except Exception:  # noqa: BLE001 - winrt missing or broken: numbers still work from UIA
                _log.exception("OCR engine failed to load")
            if _engine is None:
                _engine_failed = True
                _log.warning("no OCR engine (no OCR language for the user's profile languages?)")
        return _engine


def lines(shot: screen.Shot) -> list[Target]:
    """Every line of text in ``shot`` (screen coordinates), split at wide gaps. [] when Windows has no OCR."""
    engine = _get_engine()
    if engine is None or shot.width < 8 or shot.height < 8:
        return []
    try:
        from winrt.windows.graphics.imaging import BitmapAlphaMode, BitmapPixelFormat, SoftwareBitmap  # noqa: PLC0415
        from winrt.windows.media.ocr import OcrEngine  # noqa: PLC0415
        from winrt.windows.storage.streams import DataWriter  # noqa: PLC0415

        limit = int(OcrEngine.max_image_dimension)
        width, height, data = shot.width, shot.height, shot.bgra
        if width > limit or height > limit:  # crop: past 10 000 px a side the engine refuses the whole image
            data = b"".join(data[row * width * 4:(row * width + min(width, limit)) * 4]
                            for row in range(min(height, limit)))
            width, height = min(width, limit), min(height, limit)
        writer = DataWriter()
        writer.write_bytes(data)
        bitmap = SoftwareBitmap.create_copy_from_buffer(writer.detach_buffer(), BitmapPixelFormat.BGRA8, width,
                                                        height, BitmapAlphaMode.PREMULTIPLIED)

        async def recognize():
            return await engine.recognize_async(bitmap)

        result = asyncio.run(recognize())
    except Exception:  # noqa: BLE001 - OCR is a helper: a failure leaves the UIA tags
        _log.exception("OCR failed")
        return []
    found: list[Target] = []
    for line in result.lines:
        words = []
        for word in line.words:
            r = word.bounding_rect
            left, top = shot.left + int(r.x), shot.top + int(r.y)
            words.append(Word(word.text, Box(left, top, left + int(r.width), top + int(r.height))))
        found.extend(segments(words))
    return found
