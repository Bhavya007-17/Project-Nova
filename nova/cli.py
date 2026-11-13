"""`nova` command line. No subcommand = the tray app."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

# Subcommands that later stages fill in.
_COMING: dict[str, str] = {}


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
    say.add_argument("--chrome", type=float, nargs="?", const=30.0, default=0.0, metavar="S",
                     help="page / Canvas steps: host the Chrome bridge and wait up to S s (default 30) for the extension "
                          "(only when the tray app isn't running)")
    ev = sub.add_parser("eval", help="score the intent eval (docs/eval/intents.yaml) through the router")
    ev.add_argument("--model", help="an Ollama model to use instead of the one settings.yaml picks")
    ev.add_argument("--device", choices=("gpu", "cpu"), help="run the model on this device")
    ev.add_argument("--tier1", action="store_true", help="send every phrasing to Tier 1 (skip Tier 0)")
    ev.add_argument("--limit", type=int, default=0, metavar="N", help="only the first N positives (and N/2 negatives)")
    ev.add_argument("--quiet", action="store_true", help="don't list the misses")
    bench_llm = sub.add_parser("bench-llm", help="candidate local models x devices: accuracy, latency, load time, VRAM")
    bench_llm.add_argument("models", nargs="*", help="Ollama models to compare (default: the Stage 05 candidates)")
    bench_llm.add_argument("--devices", default="gpu,cpu", help="comma list of gpu, cpu, arc (default gpu,cpu)")
    bench_llm.add_argument("--cpu-cases", type=int, default=40, metavar="N", help="cases timed on CPU / Arc (default 40)")
    bench_llm.add_argument("--save", action="store_true", help="write the recommended models to settings.yaml")
    bench_llm.add_argument("--runtime", metavar="LIST",
                           help="compare runtimes on the GPU model instead, e.g. ollama,llama-server")
    bench_llm.add_argument("--cases", type=int, default=36, metavar="N", help="--runtime: warm cases (default 36)")
    bench_llm.add_argument("--rounds", type=int, default=2, metavar="N", help="--runtime: back-to-back rounds")
    sub.add_parser("latency", help="p50/p95 per command type from the logs")
    chrome = sub.add_parser("chrome", help="Chrome extension: --install registers its native host (then load it once)")
    what = chrome.add_mutually_exclusive_group(required=True)
    what.add_argument("--install", action="store_true", help="write the host manifest + registry key, print the manual step")
    what.add_argument("--uninstall", action="store_true", help="remove the registry key")
    what.add_argument("--status", action="store_true", help="is the host registered and the extension loaded?")
    chrome.add_argument("--browser", action="append", choices=("chrome", "testing", "chromium"),
                        help="which browser's registry (default chrome; testing = Chrome for Testing)")
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

        return say.main(args.text, dry=args.dry, chrome_wait=args.chrome)
    if command == "latency":
        from . import latency

        return latency.main()
    if command == "eval":
        from . import evals

        return evals.main(model=args.model, device=args.device, tier1_only=args.tier1, limit=args.limit,
                          misses=not args.quiet)
    if command == "bench-llm":
        from . import bench_llm

        if args.runtime:
            names = tuple(r.strip() for r in args.runtime.split(",") if r.strip())
            return bench_llm.runtimes(names, cases=args.cases, rounds=args.rounds)
        devices = tuple(d.strip() for d in args.devices.split(",") if d.strip())
        return bench_llm.main(tuple(args.models), devices=devices, cpu_cases=args.cpu_cases, save=args.save)
    if command == "chrome":
        from .chrome import install

        browsers = tuple(args.browser or ("chrome",))
        if args.status:
            state = install.status()
            print(("OK  " if state.ok else "--  ") + state.detail + (f"\n    {state.fix}" if state.fix else ""))
            return 0 if state.ok else 1
        for line in install.install(browsers) if args.install else install.uninstall(browsers):
            print(line)
        if args.install:
            print()
            print(install.MANUAL_STEP)
        return 0
    print(f"`nova {command}` arrives in Stage {_COMING[command]}.")
    return 1
