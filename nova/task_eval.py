"""`nova eval --tasks` (Stage 07d, SPEC §18): task steps from docs/eval/tasks.yaml, one pick at a time.

Each step is a look at a window: its rows, exactly as ``rows.lines`` writes them (the YAML leaves the numbers out and
the loader numbers them in order, so an open menu's rows can be put in front of a screen kept under ``screens:``), and
what a right next step is. The model gets the goal, the rows and the labels of the wanted steps before it (as the loop
would have them after doing those steps right), through ``Tier1.pick``: the same request the task loop sends. A step is
right when the pick matches one of its wants; a task is right when every step is. Pick time per step is the model's
own (``Thought.ms``).

Wants: ``{click|right|double: <row name>, area?: <its area>}``, ``{type: <text>, into?: <row name>}``,
``{press: <keys>}``, ``{scroll: up|down|left|right}``, ``done``, ``ask``, or a list of these (any one is right).
"""

from __future__ import annotations

import json
import re
import string
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from . import paths
from .actions.marks import Box, Target
from .actions.rows import ROW_CAP, Row
from .brain.task import Choice, label, parse
from .evals import percentile

TASKS_FILE = paths.ROOT / "docs" / "eval" / "tasks.yaml"

_ON_ROW = ("click", "right", "double")
_VERBS = (*_ON_ROW, "type", "press", "scroll")
_LINE = re.compile(r'^(?P<role>.+?) "(?P<name>[^"]*)"(?: (?P<state>on|off|mixed|open|closed))?(?: \((?P<area>.*)\))?$')
_NUMBER = re.compile(r"^\d+ ")
_KEY_ALIASES = {"control": "ctrl", "return": "enter", "esc": "escape", "del": "delete", "win": "windows"}

Pick = Callable[[str, Sequence[str], Sequence[str], str, str], tuple[Any, float, str]]  # -> (data, ms, error)


@dataclass(frozen=True)
class Want:
    verb: str  # click | right | double | type | press | scroll | done | ask
    rows: tuple[int, ...] = ()  # the row(s) it acts on
    text: str = ""  # type: the words; scroll: the direction
    keys: str = ""  # press
    name: str = ""  # the row's name, for the report


@dataclass(frozen=True)
class Look:
    ui: tuple[str, ...]  # numbered rows, as the model sees them
    wants: tuple[Want, ...]
    title: str = ""


@dataclass(frozen=True)
class Task:
    goal: str
    steps: tuple[Look, ...]
    app: str = ""
    source: str = ""


@dataclass(frozen=True)
class Item:
    task: int
    step: int
    goal: str
    want: str
    got: str
    ok: bool
    ms: float
    error: str = ""


@dataclass(frozen=True)
class Summary:
    steps: int
    steps_right: int
    tasks: int
    tasks_right: int
    pick_p50: float
    pick_p95: float

    @property
    def step_rate(self) -> float:
        return self.steps_right / self.steps if self.steps else 0.0

    @property
    def task_rate(self) -> float:
        return self.tasks_right / self.tasks if self.tasks else 0.0


# --- loading ---------------------------------------------------------------------------------------------------------


def _flatten(items: Iterable[Any]) -> list[str]:
    out: list[str] = []
    for item in items:
        if isinstance(item, list):
            out.extend(_flatten(item))
        else:
            out.append(str(item))
    return out


def _numbered(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, list) or not raw:
        raise ValueError("ui: a list of rows")
    lines = [_NUMBER.sub("", line.strip(), count=1) for line in _flatten(raw)]
    if len(lines) > ROW_CAP:
        raise ValueError(f"ui: {len(lines)} rows, more than the {ROW_CAP} the model ever sees")
    for line in lines:
        if not _LINE.match(line):
            raise ValueError(f"ui: not a row: {line!r}")
    return tuple(f"{i + 1} {line}" for i, line in enumerate(lines))


def _parts(line: str) -> re.Match[str]:
    match = _LINE.match(_NUMBER.sub("", line, count=1))
    assert match is not None  # checked by _numbered
    return match


def _find(ui: Sequence[str], name: str, area: str | None) -> tuple[int, ...]:
    hits = []
    for i, line in enumerate(ui):
        parts = _parts(line)
        if parts["name"].casefold() == name.casefold() and (
                area is None or (parts["area"] or "").casefold() == area.casefold()):
            hits.append(i + 1)
    if not hits:
        raise ValueError(f"no row named {name!r}" + (f" in {area!r}" if area else ""))
    if len(hits) > 1:
        raise ValueError(f"{len(hits)} rows named {name!r}: give its area")
    return tuple(hits)


def _norm_keys(keys: str) -> str:
    parts = [p.strip().lower() for p in keys.replace(" ", "").split("+") if p.strip()]
    return "+".join(_KEY_ALIASES.get(p, p) for p in parts)


def _want(raw: Any, ui: Sequence[str]) -> Want:
    if raw in ("done", "ask"):
        return Want(raw)
    if not isinstance(raw, dict):
        raise ValueError(f"want: {raw!r}")
    verbs = [v for v in _VERBS if v in raw]
    extra = set(raw) - set(_VERBS) - {"area", "into"}
    if len(verbs) != 1 or extra:
        raise ValueError(f"want: one of {', '.join(_VERBS)}, done or ask: {raw!r}")
    verb = verbs[0]
    value = str(raw[verb]).strip()
    area = raw.get("area")
    if verb in _ON_ROW:
        return Want(verb, rows=_find(ui, value, area), name=value)
    if verb == "type":
        into = raw.get("into")
        rows = _find(ui, str(into), area) if into else ()
        return Want("type", rows=rows, text=value, name=str(into or ""))
    if verb == "press":
        return Want("press", keys=_norm_keys(value))
    if value not in ("up", "down", "left", "right"):
        raise ValueError(f"want: scroll up, down, left or right: {raw!r}")
    return Want("scroll", text=value)


def _wants(raw: Any, ui: Sequence[str]) -> tuple[Want, ...]:
    return tuple(_want(r, ui) for r in raw) if isinstance(raw, list) else (_want(raw, ui),)


def load_tasks(path: Path = TASKS_FILE) -> list[Task]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    source = str(data.get("source") or "")
    tasks = []
    for n, raw in enumerate(data.get("tasks") or [], 1):
        goal = str(raw.get("goal") or "").strip()
        title = str(raw.get("title") or "")
        steps = []
        for k, step in enumerate(raw.get("steps") or [], 1):
            try:
                ui = _numbered(step.get("ui"))
                steps.append(Look(ui, _wants(step.get("want"), ui), str(step.get("title") or title)))
            except ValueError as error:
                raise ValueError(f"{path.name} task {n} ({goal!r}) step {k}: {error}") from None
        if not goal or not steps:
            raise ValueError(f"{path.name} task {n}: needs a goal and steps")
        tasks.append(Task(goal, tuple(steps), str(raw.get("app") or ""), str(raw.get("source") or source)))
    return tasks


# --- scoring ---------------------------------------------------------------------------------------------------------


def _row(ui: Sequence[str], number: int | None) -> Row | None:
    if number is None or not 1 <= number <= len(ui):
        return None
    parts = _parts(ui[number - 1])
    return Row(number, Target(Box(0, 0, 0, 0), parts["name"], parts["role"]), parts["area"] or "",
               parts["state"] or "")


def _as_choice(want: Want) -> Choice:
    return Choice(row=want.rows[0] if want.rows else None, verb=want.verb, text=want.text, keys=want.keys)


def history(task: Task) -> tuple[tuple[str, ...], ...]:
    """What ``did`` holds at each step when every step before it went as wanted (the first want's label)."""
    out, did = [], []
    for look in task.steps:
        out.append(tuple(did))
        want = look.wants[0]
        if want.verb in _VERBS:
            choice = _as_choice(want)
            did.append(label(_row(look.ui, choice.row), choice))
    return tuple(out)


_PUNCT = str.maketrans("", "", string.punctuation)


def _words(text: str) -> str:
    return " ".join(text.casefold().translate(_PUNCT).split())


def _matches(choice: Choice, want: Want) -> bool:
    if want.verb == "done":
        return choice.done
    if want.verb == "ask":
        return bool(choice.ask)
    if choice.done or choice.ask or choice.verb != want.verb:
        return False
    if want.verb in _ON_ROW:
        return choice.row in want.rows
    if want.verb == "type":
        on_row = not want.rows or choice.row is None or choice.row in want.rows
        return on_row and _words(choice.text) == _words(want.text)
    if want.verb == "press":
        return set(_norm_keys(choice.keys).split("+")) == set(want.keys.split("+"))
    return (choice.text or "down") == want.text


def right(choice: Choice | None, wants: Sequence[Want]) -> bool:
    return choice is not None and any(_matches(choice, w) for w in wants)


def _on(ui: Sequence[str], number: int | None) -> str:
    row = _row(ui, number)
    return f'{number} "{row.target.name}"' if row else f"row {number}"


def describe_want(want: Want, ui: Sequence[str]) -> str:
    if want.verb in _ON_ROW:
        return f"{want.verb} {_on(ui, want.rows[0])}"
    if want.verb == "type":
        return f'type "{want.text}"' + (f" on {_on(ui, want.rows[0])}" if want.rows else "")
    if want.verb == "press":
        return f"press {want.keys}"
    if want.verb == "scroll":
        return f"scroll {want.text}"
    return want.verb


def describe(choice: Choice | None, ui: Sequence[str], error: str = "") -> str:
    if choice is None:
        return f"nothing ({error})" if error else "nothing usable"
    if choice.done:
        return "done"
    if choice.ask:
        return f'ask "{choice.ask}"'
    if choice.verb in _ON_ROW:
        return f"{choice.verb} {_on(ui, choice.row)}"
    if choice.verb == "type":
        return f'type "{choice.text}"' + (f" on {_on(ui, choice.row)}" if choice.row is not None else "")
    if choice.verb == "press":
        return f"press {choice.keys}"
    return f"scroll {choice.text or 'down'}"


def run(tasks: Sequence[Task], pick: Pick, *, progress: Callable[[int, int], None] | None = None) -> list[Item]:
    items: list[Item] = []
    total = sum(len(t.steps) for t in tasks)
    for i, task in enumerate(tasks):
        for k, (look, did) in enumerate(zip(task.steps, history(task), strict=True)):
            data, ms, error = pick(task.goal, look.ui, did, task.app, look.title)
            choice = parse(data) if data is not None else None
            ok = right(choice, look.wants)
            items.append(Item(i, k, task.goal, " | ".join(describe_want(w, look.ui) for w in look.wants),
                              describe(choice, look.ui, error), ok, ms, error))
            if progress:
                progress(len(items), total)
    return items


def summarize(items: Sequence[Item]) -> Summary:
    by_task: dict[int, bool] = {}
    for item in items:
        by_task[item.task] = by_task.get(item.task, True) and item.ok
    times = [i.ms for i in items]
    return Summary(len(items), sum(i.ok for i in items), len(by_task), sum(by_task.values()),
                   percentile(times, 50), percentile(times, 95))


def report(summary: Summary, items: Sequence[Item], *, title: str, misses: bool = True) -> str:
    out = [title,
           f"  steps right: {summary.steps_right}/{summary.steps} ({summary.step_rate:.1%})   "
           f"whole tasks right: {summary.tasks_right}/{summary.tasks} ({summary.task_rate:.1%})",
           f"  pick time per step: p50 {summary.pick_p50:.0f} ms · p95 {summary.pick_p95:.0f} ms"]
    if misses:
        wrong = [i for i in items if not i.ok]
        if wrong:
            out.append("misses:")
            out.extend(f"  {i.task + 1}.{i.step + 1} {i.goal!r}: want {i.want} — got {i.got}" for i in wrong)
    return "\n".join(out)


def save(items: Sequence[Item], summary: Summary, path: Path, meta: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as out:
        out.write(json.dumps({"summary": {**meta, "step_rate": summary.step_rate, "task_rate": summary.task_rate,
                                          "pick_p50": summary.pick_p50, "pick_p95": summary.pick_p95}},
                             ensure_ascii=False) + "\n")
        for item in items:
            out.write(json.dumps(item.__dict__, ensure_ascii=False) + "\n")


def main(*, model: str | None = None, device: str | None = None, limit: int = 0, misses: bool = True) -> int:
    from .governor import Choice as ModelChoice
    from .say import _catalog_store, brain_parts

    _, tier1 = brain_parts(_catalog_store())
    tasks = load_tasks()[: limit or None]
    picked = None
    if model or device:
        llm = tier1.llm
        picked = ModelChoice(model or (llm.cpu_model if device == "cpu" else llm.gpu_model), device or "gpu",
                             "nova eval --tasks --model/--device")
        tier1.pin(picked)
    picked = picked or tier1.governor.choose(fresh=True)
    print(f"Warming {picked.model} on the {picked.device.upper()}…", flush=True)
    try:
        warm_ms = tier1.warm(picked)
    except Exception as error:  # noqa: BLE001
        print(f"Can't use the local model: {error}")
        return 1
    steps = sum(len(t.steps) for t in tasks)
    print(f"  ready in {warm_ms:.0f} ms. {len(tasks)} tasks, {steps} steps…", flush=True)

    def pick(goal: str, lines: Sequence[str], did: Sequence[str], app: str, title: str) -> tuple[Any, float, str]:
        thought = tier1.pick(goal, lines, did=did, front=app or None, title=title or None, choice=picked)
        return thought.data, thought.ms, thought.error or ""

    def progress(i: int, n: int) -> None:
        if i % 20 == 0 or i == n:
            print(f"  {i}/{n}", flush=True)

    items = run(tasks, pick, progress=progress)
    summary = summarize(items)
    sources = sorted({t.source for t in tasks if t.source})
    print(report(summary, items, title=f"Task eval — {picked.model} on the {picked.device.upper()}"
                                        f"{' (' + ', '.join(sources) + ' rows)' if sources else ''}", misses=misses))
    tag = picked.model.replace(":", "_").replace("/", "_")
    save(items, summary, paths.LOGS / f"eval-tasks-{tag}-{picked.device}.jsonl",
         {"model": picked.model, "device": picked.device, "sources": sources})
    budget = 1500 if picked.device == "gpu" else 3000
    good = summary.step_rate >= 0.9
    print(f"target ≥ 90 % of steps right: {'met' if good else 'NOT met'} ({summary.step_rate:.1%})")
    print(f"budget click by meaning ≤ {budget / 1000:g} s ({picked.device.upper()}): "
          f"{'met' if summary.pick_p95 <= budget else 'NOT met'} at p95 ({summary.pick_p95:.0f} ms)")
    sys.stdout.flush()
    return 0 if good else 1
