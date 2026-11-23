"""Prefetch (Stage 07d): take the front window's UIA snapshot while he is still talking, once the words so far start
like a guide or click request ("show me", "how do I", "click", "where's", "turn on"). The command's first look then
reuses it (``Pointer.prefetch``) and a cold Chromium tree is already built.

Never on every utterance: a UIA snapshot switches Chrome's accessibility tree on (GOTCHAS), and a snapshot taken
while a command runs would replace the one its clicks act on. Partials never execute; this only reads.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable

# Spoken starts, after punctuation becomes spaces ("where's" -> "where s", "right-click" -> "right click").
STARTS = (
    "show me", "how do i", "how do you", "how can i", "click", "right click", "double click",
    "where s", "where is", "where are", "turn on", "turn off",
)
# Words that may come first: the wake name, a greeting, a filler, a polite opening.
_LEAD = frozenset({"nova", "hey", "hi", "ok", "okay", "um", "uh", "so", "please", "can", "could", "would", "you"})
_KEEP = 16  # utterances remembered (one snapshot each)


def _words(text: str) -> list[str]:
    words = re.sub(r"[^a-z0-9]+", " ", text.lower()).split()
    while words and words[0] in _LEAD:
        words = words[1:]
    return words


def wants_snapshot(text: str) -> bool:
    """Do the words so far start like a request that will look at the window in front?"""
    spoken = " ".join(_words(text)) + " "
    return any(spoken.startswith(start + " ") for start in STARTS)


def _spawn(work: Callable[[], None]) -> None:
    threading.Thread(target=work, name="nova-prefetch", daemon=True).start()


class Prefetcher:
    """Partials in, at most one early snapshot per utterance out (``take`` runs on its own thread)."""

    def __init__(self, take: Callable[[], None], idle: Callable[[], bool], *,
                 spawn: Callable[[Callable[[], None]], None] = _spawn) -> None:
        self._take = take
        self._idle = idle  # no command running and no dictation typing
        self._spawn = spawn
        self._seen: list[int] = []
        self._lock = threading.Lock()

    def heard(self, utterance: int, text: str) -> bool:
        """A partial (bus thread): start the snapshot if it matches, nothing runs, and this utterance hasn't had one."""
        with self._lock:
            if utterance in self._seen or not wants_snapshot(text) or not self._idle():
                return False
            self._seen = [*self._seen[-(_KEEP - 1):], utterance]
        self._spawn(self._take)
        return True
