"""`nova eval` (SPEC §18): the intent eval in docs/eval/intents.yaml through the real router.

Positives are scored on the action and its key arguments (``want``, a small notation described
at the top of the file), not exact strings: app names resolve through the catalog, sites compare
by host, queries ignore case / punctuation / edge fillers, a one-window layout equals the snap.
Negatives (conversation) count a false action when the router would act or hand off; asking
is fine. Reports accuracy by action and by tier, the false-action rate and Tier 1 latency.
"""

from __future__ import annotations

import json
import math
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from . import paths
from .actions.catalog import Catalog, words_of
from .brain.plan import (
    AREAS,
    CANVAS_RANGES,
    ENGINES,
    MEDIA_OPS,
    NAV_OPS,
    SCROLL_AMOUNTS,
    SCROLL_DIRS,
    WINDOW_OPS,
    Plan,
    Step,
)

EVAL_FILE = paths.ROOT / "docs" / "eval" / "intents.yaml"
_SINGLE_AREA = {"left": "snap_left", "right": "snap_right", "top": "snap_top", "bottom": "snap_bottom",
                "full": "maximize", "center": "center"}
# Edge words a search engine doesn't care about ("videos about pid control" on YouTube = "pid control").
_EDGE_FILLERS = frozenset({"for", "some", "the", "a", "an", "about", "on", "please", "me", "of", "videos", "video",
                           "pictures", "picture", "images", "photos"})
_MODIFIERS = ("ctrl", "shift", "alt", "win")
_SIMPLE = {"tab": frozenset({"new", "close", "next", "prev", "reopen", "goto"}), "nav": NAV_OPS, "media": MEDIA_OPS,
           "dictation": frozenset({"start", "stop", "paste"}), "listening": frozenset({"sleep", "wake", "off"})}
_VOLUME = frozenset({"up", "down", "mute", "unmute", "set"})


# --- wants ------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Want:
    """One acceptable answer: a kind, and for act the steps (canonical patterns)."""

    kind: str  # act | clarify | teach | ignore | handoff
    steps: tuple[tuple, ...] = ()
    to: str = ""  # handoff target ("" = either)

    @property
    def label(self) -> str:
        if self.kind != "act":
            return self.kind
        return "chain" if len(self.steps) > 1 else self.steps[0][0]


def _host(url: str) -> str:
    if "//" not in url:
        url = "https://" + url
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def _name(text: str, catalog: Catalog) -> str:
    text = text.strip()
    if text == "active":
        return "active"
    if "." in text and " " not in text:
        return _host(text)
    resolution = catalog.resolve(text)
    best = resolution.best or (resolution.matches[0] if resolution.matches else None)
    if best is None:
        raise ValueError(f"no app or site called {text!r} here")
    target = best.target
    return _host(target.url) if target.kind == "site" else target.name


def _norm(text: str) -> str:
    return " ".join(words_of(text))


def _norm_query(text: str) -> str:
    words = list(words_of(text))
    while words and words[0] in _EDGE_FILLERS:
        words.pop(0)
    while words and words[-1] in _EDGE_FILLERS:
        words.pop()
    return " ".join(words)


def _norm_keys(keys: str) -> str:
    parts = [p.strip().lower() for p in keys.split("+") if p.strip()]
    mods = [m for m in _MODIFIERS if m in parts]
    rest = [p for p in parts if p not in _MODIFIERS]
    return "+".join([*mods, *rest])


def _ops(text: str, allowed: Iterable[str], what: str) -> frozenset[str]:
    ops = frozenset(text.split("|"))
    if not ops or not ops <= frozenset(allowed):
        raise ValueError(f"{what}: {text!r} isn't one of {sorted(allowed)}")
    return ops


def _single(slots: frozenset) -> tuple | None:
    if len(slots) == 1:
        app, area = next(iter(slots))
        if area in _SINGLE_AREA:
            return ("window", frozenset({_SINGLE_AREA[area]}), app)
    return None


def _want_step(text: str, catalog: Catalog) -> tuple:
    head, _, rest = text.strip().partition(" ")
    rest = rest.strip()
    if head == "open" and rest:
        return ("open", _name(rest, catalog))
    if head == "close" and rest:
        return ("close", _name(rest, catalog))
    if head == "window":
        ops, _, target = rest.partition(" ")
        if not target:
            raise ValueError(f"window needs an op and a target: {text!r}")
        return ("window", _ops(ops, WINDOW_OPS, "window"), _name(target, catalog))
    if head == "layout":
        slots = set()
        for part in rest.split(","):
            app, sep, area = part.strip().rpartition(":")
            if not sep or area not in AREAS:
                raise ValueError(f"layout slots are APP:AREA: {text!r}")
            slots.add((_name(app, catalog), area))
        frozen = frozenset(slots)
        return _single(frozen) or ("layout", frozen)
    if head == "search" or head.startswith("search@"):
        engine = head.partition("@")[2] or "google"
        if engine not in ENGINES or not rest:
            raise ValueError(f"search needs a known engine and a query: {text!r}")
        return ("search", engine, _norm_query(rest))
    if head in ("type", "type+enter") and rest:
        return ("type", _norm(rest), head == "type+enter")
    if head == "press" and rest:
        return ("press", _norm_keys(rest))
    if head in _SIMPLE and rest:
        return (head, _ops(rest, _SIMPLE[head], head))
    if head == "scroll" and rest:
        dirs, _, amount = rest.partition(" ")
        if amount and amount not in SCROLL_AMOUNTS:
            raise ValueError(f"scroll amount {amount!r}")
        return ("scroll", _ops(dirs, SCROLL_DIRS, "scroll"), amount or None)
    if head == "volume" and rest:
        ops, _, level = rest.partition(" ")
        return ("volume", _ops(ops, _VOLUME, "volume"), int(level) if level else None)
    if head == "click" and not rest:
        return ("click",)
    if head in ("scroll_to", "fill", "canvas_open", "canvas_text", "docs_new") and not rest:
        return (head,)
    if head == "canvas_due" and rest in CANVAS_RANGES:
        return ("canvas_due", rest)
    raise ValueError(f"can't read want step {text!r}")


def parse_want(text: str, catalog: Catalog) -> tuple[Want, ...]:
    wants = []
    for alt in text.split("||"):
        alt = alt.strip()
        head, _, rest = alt.partition(" ")
        if alt in ("clarify", "teach", "ignore"):
            wants.append(Want(alt))
        elif head == "handoff":
            if rest not in ("", "claude", "chatgpt"):
                raise ValueError(f"handoff to {rest!r}?")
            wants.append(Want("handoff", to=rest))
        else:
            wants.append(Want("act", tuple(_want_step(s, catalog) for s in alt.split(";"))))
    return tuple(wants)


# --- plans -> the same shape --------------------------------------------------------------------------------------


def _canon(s: Step) -> tuple:
    a = s.args
    match s.action:
        case "open_app" | "switch_app":
            return ("open", a.get("app"))
        case "open_url":
            return ("open", _host(str(a.get("url", ""))))
        case "close_app":
            return ("close", a.get("app"))
        case "window":
            return ("window", a.get("op"), a.get("target") or "active")
        case "layout":
            slots = frozenset((slot.get("app"), slot.get("area")) for slot in a.get("slots") or ())
            single = _single(slots)
            return ("window", next(iter(single[1])), single[2]) if single else ("layout", slots)
        case "web_search":
            return ("search", a.get("engine") or "google", _norm_query(str(a.get("query", ""))))
        case "type_text":
            return ("type", _norm(str(a.get("text", ""))), bool(a.get("submit")))
        case "press":
            return ("press", _norm_keys(str(a.get("keys", ""))))
        case "scroll":
            return ("scroll", a.get("dir"), a.get("amount"))
        case "volume":
            return ("volume", a.get("op"), a.get("level"))
        case "page_click" | "ui_click":
            return ("click",)
        case "scroll_to":
            return ("scroll_to",)
        case "page_fill":
            return ("fill",)
        case "canvas_due":
            return ("canvas_due", a.get("range"))
        case "canvas_open_item" | "canvas_open":
            return ("canvas_open",)
        case "canvas_item_text":
            return ("canvas_text",)
        case "docs_new":
            return ("docs_new",)
    return (s.action, a.get("op"))


def _matches(pattern: tuple, got: tuple) -> bool:
    kind = pattern[0]
    if kind != got[0]:
        return False
    if kind == "window":
        return got[1] in pattern[1] and got[2] == pattern[2]
    if kind in _SIMPLE:
        return got[1] in pattern[1]
    if kind == "scroll":
        return got[1] in pattern[1] and (pattern[2] is None or pattern[2] == got[2])
    if kind == "volume":
        return got[1] in pattern[1] and (pattern[2] is None or pattern[2] == got[2])
    return pattern == got


def _front(t: tuple, front: str | None) -> tuple:
    """"active" and the app in front are the same window."""
    if not front:
        return t
    if t[0] == "window" and t[2] == "active":
        return (t[0], t[1], front)
    if t[0] == "layout":
        return ("layout", frozenset((front if app == "active" else app, area) for app, area in t[1]))
    return t


def score(plan: Plan | None, wants: Sequence[Want], catalog: Catalog, *, kind: str = "",
          front: str | None = None) -> bool:
    """Does the router's answer match any acceptable one? ``kind``: Tier 1's kind when there's no plan;
    ``front``: the app in front ("active" means it)."""
    for want in wants:
        if want.kind in ("teach", "ignore"):
            if plan is None and kind == want.kind:
                return True
        elif plan is None:
            continue
        elif want.kind == "clarify":
            if plan.kind == "clarify":
                return True
        elif want.kind == "handoff":
            if plan.kind == "handoff" and (not want.to or (plan.handoff or {}).get("to") == want.to):
                return True
        elif plan.kind == "act" and len(plan.steps) == len(want.steps):
            now = front
            ok = True
            for p, s in zip(want.steps, plan.steps, strict=True):
                got = _canon(s)
                if not _matches(_front(p, now), _front(got, now)):
                    ok = False
                    break
                if got[0] == "open" and "." not in str(got[1]):
                    now = got[1]  # the app just opened is in front for the next step
            if ok:
                return True
    return False


def show(plan: Plan | None, kind: str = "", note: str = "") -> str:
    if plan is None:
        return f"({kind or 'none'}{': ' + note if note else ''})"
    if plan.kind == "clarify":
        return f"clarify “{plan.question}” {list(plan.choices)}"
    if plan.kind == "handoff":
        return f"handoff {dict(plan.handoff or {})}"
    return " ; ".join(repr(s) for s in plan.steps)


# --- results ----------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Item:
    text: str
    group: str  # positive | negative
    ok: bool  # positive: matched; negative: no false action
    tier: str
    kind: str  # the plan's kind, or Tier 1's kind with no plan ("ignore", "teach", "none")
    ms: float  # the whole route
    llm_ms: float | None
    action: str  # positives: what was wanted ("open", "window", "clarify", ...)
    got: str = ""
    want: str = ""
    load_ms: float | None = None
    heard: bool = True  # negatives: would the follow-up window's gate pass it to the router (starts like a command)?


def percentile(values: Sequence[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return math.nan
    rank = (len(ordered) - 1) * q / 100
    low = math.floor(rank)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


@dataclass(frozen=True)
class Summary:
    positives: int
    correct: int
    negatives: int
    false_actions: int
    asked_positive: int = 0
    asked_negative: int = 0
    false_heard: int = 0  # false actions the gate would have passed on (the real path for conversation)
    tier1: int = 0
    tier1_p50: float = math.nan
    tier1_p95: float = math.nan
    load_p50: float = math.nan
    by_action: dict[str, tuple[int, int]] = field(default_factory=dict)
    by_tier: dict[str, tuple[int, int]] = field(default_factory=dict)

    @property
    def accuracy(self) -> float:
        return self.correct / self.positives if self.positives else math.nan

    @property
    def false_rate(self) -> float:
        return self.false_actions / self.negatives if self.negatives else math.nan

    @property
    def heard_rate(self) -> float:
        """False actions on conversation the follow-up gate passes on (it only lets through what starts like a
        command), i.e. what happens in the room rather than what the router does when fed every sentence."""
        return self.false_heard / self.negatives if self.negatives else math.nan


def summarize(items: Sequence[Item]) -> Summary:
    positives = [i for i in items if i.group == "positive"]
    negatives = [i for i in items if i.group == "negative"]
    tier1 = [i for i in items if i.tier == "1"]
    by_action: dict[str, tuple[int, int]] = {}
    by_tier: dict[str, tuple[int, int]] = {}
    for item in positives:
        ok, total = by_action.get(item.action, (0, 0))
        by_action[item.action] = (ok + item.ok, total + 1)
        ok, total = by_tier.get(item.tier, (0, 0))
        by_tier[item.tier] = (ok + item.ok, total + 1)
    loads = [i.load_ms for i in tier1 if i.load_ms is not None]
    return Summary(
        positives=len(positives), correct=sum(i.ok for i in positives), negatives=len(negatives),
        false_actions=sum(not i.ok for i in negatives),
        false_heard=sum(not i.ok and i.heard for i in negatives),
        asked_positive=sum(i.kind == "clarify" and i.action != "clarify" for i in positives),
        asked_negative=sum(i.kind == "clarify" for i in negatives),
        tier1=len(tier1), tier1_p50=percentile([i.ms for i in tier1], 50), tier1_p95=percentile([i.ms for i in tier1], 95),
        load_p50=percentile(loads, 50), by_action=by_action, by_tier=by_tier,
    )


# --- running it -------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Case:
    text: str
    group: str
    wants: tuple[Want, ...] = ()
    want: str = ""
    front: str | None = None
    open_apps: tuple[str, ...] = ()
    tab: str | None = None


def load_cases(catalog: Catalog, path: Path = EVAL_FILE) -> list[Case]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    screen = data.get("screen") or {}
    front, open_apps, tab = screen.get("front"), tuple(screen.get("open") or ()), screen.get("tab")
    cases = []
    for row in data.get("positives") or []:
        try:
            wants = parse_want(row["want"], catalog)
        except ValueError as error:
            raise ValueError(f"{row.get('say')!r}: {error}") from error
        cases.append(Case(row["say"], "positive", wants, row["want"], row.get("front", front),
                          tuple(row.get("open", open_apps)), row.get("tab", tab)))
    for text in data.get("negatives") or []:
        cases.append(Case(str(text), "negative", front=front, open_apps=open_apps, tab=tab))
    return cases


def fake_context(case: Case) -> Any:
    """A Context with ``case``'s screen (no real windows)."""
    from .actions.context import AppWindow, Context, Tab
    from .actions.native import Win

    wins = tuple(AppWindow(Win(1000 + i, 0, 0, f"{name}", "Window", "", ()), name)
                 for i, name in enumerate(case.open_apps))
    fg = next((w for w in wins if w.app == case.front), wins[0] if wins else None)
    return Context(fg, wins, (), Tab(case.tab) if case.tab else None, False, 0.0)


def run_case(router: Any, case: Case, catalog: Catalog, *, tier1_only: bool = False) -> Item:
    ctx = fake_context(case)
    running = frozenset(case.open_apps)
    via = "key" if case.group == "positive" else "follow-up"
    began = time.perf_counter()
    routed = router.route(case.text, ctx, catalog, via=via, running=running, tier1_only=tier1_only)
    ms = (time.perf_counter() - began) * 1000
    plan = routed.plan
    kind = plan.kind if plan is not None else (routed.kind or "none")
    thought = routed.thought
    llm_ms = thought.ms if thought is not None else None
    load_ms = thought.reply.load_ms if thought is not None and thought.reply is not None else None
    heard = True
    if case.group == "positive":
        ok = score(plan, case.wants, catalog, kind=routed.kind, front=case.front)
        action = case.wants[0].label
    else:
        from .gate import command_words

        ok = plan is None or plan.kind not in ("act", "handoff")
        action = ""
        heard = command_words(case.text) is not None
    return Item(case.text, case.group, ok, routed.tier, kind, round(ms, 1), llm_ms, action,
                show(plan, routed.kind, routed.note), case.want, load_ms, heard)


def run(router: Any, catalog: Catalog, cases: Sequence[Case], *, tier1_only: bool = False,
        progress: Callable[[int, int, Item], None] = lambda i, n, item: None) -> list[Item]:
    items = []
    for index, case in enumerate(cases):
        item = run_case(router, case, catalog, tier1_only=tier1_only)
        items.append(item)
        progress(index + 1, len(cases), item)
    return items


def report(summary: Summary, items: Sequence[Item], *, title: str, misses: bool = True) -> str:
    s = summary
    lines = [title,
             f"plans correct: {s.correct}/{s.positives} = {s.accuracy:.1%}"
             f"   (asked instead of acting: {s.asked_positive})",
             "  by tier: " + ", ".join(f"tier {t}: {ok}/{n}" for t, (ok, n) in sorted(s.by_tier.items())),
             f"false actions on negatives: {s.false_actions}/{s.negatives} = {s.false_rate:.1%}"
             f"   (asked: {s.asked_negative}; past the follow-up gate: {s.false_heard})"]
    if s.tier1:
        lines.append(f"Tier 1: {s.tier1} calls · p50 {s.tier1_p50:.0f} ms · p95 {s.tier1_p95:.0f} ms"
                     + (f" · Ollama per-request overhead p50 {s.load_p50:.0f} ms" if not math.isnan(s.load_p50) else ""))
    lines.append("by action: " + "  ".join(f"{a} {ok}/{n}" for a, (ok, n) in sorted(s.by_action.items())))
    if misses:
        wrong = [i for i in items if not i.ok]
        if wrong:
            lines.append("misses:")
            for i in wrong:
                want = f"   want: {i.want}" if i.want else "   (conversation)"
                lines.append(f"  ✗ [{i.tier}] {i.text!r} -> {i.got}{want}")
    return "\n".join(lines)


def save(items: Sequence[Item], summary: Summary, path: Path, meta: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as out:
        out.write(json.dumps({"summary": {**meta, "accuracy": summary.accuracy, "false_rate": summary.false_rate,
                                          "tier1_p50": summary.tier1_p50, "tier1_p95": summary.tier1_p95,
                                          "load_p50": summary.load_p50}}, ensure_ascii=False) + "\n")
        for item in items:
            out.write(json.dumps(item.__dict__, ensure_ascii=False) + "\n")


def main(*, model: str | None = None, device: str | None = None, tier1_only: bool = False, limit: int = 0,
         misses: bool = True) -> int:
    from .governor import Choice
    from .say import _catalog_store, brain_parts

    store = _catalog_store()
    catalog = store.current()
    router, tier1 = brain_parts(store)
    cases = load_cases(catalog)
    if limit:
        positives = [c for c in cases if c.group == "positive"][:limit]
        negatives = [c for c in cases if c.group == "negative"][: max(1, limit // 2)]
        cases = positives + negatives
    choice = None
    if model or device:
        settings_llm = tier1.llm
        name = model or (settings_llm.cpu_model if device == "cpu" else settings_llm.gpu_model)
        choice = Choice(name, device or "gpu", "nova eval --model/--device")
        tier1.pin(choice)
    picked = choice or tier1.governor.choose(fresh=True)
    print(f"Warming {picked.model} on the {picked.device.upper()}…", flush=True)
    try:
        warm_ms = tier1.warm(picked)
    except Exception as error:  # noqa: BLE001
        print(f"Can't use the local model: {error}")
        return 1
    print(f"  ready in {warm_ms:.0f} ms. {len(cases)} cases{' (all through Tier 1)' if tier1_only else ''}…", flush=True)

    def progress(i: int, n: int, item: Item) -> None:
        if i % 25 == 0 or i == n:
            print(f"  {i}/{n}", flush=True)

    items = run(router, catalog, cases, tier1_only=tier1_only, progress=progress)
    summary = summarize(items)
    title = (f"Intent eval — {picked.model} on the {picked.device.upper()}"
             f"{' (Tier 1 only)' if tier1_only else ' (Tier 0 → Tier 1)'}")
    print(report(summary, items, title=title, misses=misses))
    tag = picked.model.replace(":", "_").replace("/", "_")
    save(items, summary, paths.LOGS / f"eval-{tag}-{picked.device}{'-t1' if tier1_only else ''}.jsonl",
         {"model": picked.model, "device": picked.device, "tier1_only": tier1_only})
    correct = summary.accuracy >= 0.9
    print(f"target ≥ 90 % correct: {'met' if correct else 'NOT met'} ({summary.accuracy:.1%})")
    print(f"target < 1 % false actions: {'met' if summary.false_rate < 0.01 else 'NOT met'} by the router fed every "
          f"sentence ({summary.false_rate:.1%}); {'met' if summary.heard_rate < 0.01 else 'NOT met'} behind the "
          f"follow-up gate ({summary.heard_rate:.1%})")
    sys.stdout.flush()
    return 0 if correct and summary.heard_rate < 0.01 else 1
