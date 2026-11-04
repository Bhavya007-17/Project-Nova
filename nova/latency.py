"""`nova latency`: p50 / p95 per path from logs/traces.jsonl (SPEC §16).

"start" = end of speech (key release counts) -> the first action began; "total" = end of
speech -> done (app start-up and the like included). Typed `nova say` commands have no
speech, so they're listed apart. Path rows (SPEC §16 budgets): tier 0, tier 1 GPU / CPU.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import paths
from .log import TRACE_FILE


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
    for record in records:
        if record.get("kind") == "utterance" and isinstance(record.get("final_ms"), (int, float)):
            finals.append(float(record["final_ms"]))
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
        for key in (name,) if record.get("asked") else tuple(k for k in (name, overall, path) if k):
            starts.setdefault(key, []).append(float(record["start_ms"]))
            if isinstance(record.get("total_ms"), (int, float)):
                totals.setdefault(key, []).append(float(record["total_ms"]))
            oks[key] = oks.get(key, 0) + (1 if record.get("ok") else 0)
    rows = []
    if finals:
        rows.append(Row("speech → text", len(finals), percentile(finals, 50), percentile(finals, 95)))
    for name in sorted(starts, key=lambda n: (n.startswith(("all", "(typed)", "tier")), n)):
        values = starts[name]
        total = totals.get(name)
        rows.append(Row(name, len(values), percentile(values, 50), percentile(values, 95),
                        percentile(total, 50) if total else None, oks.get(name, 0)))
    return rows


def render(rows: Sequence[Row]) -> str:
    if not rows:
        return "No commands in the log yet."
    lines = [f"{'path':<26} {'n':>4} {'start p50':>10} {'start p95':>10} {'done p50':>9} {'ok':>5}"]
    for row in rows:
        done = f"{row.total_p50:.0f}" if row.total_p50 is not None else ""
        ok = f"{row.ok}/{row.count}" if row.name != "speech → text" else ""
        lines.append(f"{row.name:<26} {row.count:>4} {row.p50:>10.0f} {row.p95:>10.0f} {done:>9} {ok:>5}")
    lines.append("ms from the end of speech (key release counts): start = first action began, done = finished.")
    return "\n".join(lines)


def main(path: Path | None = None) -> int:
    print(render(summarize(read_records(path or paths.LOGS / TRACE_FILE))))
    return 0
