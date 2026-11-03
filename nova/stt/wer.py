"""Word error rate for the STT bench (pure).

Both sides are normalized the same way first: lowercase, punctuation dropped,
"p.m." -> "pm", spelled numbers -> digits ("twenty five" -> "25"). Errors are
word-level Levenshtein edits (rapidfuzz); WER = errors / reference words.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from rapidfuzz.distance import Levenshtein

_UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
}
_TEENS = {
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_WORD = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")


def normalize(text: str) -> list[str]:
    t = text.lower().replace("’", "'")
    t = re.sub(r"\b([ap])\.\s?m\b\.?", r"\1m", t)
    t = re.sub(r"(?<=\b[a-z])\.(?=[a-z]\b)", "", t)  # u.s. -> us
    t = t.replace("&", " and ").replace("%", " percent ")
    t = re.sub(r"[-/]", " ", t)
    words = _WORD.findall(t)
    out: list[str] = []
    i = 0
    while i < len(words):
        word = words[i]
        if word in _TENS:
            value = _TENS[word]
            if i + 1 < len(words) and words[i + 1] in _UNITS and _UNITS[words[i + 1]] > 0:
                value += _UNITS[words[i + 1]]
                i += 1
            out.append(str(value))
        elif word in _UNITS:
            out.append(str(_UNITS[word]))
        elif word in _TEENS:
            out.append(str(_TEENS[word]))
        else:
            out.append(word)
        i += 1
    return out


def word_errors(reference: str, hypothesis: str) -> tuple[int, int]:
    """(word edits, reference word count) after normalizing both."""
    ref, hyp = normalize(reference), normalize(hypothesis)
    return Levenshtein.distance(ref, hyp), len(ref)


def corpus_wer(pairs: Iterable[tuple[str, str]]) -> float:
    errors = words = 0
    for reference, hypothesis in pairs:
        e, n = word_errors(reference, hypothesis)
        errors += e
        words += n
    return errors / words if words else 0.0
