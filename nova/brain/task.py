"""A task (SPEC §8.1, Stage 07d): a goal in the app in front, done as a loop — look, pick, act, settle, check.

- **Look:** the window in front as numbered rows (``nova.actions.rows``) plus a pixel signature.
- **Pick:** the planner model chooses one verb on one row, or ``done``, or a question (``tier1.pick``).
- **Act:** after the same risk check as any plan step (``risk.assess``; a risky one confirms in the island).
- **Settle:** poll the signature every 100 ms until two reads match, at most 2 s; then look again.
- **Check:** the same rows and the same pixels after an action → stuck, stop and say where.

At most 8 steps and 60 s. Never types words the user didn't say, never a key chord he didn't say beyond plain
navigation keys, never in system settings (guide mode only there: SPEC §14). Pure: the world (eyes + hands), the
model and the asker come in behind small interfaces, so tests run on fakes.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from ..actions import screen
from ..actions.native import ActionError
from ..actions.rows import Row, by_number
from .plan import Plan, Step, act, keys_ok
from .risk import ACTIVATES, Scene, assess

_log = logging.getLogger(__name__)

MAX_STEPS = 8
MAX_S = 60.0
SETTLE_POLL_S = 0.1
SETTLE_MAX_S = 2.0
VERBS = ("click", "right", "double", "type", "press", "scroll")
_ON_ROW = frozenset({"click", "right", "double"})
# system settings, UAC and credential prompts: Nova only points there (SPEC §14)
GUIDE_ONLY = frozenset({"systemsettings.exe", "control.exe", "consent.exe", "credentialuibroker.exe"})
# keys the loop may press without his saying so: they move around, they don't enter or delete anything
PLAIN_KEYS = frozenset({"enter", "tab", "shift+tab", "esc", "escape", "space", "up", "down", "left", "right", "home",
                        "end", "pageup", "pagedown", "alt", "f10"})
GIVE_UP = "I couldn't finish — show me?"
_VERB_LABEL = {"click": "Click", "right": "Right-click", "double": "Double-click"}


@dataclass(frozen=True)
class Choice:
    """The model's answer for one step."""

    row: int | None = None
    verb: str = "click"
    text: str = ""
    keys: str = ""
    done: bool = False
    ask: str = ""
    say: str = ""


def parse(data: Any) -> Choice | None:
    """``{step: {row, verb, text?, keys?}, done, ask, say}`` → Choice; None when it says nothing usable."""
    if not isinstance(data, dict):
        return None
    say = data.get("say") if isinstance(data.get("say"), str) else ""
    if data.get("done") is True:
        return Choice(done=True, say=say.strip())
    ask = data.get("ask")
    if isinstance(ask, str) and ask.strip():
        return Choice(ask=ask.strip(), say=say.strip())
    step = data.get("step")
    if not isinstance(step, dict):
        return None
    verb = step.get("verb") or "click"
    row = step.get("row")
    if verb not in VERBS or (row is not None and (isinstance(row, bool) or not isinstance(row, int))):
        return None
    if verb in _ON_ROW and row is None:
        return None
    text = step.get("text") if isinstance(step.get("text"), str) else ""
    keys = step.get("keys") if isinstance(step.get("keys"), str) else ""
    if verb == "type" and not text.strip() or verb == "press" and not keys.strip():
        return None
    return Choice(row=row, verb=verb, text=text.strip(), keys=keys.strip().lower(), say=say.strip())


@dataclass(frozen=True)
class View:
    """One look at the window in front."""

    rows: tuple[Row, ...]
    signature: tuple[int, ...] = ()

    @property
    def lines(self) -> tuple[str, ...]:
        return tuple(r.text for r in self.rows)


class World(Protocol):
    def look(self) -> View: ...  # rows + signature of the window in front
    def signature(self) -> tuple[int, ...]: ...  # pixels only (settle polls this)
    def where(self, s: Step | None = None) -> Scene: ...  # for risk (a typing step reads the focused control)
    def act(self, row: Row | None, choice: Choice) -> Any: ...  # raises ActionError


Picker = Callable[[str, Sequence[str], Sequence[str], str, str], Choice | None]  # goal, lines, did, app, title


def model_picker(tier1: Any) -> Picker:
    """The planner model as a Picker (``Tier1.pick``): no answer, an error or an unusable one -> None."""

    def pick(goal: str, lines: Sequence[str], did: Sequence[str], app: str, title: str) -> Choice | None:
        thought = tier1.pick(goal, lines, did=did, front=app or None, title=title or None)
        choice = parse(thought.data) if thought.data is not None else None
        _log.info("task pick %.0f ms: %s%s", thought.ms, choice, f" ({thought.error})" if thought.error else "")
        return choice

    return pick


@dataclass(frozen=True)
class Result:
    exit: str  # done | stuck | limit | stopped | failed | asked | declined | guide_only
    detail: str
    did: tuple[str, ...] = ()  # step labels, in order
    kept: tuple[Step, ...] = ()  # what "do that again" replays
    pick_ms: tuple[float, ...] = ()
    step_ms: tuple[float, ...] = ()  # acted → settled → next pick answered

    @property
    def ok(self) -> bool:
        return self.exit == "done"


def kept_plan(result: Result) -> Plan:
    return act(*result.kept)


def guide_only(scene: Scene) -> bool:
    return scene.exe.lower() in GUIDE_ONLY


_EDGE = " .,!?;:\"'“”‘’()"


def _words(text: str) -> list[str]:
    return [w for w in (w.strip(_EDGE) for w in text.casefold().split()) if w]


def _within(part: list[str], whole: list[str]) -> bool:
    n = len(part)
    return n > 0 and any(whole[i:i + n] == part for i in range(len(whole) - n + 1))


def said(text: str, goal: str) -> bool:
    """Typing: only words he said, whole words in his order (SPEC §7.4 payload rule)."""
    return _within(_words(text), _words(goal))


def keys_allowed(keys: str, goal: str) -> bool:
    """Plain navigation keys, or a chord he said as a word of its own ("ctrl+s", "ctrl + s")."""
    keys = keys.lower().replace(" ", "")
    if not keys_ok(keys):
        return False
    return keys in PLAIN_KEYS or keys in _words(" ".join(goal.split()).replace(" + ", "+"))


def _cut(text: str, limit: int = 24) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def label(row: Row | None, choice: Choice) -> str:
    if choice.verb in _ON_ROW and row is not None:
        return f"{_VERB_LABEL[choice.verb]} “{_cut(row.target.name)}”"
    if choice.verb == "type":
        return f"Type “{_cut(choice.text)}”"
    if choice.verb == "press":
        return "Press " + choice.keys.replace("+", " + ").title()
    return f"Scroll {_scroll_dir(choice)}"


def _scroll_dir(choice: Choice) -> str:
    return choice.text if choice.text in ("up", "down", "left", "right") else "down"


def _click(row: Row, verb: str = "click") -> Step:
    args: dict[str, Any] = {"name": row.target.name, "role": row.target.role}
    if verb == "right":
        args["button"] = "right"
    elif verb == "double":
        args["count"] = 2
    return Step("ui_click", args)


def steps_for(row: Row | None, choice: Choice) -> tuple[Step, ...]:
    """What the step is as plan steps: for the risk check, and kept for "do that again"."""
    if choice.verb in _ON_ROW and row is not None:
        return (_click(row, choice.verb),)
    if choice.verb == "type":
        typing = Step("type_text", {"text": choice.text})
        return (_click(row), typing) if row is not None else (typing,)
    if choice.verb == "press":
        return (Step("press", {"keys": choice.keys}),)
    return (Step("scroll", {"dir": _scroll_dir(choice)}),)


def _risk_step(s: Step) -> Step:
    """Risk judges what gets clicked by its own name."""
    return Step(s.action, {**s.args, "label": s.get("name")}) if s.action == "ui_click" else s


def _presses_blind(s: Step, scene: Scene) -> bool:
    """The model chose Enter / Space and UI Automation couldn't say what's focused: it might be a "Delete"."""
    return s.action == "press" and str(s.get("keys", "")).lower() in ACTIVATES and not scene.focus_known


class Loop:
    def __init__(self, world: World, picker: Picker, confirm: Callable[[str], bool | None], *,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                 stopped: Callable[[], bool] = lambda: False,
                 progress: Callable[[str, int], None] = lambda label, n: None,
                 changed: Callable[[tuple[int, ...], tuple[int, ...]], bool] = screen.changed,
                 max_steps: int = MAX_STEPS, max_s: float = MAX_S) -> None:
        self._world = world
        self._pick = picker
        self._confirm = confirm
        self._clock = clock
        self._sleep = sleep
        self._stopped = stopped
        self._progress = progress
        self._changed = changed
        self._max_steps = max_steps
        self._max_s = max_s

    def run(self, goal: str) -> Result:
        began = self._clock()
        did: list[str] = []
        kept: list[Step] = []
        pick_ms: list[float] = []
        step_ms: list[float] = []

        def end(exit_: str, detail: str) -> Result:
            _log.info("task %r: %s after %d steps (%s)", goal, exit_, len(did), detail)
            return Result(exit_, detail, tuple(did), tuple(kept), tuple(pick_ms), tuple(step_ms))

        def over() -> bool:
            return len(did) >= self._max_steps or self._clock() - began > self._max_s

        scene = self._world.where()
        if guide_only(scene):
            return end("guide_only", "In settings I only point — say “show me how …” and click it yourself.")
        view = self._world.look()
        acted_at: float | None = None
        while True:
            if self._stopped():
                return end("stopped", f"Stopped after {_count(did)}" if did else "Stopped")
            if over():
                return end("limit", f"I stopped after {_count(did)}: " + " · ".join(did))
            asked_at = self._clock()
            try:
                choice = self._pick(goal, view.lines, tuple(did), scene.app, scene.title)
            except Exception:  # noqa: BLE001 - a model failure ends the task, never the brain
                _log.warning("task pick failed", exc_info=True)
                choice = None
            pick_ms.append((self._clock() - asked_at) * 1000)
            if acted_at is not None:
                step_ms.append((self._clock() - acted_at) * 1000)
            if choice is None:
                return end("failed", GIVE_UP)
            if choice.done:
                return end("done", choice.say or "Done")
            if choice.ask:
                return end("asked", choice.ask)
            if over():
                return end("limit", f"I stopped after {_count(did)}: " + " · ".join(did))
            row = by_number(view.rows, choice.row) if choice.row is not None else None
            if choice.row is not None and row is None:
                return end("failed", GIVE_UP)
            if choice.verb == "type" and not said(choice.text, goal):
                return end("asked", "What should I type there?")
            if choice.verb == "press" and not keys_allowed(choice.keys, goal):
                return end("asked", f"Press {choice.keys}? Say it if you want it.")
            steps = steps_for(row, choice)
            for s in steps:
                here = self._world.where(s)
                risk = assess(_risk_step(s), here)
                if risk is not None and risk.refuse:
                    return end("failed", risk.reason)
                question = risk.question if risk is not None and risk.confirm else ""
                if not question and _presses_blind(s, here):
                    question = f"Press {choice.keys.capitalize()} here? (I couldn't check what it presses)"
                if question and not self._confirm(question):
                    return end("declined", "Cancelled")
            if self._stopped():
                return end("stopped", f"Stopped after {_count(did)}" if did else "Stopped")
            name = label(row, choice)
            self._progress(name, len(did) + 1)
            try:
                self._world.act(row, choice)
            except ActionError as error:
                return end("failed", str(error))
            did.append(name)
            kept.extend(steps)
            acted_at = self._clock()
            self._settle()
            before, view = view, self._world.look()
            if view.lines == before.lines and not self._changed(before.signature, view.signature):
                return end("stuck", f"That didn't change anything — stopped at step {len(did)}: {name}.")
            scene = self._world.where()
            if guide_only(scene):
                return end("guide_only", "That opened settings — I only point there. Say “show me how …”.")

    def _settle(self) -> None:
        """Two equal signature reads 100 ms apart, or 2 s, whichever comes first."""
        began = self._clock()
        last = self._world.signature()
        while self._clock() - began < SETTLE_MAX_S:
            self._sleep(SETTLE_POLL_S)
            now = self._world.signature()
            if now == last or not self._changed(last, now):
                return
            last = now


def _count(did: Sequence[str]) -> str:
    return f"{len(did)} step" + ("" if len(did) == 1 else "s")
