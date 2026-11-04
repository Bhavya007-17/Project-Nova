"""Checking Tier 1's plans (SPEC §7.4-7.6). Pure: the model's JSON + what he said + the catalog in, a plan out.

- Schema: kinds, actions and their arguments (``nova.brain.plan``).
- Payload rule: text to type, search queries and handoff prompts must be his own words. The
  model only points at them; the plan carries the span cut from the transcript, in his casing.
- Entities exist: app names resolve against the catalog (a short name for a site opens the site),
  window targets too; a name that could be several apps becomes a question with options; an app
  that isn't installed is said plainly. A web address must be one of his short names or a site he
  named; anything else is asked first.
- Unsure -> ask: an act plan under 0.6 confidence becomes a yes / no question, as does a handoff
  under 0.7 (SPEC §7.6). The model's own questions keep up to four choices.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from rapidfuzz import fuzz

from ..actions.catalog import Catalog, Match, Target, short_name, words_of
from .plan import KINDS, Plan, PlanError, Step, check, step
from .tier0 import ACTIVE, describe

ASK_BELOW = 0.6  # act plans less sure than this become questions
HANDOFF_ASK_BELOW = 0.7
MAX_CHOICES = 4
_NEAR = 92.0  # a payload that differs only in spacing / hyphens ("lofi" for "lo-fi") still counts
_SAME_SOUND = 60.0  # the catalog's score for a sound-alike with the very same sound code ("cloud" / "Claude")

_WORD = re.compile(r"[^\W_]+(?:['’][^\W_]+)*")
_SUBMIT_WORDS = re.compile(r"\b(enter|return|send|submit|post)\b", re.IGNORECASE)
_GENERIC = frozenset({"app", "application", "program", "browser", "window", "website", "site", "page"})
_POINTER_WORDS = frozenset({"active", "it", "this", "that", "here", "current", "window", "app", "one", "the", "my",
                            "focused", "front"})
_HOSTISH = frozenset({"www", "com", "org", "net", "edu", "io", "ai", "gov", "co", "app", "dev", "me", "tv", "us", "uk",
                      "m", "en", "web"})


@dataclass(frozen=True)
class Checked:
    plan: Plan | None
    kind: str = ""  # what the model said: act | clarify | handoff | dictate | teach | ignore
    problem: str = ""  # plain words: why there's no plan


# --- payloads ---------------------------------------------------------------------------------------------------


def _tokens(text: str) -> list[tuple[str, int, int]]:
    return [(m.group().lower().replace("’", "'"), m.start(), m.end()) for m in _WORD.finditer(text)]


def _squash(text: str) -> str:
    return text.replace("'", "")


def span(said: str, value: str) -> str | None:
    """The part of ``said`` that ``value`` copies, cut from ``said`` (his casing and punctuation), or None."""
    want = [w for w, _, _ in _tokens(value)]
    have = _tokens(said)
    if not want or not have:
        return None
    words = [w for w, _, _ in have]
    n = len(want)
    for i in range(len(have) - n + 1):
        if words[i : i + n] == want:
            return said[have[i][1] : have[i + n - 1][2]]
    key = _squash("".join(want))
    best: tuple[float, int, int] | None = None
    for size in range(max(1, n - 2), n + 3):
        for i in range(len(have) - size + 1):
            score = fuzz.ratio(key, _squash("".join(words[i : i + size])))
            if score >= _NEAR and (best is None or score > best[0]):
                best = (score, i, size)
    if best is None:
        return None
    _, i, size = best
    return said[have[i][1] : have[i + size - 1][2]]


# --- entities ---------------------------------------------------------------------------------------------------


def _is_pointer(name: str) -> bool:
    words = words_of(name)
    return not words or all(w in _POINTER_WORDS for w in words)


def _clean(name: str) -> str:
    words = list(words_of(name))
    while len(words) > 1 and words[-1] in _GENERIC:
        words.pop()
    while len(words) > 1 and words[0] in ("the", "my", "a"):
        words.pop(0)
    return " ".join(words)


def _target(name: Any, catalog: Catalog, running: frozenset[str], *, apps_only: bool) -> Target | tuple[Match, ...] | None:
    if not isinstance(name, str) or not name.strip():
        return None
    resolution = catalog.resolve(_clean(name), familiar=running)
    best = resolution.best
    if best is not None and (not apps_only or best.target.kind == "app"):
        return best.target
    # The model already read his words: only near-certain sound-alikes are worth a question ("Blender" -/- Calendar).
    offered = tuple(m for m in resolution.matches
                    if (not apps_only or m.target.kind == "app") and (m.how != "sound" or m.score >= _SAME_SOUND))
    return offered[:MAX_CHOICES] or None


def _url(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip() or any(c.isspace() for c in value.strip()):
        return None
    url = value.strip()
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    return url if urlsplit(url).hostname else None


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def url_known(url: str, said: str, catalog: Catalog) -> bool:
    """One of his short names, or a site whose name he said ("reddit" -> reddit.com, "you tube" -> youtube.com)."""
    host = _host(url)
    if not host:
        return False
    if host in {_host(t.url) for _, t in catalog.aliases if t.kind == "site"}:
        return True
    words = set(words_of(said))
    squashed = "".join(words_of(said))
    labels = [label for label in host.split(".") if label not in _HOSTISH]
    return any(label in words or (len(label) >= 4 and label in squashed) for label in labels)


# --- steps ------------------------------------------------------------------------------------------------------


class _Ask:
    """A step whose app could be one of several: the plan becomes a question."""

    def __init__(self, matches: tuple[Match, ...], verb: str, make: Callable[[Target], Step]) -> None:
        self.matches = matches
        self.verb = verb
        self.make = make


class _Unsure:
    """A step that's fine to run once he says yes (a site he didn't name)."""

    def __init__(self, s: Step) -> None:
        self.step = s


_VERBS = {"open_app": "Open", "switch_app": "Switch to", "close_app": "Close", "window": "Move", "layout": "Arrange"}


def _app_step(action: str, args: Mapping[str, Any], catalog: Catalog, running: frozenset[str]) -> Step | _Ask | str:
    name = args.get("app")
    found = _target(name, catalog, running, apps_only=action == "close_app")
    if found is None:
        return f"I can't find {str(name or 'that app').strip()} on this laptop."
    if isinstance(found, tuple):
        return _Ask(found, _VERBS[action], lambda t: _open(action, t))
    return _open(action, found)


def _open(action: str, target: Target) -> Step:
    if target.kind == "site":
        return step("open_url", url=target.url)
    return step(action, app=target.name)


def _window_step(args: Mapping[str, Any], catalog: Catalog, running: frozenset[str]) -> Step | _Ask | str:
    op = args.get("op")
    extra = {"monitor": args["monitor"]} if op == "move_monitor" and args.get("monitor") else {}
    name = args.get("target")
    if not isinstance(name, str) or _is_pointer(name):
        return step("window", op=op, target=ACTIVE, **extra)
    found = _target(name, catalog, running, apps_only=True)
    if found is None:
        return f"I can't find {name.strip()} on this laptop."
    if isinstance(found, tuple):
        return _Ask(found, _VERBS["window"], lambda t: step("window", op=op, target=t.name, **extra))
    return step("window", op=op, target=found.name, **extra)


def _layout_step(args: Mapping[str, Any], catalog: Catalog, running: frozenset[str]) -> Step | str:
    slots = args.get("slots")
    if not isinstance(slots, Sequence) or isinstance(slots, str):
        return "The plan's layout made no sense."
    fixed = []
    for slot in slots:
        if not isinstance(slot, Mapping):
            return "The plan's layout made no sense."
        name = slot.get("app")
        if not isinstance(name, str) or _is_pointer(name):
            fixed.append({"app": ACTIVE, "area": slot.get("area")})
            continue
        found = _target(name, catalog, running, apps_only=True)
        if not isinstance(found, Target):
            return f"I'm not sure which app “{name.strip()}” is."
        fixed.append({"app": found.name, "area": slot.get("area")})
    return step("layout", slots=tuple(fixed))


def _payload_step(action: str, key: str, args: Mapping[str, Any], said: str) -> Step | str:
    value = args.get(key)
    cut = span(said, value) if isinstance(value, str) else None
    if cut is None:
        return "The plan had words you never said, so I didn't use it."
    if action == "type_text":
        # Enter sends messages: only when he said so, whatever the model thinks
        return step("type_text", text=cut, submit=bool(args.get("submit")) and bool(_SUBMIT_WORDS.search(said)))
    engine = args.get("engine") or "google"
    return step("web_search", query=cut, engine=engine)


def _fix(action: str, args: Mapping[str, Any], said: str, catalog: Catalog,
         running: frozenset[str]) -> Step | _Ask | _Unsure | str:
    match action:
        case "open_app" | "switch_app" | "close_app":
            return _app_step(action, args, catalog, running)
        case "window":
            return _window_step(args, catalog, running)
        case "layout":
            return _layout_step(args, catalog, running)
        case "open_url":
            url = _url(args.get("url"))
            if url is None:
                return "The plan's web address made no sense."
            made = step("open_url", url=url)
            return made if url_known(url, said, catalog) else _Unsure(made)
        case "type_text":
            return _payload_step(action, "text", args, said)
        case "web_search":
            return _payload_step(action, "query", args, said)
        case "volume":
            kept = {k: v for k, v in args.items() if k != "level" or args.get("op") == "set"}
            return Step("volume", kept)
    return Step(action, dict(args))


# --- plans ------------------------------------------------------------------------------------------------------


def _confidence(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.5
    number = float(value)
    if number > 1.0:
        number /= 100.0  # a percentage: 95 -> 0.95
    return max(0.0, min(1.0, number))


def _yes_no(plan: Plan) -> Plan:
    said = describe(plan)
    return Plan("clarify", question=f"{said}?", choices=(said,), options=(plan,), confidence=plan.confidence)


def _checked_act(steps: list[Step], confidence: float) -> Plan | str:
    try:
        return check(Plan("act", tuple(steps), confidence=confidence))
    except PlanError as error:
        return f"The plan didn't make sense ({error})."


def _act(data: Mapping[str, Any], said: str, catalog: Catalog, running: frozenset[str], confidence: float) -> Checked:
    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        return Checked(None, "act", "The plan had no steps.")
    steps: list[Step] = []
    ask: tuple[int, _Ask] | None = None
    unsure = False
    for raw in raw_steps:
        if not isinstance(raw, Mapping) or not isinstance(raw.get("action"), str):
            return Checked(None, "act", "The plan didn't make sense.")
        made = _fix(raw["action"], {k: v for k, v in raw.items() if k != "action"}, said, catalog, running)
        if isinstance(made, str):
            return Checked(None, "act", made)
        if isinstance(made, _Ask):
            if ask is not None:
                return Checked(None, "act", "I'm not sure which apps you mean.")
            ask = (len(steps), made)
            steps.append(made.make(made.matches[0].target))  # placeholder, swapped per option below
            continue
        if isinstance(made, _Unsure):
            unsure = True
            made = made.step
        steps.append(made)
    if ask is not None:
        at, pending = ask
        options, choices = [], []
        for match in pending.matches:
            option = _checked_act([*steps[:at], pending.make(match.target), *steps[at + 1 :]], confidence)
            name = short_name(match.target.name) if match.target.kind == "app" else match.target.name
            if isinstance(option, Plan) and name not in choices:
                options.append(option)
                choices.append(name)
        if not options:
            return Checked(None, "act", "I'm not sure which app you mean.")
        question = f"{pending.verb} {choices[0]}?" if len(choices) == 1 else f"{pending.verb} which one?"
        return Checked(Plan("clarify", question=question, choices=tuple(choices), options=tuple(options),
                            confidence=confidence), "act")
    plan = _checked_act(steps, confidence)
    if isinstance(plan, str):
        return Checked(None, "act", plan)
    if unsure or confidence < ASK_BELOW:
        return Checked(_yes_no(plan), "act")
    return Checked(plan, "act")


def _question(data: Mapping[str, Any], confidence: float) -> Checked:
    question = data.get("question")
    if not isinstance(question, str) or not question.strip():
        return Checked(None, "clarify", "I wasn't sure what you meant.")
    choices: list[str] = []
    for choice in data.get("choices") or []:
        text = " ".join(str(choice).split()) if isinstance(choice, (str, int, float)) else ""
        if text and text not in choices:
            choices.append(text)
    plan = Plan("clarify", question=" ".join(question.split()), choices=tuple(choices[:MAX_CHOICES]),
                confidence=confidence)
    return Checked(plan, "clarify")


def _handoff(data: Mapping[str, Any], said: str, confidence: float) -> Checked:
    raw = data.get("handoff")
    raw = raw if isinstance(raw, Mapping) else {}
    to = raw.get("to") if raw.get("to") in ("claude", "chatgpt") else "claude"
    mode = raw.get("mode") if raw.get("mode") in ("text", "voice") else "text"
    prompt = raw.get("prompt")
    words = (span(said, prompt) if isinstance(prompt, str) else None) or said.strip()
    if not words:
        return Checked(None, "handoff", "There was nothing to hand off.")
    plan = Plan("handoff", handoff={"to": to, "mode": mode, "prompt": words}, confidence=confidence)
    if confidence < HANDOFF_ASK_BELOW:
        who = "Claude" if to == "claude" else "ChatGPT"
        said_short = f"Hand this to {who}"
        plan = Plan("clarify", question=f"{said_short}?", choices=(said_short,), options=(plan,), confidence=confidence)
    return Checked(plan, "handoff")


def check_llm(data: Any, said: str, catalog: Catalog, *, running: frozenset[str] = frozenset()) -> Checked:
    """A runnable plan from Tier 1's JSON (or a question), or why not."""
    if not isinstance(data, Mapping):
        return Checked(None, problem="The local model's answer wasn't a plan.")
    kind = data.get("kind")
    if kind not in KINDS:
        return Checked(None, problem="The local model's answer wasn't a plan.")
    confidence = _confidence(data.get("confidence"))
    if kind == "act":
        return _act(data, said, catalog, running, confidence)
    if kind == "clarify":
        return _question(data, confidence)
    if kind == "handoff":
        return _handoff(data, said, confidence)
    if kind == "dictate":
        return Checked(Plan("act", (step("dictation", op="start"),), confidence=confidence), "dictate")
    return Checked(None, kind)
