"""`nova say "<text>"`: run the whole brain on typed text — no mic, no island.

Tier 0 -> plan -> risk check -> the real Windows actions, with confirm / clarify asked
here in the console (non-interactive: treated as No). `--dry` stops after the plan.
Writes the same trace line as a spoken command (via = "say"), so `nova latency` counts it.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Sequence

from . import paths
from .actions import native
from .actions.catalog import CatalogStore
from .actions.context import snapshot
from .actions.hands import NovaControls, WindowsHands
from .brain import tier0
from .brain.brain import Command, handle
from .brain.executor import Executor
from .events import BrainProgress, CommandFinished
from .log import TraceLog


class ConsoleAsker:
    def __init__(self, interactive: bool) -> None:
        self._interactive = interactive

    def _ask(self, prompt: str) -> str | None:
        if not self._interactive:
            print(f"  ? {prompt} -> no answer (not a console)")
            return None
        try:
            return input(f"  ? {prompt} ").strip()
        except EOFError:
            return None

    def confirm(self, question: str) -> bool | None:
        from .brain.answers import yes_no

        answer = self._ask(f"{question} [y/N]")
        if not answer:
            return None
        return answer.lower() in ("y", "yes") or yes_no(answer) is True

    def choose(self, question: str, choices: Sequence[str]) -> int | None:
        from .brain.answers import pick

        listed = "  ".join(f"{i + 1}) {c}" for i, c in enumerate(choices))
        answer = self._ask(f"{question}  {listed}  [number]")
        return pick(answer, choices) if answer else None


def _catalog_store() -> CatalogStore:
    store = CatalogStore(paths.CATALOG_CACHE, paths.ALIASES)
    store.load_cached()
    if not store.loaded.is_set():
        print("Reading the Start menu (first run, ~1 s)…")
        store.refresh()
    return store


def main(text: str, *, dry: bool = False) -> int:
    started = time.perf_counter()
    store = _catalog_store()
    catalog = store.current()
    native.com_init()
    running = frozenset(w.app for w in snapshot(catalog).windows if w.app)
    plan = tier0.parse(text, catalog, running=running)
    if plan is None:
        print(f"Tier 0 didn't understand {text!r} (Stage 05's local LLM will take these).")
        return 1
    if plan.kind == "clarify":
        print(f"plan: clarify - {plan.question} {list(plan.choices)}")
    else:
        print("plan: " + " → ".join(repr(s) for s in plan.steps))
    if dry:
        return 0
    hands = WindowsHands(store.current, nova=NovaControls(
        dictation=lambda op: "Dictation runs in the tray app", listening=lambda op: "Sleep runs in the tray app"))
    executor = Executor(hands, ConsoleAsker(sys.stdin is not None and sys.stdin.isatty()),
                        progress=lambda labels, i: print(f"  … {' → '.join(labels)}  [{labels[i]}]"))
    trace = TraceLog(paths.LOGS)
    shown: list[str] = []

    def publish(event: object) -> None:
        if isinstance(event, CommandFinished):
            shown.append(("✓ " if event.ok else "✗ ") + event.detail)
        elif isinstance(event, BrainProgress) and event.state not in ("acting",):
            shown.append(f"({event.state})")

    try:
        result, record = handle(Command(text, via="say", at=started), catalog, executor, publish=publish, trace=trace)
    finally:
        trace.close()
    for line in shown:
        print(line)
    if result is not None and result.cancelled:
        print("cancelled")
    timings = [f"parse {record['parse_ms']} ms", f"context {record['ctx_ms']} ms"]
    if record["start_ms"] is not None:
        timings.append(f"action started {record['start_ms']:.0f} ms")
    timings.append(f"total {record['total_ms']:.0f} ms")
    methods = [s["method"] for s in record["steps"] if s["method"]]
    if methods:
        timings.append("foreground: " + ", ".join(methods))
    print("  " + " · ".join(timings))
    return 0 if result is not None and result.ok else 1
