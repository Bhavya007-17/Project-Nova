"""`nova say "<text>"`: run the whole brain on typed text — no mic, no island.

Router (Tier 0 -> Tier 1 -> checked plan) -> questions -> risk check -> the real Windows
actions, with confirm / clarify asked here in the console (non-interactive: treated as No).
`--dry` stops after the plan. Writes the same trace line as a spoken command (via = "say"),
so `nova latency` counts it. Dictation steps type the words given once ("type this: …", "open a Google Doc and
type this: …", "paste my last dictation"); live dictation needs the mic (the tray app).
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable, Sequence

from . import paths
from .actions import native
from .actions.catalog import CatalogStore
from .actions.context import snapshot
from .actions.hands import NovaControls, WindowsHands
from .brain.brain import Command, handle
from .brain.executor import Executor
from .brain.llama_server import connect
from .brain.router import Router, describe_handoff, running_apps
from .actions.chrome import Chrome, ChromeHands
from .brain.tier1 import Tier1
from .chrome.bridge import Bridge, BridgeInfo
from .config import ConfigStore
from .dictation.cleanup import Cleaner
from .dictation.history import History
from .dictation.session import Dictation
from .dictation.target import Target
from .events import WARMING, BrainProgress, CommandFinished
from .governor import Governor
from .log import TraceLog
from .vocab import VocabFile


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


def one_shot_runtime(runtime: str) -> str:
    """`nova say` is one command per process, and a llama-server child ends with its process: every run would load
    the model again (7 s). Ollama keeps it loaded between runs, so ``auto`` means Ollama here."""
    return "ollama" if runtime == "auto" else runtime


def brain_parts(catalogs: CatalogStore, *, warming: Callable[[], None] = lambda: None,
                one_shot: bool = False) -> tuple[Router, Tier1]:
    """The router with Tier 1 behind it, from settings.yaml (shared with `nova eval`). ``warming``: a request found
    the model cold and waits for it to load. ``one_shot``: see ``one_shot_runtime``."""
    config = ConfigStore(paths.SETTINGS)
    config.load()
    runtime = (lambda: one_shot_runtime(config.settings.llm.runtime)) if one_shot else         (lambda: config.settings.llm.runtime)
    client = connect(config.settings.ollama.url, runtime)
    governor = Governor(client, lambda: config.settings.llm)
    tier1 = Tier1(client, governor, lambda: config.settings.llm, catalogs.current, warming=warming)
    return Router(tier1), tier1


def _warming() -> None:
    print(f"  ({WARMING} the local model is loading)", flush=True)


def _bridge(wait: float) -> Bridge | None:
    """The Chrome bridge for this one command (the tray app holds it while it runs). Waits for the extension."""
    if wait <= 0:
        return None
    bridge = Bridge(BridgeInfo.ensure(paths.CHROME_BRIDGE))
    if not bridge.start():
        print(f"  (Chrome: {bridge.error} — page steps run in the tray app)")
        return None
    print(f"  (waiting up to {wait:.0f} s for Chrome's extension — click into Chrome to hurry it)")
    if not bridge.wait_connected(wait):
        print("  (Chrome's extension didn't connect)")
    return bridge


def main(text: str, *, dry: bool = False, chrome_wait: float = 0.0) -> int:
    started = time.perf_counter()
    store = _catalog_store()
    catalog = store.current()
    router, _tier1 = brain_parts(store, warming=_warming, one_shot=True)
    _tier1.preload("say")  # a cold model starts loading now, while the screen is read (as the tray app's key-down)
    native.com_init()
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
    bridge = _bridge(chrome_wait)
    chrome = ChromeHands(Chrome(bridge, store.current)) if bridge is not None else None
    if chrome is not None:
        router = Router(_tier1, page=chrome.page_rows)
    trace = TraceLog(paths.LOGS)
    config = ConfigStore(paths.SETTINGS)
    config.load()
    vocab = VocabFile(paths.VOCAB)
    cleaner = Cleaner(_tier1.governor, lambda: config.settings.llm, lambda: vocab.current().terms,
                      client=lambda: connect(config.settings.ollama.url,
                                             lambda: one_shot_runtime(config.settings.llm.runtime)),
                      enabled=lambda: config.settings.dictation.cleanup, loader=_tier1.loader)
    history = History(paths.DICTATION_HISTORY)
    dictation = Dictation(None, cleaner, Target(store.current, chrome.chrome if chrome is not None else None),
                          history, trace=trace)
    planned = {"new_doc": False}

    def dictate(op: str, said: str | None = None) -> str:
        if op == "paste":
            return dictation.paste_last()
        if op != "start":
            return "Dictation isn't on"
        if not said:
            return "Live dictation needs the mic: double-tap Copilot with the tray app running"
        if not planned.get("started"):
            cleaner.warm(block=True)
            dictation.start()
            planned["started"] = True
        before = history.last()
        dictation.begin("say", said, new_doc=planned["new_doc"])
        dictation.release()
        dictation.drain(15.0, fixes=True)
        last = history.last()
        if last is None or last == before:
            return "Nothing typed"
        return ("Typed" if last.landed else "Saved (couldn't type it)") + f" “{last.text}”"

    hands = WindowsHands(store.current, nova=NovaControls(
        dictation=dictate, listening=lambda op: "Sleep runs in the tray app"), chrome=chrome)
    executor = Executor(hands, asker, progress=lambda labels, i: print(f"  … {' → '.join(labels)}  [{labels[i]}]"))
    shown: list[str] = []

    def publish(event: object) -> None:
        if isinstance(event, CommandFinished):
            shown.append(("✓ " if event.ok else "✗ ") + event.detail)
        elif isinstance(event, BrainProgress) and event.state not in ("acting",):
            shown.append(f"({event.state})")

    try:
        _, record = handle(Command(text, via="say", at=started), catalog, executor, router=router, asker=asker,
                           publish=publish, trace=trace, snap=lambda c: snapshot(c, bridge),
                           prepare=lambda plan: planned.update(new_doc=any(s.action == "docs_new" for s in plan.steps)))
    finally:
        if planned.get("started"):
            dictation.close()
        trace.close()
        if bridge is not None:
            bridge.close()
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
