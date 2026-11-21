"""Talk-session clauses (Stage 07b, SPEC §5): one final -> the ordered things he asked for, small talk marked.

Pure and cheap: no I/O, no model, nothing from ``nova.brain``. It runs in the state machine on every final.

    "Alright, can you open up the notes app for me and once you're there can you create a new note. Great, okay."
    -> "open up the notes app" (command) · "create a new note" (command) · "Great, okay" (ack)

How a final is cut:
1. Always at ``. ! ? ;``.
2. Inside a sentence at a comma or a continuation ("and", "and then", "then", "now", "so", "next", "after that",
   "also") - but only when what follows starts like a request ("open X", "can you X", "once you're there let's X"),
   so "open Chrome and Fusion" stays one clause for Tier 0 to chain.
3. Once a clause starts with a free-text verb ("type", "search", "google" ...) the rest of the sentence is its text.
4. Each clause's ``text`` loses leading discourse ("alright", "and once you're there"), one polite frame per round
   ("can you", "let's") and trailing politeness ("for me", "thanks").

The lexicons are closed classes of filler, acknowledgement and frame words - never commands (AGENTS.md rule 4).
``said`` and ``text`` are cut out of the final, never rewritten (the payload rule, SPEC §7.4).
"""

from __future__ import annotations

import re
from dataclasses import dataclass


def _phrases(*items: str) -> tuple[tuple[str, ...], ...]:
    """Normalized word tuples, longest first, so "lets move on and" wins over "lets"."""
    return tuple(sorted({tuple(item.split()) for item in items}, key=len, reverse=True))


# Commands start with one of these (decision 9 of the 07b blueprint): the gate's verbs, Tier 0's one-word verbs and
# everyday imperatives. A misheard verb ("clothes notepad") is not in here, so it isn't worded as a request.
IMPERATIVE_VERBS: frozenset[str] = frozenset({
    # the gate's command verbs (nova/gate.py _VERBS)
    "open", "launch", "start", "run", "close", "quit", "exit", "kill", "shut", "switch", "go", "bring", "pull",
    "fire", "boot", "focus", "navigate", "jump", "take", "get", "show", "minimize", "minimise", "maximize",
    "maximise", "restore", "snap", "move", "put", "make", "click", "press", "hit", "type", "write", "scroll",
    "search", "play", "pause", "resume", "unpause", "stop", "skip", "next", "previous", "mute", "unmute", "turn",
    "volume", "new", "reload", "refresh", "reopen", "keep", "hover", "numbers", "grid", "screenshot",
    # Tier 0's other verbs and fixed commands
    "gimme", "hide", "unmaximize", "unminimize", "center", "centre", "throw", "send", "stick", "shove", "place",
    "drag", "push", "dock", "toss", "slide", "chuck", "park", "tap", "undo", "redo", "copy", "paste", "cut", "save",
    "zoom", "crank", "bump", "raise", "increase", "lower", "decrease", "reduce", "boost", "dictate", "wake", "hop",
    "head", "flip", "swap",
    # everyday imperatives
    "create", "add", "set", "change", "give", "find", "tell", "read", "delete", "remove", "rename", "select",
    "google", "look", "check", "let", "note", "say",
})

# A clause that starts with one of these is free text to the end of its sentence ("type hello and then ...").
FREE_TEXT_VERBS: frozenset[str] = frozenset({"type", "write", "search", "google", "dictate", "note", "say"})

# Said only to be nice or to keep talking: never what he wants done.
ACKS: tuple[tuple[str, ...], ...] = _phrases(
    "great", "nice", "cool", "awesome", "perfect", "good", "sweet", "excellent", "wonderful", "amazing", "lovely",
    "beautiful", "brilliant", "fantastic", "wow", "okay", "ok", "alright", "all right", "thanks", "thank you",
    "thank you so much", "thanks a lot", "got it", "sounds good", "that works", "thats great", "good job",
    "nice one", "yes", "yeah", "yep", "no", "right", "very good", "great job", "great work", "nice work",
    "good work", "well done", "nicely done", "nice job",
)

# Leading words that carry the talk along, not the command. Every acknowledgement counts too ("great, now open X").
DISCOURSE: tuple[tuple[str, ...], ...] = _phrases(
    "lets move on and", "lets move on", "moving on", "once youre there", "once you are there", "when youre there",
    "after that", "and then", "and now", "and also", "alright", "all right", "okay", "ok", "um", "uh", "uhm", "er",
    "erm", "hmm", "mm", "so", "and", "then", "now", "also", "next", "well", "oh", "right", "yeah",
    # What is left of a polite frame when he says it on its own ("go ahead", "let's move on"): never a command.
    "go ahead",
    *(" ".join(ack) for ack in ACKS),
)

# One of these may open the request itself; cut once per round, after the discourse.
POLITE: tuple[tuple[str, ...], ...] = _phrases(
    "can you", "could you", "would you", "will you", "can u", "please", "just", "go ahead and", "i need you to",
    "i want you to", "id like you to", "lets", "let us",
)

# Politeness and fillers at the end of a clause ("open the calculator too"). Free-text clauses keep theirs.
TRAILING: tuple[tuple[str, ...], ...] = _phrases(
    "for me", "please", "thanks", "thank you", "real quick", "okay", "ok", "too", "as well", "also",
)

# A clause boundary only where he said it (comma or not) and what follows starts like a request.
_CONTINUATIONS: tuple[tuple[str, ...], ...] = _phrases(
    "and", "and then", "then", "now", "and now", "so", "next", "after that", "also", "and also",
)
# A polite frame anywhere makes a clause a request ("inside this note let's make ..."); "just" alone doesn't.
_FRAMES: tuple[tuple[str, ...], ...] = tuple(frame for frame in POLITE if frame != ("just",))
# Small talk at the start of a clause only before a pause: "Next, open Chrome" / "Right, open Chrome", but "next tab"
# and "right click on the file" are commands.
_PAUSE_ONLY = frozenset({"next", "right"})
# Words that are filler on their own, for "only acknowledgements" ("um, great, thanks").
_FILLER_WORDS = frozenset(phrase[0] for phrase in DISCOURSE if len(phrase) == 1) - frozenset(a[0] for a in ACKS)
_FRAME_REACH = 6  # a frame this far into the next part still marks a new request ("inside this new note let's ...")

# Words keep inner apostrophes, dots and hyphens ("you're", "x.com", "wi-fi"): a dot inside a word ends nothing.
_TOKEN = re.compile(r"(?P<word>[^\W_]+(?:['’.\-][^\W_]+)*)|(?P<end>[.!?;]+)|(?P<pause>[,…]+)")
_HARD, _SOFT = "end", "pause"


@dataclass(frozen=True)
class Clause:
    said: str  # the clause as he said it (between its continuations), edge punctuation trimmed; a slice of the final
    text: str  # what to act on: leading discourse / polite frame and trailing politeness cut off; "" = nothing
    kind: str  # "command" | "ack" (only acknowledgements) | "empty" (nothing left)
    request: bool  # worded as a request (is_request(said))


@dataclass(frozen=True)
class _Tok:
    word: str  # normalized; "" for punctuation
    start: int
    end: int
    mark: str = ""  # punctuation: "end" (. ! ? ;) or "pause" (, …)


def _norm(word: str) -> str:
    # Lowercase without apostrophes, as Tier 0 tokenizes ("Let's" -> "lets", "you're" -> "youre").
    return word.lower().replace("’", "").replace("'", "")


def _tokens(text: str) -> list[_Tok]:
    out: list[_Tok] = []
    for m in _TOKEN.finditer(text):
        if m.group("word"):
            out.append(_Tok(_norm(m.group()), m.start(), m.end()))
        else:
            out.append(_Tok("", m.start(), m.end(), _HARD if m.group("end") else _SOFT))
    return out


def _skip_marks(toks: list[_Tok], i: int, end: int) -> int:
    while i < end and toks[i].mark:
        i += 1
    return i


def _by_first(phrases: tuple[tuple[str, ...], ...]) -> dict[str, tuple[tuple[str, ...], ...]]:
    # Phrases grouped by their first word (still longest first): a lookup instead of a scan, on every final.
    grouped: dict[str, list[tuple[str, ...]]] = {}
    for phrase in phrases:
        grouped.setdefault(phrase[0], []).append(phrase)
    return {word: tuple(group) for word, group in grouped.items()}


_Table = dict[str, tuple[tuple[str, ...], ...]]
_ACKS_T, _DISCOURSE_T, _POLITE_T = _by_first(ACKS), _by_first(DISCOURSE), _by_first(POLITE)
_CONTINUATIONS_T, _FRAMES_T = _by_first(_CONTINUATIONS), _by_first(_FRAMES)


def _match(toks: list[_Tok], i: int, end: int, table: _Table) -> int:
    """How many tokens the longest phrase at ``i`` covers (words in a row, no punctuation between), else 0."""
    if i >= end:
        return 0
    for phrase in table.get(toks[i].word, ()):
        n = len(phrase)
        if i + n <= end and all(toks[i + k].word == phrase[k] for k in range(1, n)):
            # Pause-only words count as discourse before a pause; as acknowledgements ("yeah right") they always do.
            if (n == 1 and phrase[0] in _PAUSE_ONLY and table is _DISCOURSE_T and i + 1 < end
                    and not toks[i + 1].mark):
                continue
            return n
    return 0


def _discourse_end(toks: list[_Tok], i: int, end: int) -> int:
    while True:
        i = _skip_marks(toks, i, end)
        n = _match(toks, i, end, _DISCOURSE_T)
        if not n:
            return i
        i += n


def _lead_end(toks: list[_Tok], i: int, end: int) -> int:
    """Index of the first word after leading discourse and polite frames ("and once you're there can you" ...)."""
    for _ in range(8):
        before = i
        i = _discourse_end(toks, i, end)
        i += _match(toks, i, end, _POLITE_T)
        if i == before:
            break
    return _skip_marks(toks, i, end)


def _trail_start(toks: list[_Tok], first: int, end: int) -> int:
    """One past the last word to keep: trailing politeness ("for me", ", thanks") cut, never below ``first``."""
    while True:
        while end > first and toks[end - 1].mark:
            end -= 1
        for phrase in TRAILING:
            n = len(phrase)
            if end - n >= first + 1 and all(toks[end - n + k].word == phrase[k] for k in range(n)):
                end -= n
                break
        else:
            return end


def _text_span(toks: list[_Tok], i: int, end: int) -> tuple[int, int] | None:
    """Token range of a clause's text, or None when nothing is left."""
    first = _lead_end(toks, i, end)
    if first >= end:
        return None
    last = end if toks[first].word in FREE_TEXT_VERBS else _trail_start(toks, first, end)
    while last > first and toks[last - 1].mark:
        last -= 1
    return (first, last) if last > first else None


def _starts_like_request(toks: list[_Tok], i: int, end: int) -> bool:
    i = _discourse_end(toks, i, end)
    return i < end and (toks[i].word in IMPERATIVE_VERBS or _match(toks, i, end, _POLITE_T) > 0)


def _frame_then_verb(toks: list[_Tok], i: int, end: int) -> bool:
    """A polite frame and then a verb near the start ("inside this new note let's make the title ...")."""
    words = [k for k in range(i, end) if not toks[k].mark][:_FRAME_REACH]
    for k in words:
        n = _match(toks, k, end, _FRAMES_T)
        if n:
            after = _skip_marks(toks, k + n, end)
            while after < end and toks[after].word in ("please", "just"):
                after += 1
            if after < end and toks[after].word in IMPERATIVE_VERBS:
                return True
    return False


def _covered_by_acks(toks: list[_Tok]) -> bool:
    words = [t for t in toks if not t.mark]
    i, acks = 0, 0
    while i < len(words):
        n = _match(words, i, len(words), _ACKS_T)
        if n:
            i, acks = i + n, acks + 1
        elif words[i].word in _FILLER_WORDS:
            i += 1
        else:
            return False
    return acks > 0


def _sentence_end(toks: list[_Tok], i: int) -> int:
    while i < len(toks) and toks[i].mark != _HARD:
        i += 1
    return i


def _clause(final: str, toks: list[_Tok], i: int, end: int) -> Clause | None:
    words = [k for k in range(i, end) if not toks[k].mark]
    if not words:
        return None
    said = final[toks[words[0]].start : toks[words[-1]].end]
    span = _text_span(toks, i, end)
    text = "" if span is None else final[toks[span[0]].start : toks[span[1] - 1].end]
    part = toks[words[0] : words[-1] + 1]
    if _covered_by_acks(part):
        kind = "ack"
    elif not text:
        kind = "empty"
    else:
        kind = "command"
    return Clause(said, text, kind, _request(part))


def split(final: str) -> tuple[Clause, ...]:
    """Every clause of ``final`` in order, small talk and empty ones included (the caller drops them)."""
    toks = _tokens(final)
    found: list[Clause] = []
    start = 0
    i = 0
    while i < len(toks):
        tok = toks[i]
        if tok.mark == _HARD:
            clause = _clause(final, toks, start, i)
            if clause is not None:
                found.append(clause)
            start = i = i + 1
            continue
        rest = i + 1 if tok.mark else i
        n = _match(toks, rest, len(toks), _CONTINUATIONS_T) if rest < len(toks) and not toks[rest].mark else 0
        if (tok.mark or n) and i > start:
            first = _lead_end(toks, start, i)
            locked = first < i and toks[first].word in FREE_TEXT_VERBS
            nxt = rest + n
            stop = _sentence_end(toks, nxt)
            # A leading frame is never cut at its own "and" ("go ahead and minimize ..."): the lead read over the
            # whole sentence must end before this boundary.
            inside_lead = _lead_end(toks, start, stop) > i
            if (first < i and not locked and not inside_lead
                    and (_starts_like_request(toks, nxt, stop) or _frame_then_verb(toks, nxt, stop))):
                clause = _clause(final, toks, start, i)
                if clause is not None:
                    found.append(clause)
                start = i = nxt
                continue
        i += 1
    clause = _clause(final, toks, start, len(toks))
    if clause is not None:
        found.append(clause)
    return tuple(found)


def strip(said: str) -> str:
    """What to act on in one clause: ``said`` without leading discourse, polite frames and trailing politeness."""
    toks = _tokens(said)
    span = _text_span(toks, 0, len(toks))
    return "" if span is None else said[toks[span[0]].start : toks[span[1] - 1].end]


def is_acknowledgement(text: str) -> bool:
    """Only acknowledgements ("great", "nice, okay", "cool, thank you"), maybe with fillers. Filler alone isn't one."""
    return _covered_by_acks(_tokens(text))


def _request(toks: list[_Tok]) -> bool:
    end = len(toks)
    first = _discourse_end(toks, 0, end)
    if first < end and toks[first].word in IMPERATIVE_VERBS:
        return True
    return any(_match(toks, k, end, _FRAMES_T) for k in range(end) if not toks[k].mark)


def is_request(text: str) -> bool:
    """Worded as a request: a command verb first (after discourse), or a polite frame anywhere ("can you ...")."""
    return _request(_tokens(text))


def starts_like_request(text: str) -> bool:
    """After leading discourse: a command verb or a polite frame first."""
    toks = _tokens(text)
    return _starts_like_request(toks, 0, len(toks))
