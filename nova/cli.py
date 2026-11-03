"""`nova` command line. No subcommand = the tray app."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

# Subcommands that later stages fill in.
_COMING = {"say": "04", "eval": "05", "latency": "11"}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nova", description="Local voice layer for Windows.")
    sub = parser.add_subparsers(dest="command", metavar="command")
    sub.add_parser("run", help="start the tray app (default)")
    sub.add_parser("doctor", help="check this laptop has what Nova needs")
    listen = sub.add_parser("listen", help="print what the mic hears (live words, finals with timings)")
    listen.add_argument("--device", help='mic to use (part of its name); default: audio.device in settings.yaml')
    listen.add_argument("--wav", nargs="+", type=Path, metavar="FILE", help="play 16 kHz WAV files through the live path instead")
    listen.add_argument("--seconds", type=float, metavar="N", help="stop after N seconds (then print the summary)")
    bench = sub.add_parser("bench-stt", help="speech-to-text accuracy (WER) and speed on the recordings")
    bench.add_argument("--live", type=int, default=0, metavar="N", help="also replay N recordings in real time through the live worker")
    sub.add_parser("say", help="run a command from text, no mic").add_argument("text")
    sub.add_parser("eval", help="score the intent eval")
    sub.add_parser("latency", help="p50/p95 per path from the logs")
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

        return listen.main(device=args.device, wavs=args.wav or (), seconds=args.seconds)
    if command == "bench-stt":
        from .stt import bench

        return bench.run(live=args.live)
    print(f"`nova {command}` arrives in Stage {_COMING[command]}.")
    return 1
