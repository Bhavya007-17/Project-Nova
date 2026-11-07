"""Render the README's island screenshots + GIF from the `nova island --demo` scenes (fake data, no mic, no models).

    uv run --with pillow python scripts/island_screenshots.py [--out DIR] [--scenes a,b] [--gif a,b]

The island shows at the top of the screen for a minute or two; it never takes focus. Frames come from Qt's own
renderer (QQuickWindow.grabWindow), so nothing behind the island is ever captured. Pillow is only needed here,
which is why it's pulled in with `--with` instead of being a project dependency.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw  # noqa: E402
from PySide6.QtCore import QEventLoop  # noqa: E402
from PySide6.QtGui import QGuiApplication, QImage  # noqa: E402

from nova.bus import Bus  # noqa: E402
from nova.events import Asking, CommandFinished, DictatedText, JobChip, PartialTranscript, PinToggled  # noqa: E402
from nova.events import StepsChanged  # noqa: E402
from nova.island.demo import SCENES, Go, Runner  # noqa: E402
from nova.island.window import Island  # noqa: E402

KEY_EVENTS = (Asking, CommandFinished, StepsChanged, DictatedText, JobChip, PinToggled)
KEY_STATES = {"teaching", "passthrough", "dictating", "error", "confirm", "clarify", "acting", "done"}
TOP, BOTTOM = (46, 64, 122), (139, 104, 168)  # backdrop gradient (a generic "wallpaper")


def shot_times(scene) -> list[float]:
    """Seconds after the scene starts worth a still: just after each key event, once it has settled."""
    times, t = [], 0.0
    for i, beat in enumerate(scene.beats):
        t += beat.wait
        nxt = scene.beats[i + 1] if i + 1 < len(scene.beats) else None
        wait = nxt.wait if nxt else 1.0
        partial = any(isinstance(e, PartialTranscript) for e in beat.events)
        last_partial = partial and not (nxt and any(isinstance(e, PartialTranscript) for e in nxt.events))
        key = any(isinstance(e, KEY_EVENTS) or (isinstance(e, Go) and e.state in KEY_STATES) for e in beat.events)
        if key or last_partial:
            times.append(t + min(0.7, wait * 0.8))
    return times


def grab(island: Island) -> Image.Image | None:
    if not island.window.isVisible():
        return None
    image = island.window.grabWindow().convertToFormat(QImage.Format.Format_RGBA8888)
    data = bytes(image.constBits())
    return Image.frombuffer("RGBA", (image.width(), image.height()), data, "raw", "RGBA", image.bytesPerLine(), 1).copy()


def backdrop(size: tuple[int, int]) -> Image.Image:
    width, height = size
    image = Image.new("RGBA", size)
    draw = ImageDraw.Draw(image)
    for y in range(height):
        f = y / max(1, height - 1)
        draw.line([(0, y), (width, y)], fill=tuple(int(a + (b - a) * f) for a, b in zip(TOP, BOTTOM)) + (255,))
    return image


def framed(shot: Image.Image, box: tuple[int, int, int, int], radius: int) -> Image.Image:
    crop = shot.crop(box)
    out = backdrop(crop.size)
    out.alpha_composite(crop)
    mask = Image.new("L", crop.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, crop.size[0] - 1, crop.size[1] - 1], radius, fill=255)
    out.putalpha(mask)
    return out


def still_box(shot: Image.Image, scale: float) -> tuple[int, int, int, int] | None:
    bbox = shot.getchannel("A").point(lambda a: 255 if a > 8 else 0).getbbox()
    if bbox is None:
        return None
    pad, min_w = int(28 * scale), int(620 * scale)
    left, top, right, bottom = bbox
    extra = max(0, min_w - (right - left)) // 2
    return (max(0, left - pad - extra), 0, min(shot.width, right + pad + extra), min(shot.height, bottom + pad))


def play(app: QGuiApplication, bus: Bus, island: Island, scene, *, every: float = 0.0, at=()) -> list:
    """Play one scene; grab at the given times and/or every `every` seconds. Returns (t, image | None) pairs."""
    runner = Runner(bus, (scene,), loop=False, say=lambda _text: None)
    pending, frames, next_tick = sorted(at), [], 0.0
    start = time.perf_counter()
    runner.start()
    while not runner.finished.is_set():
        app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 10)
        now = time.perf_counter() - start
        while pending and now >= pending[0]:
            frames.append((pending.pop(0), grab(island)))
        if every and now >= next_tick:
            frames.append((now, grab(island)))
            next_tick += every
        time.sleep(0.003)
    runner.stop()
    return frames


def main() -> int:
    names = [s.name for s in SCENES]
    parser = argparse.ArgumentParser(description="Island screenshots for the README.")
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "images")
    parser.add_argument("--scenes", default=",".join(names), help="scenes to shoot as stills")
    parser.add_argument("--gif", default="listening,confirm,dictating,chips", help="scenes for island.gif ('' = none)")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    app = QGuiApplication.instance() or QGuiApplication(["nova-screenshots"])
    bus = Bus()
    island = Island(bus)
    scale = island.window.devicePixelRatio()
    for _ in range(50):  # let the warm start finish
        app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 10)
        time.sleep(0.01)

    by_name = {s.name: s for s in SCENES}
    for name in filter(None, args.scenes.split(",")):
        frames = play(app, bus, island, by_name[name], at=shot_times(by_name[name]))
        for k, (_t, shot) in enumerate(f for f in frames if f[1] is not None):
            box = still_box(shot, scale)
            if box:
                framed(shot, box, int(16 * scale)).save(args.out / f"{name}-{k}.png", optimize=True)
        print(f"{name}: {len(frames)} stills")

    gif_frames: list[Image.Image] = []
    for name in filter(None, args.gif.split(",")):
        for _t, shot in play(app, bus, island, by_name[name], every=0.1):
            width, height = int(1040 * scale), int(200 * scale)
            if shot is None:
                frame = backdrop((width, height))
            else:
                left = (shot.width - width) // 2
                frame = framed(shot, (left, 0, left + width, height), 0)
            gif_frames.append(frame.convert("RGB").resize((800, int(800 * height / width)), Image.Resampling.LANCZOS))
    if gif_frames:
        palette = [f.quantize(colors=96, method=Image.Quantize.MEDIANCUT) for f in gif_frames]
        palette[0].save(args.out / "island.gif", save_all=True, append_images=palette[1:], duration=100, loop=0,
                        optimize=True, disposal=1)
        print(f"island.gif: {len(gif_frames)} frames")
    island.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
