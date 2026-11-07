"""Dictation history (SPEC §11): the last 50 dictations, so "paste my last dictation" can bring one back.

One entry per run of text typed into one place; text that couldn't be typed is saved as an entry of its own
(``landed`` False). Local JSONL in data/, rewritten whole on each change (50 short lines).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, replace
from pathlib import Path

_log = logging.getLogger(__name__)

LIMIT = 50


@dataclass(frozen=True)
class Entry:
    at: str  # local ISO time it started
    text: str
    app: str = ""  # where it went ("Notepad", "Google Docs")
    landed: bool = True  # False: couldn't be typed — saved instead


class History:
    def __init__(self, path: Path, limit: int = LIMIT) -> None:
        self._path = Path(path)
        self._limit = limit
        self._lock = threading.Lock()
        self._entries: tuple[Entry, ...] = self._load()

    def _load(self) -> tuple[Entry, ...]:
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return ()
        out = []
        for line in lines:
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if isinstance(data, dict) and isinstance(data.get("text"), str):
                out.append(Entry(str(data.get("at", "")), data["text"], str(data.get("app", "")),
                                 bool(data.get("landed", True))))
        return tuple(out[-self._limit :])

    def entries(self) -> tuple[Entry, ...]:
        return self._entries

    def last(self) -> Entry | None:
        return self._entries[-1] if self._entries else None

    def add(self, entry: Entry) -> None:
        with self._lock:
            self._entries = (*self._entries, entry)[-self._limit :]
            self._save()

    def update_last(self, entry: Entry) -> None:
        with self._lock:
            self._entries = (*self._entries[:-1], entry) if self._entries else (entry,)
            self._save()

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temp = self._path.with_suffix(self._path.suffix + ".tmp")
            temp.write_text("".join(json.dumps(asdict(e), ensure_ascii=False) + "\n" for e in self._entries),
                            encoding="utf-8")
            os.replace(temp, self._path)
        except OSError as error:
            _log.warning("couldn't save the dictation history: %s", error)


def grown(entry: Entry, text: str) -> Entry:
    return replace(entry, text=entry.text + text)
