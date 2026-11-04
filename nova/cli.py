"""`nova` command line. No subcommand = the tray app."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

# Subcommands that later stages fill in.
_COMING = {"eval": "05"}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nova", description="Local voice layer for Windows.")
    sub = parser.add_subparsers(dest="command", metavar="command")
    sub.add_parser("run", help="start the tray app (default)")
    sub.add_parser("doctor", help="check this laptop has what Nova needs")
    listen = sub.add_parser("listen", help="print what the mic hears (live words, finals with timings)")
    listen.add_argument("--device", help='mic to use (part of its name); default: audio.device in settings.yaml')
    listen.add_argument("--wav", nargs="+", type=Path, metavar="FILE", help="play 16 kHz WAV files through the live path instead")
    listen.add_argument("--seconds", type=float, metavar="N", help="stop after N seconds (then print the summary)")
    listen.add_argument("--gate", action="store_true", help="also run the triggers: ACCEPTED / IGNORED per final")
    keys = sub.add_parser("keys", help="keyboard checks: --probe logs what the Copilot key sends")
    keys.add_argument("--probe", action="store_true", required=True, help="log raw key events (character keys hidden)")
    keys.add_argument("--seconds", type=float, default=20.0, metavar="N", help="stop after N seconds (default 20; Esc stops early)")
    keys.add_argument("--raw", action="store_true", help="swallow nothing (Copilot will open)")
    island = sub.add_parser("island", help="the Dynamic-Island bar: --demo cycles every state with fake data")
    island.add_argument("--demo", action="store_true", required=True, help="play fake events (no mic, no keys)")
    from .island.demo import NAMES  # no Qt at import

    island.add_argument("scene", nargs="?", choices=NAMES, metavar="SCENE", help="loop one scene: " + ", ".join(NAMES))
    bench = sub.add_parser("bench-stt", help="speech-to-text accuracy (WER) and speed on the recordings")
    bench.add_argument("--live", type=int, default=0, metavar="N", help="also replay N recordings in real time through the live worker")
    say = sub.add_parser("say", help="run a command from text, no mic (the whole brain + real actions)")
    say.add_argument("text")
    say.add_argument("--dry", action="store_true", help="only show the plan; do nothing")
    sub.add_parser("eval", help="score the intent eval")
    sub.add_parser("latency", help="p50/p95 per command type from the logs")
    return parser


def _safe_console() -> None:
    # Device and app names can hold characters the console code page lacks (e.g. "Intel®").
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")


def main(argv: Sequence[str] | None = None) -> int:
    _safe_console()
    args = _parser().parse_args(argv)
    command = args.command or "run"
    if command == "run":
        from . import app

        return app.run()
    if command == "doctor":
        from . import doctor

        return doctor.main()
    if command == "listen":
        from . import listen

        return listen.main(device=args.device, wavs=args.wav or (), seconds=args.seconds, gate=args.gate)
    if command == "keys":
        from . import keyprobe

        return keyprobe.main(seconds=args.seconds, raw=args.raw)
    if command == "island":
        from .island import demo

        return demo.main(args.scene)
    if command == "bench-stt":
        from .stt import bench

        return bench.run(live=args.live)
    if command == "say":
        from . import say

        return say.main(args.text, dry=args.dry)
    if command == "latency":
        from . import latency

        return latency.main()
    print(f"`nova {command}` arrives in Stage {_COMING[command]}.")
    return 1
