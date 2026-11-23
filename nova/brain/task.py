"""A task (SPEC §8.1, Stage 07d): a goal in the app in front, done as a loop — look, pick, act, settle, check.

- **Look:** the window in front as numbered rows (``nova.actions.rows``) plus a pixel signature.
- **Pick:** the planner model chooses one verb on one row, or ``done``, or a question (``tier1.pick``).
- **Act:** after the same risk check as any plan step (``risk.assess``; a risky one confirms in the island).
- **Settle:** poll the signature every 100 ms until two reads match, at most 2 s; then look again.
- **Check:** the same rows and the same pixels after an action → stuck, stop and say where.

At most 8 steps and 60 s. Never types words the user didn't say, never a key chord he didn't say beyond plain
navigation keys, never in system settings (guide mode only there: SPEC §14). Pure: the world (eyes + hands), the
model and the asker come in behind small interfaces, so tests run on fakes.

**Guide mode** ("show me how …"): the same look and pick, but Nova rings the row and waits for his own click. It polls
the pixels every 150 ms (no mouse hook); a change that moves the rows or the focus is a click — a hover or a tooltip
moves neither. The mouse on the ring before or after it means he hit the step; anywhere else gets "That opened X —
the step was Y" and the next pick starts from where he is. A nudge at 30 s, out at 3 min. "just do it" mid-wait does
the ringed step and the rest; "let me do it" mid-task rings the next one (``Steer``).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..actions import screen
from ..actions.marks import Box
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
_SPELLED = {"right-click": "right", "double-click": "double"}  # how the pick schema spells them
# system settings, UAC and credential prompts: Nova only points there (SPEC §14)
GUIDE_ONLY = frozenset({"systemsettings.exe", "control.exe", "consent.exe", "credentialuibroker.exe"})
# keys the loop may press without his saying so: they move around, they don't enter or delete anything
PLAIN_KEYS = frozenset({"enter", "tab", "shift+tab", "esc", "escape", "space", "up", "down", "left", "right", "home",
                        "end", "pageup", "pagedown", "alt", "f10"})
GIVE_UP = "I couldn't finish — show me?"
_VERB_LABEL = {"click": "Click", "right": "Right-click", "double": "Double-click"}
GUIDE_POLL_S = 0.15
NUDGE_S = 30.0
GUIDE_MAX_S = 180.0
NUDGE = "Still here — say “do it” or “stop”"
ONLY_POINT = "In settings I only point — click it yourself"
_SLACK = 8  # px around the ring that still count as hitting it


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
    """``{step: {row?, verb, text?, keys?}}`` | ``{done, say}`` | ``{ask}`` → Choice; None when it says nothing usable.
    A usable step wins over a question written beside it."""
    if not isinstance(data, dict):
        return None
    say = data.get("say") if isinstance(data.get("say"), str) else ""
    if data.get("done") is True:
        return Choice(done=True, say=say.strip())
    step = _step(data.get("step"))
    if step is not None:
        return step
    ask = data.get("ask")
    if isinstance(ask, str) and ask.strip():
        return Choice(ask=ask.strip(), say=say.strip())
    return None


def _step(step: Any) -> Choice | None:
    if not isinstance(step, dict):
        return None
    verb = step.get("verb") or "click"
    verb = _SPELLED.get(verb, verb) if isinstance(verb, str) else verb
    row = step.get("row")
    if verb not in VERBS or (row is not None and (isinstance(row, bool) or not isinstance(row, int))):
        return None
    if verb in _ON_ROW and row is None:
        return None
    text = step.get("text") if isinstance(step.get("text"), str) else ""
    keys = step.get("keys") if isinstance(step.get("keys"), str) else ""
    if verb == "type" and not text.strip() or verb == "press" and not keys.strip():
        return None
    return Choice(row=row, verb=verb, text=text.strip(), keys=keys.strip().lower())


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
    def show(self, row: Row | None, caption: str) -> Box | None: ...  # ring the row where it is now; its box
    def hide(self) -> None: ...
    def cursor(self) -> tuple[int, int] | None: ...  # the mouse, in the rows' pixels


class Steer:
    """Do or guide, flipped from the bus thread while a task runs ("just do it" / "let me do it" / "show me")."""

    def __init__(self, mode: str = "do") -> None:
        self.mode = mode
        self.poke = ""  # "next" (he says he did the step) | "where" (ring it again): read once by the guide


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
    step_ms: tuple[float, ...] = ()  # acted (or his click seen) → settled → next pick answered
    ring_ms: tuple[float, ...] = ()  # guide: his click seen → the next ring up (not a "next" he said)

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


@dataclass
class _Run:
    """One task's running tally."""

    goal: str
    began: float
    guided: bool = False
    did: list[str] = field(default_factory=list)
    kept: list[Step] = field(default_factory=list)
    pick_ms: list[float] = field(default_factory=list)
    step_ms: list[float] = field(default_factory=list)
    ring_ms: list[float] = field(default_factory=list)
    clicked_at: float | None = None  # his last click the pixels saw, until the next ring goes up

    def end(self, exit_: str, detail: str) -> Result:
        _log.info("task %r: %s after %d steps (%s)", self.goal, exit_, len(self.did), detail)
        return Result(exit_, detail, tuple(self.did), tuple(self.kept), tuple(self.pick_ms), tuple(self.step_ms),
                      tuple(self.ring_ms))

    def stopped(self) -> Result:
        return self.end("stopped", f"Stopped after {_count(self.did)}" if self.did else "Stopped")

    def limit(self) -> Result:
        if self.guided:
            return self.end("limit", "I stopped showing — say “show me” to start again")
        return self.end("limit", f"I stopped after {_count(self.did)}: " + " · ".join(self.did))


@dataclass(frozen=True)
class _Seen:
    """What came of one guided step: his (he hit it) | wrong | do (he said "just do it") | stopped | limit.
    ``clicked``: the pixels saw it (not a "next" he said)."""

    what: str
    view: View
    scene: Scene
    at: float
    clicked: bool = False


def _inside(box: Box, at: tuple[int, int] | None, slack: int = 0) -> bool:
    return (at is not None and box.left - slack <= at[0] <= box.right + slack
            and box.top - slack <= at[1] <= box.bottom + slack)


def _under(rows: Sequence[Row], at: tuple[int, int] | None) -> Row | None:
    """The smallest row under the mouse (a menu item, not the menu around it)."""
    hits = [r for r in rows if _inside(r.target.box, at)]
    return min(hits, key=lambda r: r.target.box.area) if hits else None


class Loop:
    def __init__(self, world: World, picker: Picker, confirm: Callable[[str], bool | None], *,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                 stopped: Callable[[], bool] = lambda: False,
                 progress: Callable[[str, int], None] = lambda label, n: None,
                 note: Callable[[str], None] = lambda text: None,
                 steer: Steer | None = None,
                 changed: Callable[[tuple[int, ...], tuple[int, ...]], bool] = screen.changed,
                 max_steps: int = MAX_STEPS, max_s: float = MAX_S) -> None:
        self._world = world
        self._pick = picker
        self._confirm = confirm
        self._clock = clock
        self._sleep = sleep
        self._stopped = stopped
        self._progress = progress
        self._note = note
        self._steer = steer if steer is not None else Steer()
        self._changed = changed
        self._max_steps = max_steps
        self._max_s = max_s

    def run(self, goal: str, mode: str = "do", point: bool = False) -> Result:
        """``mode`` "do" acts, "guide" rings each step for his click; ``point`` rings the first step and returns
        ("where is X"), leaving the ring for the caller to time out."""
        self._steer.mode = "guide" if point else mode
        run = _Run(goal, self._clock(), guided=self._steer.mode == "guide")
        scene = self._world.where()
        if self._steer.mode == "do" and guide_only(scene):
            return run.end("guide_only", "In settings I only point — say “show me how …” and click it yourself.")
        view = self._world.look()
        since: float | None = None  # when the last step was seen done (acted, or his click seen)
        while True:
            if self._stopped():
                return run.stopped()
            if self._over(run):
                return run.limit()
            asked_at = self._clock()
            try:
                choice = self._pick(goal, view.lines, tuple(run.did), scene.app, scene.title)
            except Exception:  # noqa: BLE001 - a model failure ends the task, never the brain
                _log.warning("task pick failed", exc_info=True)
                choice = None
            run.pick_ms.append((self._clock() - asked_at) * 1000)
            if since is not None:
                run.step_ms.append((self._clock() - since) * 1000)
            if choice is None:
                return run.end("failed", GIVE_UP)
            if choice.done:
                return run.end("done", choice.say or "Done")
            if choice.ask:
                return run.end("asked", choice.ask)
            if self._over(run):
                return run.limit()
            row = by_number(view.rows, choice.row) if choice.row is not None else None
            if choice.row is not None and row is None:
                return run.end("failed", GIVE_UP)
            name = label(row, choice)
            steps = steps_for(row, choice)
            if self._steer.mode == "do" and guide_only(scene):  # "just do it" said between steps, in settings
                self._note(ONLY_POINT)
                self._steer.mode = "guide"
            if self._steer.mode == "guide":
                run.guided = True
                self._progress(name, len(run.did) + 1)
                if point:
                    self._world.show(row, name)
                    return run.end("done", f"There: “{_cut(row.target.name)}”" if row is not None else name)
                seen = self._guide(run, view, scene, row, name)
                if seen.what == "stopped":
                    return run.stopped()
                if seen.what == "limit":
                    return run.limit()
                if seen.what != "do":
                    if seen.what == "his":
                        run.did.append(name)
                        run.kept.extend(steps)
                    view, scene, since = seen.view, seen.scene, seen.at
                    run.clicked_at = seen.at if seen.clicked else None
                    continue
            run.clicked_at = None
            # do it: Nova acts (from the start, or he said "just do it" while a step was ringed)
            if choice.verb == "type" and not said(choice.text, goal):
                return run.end("asked", "What should I type there?")
            if choice.verb == "press" and not keys_allowed(choice.keys, goal):
                return run.end("asked", f"Press {choice.keys}? Say it if you want it.")
            for s in steps:
                here = self._world.where(s)
                if guide_only(here):
                    return run.end("guide_only", "That's settings — I only point there. Say “show me how …”.")
                risk = assess(_risk_step(s), here)
                if risk is not None and risk.refuse:
                    return run.end("failed", risk.reason)
                question = risk.question if risk is not None and risk.confirm else ""
                if not question and _presses_blind(s, here):
                    question = f"Press {choice.keys.capitalize()} here? (I couldn't check what it presses)"
                if question and not self._confirm(question):
                    return run.end("declined", "Cancelled")
            if self._stopped():
                return run.stopped()
            self._progress(name, len(run.did) + 1)
            try:
                self._world.act(row, choice)
            except ActionError as error:
                return run.end("failed", str(error))
            run.did.append(name)
            run.kept.extend(steps)
            since = self._clock()
            self._settle()
            before, view = view, self._world.look()
            if view.lines == before.lines and not self._changed(before.signature, view.signature):
                return run.end("stuck", f"That didn't change anything — stopped at step {len(run.did)}: {name}.")
            scene = self._world.where()
            if guide_only(scene) and self._steer.mode == "do":
                return run.end("guide_only", "That opened settings — I only point there. Say “show me how …”.")

    def _over(self, run: _Run) -> bool:
        limit = GUIDE_MAX_S if run.guided else self._max_s
        return len(run.did) >= self._max_steps or self._clock() - run.began > limit

    def _guide(self, run: _Run, view: View, scene: Scene, row: Row | None, name: str) -> _Seen:
        """Ring the row and wait for his own click; the ring follows the window and goes when this returns."""
        world = self._world
        steer = self._steer
        steer.poke = ""  # a "next" said before this ring went up was for an earlier step
        try:
            box = world.show(row, name)
            if run.clicked_at is not None:
                run.ring_ms.append((self._clock() - run.clicked_at) * 1000)
                run.clicked_at = None
            base = world.signature()
            last = world.cursor()
            shown_at = self._clock()
            nudged = False
            while True:
                now = self._clock()
                if self._stopped():
                    return _Seen("stopped", view, scene, now)
                if steer.mode == "do":
                    if not guide_only(scene):
                        return _Seen("do", view, scene, now)
                    self._note(ONLY_POINT)
                    steer.mode = "guide"
                poke, steer.poke = steer.poke, ""
                if poke == "next":  # he says it's done: a click the pixels missed, a step he did his own way
                    self._settle()
                    return _Seen("his", world.look(), world.where(), now)
                if poke == "where":  # ring it again where it is now; the nudge waits another 30 s
                    world.hide()
                    box = world.show(row, name) or box
                    shown_at, nudged = now, False
                if now - run.began > GUIDE_MAX_S:
                    return _Seen("limit", view, scene, now)
                if not nudged and now - shown_at >= NUDGE_S:
                    self._note(NUDGE)
                    nudged = True
                self._sleep(GUIDE_POLL_S)
                box = world.show(row, name) or box
                pixels = world.signature()
                at = world.cursor()
                if not self._changed(base, pixels):
                    last = at
                    continue
                seen_at = self._clock()
                self._settle()
                after = world.look()
                moved = world.where()
                if after.lines == view.lines and moved.focus_name == scene.focus_name:
                    base, last = after.signature, at  # a hover, a tooltip, a caret: nothing he did
                    continue
                if box is None or _inside(box, at, _SLACK) or _inside(box, last, _SLACK):
                    return _Seen("his", after, moved, seen_at, clicked=True)
                hit = _under(view.rows, at) or _under(view.rows, last)
                step = _cut(row.target.name) if row is not None else name
                self._note(f"That opened “{_cut(hit.target.name)}” — the step was “{step}”" if hit is not None
                           else f"That wasn't it — the step was “{step}”")
                return _Seen("wrong", after, moved, seen_at, clicked=True)
        finally:
            world.hide()

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
