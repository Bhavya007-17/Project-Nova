"""What he says while the island asks (SPEC §6). Pure.

Confirm: "yes / do it / confirm" or "no / cancel". Clarify: "the second one", "two",
"number three", "the last one", or (part of) a choice's name.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from rapidfuzz import fuzz

_WORD = re.compile(r"[a-z0-9']+")
_YES = frozenset({"yes", "yeah", "yep", "yup", "sure", "confirm", "confirmed", "ok", "okay", "correct", "affirmative",
                  "absolutely", "definitely", "yea", "ya"})
_YES_PHRASES = (("do", "it"), ("go", "ahead"), ("go", "for", "it"), ("send", "it"), ("submit", "it"), ("please", "do"),
                ("that", "one"), ("thats", "right"), ("sounds", "good"))
_NO = frozenset({"no", "nope", "nah", "cancel", "stop", "dont", "wait", "abort", "negative"})
_NO_PHRASES = (("never", "mind"), ("forget", "it"), ("not", "that"), ("hold", "on"))
_ORDINALS = {"first": 0, "one": 0, "1": 0, "1st": 0, "second": 1, "two": 1, "2": 1, "2nd": 1, "third": 2, "three": 2,
             "3": 2, "3rd": 2, "fourth": 3, "four": 3, "4": 3, "4th": 3}
_FILLER = frozenset({"the", "one", "number", "option", "choice", "um", "uh", "please", "a", "that"})
_BEYOND = frozenset({"fifth", "five", "5", "5th", "sixth", "six", "6", "seventh", "eighth", "ninth", "tenth"})


def _words(text: str) -> list[str]:
    return [w.replace("'", "") for w in _WORD.findall(text.lower())]


def _has(words: list[str], phrases: Sequence[tuple[str, ...]]) -> bool:
    return any(tuple(words[i : i + len(p)]) == p for p in phrases for i in range(len(words)))


def yes_no(text: str) -> bool | None:
    words = _words(text)
    if not words:
        return None
    if words[0] in _NO or _has(words, _NO_PHRASES):
        return False
    if words[0] in _YES or _has(words, _YES_PHRASES):
        return True
    return None


def pick(text: str, choices: Sequence[str]) -> int | None:
    """Which choice he picked (0-based), or None."""
    words = _words(text)
    if not words or not choices:
        return None
    if yes_no(text) is not None:
        return 0 if yes_no(text) and len(choices) == 1 else None
    for word in words:
        if word in _ORDINALS and word != "one":
            index = _ORDINALS[word]
            return index if index < len(choices) else None
        if word == "last":
            return len(choices) - 1
        if word in _BEYOND:
            return None
    if "one" in words and all(w in _FILLER for w in words):  # "one" / "number one" (not "the fifth one")
        return 0
    core = " ".join(w for w in words if w not in _FILLER)
    if not core:
        return None
    scored = [(fuzz.partial_ratio(core, choice.lower()) if len(core) >= 4 else fuzz.ratio(core, choice.lower()), i)
              for i, choice in enumerate(choices)]
    scored.sort(reverse=True)
    best, index = scored[0]
    if best >= 80 and (len(scored) == 1 or scored[1][0] < best - 10):
        return index
    return None
