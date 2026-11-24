"""`nova numbers --dump`: what "show numbers" would tag on a window, saved for counting misses (prompt 07c).

Writes ``data/numbers-<time>.json`` (every tag: number, name, role, source, box) and ``data/numbers-<time>.png``
(the window's pixels, same physical coordinates) so the things numbers missed on his real apps can be counted.
Nothing is clicked and no focus is taken: the window is whatever is in front after ``--delay`` seconds, or the
first app window whose title holds ``--title``. ``records`` is pure.
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from .. import paths
from . import marks, native, screen
from .capture import to_rgb
from .marks import Mark


def records(numbered: Sequence[Mark], origin: tuple[int, int] = (0, 0)) -> list[dict]:
    """One dict per tag. ``box`` is on the screen; ``in_png`` is the same box inside the saved picture."""
    ox, oy = origin
    out = []
    for m in numbered:
        b = m.target.box
        out.append({"number": m.number, "name": m.target.name, "role": m.target.role, "source": m.target.source,
                    "box": [b.left, b.top, b.right, b.bottom],
                    "in_png": [b.left - ox, b.top - oy, b.right - ox, b.bottom - oy]})
    return out


def _window(title: str | None) -> native.Win | None:
    if title:
        wanted = title.casefold()
        return next((w for w in native.app_windows() if wanted in w.title.casefold()), None)
    return native.describe(native.foreground())


def main(*, scope: str = "here", delay: float = 3.0, title: str | None = None, out: Path | None = None) -> int:
    import mss.tools  # noqa: PLC0415 - mss is loaded lazily (screen.py)

    from .pointing import Pointer  # noqa: PLC0415 - pulls in OCR and UIA
    from .uia import FocusReader  # noqa: PLC0415

    if not title and delay > 0:
        print(f"Switch to the window to number; dumping it in {delay:g} s ...")
        time.sleep(delay)
    win = _window(title)
    if win is None:
        print("No window found" + (f" with {title!r} in its title." if title else " in front."))
        return 1
    started = time.perf_counter()
    targets = Pointer(FocusReader()).gather(win, scope)
    gathered_ms = (time.perf_counter() - started) * 1000
    ordered = marks.reading_order(targets)
    pages = marks.pages(len(ordered))
    rect = native.frame_rect(win.hwnd)
    shot = screen.grab(rect)
    # page p's tags are numbered 1..99 again: (page, number) is the key
    tags = [dict(r, page=page) for page in range(pages)
            for r in records(marks.number(ordered, page=page), (shot.left, shot.top))]
    folder = out or paths.DATA
    folder.mkdir(parents=True, exist_ok=True)
    stem = folder / f"numbers-{datetime.now():%Y%m%d-%H%M%S}"
    png = mss.tools.to_png(to_rgb(shot.bgra, shot.width, shot.height), (shot.width, shot.height), level=1)
    stem.with_suffix(".png").write_bytes(png)
    by_source: dict[str, int] = {}
    for t in ordered:
        by_source[t.source] = by_source.get(t.source, 0) + 1
    doc = {"window": {"title": win.title, "exe": win.exe_name, "class": win.cls, "rect": list(rect)},
           "scope": scope, "front": native.foreground() == win.hwnd, "gather_ms": round(gathered_ms),
           "pages": pages, "by_source": by_source, "tags": tags}
    stem.with_suffix(".json").write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
    sources = ", ".join(f"{n} {s}" for s, n in sorted(by_source.items()))
    print(f"{win.exe_name} '{win.title[:60]}': {len(ordered)} tags ({sources or 'none'}) on {pages} page(s), "
          f"gathered in {gathered_ms:.0f} ms")
    if not doc["front"]:
        print("  (not the front window: the picture shows whatever covers it)")
    print(f"  {stem.with_suffix('.json')}\n  {stem.with_suffix('.png')}")
    return 0
