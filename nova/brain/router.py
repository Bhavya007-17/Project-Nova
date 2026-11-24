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
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from ..actions.catalog import Catalog
from ..actions.marks import parse_number
from ..gate import WakeName, is_stop
from . import tier0
from .plan import Plan, Step
from .tier1 import Seen, Thought, Tier1
from .validate import check_llm

MAX_REPLANS = 2
# Not addressed by name or key: "not a command" stays silent. "talk": a talk-session clause (SPEC §5) — he talks
# freely between commands, so whatever the model calls chat is dropped without a word in the island.
_QUIET_VIA = frozenset({"follow-up", "always", "talk"})
MAX_BEFORE = 5  # a talk session's last executed steps Tier 1 sees, so "there", "it", "this new note" resolve
_NO_WAKE = WakeName.of("", enabled=False)  # the gate already took the name off
_BEFORE_CHARS = 60  # each one: short enough that five cost ~75 tokens


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


class PageSource(Protocol):
    """The page in front, ranked for what he said (``nova.actions.chrome.ChromeHands.page_rows``); None when he
    doesn't point at the page or there's no page to see."""

    def __call__(self, text: str, ctx: Any) -> Any: ...  # PageRows: .scored (numbered from 1), .rows(), .ms


@dataclass(frozen=True)
class Routed:
    plan: Plan | None
    tier: str  # "0" | "skill" | "page" | "screen" | "1" | "none"
    note: str = ""  # island text when there's no plan
    kind: str = ""  # Tier 1: what the model said (act / clarify / handoff / dictate / teach / ignore)
    thought: Thought | None = None
    parse_ms: float = 0.0  # Tier 0
    quiet: bool = False  # not a command and he didn't address Nova: no island message
    cancelled: bool = False
    page_ms: float | None = None  # the page map Tier 1 was shown (None = none fetched)


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
    def __init__(self, tier1: Tier1 | None = None, skills: Skills | None = None, page: PageSource | None = None,
                 numbers: Callable[[], bool] | None = None, scrolling: Callable[[], bool] | None = None,
                 buttons: Callable[[str, Any], str | None] | None = None) -> None:
        self._tier1 = tier1
        self._skills = skills or NoSkills()
        self._page = page
        self._numbers = numbers or (lambda: False)  # numbers / a grid on screen: a bare number picks one
        self._scrolling = scrolling or (lambda: False)  # steady scrolling: a bare "stop" stops it
        self._buttons = buttons  # a button of the window in front named exactly what he said ("don't save")
        self._shown: Any = None  # the page rows the model saw last (its answer's element numbers point into them)
        # The talk session's context (07b): "<what he said>: <Nova's result>", oldest first. Only his own words and
        # Nova's own short result, never page text (SPEC §14). Read and changed on the brain thread only.
        self._before: tuple[str, ...] = ()
        self._context: tuple[str, ...] = ()  # what the model was shown for this command (a re-plan keeps it)

    def _steer(self, text: str) -> Step | None:
        """ "seven" while numbers show, "stop" while steady scrolling runs (prompt 07c §2, §4)."""
        if self._numbers():
            n = parse_number(text)
            if n is not None:
                return Step("pick", {"n": n})
        if self._scrolling() and is_stop(text, _NO_WAKE):
            return Step("autoscroll", {"op": "stop"})
        return None

    def abort(self) -> None:
        if self._tier1 is not None:
            self._tier1.abort()

    @property
    def before(self) -> tuple[str, ...]:
        return self._before

    def remember(self, said: str, ok: bool, detail: str) -> None:
        """A talk-session step ran: keep it for the next clauses ("there", "it" point at it)."""
        entry = " ".join(f"{said.strip()}: {detail.strip() if ok else f'failed, {detail.strip()}'}".split())
        if len(entry) > _BEFORE_CHARS:
            entry = entry[: _BEFORE_CHARS - 1].rstrip() + "…"
        self._before = (*self._before, entry)[-MAX_BEFORE:]

    def forget_talk(self) -> None:
        """A talk session opened: it starts with nothing to point at (clauses of the last one have all run)."""
        self._before = ()
        self._context = ()

    def route(self, text: str, ctx: Any, catalog: Catalog, *, strict: bool = False, via: str = "key",
              running: frozenset[str] = frozenset(), tier1_only: bool = False, talk: bool = False,
              request: bool = True) -> Routed:
        """``tier1_only``: skip Tier 0 and skills (the bench compares models on every phrasing). ``talk``: a command
        of a talk session (Tier 1 sees the session's last steps); ``request``: worded as one (verb first or a polite
        frame) — a session clause that isn't one and that Nova isn't sure about is dropped quietly."""
        self._context = self._before if talk else ()
        routed = self._route(text, ctx, catalog, strict=strict, via=via, running=running, tier1_only=tier1_only)
        if routed.cancelled or routed.quiet or via != "talk" or request:
            return routed
        if routed.plan is None or routed.plan.kind == "clarify":
            # Said in passing, not asked ("notepad would be handy"): no question, nothing in the island (07b).
            return replace(routed, plan=None, quiet=True)
        return routed

    def _route(self, text: str, ctx: Any, catalog: Catalog, *, strict: bool, via: str, running: frozenset[str],
               tier1_only: bool) -> Routed:
        began = time.perf_counter()
        steer = None if tier1_only else self._steer(text)
        if steer is not None:
            return Routed(Plan("act", (steer,), confidence=1.0), "0", parse_ms=round((time.perf_counter() - began) * 1000, 2))
        plan = None if tier1_only else tier0.parse(text, catalog, strict=strict, running=running)
        parse_ms = round((time.perf_counter() - began) * 1000, 2)
        if plan is not None and strict and not tier0.safe_plan(plan, text):
            plan = None
        if plan is not None and plan.guess and not strict and self._tier1 is not None:
            # Tier 0 only has a sound-alike guess to ask about: let the model try; ask Tier 0's question if it
            # doesn't act ("I need a new dab" is a new tab, not YouTube; "open foam" still asks "Open Chrome?")
            routed = self._ask_model(text, ctx, catalog, running, via)
            if routed.plan is not None and routed.plan.kind in ("act", "handoff"):
                return Routed(routed.plan, routed.tier, routed.note, routed.kind, routed.thought, parse_ms,
                              page_ms=routed.page_ms)
            if routed.cancelled:
                return Routed(None, "1", routed.note, thought=routed.thought, parse_ms=parse_ms, cancelled=True,
                              page_ms=routed.page_ms)
            return Routed(plan, "0", thought=routed.thought, parse_ms=parse_ms, page_ms=routed.page_ms)
        if plan is not None:
            return Routed(plan, "0", parse_ms=parse_ms)
        skill = None if tier1_only else self._skills.match(text, ctx)
        if skill is not None:
            return Routed(skill, "skill", parse_ms=parse_ms)
        if strict or not text.strip():
            return Routed(None, "none", didnt_get(text), parse_ms=parse_ms)
        self._shown = self._page(text, ctx) if self._page is not None and not tier1_only else None
        page_ms = round(self._shown.ms, 1) if self._shown is not None else None
        direct = getattr(self._shown, "direct", None)
        if direct is not None:  # his words name one element on the page plainly: no LLM needed
            return Routed(Plan("act", (direct,), confidence=0.9), "page", parse_ms=parse_ms, page_ms=page_ms)
        named = self._buttons(text, ctx) if self._buttons is not None and not tier1_only else None
        if named is not None:  # a bare dialog answer ("don't save"): the button by that name, no LLM needed
            return Routed(Plan("act", (Step("ui_click", {"name": named}),), confidence=0.9), "screen",
                          parse_ms=parse_ms, page_ms=page_ms)
        if self._tier1 is None:
            return Routed(None, "none", didnt_get(text), parse_ms=parse_ms, page_ms=page_ms)
        routed = self._ask_model(text, ctx, catalog, running, via, page_ready=True)
        return Routed(routed.plan, routed.tier, routed.note, routed.kind, routed.thought, parse_ms, routed.quiet,
                      routed.cancelled, page_ms)

    def _ask_model(self, text: str, ctx: Any, catalog: Catalog, running: frozenset[str], via: str,
                   asked: str | None = None, answer: str | None = None, page_ready: bool = False) -> Routed:
        assert self._tier1 is not None
        page_ms = None
        if asked is None and not page_ready:  # a re-plan with his answer keeps the page it was shown
            self._shown = self._page(text, ctx) if self._page is not None else None
            page_ms = round(self._shown.ms, 1) if self._shown is not None else None
        shown = self._shown
        extra: dict[str, Any] = {"page": shown.rows()} if shown is not None else {}
        if self._context:
            extra["before"] = self._context
        thought = self._tier1.think(text, Seen.of(ctx), catalog, asked=asked, answer=answer, **extra)
        if thought.cancelled:
            return Routed(None, "1", "Cancelled", thought=thought, cancelled=True, page_ms=page_ms)
        if thought.data is None:
            return Routed(None, "1", thought.error or didnt_get(text), thought=thought, page_ms=page_ms)
        checked = check_llm(thought.data, text, catalog, running=running,
                            page=shown.scored if shown is not None else None)
        if checked.plan is not None:
            return Routed(checked.plan, "1", kind=checked.kind, thought=thought, page_ms=page_ms)
        note = checked.problem or _KIND_NOTES.get(checked.kind) or didnt_get(text)
        quiet = checked.kind == "ignore" and via in _QUIET_VIA
        return Routed(None, "1", note, checked.kind, thought, quiet=quiet, page_ms=page_ms)

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
