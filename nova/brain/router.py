"""The router (SPEC §7): Tier 0 -> learned skills (Stage 08: a stub) -> Tier 1 -> checked plan.

``route`` never asks anything: it returns a plan (act / clarify / handoff) or plain words for
the island. ``settle`` asks the questions a plan carries until there's something to run:
a question with options (Tier 0's "which Chrome?", Tier 1's unsure "Open Notepad?") runs
the option he picks; the model's own question goes back to the model with his answer. Always-
listening commands (``strict``) stay on Tier 0: the gate only lets through what Tier 0 resolves.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..actions.catalog import Catalog
from . import tier0
from .plan import Plan
from .tier1 import Seen, Thought, Tier1
from .validate import check_llm

MAX_REPLANS = 2
_QUIET_VIA = frozenset({"follow-up", "always"})  # not addressed by name or key: "not a command" stays silent


class Skills(Protocol):
    def match(self, text: str, ctx: Any) -> Plan | None: ...


class NoSkills:
    """Stage 08 learns skills; until then nothing matches."""

    def match(self, text: str, ctx: Any) -> Plan | None:
        return None


class Asker(Protocol):
    def confirm(self, question: str) -> bool | None: ...
    def choose(self, question: str, choices: Sequence[str]) -> int | None: ...
    def answer(self, question: str) -> str | None: ...


@dataclass(frozen=True)
class Routed:
    plan: Plan | None
    tier: str  # "0" | "skill" | "1" | "none"
    note: str = ""  # island text when there's no plan
    kind: str = ""  # Tier 1: what the model said (act / clarify / handoff / dictate / teach / ignore)
    thought: Thought | None = None
    parse_ms: float = 0.0  # Tier 0
    quiet: bool = False  # not a command and he didn't address Nova: no island message
    cancelled: bool = False


@dataclass(frozen=True)
class Settled:
    plan: Plan | None  # act / handoff, or None
    rounds: tuple[Routed, ...] = field(default_factory=tuple)
    asked: str = ""  # "clarify" if he answered a question on the way
    note: str = ""
    cancelled: bool = False
    quiet: bool = False


def didnt_get(text: str) -> str:
    text = text.strip()
    return f"Didn't get “{text}”" if text else "Didn't catch that"


_KIND_NOTES = {
    "teach": "I don't know how to do that yet — show me?",
    "ignore": "That didn't sound like a command for me.",
}


class Router:
    def __init__(self, tier1: Tier1 | None = None, skills: Skills | None = None) -> None:
        self._tier1 = tier1
        self._skills = skills or NoSkills()

    def abort(self) -> None:
        if self._tier1 is not None:
            self._tier1.abort()

    def route(self, text: str, ctx: Any, catalog: Catalog, *, strict: bool = False, via: str = "key",
              running: frozenset[str] = frozenset(), tier1_only: bool = False) -> Routed:
        """``tier1_only``: skip Tier 0 and skills (the bench compares models on every phrasing)."""
        began = time.perf_counter()
        plan = None if tier1_only else tier0.parse(text, catalog, strict=strict, running=running)
        parse_ms = round((time.perf_counter() - began) * 1000, 2)
        if plan is not None and strict and not tier0.safe_plan(plan, text):
            plan = None
        if plan is not None and plan.guess and not strict and self._tier1 is not None:
            # Tier 0 only has a sound-alike guess to ask about: let the model try; ask Tier 0's question if it
            # doesn't act ("I need a new dab" is a new tab, not YouTube; "open foam" still asks "Open Chrome?")
            routed = self._ask_model(text, ctx, catalog, running, via)
            if routed.plan is not None and routed.plan.kind in ("act", "handoff"):
                return Routed(routed.plan, routed.tier, routed.note, routed.kind, routed.thought, parse_ms)
            if routed.cancelled:
                return Routed(None, "1", routed.note, thought=routed.thought, parse_ms=parse_ms, cancelled=True)
            return Routed(plan, "0", thought=routed.thought, parse_ms=parse_ms)
        if plan is not None:
            return Routed(plan, "0", parse_ms=parse_ms)
        skill = None if tier1_only else self._skills.match(text, ctx)
        if skill is not None:
            return Routed(skill, "skill", parse_ms=parse_ms)
        if strict or self._tier1 is None or not text.strip():
            return Routed(None, "none", didnt_get(text), parse_ms=parse_ms)
        routed = self._ask_model(text, ctx, catalog, running, via)
        return Routed(routed.plan, routed.tier, routed.note, routed.kind, routed.thought, parse_ms, routed.quiet,
                      routed.cancelled)

    def _ask_model(self, text: str, ctx: Any, catalog: Catalog, running: frozenset[str], via: str,
                   asked: str | None = None, answer: str | None = None) -> Routed:
        assert self._tier1 is not None
        thought = self._tier1.think(text, Seen.of(ctx), catalog, asked=asked, answer=answer)
        if thought.cancelled:
            return Routed(None, "1", "Cancelled", thought=thought, cancelled=True)
        if thought.data is None:
            return Routed(None, "1", thought.error or didnt_get(text), thought=thought)
        checked = check_llm(thought.data, text, catalog, running=running)
        if checked.plan is not None:
            return Routed(checked.plan, "1", kind=checked.kind, thought=thought)
        note = checked.problem or _KIND_NOTES.get(checked.kind) or didnt_get(text)
        quiet = checked.kind == "ignore" and via in _QUIET_VIA
        return Routed(None, "1", note, checked.kind, thought, quiet=quiet)

    def settle(self, routed: Routed, text: str, ctx: Any, catalog: Catalog, asker: Asker, *, via: str = "key",
               running: frozenset[str] = frozenset(),
               progress: Callable[[str], None] = lambda state: None) -> Settled:
        """Ask what the plan needs answered; the plan to run (act / handoff), or why there's none.

        ``progress("thinking")`` while his answer goes back to the model."""
        rounds = [routed]
        plan = routed.plan
        asked = ""
        replans = 0
        while plan is not None and plan.kind == "clarify":
            asked = "clarify"
            if plan.options:
                if len(plan.choices) == 1:  # "Open Notepad?": yes / no
                    index = 0 if asker.confirm(plan.question) else None
                else:
                    index = asker.choose(plan.question, plan.choices)
                chosen = plan.option(index) if index is not None else None
                if chosen is None:
                    return Settled(None, tuple(rounds), asked, "Cancelled", cancelled=True)
                plan = chosen
                continue
            if replans >= MAX_REPLANS or self._tier1 is None:
                return Settled(None, tuple(rounds), asked, "I'm still not sure what you mean — try saying it another way.")
            if plan.choices:
                index = asker.choose(plan.question, plan.choices)
                reply = plan.choices[index] if index is not None and 0 <= index < len(plan.choices) else None
            else:
                reply = asker.answer(plan.question)
            if not reply:
                return Settled(None, tuple(rounds), asked, "Cancelled", cancelled=True)
            replans += 1
            progress("thinking")
            again = self._ask_model(text, ctx, catalog, running, via, asked=plan.question, answer=reply)
            rounds.append(again)
            if again.plan is None:
                return Settled(None, tuple(rounds), asked, again.note, again.cancelled, again.quiet)
            plan = again.plan
        last = rounds[-1]
        if plan is None:
            return Settled(None, tuple(rounds), asked, last.note, last.cancelled, last.quiet)
        return Settled(plan, tuple(rounds), asked)


def describe_handoff(plan: Plan) -> str:
    """Stage 09 hands off for real; until then the island says what it would do."""
    handoff = plan.handoff or {}
    who = "ChatGPT" if handoff.get("to") == "chatgpt" else "Claude"
    prompt = " ".join(str(handoff.get("prompt", "")).split())
    if len(prompt) > 60:
        prompt = prompt[:59] + "…"
    voice = " (voice)" if handoff.get("mode") == "voice" else ""
    return f"Would hand off to {who}{voice}: “{prompt}”"


def running_apps(ctx: Any) -> frozenset[str]:
    return frozenset(w.app for w in getattr(ctx, "windows", ()) if w.app)
