"""`nova latency`: p50 / p95 per path from logs/traces.jsonl (SPEC §16).

"start" = end of speech (key release counts) -> the first action began; "total" = end of
speech -> done (app start-up and the like included). Typed `nova say` commands have no
speech, so they're listed apart. Path rows (SPEC §16 budgets): tier 0, tier 1 GPU / CPU; talk-session clauses
(07b) have their own "session tier …" rows without the time they waited behind earlier clauses, which is
the "session queue wait" row.
Dictation rows (SPEC §11: a segment lands <= 1 s after the pause): p50 / p95 from the moment
the pause ended the segment to the text landing; "done" = from the end of his speech. A misheard
word the local model found is fixed in place later: its row says when the fix landed.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import paths
from .log import TRACE_FILE


TASK_PICK = "task pick (by meaning)"  # rows → the model's next step (SPEC §16: <= 1.5 s GPU, 3 s CPU)
TASK_STEP = "task step → next pick"  # acted (or his click seen) → settled → next pick answered (<= 2 s GPU)
GUIDE_RING = "guide click → next ring"  # his click seen → the next ring up (<= 0.8 s)
_HOPS = {"pick_ms": TASK_PICK, "step_ms": TASK_STEP, "ring_ms": GUIDE_RING}


@dataclass(frozen=True)
class Row:
    name: str
    count: int
    p50: float
    p95: float
    total_p50: float | None = None
    ok: int = 0


def percentile(values: Sequence[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return math.nan
    rank = (len(ordered) - 1) * q / 100
    low = math.floor(rank)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def read_records(path: Path) -> list[dict]:
    """The trace file and its rotated copies (oldest first)."""
    files = sorted(path.parent.glob(path.name + ".*"), key=lambda p: -int(p.suffix[1:]) if p.suffix[1:].isdigit() else 0)
    records = []
    for file in [*files, path]:
        try:
            lines = file.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                records.append(record)
    return records


def summarize(records: Iterable[dict]) -> list[Row]:
    starts: dict[str, list[float]] = {}
    totals: dict[str, list[float]] = {}
    oks: dict[str, int] = {}
    finals: list[float] = []
    dictated: dict[str, tuple[list[float], list[float], list[int]]] = {}
    hops: dict[str, list[float]] = {}
    for record in records:
        steps = record.get("steps") if record.get("kind") == "command" else None
        for step in steps if isinstance(steps, list) else ():
            for key, name in _HOPS.items():
                values = step.get(key) if isinstance(step, dict) else None
                if isinstance(values, list):
                    hops.setdefault(name, []).extend(
                        float(v) for v in values if isinstance(v, (int, float)) and not isinstance(v, bool))
        if record.get("kind") == "utterance" and isinstance(record.get("final_ms"), (int, float)):
            finals.append(float(record["final_ms"]))
        kind = record.get("kind")
        if kind in ("dictation", "dictation_fix") and not record.get("initial")                 and isinstance(record.get("after_pause_ms"), (int, float)):
            key = "dictation segment" if kind == "dictation" else "  misheard word fixed"
            after, total, landed = dictated.setdefault(key, ([], [], []))
            after.append(float(record["after_pause_ms"]))
            if isinstance(record.get("total_ms"), (int, float)):
                total.append(float(record["total_ms"]))
            landed.append(0 if record.get("landed") is False or record.get("fix_landed") is False else 1)
        if record.get("kind") != "command" or not isinstance(record.get("start_ms"), (int, float)):
            continue
        name = str(record.get("type") or "?")
        if record.get("asked"):
            name += " (asked first)"  # includes the time he took to answer
        if record.get("via") == "say":
            name = f"(typed) {name}"
        overall = "all typed" if record.get("via") == "say" else "all commands"
        tier = record.get("tier")
        path = "tier 0" if tier == "0" else (f"tier 1 {str(record.get('device') or '?').upper()}" if tier == "1" else "")
        if path and record.get("via") == "say":
            path = f"(typed) {path}"
        # A talk-session clause (07b) may wait behind earlier ones: its path row takes that wait out, and the wait
        # gets a row of its own.
        waited = record.get("queue_ms") if record.get("talk") else None
        waited = float(waited) if isinstance(waited, (int, float)) else 0.0
        if path and record.get("talk"):
            path = f"session {path}"
            if waited:
                starts.setdefault("session queue wait", []).append(waited)
        for key in (name,) if record.get("asked") else tuple(k for k in (name, overall, path) if k):
            starts.setdefault(key, []).append(float(record["start_ms"]) - (waited if key == path else 0.0))
            if isinstance(record.get("total_ms"), (int, float)):
                totals.setdefault(key, []).append(float(record["total_ms"]))
            oks[key] = oks.get(key, 0) + (1 if record.get("ok") else 0)
    rows = []
    if finals:
        rows.append(Row("speech → text", len(finals), percentile(finals, 50), percentile(finals, 95)))
    for key in ("dictation segment", "  misheard word fixed"):
        if key in dictated:
            after, total, landed = dictated[key]
            rows.append(Row(key, len(after), percentile(after, 50), percentile(after, 95),
                            percentile(total, 50) if total else None, sum(landed)))
    for name in sorted(starts, key=lambda n: (n.startswith(("all", "(typed)", "tier", "session")), n)):
        values = starts[name]
        total = totals.get(name)
        rows.append(Row(name, len(values), percentile(values, 50), percentile(values, 95),
                        percentile(total, 50) if total else None, oks.get(name, 0)))
    rows.extend(Row(name, len(hops[name]), percentile(hops[name], 50), percentile(hops[name], 95))
                for name in _HOPS.values() if hops.get(name))
    return rows


def render(rows: Sequence[Row]) -> str:
    if not rows:
        return "No commands in the log yet."
    lines = [f"{'path':<26} {'n':>4} {'start p50':>10} {'start p95':>10} {'done p50':>9} {'ok':>5}"]
    for row in rows:
        done = f"{row.total_p50:.0f}" if row.total_p50 is not None else ""
        ok = f"{row.ok}/{row.count}" if row.name != "speech → text" and row.name not in _HOPS.values() else ""
        lines.append(f"{row.name:<26} {row.count:>4} {row.p50:>10.0f} {row.p95:>10.0f} {done:>9} {ok:>5}")
    lines.append("ms from the end of speech (key release counts): start = first action began, done = finished.")
    if any(row.name in _HOPS.values() for row in rows):
        lines.append("task / guide rows: ms per step, not from speech — p50 / p95 in the start columns.")
    return "\n".join(lines)


def main(path: Path | None = None) -> int:
    print(render(summarize(read_records(path or paths.LOGS / TRACE_FILE))))
    return 0
