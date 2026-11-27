"""Does what was said so far sound finished? Pure: text in, verdict out.

The endpoint rule (SPEC §4) ends a short command after 300 ms of silence, which cuts
"open the ... Chrome settings" in two at the breath. The speech worker already decodes
the audio 160 ms into every pause, and the final can't be published before that decode
is back anyway, so the endpoint may as well read it: words that plainly aren't done
("switch to", "open the", "search for cats and") wait longer for the rest; everything
else ends exactly when it did before. Costs nothing when the sentence is complete.

No phrase lists of commands here: only the grammar of an unfinished English sentence.
"""

from __future__ import annotations

import re

_TOKEN = re.compile(r"[^\W_]+(?:['’][^\W_]+)*")

# Words a sentence doesn't end on. ("on", "in", "up", "off" are left out: "turn it on", "zoom in".)
_DANGLING = frozenset({
    "the", "a", "an", "my", "your", "our", "his", "her", "their", "its", "some", "any", "every",
    "to", "of", "for", "with", "from", "by", "at", "into", "onto", "about", "as", "than", "between", "like",
    "and", "or", "but", "then", "plus", "because", "if", "so", "while", "when", "where", "which", "who",
    "is", "are", "was", "be", "called", "named", "titled", "says", "saying", "that's", "thats", "it's",
    "um", "uh", "uhm", "er", "erm", "hmm",
    "very", "really", "another", "other",
})
# A command verb that is nothing without an object, when it's all that was said ("Nova, open").
_NEEDS_OBJECT = frozenset({
    "open", "launch", "switch", "go", "type", "write", "click", "press", "hit", "search", "find", "start", "run",
    "bring", "pull", "show", "navigate", "focus", "move", "put", "make", "set", "turn", "send", "tell", "ask",
    "google", "look", "take", "get", "give", "add", "create", "remind", "email", "message", "text", "call",
})
_ASKING = frozenset({("can", "you"), ("could", "you"), ("would", "you"), ("will", "you"), ("i", "want"), ("i", "need")})
_SHORT_WORDS = 4  # "hey Nova can you open": the bare-verb check only looks at openings this short
_ENDS_SENTENCE = (".", "!", "?")
_TRAILS_OFF = (",", ":", ";", "-", "–", "—", "…", "...")


def looks_unfinished(text: str) -> bool:
    """True if ``text`` is plainly the first part of something longer."""
    stripped = text.strip().rstrip("\"'”’)")
    words = [match.group().lower().replace("’", "'") for match in _TOKEN.finditer(stripped)]
    if not words:
        return False  # nothing decoded (a cough, a click): no reason to wait
    if stripped.endswith(_TRAILS_OFF):
        return True
    last = words[-1]
    if last in _DANGLING:
        return True
    if tuple(words[-2:]) in _ASKING:
        return True
    return len(words) <= _SHORT_WORDS and last in _NEEDS_OBJECT


def looks_finished(text: str) -> bool:
    """True if the decode ends like a sentence: Parakeet writes the full stop when the voice came down."""
    stripped = text.strip().rstrip("\"'”’)")
    return stripped.endswith(_ENDS_SENTENCE) and not stripped.endswith(_TRAILS_OFF) and not looks_unfinished(stripped.rstrip(".!?"))
