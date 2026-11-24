"""The Plan (SPEC §7.5): what Tier 0 (and later Tier 1 / skills) hands the executor.

``kind``: act | dictate | handoff | clarify | teach | ignore. An ``act`` plan is a list of
steps, each an action name plus arguments (SPEC §8). ``from_dict`` validates untrusted
plans (the LLM's JSON in Stage 05) the same way Tier 0's are checked in tests.
Everything here is frozen: a plan never changes once made.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

KINDS = frozenset({"act", "dictate", "handoff", "clarify", "teach", "ignore"})
WINDOW_OPS = frozenset({
    "maximize", "minimize", "restore", "close", "fullscreen", "snap_left", "snap_right", "snap_top", "snap_bottom",
    "center", "move_monitor",
})
AREAS = frozenset({
    "left", "right", "top", "bottom", "top_left", "top_right", "bottom_left", "bottom_right",
    "left_third", "middle_third", "right_third", "full",
})
MONITORS = frozenset({"next", "prev", "left", "right", "primary", "other"})
TAB_OPS = frozenset({"new", "close", "next", "prev", "reopen", "goto"})
NAV_OPS = frozenset({"back", "forward", "reload", "up"})  # up: a folder up in File Explorer
SCROLL_DIRS = frozenset({"up", "down", "left", "right", "top", "bottom"})
SCROLL_AMOUNTS = frozenset({"bit", "small", "half", "page", "large"})
AUTOSCROLL_OPS = frozenset({"start", "faster", "slower", "stop"})
AUTOSCROLL_DIRS = frozenset({"up", "down", "left", "right"})
SPEEDS = frozenset({"slow", "keep", "fast"})
DOC_OPS = frozenset({"goto_page", "next_page", "prev_page", "zoom_in", "zoom_out", "zoom_reset", "fit_width"})
BUTTONS = frozenset({"left", "right", "middle", "hover"})
CLICK_VERBS = frozenset({"click", "right", "double", "middle", "hover"})
NUMBERS_OPS = frozenset({"show", "hide", "more"})
SCOPES = frozenset({"here", "everywhere"})  # the front window / its whole screen
SHOT_TARGETS = frozenset({"screen", "window"})
SYSTEM_OPS = frozenset({"lock", "sleep"})
NOVA_OPS = frozenset({"help", "again", "undo"})
TASK_MODES = frozenset({"do", "guide"})  # do: Nova clicks; guide: Nova points, he clicks
HELP_TOPICS = frozenset({"apps", "web", "screen", "system"})
VOLUME_OPS = frozenset({"up", "down", "mute", "unmute", "set"})
MEDIA_OPS = frozenset({"play_pause", "play", "pause", "next", "prev", "stop"})
DICTATION_OPS = frozenset({"start", "stop", "paste"})  # paste: his last dictation again
LISTENING_OPS = frozenset({"sleep", "wake", "off", "end_session"})  # end_session: close the talk session (07b)
ENGINES = frozenset({"google", "youtube", "bing", "duckduckgo", "wikipedia", "amazon", "github", "maps", "images"})
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
CANVAS_RANGES = frozenset({"today", "tomorrow", "week", "next_week", "weekend", "upcoming", "overdue", *WEEKDAYS})
READ_WHATS = frozenset({"page", "selection"})
_KEY = re.compile(r"(ctrl|shift|alt|win|enter|esc|tab|space|backspace|delete|insert|up|down|left|right|home|end"
                  r"|pageup|pagedown|f(?:[1-9]|1[0-9]|2[0-4])|[a-z0-9]|[\[\];',./`=\\-])")


class PlanError(ValueError):
    """A plan that can't be run as given."""


def _frozen(args: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(dict(args))


@dataclass(frozen=True)
class Step:
    action: str
    args: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "args", _frozen(self.args))

    def get(self, key: str, default: Any = None) -> Any:
        return self.args.get(key, default)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Step) and self.action == other.action and dict(self.args) == dict(other.args)

    def __hash__(self) -> int:
        return hash((self.action, tuple(sorted((k, repr(v)) for k, v in self.args.items()))))

    def __repr__(self) -> str:
        inner = ", ".join(f"{key}={value!r}" for key, value in self.args.items())
        return f"{self.action}({inner})"

    def to_dict(self) -> dict[str, Any]:
        return {"action": self.action, **{key: _plain(value) for key, value in self.args.items()}}


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def step(action: str, **args: Any) -> Step:
    return Step(action, args)


@dataclass(frozen=True)
class Plan:
    kind: str
    steps: tuple[Step, ...] = ()
    question: str = ""
    choices: tuple[str, ...] = ()
    options: tuple[Plan, ...] = ()  # clarify: the plan behind each choice (Tier 0 knows them up front)
    confidence: float = 1.0
    say: str = ""
    handoff: Mapping[str, Any] | None = None
    guess: bool = False  # clarify from weak sound-alikes / partial names: Tier 1 may know better

    def option(self, index: int) -> Plan | None:
        return self.options[index] if 0 <= index < len(self.options) else None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"kind": self.kind, "confidence": self.confidence}
        if self.steps:
            data["steps"] = [s.to_dict() for s in self.steps]
        if self.question:
            data["question"] = self.question
            data["choices"] = list(self.choices)
        if self.say:
            data["say"] = self.say
        if self.handoff is not None:
            data["handoff"] = _plain(self.handoff)
        return data


def act(*steps: Step, confidence: float = 1.0, say: str = "") -> Plan:
    return Plan("act", tuple(steps), confidence=confidence, say=say)


# --- validation ------------------------------------------------------------------------------------------


def _text(args: Mapping[str, Any], key: str, *, required: bool = True) -> str | None:
    value = args.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        raise PlanError(f"{key} must be text")
    return value


def _one_of(args: Mapping[str, Any], key: str, allowed: frozenset[str], *, required: bool = True) -> None:
    value = args.get(key)
    if value is None and not required:
        return
    if value not in allowed:
        raise PlanError(f"{key} {value!r} is not one of {sorted(allowed)}")


def _number(args: Mapping[str, Any], key: str, low: int, high: int, *, required: bool = True) -> None:
    value = args.get(key)
    if value is None and not required:
        return
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise PlanError(f"{key} must be a whole number {low}-{high}")


def _url(value: str) -> None:
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname or any(c.isspace() for c in value):
        raise PlanError(f"not a web address: {value!r}")


_MODIFIERS = frozenset({"ctrl", "shift", "alt", "win"})


def keys_ok(keys: str) -> bool:
    """Known key names, and at most one key besides the modifiers ("ctrl+v+v" is not a chord: it pastes twice)."""
    parts = keys.lower().split("+")
    return (bool(parts) and all(_KEY.fullmatch(part) for part in parts)
            and sum(part not in _MODIFIERS for part in parts) <= 1)


def _check_step(s: Step) -> None:
    a = s.args
    match s.action:
        case "open_app" | "switch_app" | "close_app":
            _text(a, "app")
        case "window":
            _one_of(a, "op", WINDOW_OPS)
            _text(a, "target", required=False)
            _one_of(a, "monitor", MONITORS, required=False)
            if not isinstance(a.get("tab", False), bool):  # "close this": its tab, in a tabbed app
                raise PlanError("tab must be true or false")
        case "layout":
            slots = a.get("slots")
            if not isinstance(slots, (list, tuple)) or not 1 <= len(slots) <= 4:
                raise PlanError("layout needs 1-4 slots")
            for slot in slots:
                if not isinstance(slot, Mapping):
                    raise PlanError("layout slots are {app, area}")
                _text(slot, "app")
                _one_of(slot, "area", AREAS)
        case "open_url":
            _url(_text(a, "url") or "")
        case "web_search":
            _text(a, "query")
            _one_of(a, "engine", ENGINES, required=False)
        case "tab":
            _one_of(a, "op", TAB_OPS)
            if a.get("op") == "goto":
                _text(a, "match")
        case "nav":
            _one_of(a, "op", NAV_OPS)
        case "scroll":
            _one_of(a, "dir", SCROLL_DIRS)
            _one_of(a, "amount", SCROLL_AMOUNTS, required=False)
        case "type_text":
            _text(a, "text")
        case "page_click" | "scroll_to":
            _text(a, "description")
            _text(a, "element", required=False)
        case "page_fill":
            _text(a, "description")
            _text(a, "text")
            _text(a, "element", required=False)
        case "page_read":
            _one_of(a, "what", READ_WHATS)
        case "canvas_due":
            _one_of(a, "range", CANVAS_RANGES)
            _text(a, "item", required=False)  # his words, when they name a course ("for physics")
        case "canvas_open_item" | "canvas_item_text":
            _text(a, "item")
        case "canvas_open":  # a resolved canvas_open_item: the item's address
            _url(_text(a, "url") or "")
            _text(a, "title")
        case "press":
            if not keys_ok(_text(a, "keys") or ""):
                raise PlanError(f"can't press {a.get('keys')!r}")
        case "volume":
            _one_of(a, "op", VOLUME_OPS)
            level = a.get("level")
            if level is not None and (isinstance(level, bool) or not isinstance(level, int) or not 0 <= level <= 100):
                raise PlanError("volume level is 0-100")
        case "media":
            _one_of(a, "op", MEDIA_OPS)
        case "dictation":
            _one_of(a, "op", DICTATION_OPS)
            _text(a, "text", required=False)  # start: words he already said ("type this: ...")
        case "docs_new":
            pass
        case "listening":
            _one_of(a, "op", LISTENING_OPS)
        case "autoscroll":
            _one_of(a, "op", AUTOSCROLL_OPS)
            _one_of(a, "dir", AUTOSCROLL_DIRS, required=False)
            _one_of(a, "speed", SPEEDS, required=False)
        case "doc":
            _one_of(a, "op", DOC_OPS)
            _number(a, "page", 1, 99999, required=a.get("op") == "goto_page")
        case "ui_click":
            _text(a, "name")
            _number(a, "element", 1, 999, required=False)
            _one_of(a, "button", BUTTONS, required=False)
            _number(a, "count", 1, 3, required=False)
            _text(a, "role", required=False)
            _text(a, "near", required=False)
        case "numbers":
            _one_of(a, "op", NUMBERS_OPS, required=False)
            _one_of(a, "scope", SCOPES, required=False)
        case "grid":
            _one_of(a, "scope", SCOPES, required=False)
        case "pick":
            _number(a, "n", 1, 999)
            _one_of(a, "verb", CLICK_VERBS, required=False)
            _number(a, "to", 1, 999, required=False)
        case "screenshot":
            _one_of(a, "target", SHOT_TARGETS, required=False)
        case "camera":
            _one_of(a, "op", frozenset({"photo"}), required=False)
        case "system":
            _one_of(a, "op", SYSTEM_OPS)
        case "open_path":
            _text(a, "name", required=bool(not a.get("path")))
            _text(a, "folder", required=False)
            _text(a, "path", required=False)  # resolved: the file on disk (never from the model; see validate)
        case "nova":
            _one_of(a, "op", NOVA_OPS)
            _one_of(a, "topic", HELP_TOPICS, required=False)
        case "task":  # no goal = the last task ("just do it", "let me do it", "show me again")
            _text(a, "goal", required=False)
            _one_of(a, "mode", TASK_MODES, required=False)
            if "point" in a and not isinstance(a["point"], bool):
                raise PlanError("point must be true or false")
        case _:
            raise PlanError(f"unknown action {s.action!r}")


ACTIONS = frozenset({
    "open_app", "switch_app", "close_app", "window", "layout", "open_url", "web_search", "tab", "nav", "scroll",
    "type_text", "page_click", "scroll_to", "page_fill", "page_read", "canvas_due", "canvas_open_item",
    "canvas_item_text", "canvas_open", "press", "volume", "media", "dictation", "docs_new", "listening", "autoscroll",
    "doc", "ui_click", "numbers", "grid", "pick", "screenshot", "camera", "system", "open_path", "nova", "task",
})  # every action _check_step knows (the help card is checked against it)


def dictate_after_docs(steps: tuple[Step, ...]) -> tuple[Step, ...]:
    """ "Open a Google Doc and type …": the words go into the new doc as dictation (cleaned, and he can keep
    going), not as one-off typing."""
    out = list(steps)
    for i in range(1, len(out)):
        writes = out[i].action == "type_text" and not out[i].get("submit") or \
            (out[i].action == "dictation" and out[i].get("op") == "start")
        if writes and out[i - 1].action == "open_url" and _docs_home(str(out[i - 1].get("url", ""))):
            out[i - 1] = step("docs_new")  # "open Google Docs and type …": the home page has nowhere to type
        if out[i - 1].action == "docs_new" and out[i].action == "type_text" and not out[i].get("submit"):
            out[i] = step("dictation", op="start", text=out[i].get("text"))
    return tuple(out)


def _docs_home(url: str) -> bool:
    parts = urlsplit(url)
    return parts.hostname == "docs.google.com" and parts.path.rstrip("/") in ("", "/document", "/document/u/0")


def check(plan: Plan) -> Plan:
    if plan.kind not in KINDS:
        raise PlanError(f"unknown plan kind {plan.kind!r}")
    if plan.kind == "act":
        if not plan.steps:
            raise PlanError("an act plan needs steps")
        for s in plan.steps:
            _check_step(s)
    if plan.kind == "clarify" and (not plan.question.strip() or not plan.choices):
        raise PlanError("clarify needs a question and choices")
    for option in plan.options:
        check(option)
    return plan


def from_dict(data: Mapping[str, Any]) -> Plan:
    """Build and validate a plan from JSON-like data. Raises PlanError."""
    if not isinstance(data, Mapping):
        raise PlanError("a plan is an object")
    raw_steps = data.get("steps") or []
    if not isinstance(raw_steps, list):
        raise PlanError("steps must be a list")
    steps = []
    for raw in raw_steps:
        if not isinstance(raw, Mapping) or not isinstance(raw.get("action"), str):
            raise PlanError("each step needs an action")
        args = {key: value for key, value in raw.items() if key != "action"}
        steps.append(Step(raw["action"], args))
    confidence = data.get("confidence", 1.0)
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise PlanError("confidence must be a number")
    choices = data.get("choices") or []
    plan = Plan(
        kind=str(data.get("kind", "")),
        steps=tuple(steps),
        question=str(data.get("question") or ""),
        choices=tuple(str(c) for c in choices),
        confidence=float(confidence),
        say=str(data.get("say") or ""),
        handoff=data.get("handoff"),
    )
    return check(plan)
