"""`nova rows --dump`: the rows the planner model would see for a window (Stage 07d probe a), saved to data/.

Writes ``data/rows-<time>.json`` (the lines, each row's box and source, the time it took and a token estimate).
Nothing is clicked and no focus is taken: the window is whatever is in front after ``--delay`` seconds, or the first
app window whose title holds ``--title``. Use a throwaway window: rows hold whatever the window shows.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from .. import paths
from . import native
from . import rows as rows_
from .numbers_dump import _window

CHARS_PER_TOKEN = 3.6  # Qwen-style BPE on short English labels; the real count comes from the model's reply


def main(*, delay: float = 3.0, title: str | None = None, out: Path | None = None) -> int:
    from .pointing import Pointer  # noqa: PLC0415 - pulls in OCR and UIA
    from .uia import FocusReader  # noqa: PLC0415

    if not title and delay > 0:
        print(f"Switch to the window to read; dumping its rows in {delay:g} s ...")
        time.sleep(delay)
    win = _window(title)
    if win is None:
        print("No window found" + (f" with {title!r} in its title." if title else " in front."))
        return 1
    started = time.perf_counter()
    found = Pointer(FocusReader()).rows(win)
    took_ms = (time.perf_counter() - started) * 1000
    text = "\n".join(rows_.lines(found))
    tokens = round(len(text) / CHARS_PER_TOKEN)
    folder = out or paths.DATA
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"rows-{datetime.now():%Y%m%d-%H%M%S}.json"
    doc = {"window": {"title": win.title, "exe": win.exe_name, "class": win.cls}, "ms": round(took_ms),
           "count": len(found), "tokens_estimate": tokens, "lines": rows_.lines(found),
           "rows": [{"number": r.number, "source": r.target.source,
                     "box": [r.target.box.left, r.target.box.top, r.target.box.right, r.target.box.bottom]}
                    for r in found]}
    path.write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
    ocr = sum(r.target.source == "ocr" for r in found)
    print(f"{win.exe_name} '{win.title[:60]}': {len(found)} rows ({ocr} from OCR), ~{tokens} tokens, "
          f"read in {took_ms:.0f} ms\n  {path}")
    if native.foreground() != win.hwnd:
        print("  (not the front window)")
    return 0
