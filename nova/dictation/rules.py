"""Dictation rules (SPEC §11). Pure: one segment's transcript in, the text to type out.

``parse`` turns what he said into what he meant, in this order:
1. spoken commands: "new line" / "new paragraph", "scratch that" / "delete that" (the last sentence of this
   segment, or the last segment already typed), punctuation said out loud ("comma", "question mark"...);
2. fillers: um / uh / erm / hmm always; "like" and "you know" only when set off by commas or opening a
   sentence; stutters on small words ("the the");
3. self-corrections: "Tuesday, no wait, Wednesday" keeps "Wednesday" (the marker must start a new clause, so
   "I said no, wait until tomorrow" is left alone);
4. tidy: spacing, doubled punctuation, capitals at sentence starts.

``split_stop`` finds "stop dictation" at the end of a segment. ``join`` fits a segment to the text before the
cursor (space or not, capital or not, a period speech-to-text put in the middle of a sentence). ``landed``
checks that text arrived at the cursor.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# --- tokens ------------------------------------------------------------------------------------------------------

_TOKEN = re.compile(
    r"(?P<word>"
    r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"  # e-mail
    r"|(?:[A-Za-z]\.){2,}"  # p.m., e.g., U.S.
    r"|(?:Mr|Mrs|Ms|Dr|Prof|St|vs|etc|Jr|Sr)\.(?=\s|$)"
    r"|\d+(?:[.,:]\d+)+(?:st|nd|rd|th|am|pm|%)?"  # 3.5, 1,000, 3:30
    r"|[^\W_]+(?:[-'’][^\W_]+)*"
    r")"
    r"|(?P<punct>\.\.\.|…|[.,!?;:—–\"“”()\[\]]|-(?=\s|$))"
)
_END = frozenset({".", "!", "?", "…", "..."})
_ATTACH_LEFT = frozenset({".", ",", "!", "?", ";", ":", "…", "...", ")", "]", "”"})
_ATTACH_RIGHT = frozenset({"(", "[", "“"})


@dataclass(frozen=True)
class Tok:
    text: str
    kind: str  # word | punct | break | scratch

    @property
    def low(self) -> str:
        return self.text.lower().replace("’", "'")


def _tokens(text: str) -> list[Tok]:
    out: list[Tok] = []
    for m in _TOKEN.finditer(text.replace("\r\n", "\n")):
        out.append(Tok(m.group(), "word" if m.group("word") else "punct"))
    return out


def _render(toks: list[Tok]) -> str:
    parts: list[str] = []
    quotes = 0
    glue = True  # no space before the next token
    for t in toks:
        if t.kind == "break":
            while parts and parts[-1] == " ":
                parts.pop()
            parts.append("\n")
            glue = True
            continue
        text = t.text
        if t.kind == "punct" and text == '"':
            opening = quotes % 2 == 0
            quotes += 1
            if not opening:
                parts.append(text)
                glue = False
                continue
        attach = t.kind == "punct" and text in _ATTACH_LEFT
        if not glue and not attach:
            parts.append(" ")
        parts.append(text)
        glue = t.kind == "punct" and (text in _ATTACH_RIGHT or text == '"')
    return "".join(parts)


# --- vocabulary --------------------------------------------------------------------------------------------------

_FILLERS = frozenset({"um", "umm", "uh", "uhh", "uhm", "er", "erm", "hmm", "hm", "mm", "mmm"})
_STUTTER = frozenset({"i", "the", "a", "an", "to", "and", "we", "it", "is", "of", "in", "on", "my", "so", "but", "you",
                      "he", "she", "they"})
_NO_COMMA_AFTER = frozenset({
    "the", "a", "an", "to", "of", "and", "or", "but", "with", "for", "in", "on", "at", "by", "from", "my", "your",
    "our", "their", "his", "her", "its", "this", "that", "these", "those", "is", "are", "was", "were", "be", "i", "we",
    "you", "they", "he", "she", "it", "will", "would", "can", "could", "should", "have", "has", "had", "do", "does",
    "did", "about", "into", "than", "because", "if", "as", "it's", "that's", "there's", "he's", "she's", "we're",
    "you're", "they're", "i'm", "was", "just", "really", "very",
})
_DETERMINERS = frozenset({"a", "an", "the", "this", "that", "these", "those", "each", "every", "one", "per", "any",
                          "some", "my", "your", "his", "her", "our", "their", "its", "no", "another"})
_PUNCT_WORDS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("question", "mark"), "?"), (("exclamation", "point"), "!"), (("exclamation", "mark"), "!"),
    (("full", "stop"), "."), (("semi", "colon"), ";"), (("period",), "."), (("comma",), ","), (("colon",), ":"),
    (("semicolon",), ";"),
)
_BREAKS: tuple[tuple[tuple[str, ...], int], ...] = (
    (("new", "paragraph"), 2), (("new", "line"), 1), (("next", "line"), 1), (("newline",), 1),
)
_SCRATCH = (("scratch", "that"), ("delete", "that"), ("strike", "that"))
_MARKERS = (("no", "sorry", "i", "mean"), ("sorry", "i", "mean"), ("no", "i", "mean"), ("no", "wait"),
            ("wait", "no"), ("no", "sorry"), ("or", "rather"))
_CLAUSE = frozenset({",", ".", ";", "—", "–", "-", "!", "?"})
# Words that only start a sentence because a pause cut it: written lowercase when the sentence goes on.
COMMON_LOWER = frozenset("""
a an the and or but nor so yet because since as at by for from in into of off on onto to with without about above
after against along among around before behind below beneath beside between beyond during except inside like near
outside over past through toward towards under until unless upon via within than then that this these those which who
whom whose what when where while why how if whether though although is are was were be been being am it its we you
they he she them him her us our your their my me not no just also too very really maybe even still only all some any
each every both either neither more most much many such there here now once again later soon instead otherwise
however therefore do does did done have has had will would can could should may might must shall let lets let's
well next first last please plus including especially up down out
""".split())
# A sentence can't end on these: a period right after one is speech-to-text cutting the sentence at a pause.
_CONTINUES = frozenset({"the", "a", "an", "to", "of", "and", "or", "but", "with", "for", "from", "by", "my", "your",
                        "our", "their", "his", "her", "its", "than", "because", "is", "are", "was", "were", "these",
                        "those", "into", "onto", "at"})


def _is_filler(t: Tok) -> bool:
    return t.kind == "word" and t.low in _FILLERS and not (len(t.text) > 1 and t.text.isupper())


def _match(toks: list[Tok], i: int, phrase: tuple[str, ...]) -> int:
    """End index (exclusive) if the words of ``phrase`` start at ``i`` (commas between them allowed), else 0."""
    j = i
    for k, word in enumerate(phrase):
        if k:
            while j < len(toks) and toks[j].kind == "punct" and toks[j].text == ",":
                j += 1
        if j >= len(toks) or toks[j].kind != "word" or toks[j].low != word:
            return 0
        j += 1
    return j


def _prev_word(toks: list[Tok], i: int) -> Tok | None:
    return toks[i - 1] if i > 0 and toks[i - 1].kind == "word" else None


# --- parse ---------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Parsed:
    text: str  # what to type ("" = nothing); may hold "\n" from "new line" / "new paragraph"
    scratch: int = 0  # segments already typed that "scratch that" takes back first


def _commands(toks: list[Tok]) -> list[Tok]:
    out: list[Tok] = []
    i = 0
    while i < len(toks):
        t = toks[i]
        if t.kind != "word":
            out.append(t)
            i += 1
            continue
        noun = t.low in _DETERMINERS or (out and out[-1].kind == "word" and out[-1].low in _DETERMINERS)
        made = None
        for phrase, mark in _PUNCT_WORDS:
            end = _match(toks, i, phrase)
            if end and not noun:
                while out and out[-1].kind == "punct" and out[-1].text in (",", ".", ";", ":"):
                    out.pop()  # speech-to-text's own mark where he said one
                made = ([Tok(mark, "punct")], end)
                break
        if made is None:
            for phrase, count in _BREAKS:
                end = _match(toks, i, phrase)
                if end and not noun:
                    made = ([Tok("\n", "break")] * count, end)
                    break
        if made is None:
            for phrase in _SCRATCH:
                end = _match(toks, i, phrase)
                if end:
                    made = ([Tok("", "scratch")], end)
                    break
        if made is None:
            out.append(t)
            i += 1
            continue
        out.extend(made[0])
        i = made[1]
        while i < len(toks) and toks[i].kind == "punct" and toks[i].text in (",", ".", ";", ":", "!", "?"):
            i += 1  # the mark speech-to-text put after the command word
    return out


def _sentence_start(toks: list[Tok], i: int) -> bool:
    j = i - 1
    while j >= 0 and toks[j].kind == "punct" and toks[j].text in ('"', "“", "(", "["):
        j -= 1
    return j < 0 or toks[j].kind == "break" or (toks[j].kind == "punct" and toks[j].text in _END)


def _comma(toks: list[Tok], i: int) -> bool:
    return 0 <= i < len(toks) and toks[i].kind == "punct" and toks[i].text == ","


def _drop(toks: list[Tok], i: int, n: int) -> list[Tok]:
    """Remove ``n`` tokens at ``i`` and the commas that only set them off. A comma in front stays when it could be
    real ("apples, um, oranges"), not after a word no comma follows ("the, uh, robotics")."""
    start, end = i, i + n
    before, after = _comma(toks, i - 1), _comma(toks, end)
    lone = i >= 2 and toks[i - 2].kind == "word" and toks[i - 2].low in _NO_COMMA_AFTER
    if after:
        end += 1
        if before and lone:
            start -= 1
    elif before and (lone or end >= len(toks) or toks[end].kind != "word"):
        start -= 1  # "the, uh robotics" / "we went, um."
    if _sentence_start(toks, start):
        while _comma(toks, end) or (end < len(toks) and toks[end].kind == "punct" and toks[end].text == ";"):
            end += 1
    return toks[:start] + toks[end:]


def _fillers(toks: list[Tok]) -> list[Tok]:
    i = 0
    while i < len(toks):
        t = toks[i]
        if _is_filler(t):
            whole = _sentence_start(toks, i) and i + 1 < len(toks) and toks[i + 1].kind == "punct" \
                and toks[i + 1].text in _END
            toks = toks[: i] + toks[i + 2 :] if whole else _drop(toks, i, 1)
            i = max(0, i - 1)
            continue
        if t.kind == "word" and t.low in ("like", "you"):
            n = 2 if t.low == "you" else 1
            if n == 2 and not (i + 1 < len(toks) and toks[i + 1].kind == "word" and toks[i + 1].low == "know"):
                i += 1
                continue
            before = i > 0 and toks[i - 1].kind == "punct" and toks[i - 1].text == ","
            after_tok = toks[i + n] if i + n < len(toks) else None
            after_comma = after_tok is not None and after_tok.kind == "punct" and after_tok.text == ","
            after_end = after_tok is None or (after_tok.kind == "punct" and after_tok.text in _END)
            starts = _sentence_start(toks, i)
            if (before and (after_comma or (n == 2 and after_end))) or (starts and after_comma):
                if before and after_end:
                    toks = toks[: i - 1] + toks[i + n :]  # ", you know." -> "."
                else:
                    toks = _drop(toks, i, n)
                i = max(0, i - 1)
                continue
        if (t.kind == "word" and t.low in _STUTTER and i + 1 < len(toks)):
            j = i + 1
            if toks[j].kind == "punct" and toks[j].text == ",":
                j += 1
            if j < len(toks) and toks[j].kind == "word" and toks[j].low == t.low:
                toks = toks[:i] + toks[j:]
                continue
        i += 1
    return toks


def _clause_start(toks: list[Tok], end: int) -> int:
    """First token of the sentence that ends at ``end`` (exclusive)."""
    j = end - 1
    while j >= 0 and not (toks[j].kind == "break" or (toks[j].kind == "punct" and toks[j].text in _END)):
        j -= 1
    return j + 1


def _corrections(toks: list[Tok]) -> list[Tok]:
    i = 1
    while i < len(toks):
        marker_end = 0
        if toks[i].kind == "word" and toks[i - 1].kind == "punct" and toks[i - 1].text in _CLAUSE:
            for marker in _MARKERS:
                marker_end = _match(toks, i, marker)
                if marker_end:
                    break
        if not marker_end:
            i += 1
            continue
        lead = i - 1  # the punctuation in front of the marker
        start = _clause_start(toks, lead if toks[lead].text in _END else i)
        x = [k for k in range(start, lead) if toks[k].kind == "word"]
        after = marker_end
        while after < len(toks) and toks[after].kind == "punct" and toks[after].text in (",", ".", "—", "–", "-", ":"):
            after += 1
        y = after if after < len(toks) and toks[after].kind == "word" else None
        if not x or y is None:
            i += 1
            continue
        # The correction restarts at the last word it repeats ("send it to John, no wait, to Mary"); otherwise it
        # replaces the last word ("Tuesday, no wait, Wednesday" / "five, no wait, six chairs").
        head = toks[y].low
        cut = next((k for k in reversed(x) if toks[k].low == head), x[-1])
        toks = toks[:cut] + toks[after:]
        i = cut + 1
    return toks


def correction_lead(said: str) -> str:
    """A segment that opens with a correction marker corrects the segment before it (a pause split "Tuesday, no
    wait, Wednesday"): "only" = just the marker ("No wait.": the correction comes next), "lead" = marker, a
    comma / period, then words ("No wait, Wednesday."), "" = neither ("No wait times today")."""
    toks = _tokens(said)
    i = 0
    while i < len(toks) and toks[i].kind == "punct":
        i += 1
    for marker in _MARKERS:
        end = _match(toks, i, marker)
        if not end:
            continue
        rest = [t for t in toks[end:] if t.kind == "word"]
        if not rest:
            return "only"
        return "lead" if toks[end].kind == "punct" and toks[end].text in _CLAUSE else ""
    return ""


def _scratch(toks: list[Tok]) -> tuple[list[Tok], int]:
    taken = 0
    while True:
        p = next((k for k, t in enumerate(toks) if t.kind == "scratch"), None)
        if p is None:
            return toks, taken
        words = [k for k in range(p) if toks[k].kind == "word"]
        if words:
            start = _clause_start(toks, words[-1] + 1)
            toks = toks[:start] + toks[p + 1 :]
        elif any(t.kind == "break" for t in toks[:p]):
            last = max(k for k in range(p) if toks[k].kind == "break")
            first = last
            while first > 0 and toks[first - 1].kind == "break":
                first -= 1
            toks = toks[:first] + toks[p + 1 :]
        else:
            taken += 1
            toks = toks[p + 1 :]


def _tidy(toks: list[Tok]) -> list[Tok]:
    out: list[Tok] = []
    for t in toks:
        if t.kind == "punct" and t.text not in ('"', "“", "(", "["):
            if not out or out[-1].kind == "break":
                continue  # nothing for it to follow
            if out[-1].kind == "punct" and out[-1].text in (",", ";", ":") and t.text in (*_END, ",", ";", ":"):
                out[-1] = t  # ", ." -> "."
                continue
            if out[-1].kind == "punct" and out[-1].text in _END and t.text in (*_END, ","):
                continue  # ".." / "?." / ".,"
        out.append(t)
    return out


def _capitals(toks: list[Tok]) -> list[Tok]:
    out = list(toks)
    for i, t in enumerate(out):
        if t.kind == "word" and _sentence_start(out, i) and t.text[:1].islower() and t.text.islower():
            out[i] = Tok(t.text[0].upper() + t.text[1:], "word")
    return out


def parse(said: str) -> Parsed:
    toks = _commands(_tokens(said))
    toks = _fillers(toks)
    toks = _corrections(toks)
    toks, taken = _scratch(toks)
    toks = _capitals(_tidy(toks))
    if not any(t.kind in ("word", "break") for t in toks):
        return Parsed("", taken)
    return Parsed(_render(toks), taken)


# --- stop -----------------------------------------------------------------------------------------------------------

_STOPS = (("stop", "dictation"), ("stop", "dictating"), ("stop", "the", "dictation"), ("end", "dictation"),
          ("end", "dictating"), ("end", "the", "dictation"), ("finish", "dictation"), ("dictation", "off"),
          ("turn", "off", "dictation"), ("turn", "off", "the", "dictation"), ("turn", "dictation", "off"),
          ("stop", "typing"))
# Only when that's all he said: "end dictation" comes out of speech-to-text as "And dictation." (seen live), and
# "Nova, stop".
_STOPS_ALONE = frozenset({("and", "dictation"), ("and", "dictating"), ("and", "the", "dictation"), ("in", "dictation"),
                          ("nova", "stop"), ("stop", "nova"), ("nova", "end"), ("okay", "stop", "dictation")})
_STOP_TAIL = frozenset({"please", "now", "thanks", "thank", "you"})


def split_stop(said: str) -> tuple[str, bool]:
    """(what to type first, True) when the segment ends with "stop dictation" (or "Nova, stop dictation"); the
    phrase must stand on its own: at the start, after punctuation, or after the wake name."""
    found = list(re.finditer(r"[^\W_]+(?:['’][^\W_]+)*", said))
    words = [m.group().lower() for m in found]
    end = len(words)
    while end and words[end - 1] in _STOP_TAIL:
        end -= 1
    if tuple(words[:end]) in _STOPS_ALONE:
        return "", True
    for phrase in _STOPS:
        n = len(phrase)
        if end < n or tuple(words[end - n : end]) != phrase:
            continue
        start = end - n
        if start and words[start - 1] == "nova":
            start -= 1
        cut = found[start].start()
        head = said[:cut]
        if head.strip() and not re.search(r"[,.;:!?—–-]\s*$", head):
            return said, False  # "I want to stop the dictation": talk, not a command
        return head.rstrip().rstrip(",;:—–-").rstrip(), True
    return said, False


# --- joins at the cursor --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Joined:
    insert: str  # what to paste
    erase: int = 0  # characters before the cursor to take back first (a period that cut a sentence in half)


def _first_word(text: str) -> re.Match[str] | None:
    return re.match(r"\s*([^\W_]+(?:['’][^\W_]+)*)", text)


def capitalize(text: str) -> str:
    m = _first_word(text)
    if m is None or not m.group(1).islower():
        return text
    k = m.start(1)
    return text[:k] + text[k].upper() + text[k + 1 :]


def _lower(text: str) -> str:
    m = _first_word(text)
    if m is None:
        return text
    word = m.group(1)
    if word.lower().replace("’", "'") not in COMMON_LOWER or not (word[0].isupper() and word[1:] == word[1:].lower()):
        return text
    if word in ("I", "I'm", "I'll", "I'd", "I've") or word.startswith(("I'", "I’")):
        return text
    k = m.start(1)
    return text[:k] + text[k].lower() + text[k + 1 :]


def join(before: str | None, text: str, *, fix_period: bool = True) -> Joined:
    """Fit ``text`` to ``before`` (the text before the cursor; "" = an empty field, None = unknown). ``fix_period``:
    the text before is Nova's own last segment, so a period that cut its sentence in half may be taken back."""
    if not text:
        return Joined("")
    if before is None or text.startswith("\n"):
        return Joined(text)
    tail = before.rstrip(" \t ")
    spaced = len(tail) < len(before)
    if not tail or tail.endswith(("\n", "\r")):
        return Joined(capitalize(text.lstrip(" ")))
    if tail[-1] in "([{“‘" or (tail[-1] in "\"'" and (len(tail) == 1 or tail[-2].isspace())):
        return Joined(text.lstrip(" "))
    space = "" if spaced else " "
    core = tail.rstrip("\"'”’)]")
    if core and core[-1] in ".!?…":
        if fix_period and core[-1] == "." and not spaced and core == tail:
            last = re.search(r"([^\W_]+)\.$", core)
            if last is not None and last.group(1).lower() in _CONTINUES:
                return Joined(" " + _lower(text), erase=1)
        return Joined(space + capitalize(text))
    if core and core[-1] == ":":
        return Joined(space + text)
    return Joined(space + _lower(text))


def _norm(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace(" ", " ").replace("​", "")
    return re.sub(r"[ \t]+", " ", text)


def landed(field_before_cursor: str | None, inserted: str, tail: int = 60) -> bool:
    """Does the text before the cursor now end with what was inserted? (False when there's nothing to compare.)"""
    if field_before_cursor is None:
        return False
    want = _norm(inserted).strip()
    if not want:
        return False
    return _norm(field_before_cursor).rstrip().endswith(want[-tail:])
