"""Logs: a plain app log (logs/nova.log) and JSONL utterance traces (logs/traces.jsonl, 2 MB x 10).

Every utterance will write one trace line with the SPEC §16 hop timings
(speech_end, stt_ms, tier, ctx_ms, llm_ms, exec_ms, verify_ms, total_ms).
"""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Callable
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

APP_FILE = "nova.log"
TRACE_FILE = "traces.jsonl"
# Fields that hold what Bhavya said or typed; dropped when logs.transcripts is false.
TRANSCRIPT_FIELDS = frozenset({"text", "raw_text", "changes", "transcript", "prompt"})

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_installed: tuple[logging.Handler, ...] = ()


class _RotatingFile(RotatingFileHandler):
    """RotatingFileHandler that survives Windows file locks.

    The stock rollover shifts the backups first and renames the live file last; if
    something holds the live file open (a log viewer, `Get-Content -Wait`), that rename
    fails, the record is dropped and every later write shifts and deletes another backup.
    Here the live file moves first; if it can't, writing continues and the next record retries.
    """

    def doRollover(self) -> None:
        if self.stream:
            self.stream.close()
            self.stream = None
        moving = self.baseFilename + ".rolling"
        try:
            os.replace(self.baseFilename, moving)
        except OSError:
            self.stream = self._open()
            return
        for i in range(self.backupCount - 1, 0, -1):
            source = f"{self.baseFilename}.{i}"
            if os.path.exists(source):
                os.replace(source, f"{self.baseFilename}.{i + 1}")
        os.replace(moving, f"{self.baseFilename}.1")
        self.stream = self._open()


def setup_app_log(logs_dir: Path, *, console: bool) -> Callable[[], None]:
    """Send the ``nova`` logger to logs/nova.log (1 MB x 3), and to stderr when there is a console.

    Replaces any earlier setup. Returns a function that removes and closes the handlers.
    """
    _remove_app_log()
    logs_dir = Path(logs_dir)
    logs_dir.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        _RotatingFile(logs_dir / APP_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8", delay=True)
    ]
    if console and sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    logger = logging.getLogger("nova")
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter(_FORMAT)
    for handler in handlers:
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    global _installed
    _installed = tuple(handlers)
    return _remove_app_log


def _remove_app_log() -> None:
    global _installed
    logger = logging.getLogger("nova")
    for handler in _installed:
        logger.removeHandler(handler)
        handler.close()
    _installed = ()


class TraceLog:
    """Append-only JSONL trace file. Thread-safe (the handler holds a lock per write)."""

    def __init__(
        self,
        logs_dir: Path,
        *,
        transcripts: bool = True,
        max_bytes: int = 2_000_000,
        backups: int = 10,
    ) -> None:
        logs_dir = Path(logs_dir)
        logs_dir.mkdir(parents=True, exist_ok=True)
        self.transcripts = transcripts
        self._handler = _RotatingFile(
            logs_dir / TRACE_FILE, maxBytes=max_bytes, backupCount=backups, encoding="utf-8", delay=True
        )
        self._handler.setFormatter(logging.Formatter("%(message)s"))

    def write(self, **fields: Any) -> dict[str, Any]:
        """Append one line: a local ISO timestamp plus ``fields``. Returns what was logged."""
        record = {"ts": datetime.now().astimezone().isoformat(timespec="milliseconds"), **fields}
        if not self.transcripts:
            record = {key: value for key, value in record.items() if key not in TRANSCRIPT_FIELDS}
        line = json.dumps(record, ensure_ascii=False, default=str)
        self._handler.handle(logging.makeLogRecord({"msg": line, "levelno": logging.INFO, "levelname": "INFO"}))
        return record

    def close(self) -> None:
        self._handler.close()
