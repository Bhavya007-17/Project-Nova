"""`nova say "<text>"`: run the whole brain on typed text — no mic, no island.

Router (Tier 0 -> Tier 1 -> checked plan) -> questions -> risk check -> the real Windows
actions, with confirm / clarify asked here in the console (non-interactive: treated as No).
`--dry` stops after the plan. Writes the same trace line as a spoken command (via = "say"),
so `nova latency` counts it.
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
from .brain.brain import Command, handle
from .brain.executor import Executor
from .brain.llm import Ollama
from .brain.router import Router, describe_handoff, running_apps
from .brain.tier1 import Tier1
from .config import ConfigStore
from .events import BrainProgress, CommandFinished
from .governor import Governor
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

    def answer(self, question: str) -> str | None:
        return self._ask(question) or None


def _catalog_store() -> CatalogStore:
    store = CatalogStore(paths.CATALOG_CACHE, paths.ALIASES)
    store.load_cached()
    if not store.loaded.is_set():
        print("Reading the Start menu (first run, ~1 s)…")
        store.refresh()
    return store


def brain_parts(catalogs: CatalogStore) -> tuple[Router, Tier1]:
    """The router with Tier 1 behind it, from settings.yaml (shared with `nova eval`)."""
    config = ConfigStore(paths.SETTINGS)
    config.load()
    client = Ollama(config.settings.ollama.url)
    governor = Governor(client, lambda: config.settings.llm)
    tier1 = Tier1(client, governor, lambda: config.settings.llm, catalogs.current)
    return Router(tier1), tier1


def main(text: str, *, dry: bool = False) -> int:
    started = time.perf_counter()
    store = _catalog_store()
    catalog = store.current()
    native.com_init()
    router, _tier1 = brain_parts(store)
    interactive = sys.stdin is not None and sys.stdin.isatty()
    asker = ConsoleAsker(interactive)
    if dry:
        ctx = snapshot(catalog)
        routed = router.route(text, ctx, catalog, running=running_apps(ctx))
        thought = routed.thought
        llm = f"  (tier {routed.tier}" + (f", {thought.choice.model} on {thought.choice.device}, {thought.ms:.0f} ms"
                                          if thought is not None and thought.choice is not None else "") + ")"
        plan = routed.plan
        if plan is None:
            print(f"no plan: {routed.note}{llm}")
            return 1
        if plan.kind == "clarify":
            print(f"plan: clarify - {plan.question} {list(plan.choices)}{llm}")
        elif plan.kind == "handoff":
            print(f"plan: {describe_handoff(plan)}{llm}")
        else:
            print("plan: " + " → ".join(repr(s) for s in plan.steps) + f" (confidence {plan.confidence:.2f}){llm}")
        return 0
    hands = WindowsHands(store.current, nova=NovaControls(
        dictation=lambda op: "Dictation runs in the tray app", listening=lambda op: "Sleep runs in the tray app"))
    executor = Executor(hands, asker, progress=lambda labels, i: print(f"  … {' → '.join(labels)}  [{labels[i]}]"))
    trace = TraceLog(paths.LOGS)
    shown: list[str] = []

    def publish(event: object) -> None:
        if isinstance(event, CommandFinished):
            shown.append(("✓ " if event.ok else "✗ ") + event.detail)
        elif isinstance(event, BrainProgress) and event.state not in ("acting",):
            shown.append(f"({event.state})")

    try:
        _, record = handle(Command(text, via="say", at=started), catalog, executor, router=router, asker=asker,
                                publish=publish, trace=trace)
    finally:
        trace.close()
    for line in shown:
        print(line)
    if record["cancelled"]:
        print("cancelled")
    timings = [f"tier {record['tier']}", f"parse {record['parse_ms']} ms", f"context {record['ctx_ms']} ms"]
    if record.get("llm_ms") is not None:
        timings.append(f"LLM {record['llm_ms']:.0f} ms ({record.get('model')} on {record.get('device')})")
    if record["start_ms"] is not None:
        timings.append(f"action started {record['start_ms']:.0f} ms")
    timings.append(f"total {record['total_ms']:.0f} ms")
    methods = [s["method"] for s in record["steps"] if s["method"]]
    if methods:
        timings.append("foreground: " + ", ".join(methods))
    print("  " + " · ".join(timings))
    return 0 if record["ok"] else 1
