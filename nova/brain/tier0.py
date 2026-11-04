"""Tier 0 (SPEC §7.2): fast patterns, no AI, well under 5 ms.

A verb-first matcher, not a grammar: verb synonyms ("open / launch / pull up / bring up /
fire up / get me" -> open) + entity resolution against the catalog, plus a few fixed
templates (tabs, back / forward, scroll, volume, media, dictation, sleep). Commands chain
with "and / then / ," and a clause without a verb borrows the last one ("open Chrome and
Fusion", "put Fusion left and Claude right"). Two or more placements make one layout.

Text to type or search is cut from what he said (original casing and punctuation), so it is
always a substring of the transcript (SPEC §7.4). A name Tier 0 can't pin down becomes a
question with choices (a clarify plan), never a guess; with ``strict`` (always-listening)
only whole names and aliases count and anything unsure is simply not a command.
``parse`` returns None for what it doesn't understand: Tier 1 (Stage 05) takes it from there.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from ..actions.catalog import Catalog, Match, Target, sounds_like
from ..chrome import canvas
from ..gate import Resolved
from .plan import Plan, PlanError, Step, check, step

_log = logging.getLogger(__name__)

# --- tokens ---------------------------------------------------------------------------------------------------

_TLDS = ("com", "org", "net", "edu", "io", "ai", "gov", "co", "dev", "app", "me", "tv", "us", "uk", "ca", "de", "so",
         "gg", "xyz", "info", "ly")
_TOKEN = re.compile(
    r"(?P<domain>[A-Za-z0-9][A-Za-z0-9-]*(?:\.[A-Za-z0-9-]+)*\.(?:" + "|".join(_TLDS) + r"))(?![A-Za-z0-9])"
    r"|(?P<word>[A-Za-z0-9]+(?:['’][A-Za-z]+)*)"
    r"|(?P<plus>\+)"
    r"|(?P<punct>[,;:.!?])"
)


@dataclass(frozen=True)
class Tok:
    word: str  # lowercase, no apostrophes; "" for punctuation
    start: int
    end: int
    punct: str = ""


def tokenize(text: str) -> list[Tok]:
    toks: list[Tok] = []
    for m in _TOKEN.finditer(text):
        if m.group("domain"):
            toks.append(Tok(m.group().lower(), m.start(), m.end()))
        elif m.group("word"):
            toks.append(Tok(m.group().lower().replace("’", "").replace("'", ""), m.start(), m.end()))
        elif m.group("plus"):
            toks.append(Tok("plus", m.start(), m.end()))
        else:
            toks.append(Tok("", m.start(), m.end(), m.group()))
    out: list[Tok] = []
    i = 0
    while i < len(toks):  # "reddit dot com" -> reddit.com
        t = toks[i]
        if i + 2 < len(toks) and t.word and toks[i + 1].word == "dot" and toks[i + 2].word in _TLDS:
            out.append(Tok(f"{t.word}.{toks[i + 2].word}", t.start, toks[i + 2].end))
            i += 3
        else:
            out.append(t)
            i += 1
    return out


@dataclass(frozen=True)
class Clause:
    first: int  # token indexes (inclusive) into the utterance's tokens
    last: int
    words: tuple[str, ...]


_CONJUNCTIONS = (("and", "then"), ("and", "also"), ("after", "that"), ("and",), ("then",), ("also",))


def _conjunction_at(toks: Sequence[Tok], i: int) -> int:
    for conj in _CONJUNCTIONS:
        if tuple(t.word for t in toks[i : i + len(conj)]) == conj:
            return len(conj)
    return 0


def split_clauses(toks: Sequence[Tok]) -> list[Clause]:
    clauses: list[Clause] = []
    current: list[int] = []

    def flush() -> None:
        if current:
            clauses.append(Clause(current[0], current[-1], tuple(toks[j].word for j in current)))
            current.clear()

    i = 0
    while i < len(toks):
        if toks[i].punct:
            flush()
            i += 1
            continue
        n = _conjunction_at(toks, i)
        if n:
            flush()
            i += n
            continue
        current.append(i)
        i += 1
    flush()
    return clauses


# --- fillers -----------------------------------------------------------------------------------------------------

_FILLERS = frozenset({
    "um", "uh", "uhm", "er", "erm", "hmm", "mm", "yo", "hey", "ok", "okay", "so", "now", "alright", "well", "oh",
    "yeah", "please", "just", "quickly", "kindly", "right", "and", "then", "also", "but", "or",
})
_FRONT = (("to", "the", "front"), ("in", "the", "front"), ("in", "front"), ("up", "front"), ("to", "front"),
          ("forward",))  # "put notepad in front" = switch to it
_POLITE = sorted(
    [("can", "you"), ("could", "you"), ("would", "you"), ("will", "you"), ("can", "u"), ("could", "u"),
     ("go", "ahead", "and"), ("i", "need", "you", "to"), ("i", "want", "you", "to"), ("id", "like", "you", "to"),
     ("i", "would", "like", "you", "to"), ("lets",), ("let", "us"), ("would", "you", "mind")],
    key=len, reverse=True,
)
_TRAILING = sorted(
    [("please",), ("for", "me"), ("for", "now"), ("for", "a", "bit"), ("for", "a", "while"), ("now",),
     ("right", "now"), ("thanks",), ("thank", "you"), ("real", "quick"),
     ("really", "quick"), ("quickly",), ("asap",), ("if", "you", "can"), ("will", "you"), ("would", "you"),
     ("okay",), ("ok",), ("yeah",), ("but",), ("and",), ("so",), ("or",), ("um",), ("uh",)],
    key=len, reverse=True,
)


def _lead(words: Sequence[str]) -> int:
    """How many leading words are fillers / polite frames ("um, can you please ...")."""
    i = 0
    for _ in range(6):
        before = i
        while i < len(words) and words[i] in _FILLERS:
            i += 1
        for frame in _POLITE:
            if tuple(words[i : i + len(frame)]) == frame:
                i += len(frame)
                break
        if i == before:
            break
    return i


def _trail(words: tuple[str, ...]) -> tuple[str, ...]:
    for _ in range(4):
        for tail in _TRAILING:
            if len(words) > len(tail) and words[-len(tail):] == tail:
                words = words[: -len(tail)]
                break
        else:
            break
    return words


# --- verbs -------------------------------------------------------------------------------------------------------


def _table(pairs: dict[str, str]) -> list[tuple[tuple[str, ...], str]]:
    return sorted(((tuple(phrase.split()), family) for phrase, family in pairs.items()), key=lambda p: -len(p[0]))


_VERBS = _table({
    # open
    "open": "open", "open up": "open", "launch": "open", "start": "open", "start up": "open", "run": "open",
    "pull up": "open", "bring up": "open", "fire up": "open", "boot up": "open", "load up": "open", "get me": "open",
    "give me": "open", "show me": "open", "gimme": "open", "i need": "open", "hop on": "open", "hop into": "open",
    # switch
    "switch to": "switch", "switch over to": "switch", "switch back to": "switch", "swap to": "switch",
    "flip to": "switch", "flip over to": "switch", "change to": "switch", "go to": "switch", "go over to": "switch",
    "go back to": "switch", "go into": "switch", "jump to": "switch", "jump over to": "switch",
    "jump back to": "switch", "head to": "switch", "head over to": "switch", "hop over to": "switch",
    "take me to": "switch", "take me back to": "switch", "navigate to": "switch", "focus": "switch",
    "focus on": "switch", "back to": "switch", "show": "switch", "bring": "switch",
    # close
    "close": "close", "close out": "close", "close out of": "close", "quit": "close", "exit": "close",
    "exit out of": "close", "kill": "close", "shut": "close", "shut down": "close", "get rid of": "close",
    # window ops with an optional target
    "maximize": "maximize", "maximise": "maximize", "max out": "maximize",
    "minimize": "minimize", "minimise": "minimize", "hide": "minimize",
    "restore": "restore", "unmaximize": "restore", "un maximize": "restore", "unminimize": "restore",
    "center": "center", "centre": "center",
    # placement: needs an area or a monitor
    "snap": "place", "put": "place", "throw": "place", "move": "place", "send": "place", "stick": "place",
    "shove": "place", "place": "place", "drag": "place", "push": "place", "dock": "place", "toss": "place",
    "slide": "place", "chuck": "place", "park": "place",
    # payloads
    "type": "type", "type out": "type", "write": "type", "write out": "type",
    "search": "search", "search for": "search", "look up": "search", "look for": "search",
    "press": "press", "hit": "press", "push the": "press", "tap": "press", "press the": "press", "hit the": "press",
    "click": "click", "click on": "click", "tap on": "click", "double click": "click", "double click on": "click",
})
_FAMILY_VERB = {"open": "Open", "switch": "Switch to", "close": "Close", "place": "Move", "maximize": "Maximize",
                "minimize": "Minimize", "restore": "Restore", "center": "Center", "fullscreen": "Full screen"}


def _verb(words: Sequence[str]) -> tuple[str, int] | None:
    for phrase, family in _VERBS:
        if tuple(words[: len(phrase)]) == phrase:
            return family, len(phrase)
    return None


# --- templates (whole-clause patterns) ---------------------------------------------------------------------------

_AMOUNT = r"(?: (?P<amt>a bit|a little bit|a little|slightly|a tad|a lot|way more|way|a bunch|lots|much more|more|some))?"
_SMALL_AMOUNTS = frozenset({"a bit", "a little bit", "a little", "slightly", "a tad"})
_LARGE_AMOUNTS = frozenset({"a lot", "way more", "way", "a bunch", "lots", "much more"})


def _volume_step(m: re.Match) -> dict:
    amount = m.groupdict().get("amt")
    if amount in _SMALL_AMOUNTS:
        return {"step": 4}
    if amount in _LARGE_AMOUNTS:
        return {"step": 20}
    return {}


def _scroll_amount(m: re.Match) -> str:
    amount = m.groupdict().get("amt")
    if amount in _SMALL_AMOUNTS:
        return "small"
    if amount in _LARGE_AMOUNTS:
        return "large"
    return "page"


_UP_WORDS = {"louder": "up", "raise": "up", "increase": "up", "boost": "up", "quieter": "down", "softer": "down",
             "lower": "down", "decrease": "down", "reduce": "down"}
_MEDIA_PLAY = {"play": "play", "resume": "play", "unpause": "play", "pause": "pause"}
_EDGE = {"top": "top", "start": "top", "beginning": "top", "bottom": "bottom", "end": "bottom"}

_TEMPLATES: list[tuple[re.Pattern[str], Callable[[re.Match], Step]]] = [
    (re.compile(p), make) for p, make in [
        # tabs and windows of the browser
        (r"(?:open |make |create )?(?:a )?(?:new|another) tab", lambda m: step("tab", op="new")),
        (r"close (?:the |this |that |current |the current )?tab", lambda m: step("tab", op="close")),
        (r"(?:go to |switch to |move to )?(?:the )?next tab|switch tabs?", lambda m: step("tab", op="next")),
        (r"(?:go to |switch to |move to )?(?:the )?(?:previous|last|prior) tab", lambda m: step("tab", op="prev")),
        (r"(?:go )?back (?:a|one) tab|(?:go )?(?:a|one) tab back", lambda m: step("tab", op="prev")),
        (r"(?:go )?forward (?:a|one) tab|(?:go )?(?:a|one) tab (?:forward|over)", lambda m: step("tab", op="next")),
        (r"(?:reopen|re open)(?: the| that| my)?(?: last)?(?: closed| close)? tab"
         r"|(?:restore|bring back|undo close|undo closed|open)(?: the| that| my)?(?: last)? (?:closed|close) tab",
         lambda m: step("tab", op="reopen")),
        (r"(?:open )?(?:a )?new (?:incognito|private) window", lambda m: step("press", keys="ctrl+shift+n")),
        (r"(?:open )?(?:a )?new window", lambda m: step("press", keys="ctrl+n")),
        # back / forward / reload
        (r"(?:go )?back(?: (?:a|one) page)?|go back to the (?:previous|last) page|previous page",
         lambda m: step("nav", op="back")),
        (r"(?:go )?forward(?: (?:a|one) page)?", lambda m: step("nav", op="forward")),
        (r"(?:reload|refresh)(?: (?:the|this))?(?: (?:page|tab|site|website))?", lambda m: step("nav", op="reload")),
        # scroll
        (r"scroll(?: all the way)?(?: back)?(?: up| down)?(?: to)? (?:the )?(?P<edge>top|bottom|end|start|beginning)",
         lambda m: step("scroll", dir=_EDGE[m["edge"]])),
        (r"scroll(?: (?P<dir>up|down|left|right))?" + _AMOUNT,
         lambda m: step("scroll", dir=m["dir"] or "down", amount=_scroll_amount(m))),
        (r"page (?P<dir>up|down)", lambda m: step("scroll", dir=m["dir"], amount="page")),
        (r"(?:go|take me|bring me|jump|skip|head)(?: all the way)?(?: back)?(?: up| down)? to the "
         r"(?P<edge>top|bottom|end|start|beginning) of (?:the|this) page",
         lambda m: step("scroll", dir=_EDGE[m["edge"]])),
        # volume
        (r"(?:set |change |put |turn )?(?:the )?(?:volume|sound)(?: level)?(?: (?:to|at))? (?P<n>\d{1,3})(?: percent)?",
         lambda m: step("volume", op="set", level=int(m["n"]))),
        (r"(?:max|maximum|full) volume|volume (?:max|all the way up)", lambda m: step("volume", op="set", level=100)),
        (r"volume all the way down", lambda m: step("volume", op="set", level=0)),
        (r"(?:turn |crank |bump )?(?:the )?(?:volume|sound) (?P<dir>up|down)" + _AMOUNT,
         lambda m: step("volume", op=m["dir"], **_volume_step(m))),
        (r"(?:turn|crank|bump)(?: (?:it|the volume|the sound|the music|this|that|things))? (?P<dir>up|down)" + _AMOUNT,
         lambda m: step("volume", op=m["dir"], **_volume_step(m))),
        (r"(?:turn|crank|bump) (?P<dir>up|down) (?:the )?(?:volume|sound|music)" + _AMOUNT,
         lambda m: step("volume", op=m["dir"], **_volume_step(m))),
        (r"(?:make it )?(?:a bit |a little )?(?P<word>louder|quieter|softer)" + _AMOUNT,
         lambda m: step("volume", op=_UP_WORDS[m["word"]], **_volume_step(m))),
        (r"(?P<word>raise|increase|boost|lower|decrease|reduce) (?:the )?(?:volume|sound)" + _AMOUNT,
         lambda m: step("volume", op=_UP_WORDS[m["word"]], **_volume_step(m))),
        (r"(?P<op>mute|unmute)(?: (?:the )?(?:sound|audio|volume|it|this|music|computer|laptop|speakers|everything))?",
         lambda m: step("volume", op=m["op"])),
        # media
        (r"play ?pause", lambda m: step("media", op="play_pause")),
        (r"(?P<op>play|pause|resume|unpause)(?: (?:the )?(?:music|song|video|track|playback|audio|podcast|it|this|that))?",
         lambda m: step("media", op=_MEDIA_PLAY[m["op"]])),
        (r"stop (?:the )?(?:music|song|video|playback|audio|podcast)", lambda m: step("media", op="stop")),
        (r"skip(?: (?:this|the|that))?(?: (?:song|track|video|ad))?|(?:play )?(?:the )?next (?:song|track|video)",
         lambda m: step("media", op="next")),
        (r"(?:play )?(?:the )?(?:previous|last) (?:song|track|video)|go back a (?:song|track)",
         lambda m: step("media", op="prev")),
        # dictation and listening
        (r"(?:start|begin|turn on)(?: (?:the|a))? dictation|start dictating|dictation on",
         lambda m: step("dictation", op="start")),
        (r"(?:stop|end|turn off)(?: the)? dictation|stop dictating|dictation off", lambda m: step("dictation", op="stop")),
        (r"(?:stop listening|go to sleep|sleep|take a break)(?: (?:for now|for a bit|for a while|for a minute"
         r"|for a few minutes|for the night))?",
         lambda m: step("listening", op="sleep")),
        (r"wake up|start listening|resume listening|listen again", lambda m: step("listening", op="wake")),
        # everyday shortcuts
        (r"undo(?: (?:that|it|this|the last thing))?", lambda m: step("press", keys="ctrl+z")),
        (r"redo(?: (?:that|it|this))?", lambda m: step("press", keys="ctrl+y")),
        (r"copy(?: (?:that|this|it|the text|everything))?", lambda m: step("press", keys="ctrl+c")),
        (r"paste(?: (?:that|this|it|the text))?", lambda m: step("press", keys="ctrl+v")),
        (r"cut(?: (?:that|this|it|the text))?", lambda m: step("press", keys="ctrl+x")),
        (r"select (?:all|everything)(?: (?:the )?text)?", lambda m: step("press", keys="ctrl+a")),
        (r"save(?: (?:it|this|that|the file|my work|the document))?", lambda m: step("press", keys="ctrl+s")),
        (r"show (?:me )?(?:the |my )?desktop|(?:minimize|minimise|hide) (?:everything|all(?: (?:the )?windows)?)",
         lambda m: step("press", keys="win+d")),
        (r"zoom in", lambda m: step("press", keys="ctrl+=")),
        (r"zoom out", lambda m: step("press", keys="ctrl+-")),
        (r"reset (?:the )?zoom", lambda m: step("press", keys="ctrl+0")),
        # full screen, without a named window
        (r"(?:go |make (?:it |this |that )?|set to |switch to |turn on )?full ?screen(?: (?:it|this|that|mode|this window|the window))?",
         lambda m: step("window", op="fullscreen", target="active")),
    ]
]


def _template(words: tuple[str, ...]) -> Step | None:
    phrase = " ".join(words)
    for pattern, make in _TEMPLATES:
        m = pattern.fullmatch(phrase)
        if m:
            return make(m)
    return None


# --- keys --------------------------------------------------------------------------------------------------------

_MODIFIERS = {"control": "ctrl", "ctrl": "ctrl", "ctl": "ctrl", "shift": "shift", "alt": "alt", "alternate": "alt",
              "option": "alt", "windows": "win", "win": "win"}
_KEYS = {
    "enter": "enter", "return": "enter", "escape": "esc", "esc": "esc", "tab": "tab", "space": "space",
    "spacebar": "space", "backspace": "backspace", "delete": "delete", "del": "delete", "insert": "insert",
    "up": "up", "down": "down", "left": "left", "right": "right", "home": "home", "end": "end",
    "pageup": "pagedown" if False else "pageup", "pagedown": "pagedown",
}
_TWO_WORD_KEYS = {("page", "up"): "pageup", ("page", "down"): "pagedown", ("space", "bar"): "space",
                  ("back", "space"): "backspace"}
_NUMBER_WORDS = {"zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
                 "seven": "7", "eight": "8", "nine": "9", "ten": "10", "eleven": "11", "twelve": "12"}
_MODIFIER_ORDER = ("ctrl", "shift", "alt", "win")


def parse_keys(words: Sequence[str]) -> str | None:
    """Spoken keys -> "ctrl+shift+t". One non-modifier key at most; None if any word isn't a key."""
    words = [w for w in words if w not in ("plus", "the", "key", "keys", "button", "arrow")]
    mods: list[str] = []
    keys: list[str] = []
    i = 0
    while i < len(words):
        word = words[i]
        pair = tuple(words[i : i + 2])
        if pair in _TWO_WORD_KEYS:
            keys.append(_TWO_WORD_KEYS[pair])
            i += 2
            continue
        if word in _MODIFIERS:
            mods.append(_MODIFIERS[word])
        elif word in _KEYS:
            keys.append(_KEYS[word])
        elif re.fullmatch(r"f([1-9]|1[0-9]|2[0-4])", word):
            keys.append(word)
        elif word == "f" and i + 1 < len(words) and (words[i + 1].isdigit() or words[i + 1] in _NUMBER_WORDS):
            number = _NUMBER_WORDS.get(words[i + 1], words[i + 1])
            keys.append(f"f{number}")
            i += 2
            continue
        elif len(word) == 1 and word.isalnum():
            keys.append(word)
        elif word in _NUMBER_WORDS and len(_NUMBER_WORDS[word]) == 1:
            keys.append(_NUMBER_WORDS[word])
        else:
            return None
        i += 1
    if len(keys) > 1 or (not keys and not mods):
        return None
    ordered = [m for m in _MODIFIER_ORDER if m in mods]
    return "+".join([*ordered, *keys])


# --- targets -----------------------------------------------------------------------------------------------------

_LEADING_ARTICLES = frozenset({"the", "a", "an", "my", "our", "up", "over"})
_GENERIC_NOUNS = frozenset({"app", "application", "program", "window", "browser", "website", "site", "page",
                            "document", "file", "thing", "one"})
_POINTERS = frozenset({"it", "this", "that", "here", "current", "active", "window", "one", "app", "thing"})
_IN_BROWSER = (("in", "a", "new", "tab"), ("in", "new", "tab"), ("in", "chrome"), ("in", "the", "browser"),
               ("in", "my", "browser"), ("on", "chrome"), ("in", "google", "chrome"))
ACTIVE = "active"


@dataclass(frozen=True)
class Found:
    target: Target
    confidence: float


@dataclass(frozen=True)
class Ask:
    """A clause whose name could be one of several things: ask before acting."""

    matches: tuple[Match, ...]
    verb: str  # "Open" / "Close" / ...
    make: Callable[[Target], list[object]]  # the clause's items, once he picks one


def _is_pointer(words: Sequence[str]) -> bool:
    """ "it" / "this" / "this window" / "the current window" / "that one"."""
    core = [w for w in words if w not in ("the", "this", "that", "my")]
    if not words:
        return False
    if not core:
        return any(w in ("this", "that") for w in words)
    return all(w in _POINTERS for w in core) and any(w in ("it", "this", "that", "here", "current", "active", "window")
                                                     for w in words)


def _clean_target(words: Sequence[str]) -> tuple[str, ...]:
    words = tuple(words)
    for suffix in _IN_BROWSER:
        if len(words) > len(suffix) and words[-len(suffix):] == suffix:
            words = words[: -len(suffix)]
            break
    while len(words) > 1 and words[0] in _LEADING_ARTICLES:
        words = words[1:]
    while len(words) > 1 and words[-1] in _GENERIC_NOUNS:
        words = words[:-1]
    return words


_SAME_SOUND = 60.0  # Catalog's score for a sound-alike with the very same sound code ("cloud" / "Claude")


def _find(words: Sequence[str], catalog: Catalog, strict: bool, *, apps_only: bool = False, bare: bool = False,
          familiar: frozenset[str] = frozenset(), same_sound: bool = False) -> Found | tuple[Match, ...] | None:
    """A target for a name: Found, matches to ask about, or None.

    familiar: apps with a window open (sound-alikes may be offered for them). same_sound: take a single
    sound-alike with the very same sound code instead of asking (safe actions only: open / switch)."""
    words = _clean_target(words)
    if not words or words[0] in _LEADING_ARTICLES and len(words) == 1:
        return None
    if len(words) == 1 and "." in words[0]:
        if apps_only:
            return None
        return Found(Target("site", words[0], url=f"https://{words[0]}"), 1.0)
    resolution = catalog.resolve(" ".join(words), strict=strict, familiar=familiar)
    best = resolution.best
    if best is not None and (not apps_only or best.target.kind == "app"):
        if bare and best.how not in ("alias", "exact", "short"):
            return None
        return Found(best.target, min(1.0, best.score / 100.0))
    if strict or bare:
        return None
    offered = tuple(m for m in resolution.matches if not apps_only or m.target.kind == "app")
    if (same_sound and offered and offered[0].how == "sound" and offered[0].score >= _SAME_SOUND
            and (len(offered) == 1 or offered[1].score < _SAME_SOUND)):
        return Found(offered[0].target, 0.75)
    return offered[:4] or None


# --- placement ---------------------------------------------------------------------------------------------------

_AREA_WORDS = {
    ("top", "left"): "top_left", ("upper", "left"): "top_left", ("left", "top"): "top_left",
    ("top", "right"): "top_right", ("upper", "right"): "top_right", ("right", "top"): "top_right",
    ("bottom", "left"): "bottom_left", ("lower", "left"): "bottom_left", ("left", "bottom"): "bottom_left",
    ("bottom", "right"): "bottom_right", ("lower", "right"): "bottom_right", ("right", "bottom"): "bottom_right",
    ("left", "third"): "left_third", ("middle", "third"): "middle_third", ("center", "third"): "middle_third",
    ("right", "third"): "right_third", ("full", "screen"): "full", ("whole", "screen"): "full",
    ("left",): "left", ("right",): "right", ("top",): "top", ("bottom",): "bottom", ("upper",): "top",
    ("lower",): "bottom", ("middle",): "center", ("center",): "center", ("centre",): "center", ("full",): "full",
}
_AREA_SUFFIXES = (("of", "the", "screen"), ("of", "my", "screen"), ("of", "the", "monitor"), ("side",), ("half",),
                  ("part",), ("corner",), ("portion",), ("of", "it"))
_MONITOR_WORDS = {"other": "other", "another": "other", "next": "next", "second": "next", "2nd": "next",
                  "third": "next", "external": "other", "previous": "prev", "left": "left", "right": "right",
                  "primary": "primary", "main": "primary", "first": "primary", "laptop": "primary",
                  "laptops": "primary", "built": "primary"}
_SCREEN_NOUNS = frozenset({"monitor", "screen", "display"})
_PREPOSITIONS = (("over", "to", "the"), ("on", "to", "the"), ("over", "to"), ("to", "the"), ("on", "the"),
                 ("onto", "the"), ("in", "the"), ("into", "the"), ("at", "the"), ("to",), ("on",), ("onto",),
                 ("into",), ("in",), ("at",), ("the",), ("my",))


@dataclass(frozen=True)
class Place:
    target: Target | None  # None = the active window
    area: str


def _strip_prep(words: tuple[str, ...]) -> tuple[str, ...]:
    for prep in _PREPOSITIONS:
        if len(words) >= len(prep) and words[-len(prep):] == prep:
            return words[: -len(prep)]
    return words


def _split_place(words: tuple[str, ...]) -> tuple[tuple[str, ...], str, str] | None:
    """(target words, "area" | "monitor", value) from "<target> [to the] left [side]" / "<target> to the other monitor"."""
    w = words
    if w and w[-1] in _SCREEN_NOUNS:
        rest = w[:-1]
        if rest and rest[-1] in ("the",):
            rest = rest[:-1]
        if rest and rest[-1] in _MONITOR_WORDS:
            value = _MONITOR_WORDS[rest[-1]]
            rest = rest[:-1]
            if rest and rest[-1] == "in" and value == "primary":  # "built in screen"
                rest = rest[:-1]
            return _strip_prep(rest), "monitor", value
        return None
    for suffix in _AREA_SUFFIXES:
        if len(w) > len(suffix) and w[-len(suffix):] == suffix:
            w = w[: -len(suffix)]
            break
    for size in (2, 1):
        if len(w) >= size and tuple(w[-size:]) in _AREA_WORDS:
            area = _AREA_WORDS[tuple(w[-size:])]
            return _strip_prep(w[:-size]), "area", area
    return None


def _place_items(rest: tuple[str, ...], catalog: Catalog, strict: bool, *, need_target: bool,
                 familiar: frozenset[str] = frozenset()) -> list[object] | Ask | None:
    split = _split_place(rest)
    if split is None:
        return None
    target_words, kind, value = split
    if not target_words or _is_pointer(target_words):
        if need_target and not target_words:
            return None
        target: Target | None = None
    else:
        found = _find(target_words, catalog, strict, apps_only=True, familiar=familiar)
        if found is None:
            return None
        if isinstance(found, tuple):
            return Ask(found, "Move", lambda t: _place_made(t, kind, value))
        target = found.target
    return _place_made(target, kind, value)


def _place_made(target: Target | None, kind: str, value: str) -> list[object]:
    if kind == "monitor":
        return [step("window", op="move_monitor", target=target.name if target else ACTIVE, monitor=value)]
    return [Place(target, value)]


# --- clauses -----------------------------------------------------------------------------------------------------

_WINDOW_FAMILIES = {"maximize": "maximize", "minimize": "minimize", "restore": "restore", "center": "center"}


@dataclass
class _State:
    catalog: Catalog
    strict: bool
    family: str | None = None  # the last clause's verb family (a clause without a verb borrows it)
    confidence: float = 1.0
    running: frozenset[str] = frozenset()  # apps with a window open (from the context snapshot)


def _target_items(family: str, rest: tuple[str, ...], st: _State) -> list[object] | Ask | None:
    catalog, strict, running = st.catalog, st.strict, st.running
    if family in ("open", "switch", "place"):
        for suffix in _FRONT:
            if len(rest) > len(suffix) and rest[-len(suffix):] == suffix:
                rest, family = rest[: -len(suffix)], "switch"
                break
    if family in ("open", "switch"):
        if not rest or _is_pointer(rest):
            return None
        found = _find(rest, catalog, strict, familiar=running, same_sound=True)
        if found is None:
            return None
        if isinstance(found, tuple):
            return Ask(found, _FAMILY_VERB[family], lambda t: [_open_step(family, t)])
        st.confidence = min(st.confidence, found.confidence)
        return [_open_step(family, found.target)]
    if family == "close":
        if not rest:
            return None
        if _is_pointer(rest):
            return [step("window", op="close", target=ACTIVE)]
        found = _find(rest, catalog, strict, apps_only=True, familiar=running)
        if isinstance(found, tuple) and running:  # only what's open can be closed
            found = tuple(m for m in found if m.target.name in running) or None
        if found is None:
            return None
        if isinstance(found, tuple):
            return Ask(found, "Close", lambda t: [step("close_app", app=t.name)])
        st.confidence = min(st.confidence, found.confidence)
        return [step("close_app", app=found.target.name)]
    if family in _WINDOW_FAMILIES:
        op = _WINDOW_FAMILIES[family]
        if not rest or _is_pointer(rest):
            return [step("window", op=op, target=ACTIVE)]
        found = _find(rest, catalog, strict, apps_only=True, familiar=running)
        if found is None:
            return None
        if isinstance(found, tuple):
            return Ask(found, _FAMILY_VERB[family], lambda t: [step("window", op=op, target=t.name)])
        return [step("window", op=op, target=found.target.name)]
    if family == "place":
        return _place_items(rest, catalog, strict, need_target=False, familiar=running)
    return None


def _open_step(family: str, target: Target) -> Step:
    if target.kind == "site":
        return step("open_url", url=target.url)
    return step("open_app" if family == "open" else "switch_app", app=target.name)


def _fullscreen(words: tuple[str, ...], st: _State) -> list[object] | None:
    """ "make chrome full screen" / "put this in full screen"."""
    if len(words) < 3 or words[-2:] != ("full", "screen") and words[-1] != "fullscreen":
        return None
    body = words[:-2] if words[-2:] == ("full", "screen") else words[:-1]
    if not body or body[0] not in ("make", "put", "set", "turn", "go", "switch"):
        return None
    body = _strip_prep(body[1:])
    if not body or _is_pointer(body):
        return [step("window", op="fullscreen", target=ACTIVE)]
    found = _find(body, st.catalog, st.strict, apps_only=True)
    if isinstance(found, Found):
        return [step("window", op="fullscreen", target=found.target.name)]
    return None


def _clause(words: tuple[str, ...], st: _State) -> list[object] | Ask | None:
    template = _template(words)
    if template is not None:
        st.family = None
        return [template]
    verb = _verb(words)
    if verb is not None:
        family, n = verb
        rest = words[n:]
        if family == "press":
            keys = parse_keys(rest)
            st.family = None
            return [step("press", keys=keys)] if keys else None
        if family in ("type", "search", "click"):
            return None  # handled with the original text (see parse)
        if family == "switch" and len(rest) >= 2 and rest[-1] == "tab":  # "switch to the youtube tab"
            name = [w for w in rest[:-1] if w not in ("the", "my", "a", "that", "this")]
            if name and name[0] not in _NOT_TAB_NAMES:
                st.family = None
                return [step("tab", op="goto", match=" ".join(name))]
        if family == "open" and rest:
            fixed = _template(rest[1:] if rest[0] in ("a", "the", "some") and _template(rest) is None else rest)
            if fixed is not None:  # "I need a new tab", "give me full screen"
                st.family = None
                return [fixed]
        made = _target_items(family, rest, st)
        if made is not None:
            st.family = family
            return made
        fullscreen = _fullscreen(words, st)
        if fullscreen is not None:
            return fullscreen
        return None
    fullscreen = _fullscreen(words, st)
    if fullscreen is not None:
        return fullscreen
    placed = _place_items(words, st.catalog, st.strict, need_target=True, familiar=st.running)
    if placed is not None:
        st.family = "place"
        return placed
    if st.family is not None:
        return _target_items(st.family, words, st)
    if not st.strict:  # a bare name with the key or the wake name: "Nova, Chrome."
        found = _find(words, st.catalog, st.strict, bare=True)
        if isinstance(found, Found):
            st.family = "open"
            return [_open_step("open", found.target)]
    return None


# --- Chrome and Canvas (Stage 06) --------------------------------------------------------------------------------

_NOT_TAB_NAMES = frozenset({"new", "next", "previous", "last", "another", "other", "current", "prior", "first", "fresh",
                            "blank", "empty", "extra", "second", "different"})
_COURSEWORK = frozenset({"assignment", "homework", "hw", "quiz", "lab", "project", "essay", "paper", "discussion",
                         "reading", "worksheet", "pset", "exam", "midterm", "report", "prelab"})
_NOT_COURSEWORK = frozenset({
    "folder", "folders", "file", "files", "doc", "docs", "document", "documents", "notes", "note", "link", "links",
    "button", "tab", "tabs", "page", "pages", "section", "menu", "claude", "chatgpt", "word", "excel", "pdf", "drive",
    "app", "window", "spreadsheet", "slides", "sheet", "email", "emails",
    # a control on the page, not the coursework itself ("click submit assignment", "start the quiz")
    "submit", "submission", "submissions", "turn", "start", "upload", "delete", "remove", "edit", "post", "reply",
    "comment", "comments", "grade", "grades", "rubric", "feedback", "attempt", "download", "save", "cancel", "view",
    "details", "preview", "resume", "retake", "mark", "done", "complete",
})
_DUE_WORDS = frozenset({"due", "overdue"})
_COURSE_NOUNS = frozenset({"assignment", "assignments", "homework", "hw", "quiz", "quizzes", "todo", "todos", "deadline",
                           "deadlines"})
_QUESTION_START = frozenset({"whats", "what", "do", "is", "are", "any", "anything", "show", "tell", "list", "check",
                             "have", "got", "when", "whens"})
_NOT_QUESTIONS = frozenset({"click", "open", "pull", "submit", "type", "search", "put", "send", "write"})
_WEEKDAY_WORDS = frozenset({"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"})
_RANGE_HINTS = frozenset({"today", "todays", "tonight", "tomorrow", "week", "weekend", "overdue", "late", "missing",
                          "next", "upcoming", *_WEEKDAY_WORDS})
_ROLE_NOUNS = frozenset({"button", "link", "icon", "checkbox", "toggle", "switch"})
_ORDINAL_WORDS = frozenset({"first", "second", "third", "fourth", "fifth", "last", "top", "1st", "2nd", "3rd", "4th"})
_PAGE_THINGS = frozenset({"result", "link", "video", "article", "post", "story", "item", "option", "entry", "one"})
_READ_WORDS = frozenset({"say", "says", "about", "instructions", "description", "asking", "ask", "asks", "require",
                         "requires", "want", "wants"})
_INTO_BOX = re.compile(r"^(?P<text>.+?)\s+(?:in|into|in to)\s+(?P<target>(?:the|my|that|this)\s+(?:[\w'’-]+\s+){0,3}"
                       r"(?:box|field|bar|input))$", re.IGNORECASE)


def canvas_item(words: Sequence[str]) -> bool:
    """Does this name a piece of coursework ("my assignment for today", "the thermo homework", "HW 5")?"""
    found = set(words)
    return bool(found & _COURSEWORK) and not found & _NOT_COURSEWORK


def _due_range(words: Sequence[str]) -> str:
    for i, w in enumerate(words):
        if w in ("today", "todays", "tonight", "tonights"):
            return "today"
        if w in ("tomorrow", "tomorrows"):
            return "tomorrow"
        if w == "next" and i + 1 < len(words) and words[i + 1] == "week":
            return "next_week"
        if w in ("weekend", "weekends"):
            return "weekend"
        if w == "week":
            return "week"
        if w in ("overdue", "late", "missing"):
            return "overdue"
        if w in _WEEKDAY_WORDS:
            return w
    return "upcoming"


def _page_step(words: tuple[str, ...], verb: tuple[str, int] | None, text: str, toks: Sequence[Tok], clause: Clause,
               lead: int, clauses: Sequence[Clause], ci: int, st: _State) -> tuple[Step, int] | None:
    """Clauses about the page or Canvas that keep his own words: "scroll to the comments", "press the submit button",
    "open my thermo homework", "go to the reviews section". (step, next clause index) or None."""

    def payload(k: int) -> tuple[str, int] | None:  # from word k of this clause on
        begin, end, after = _payload(text, toks, clause.first + lead + k, clauses, ci, st)
        cut = _clean_payload(text[begin:end])
        return (cut, after) if cut else None

    if words[0] == "scroll" and _template(words) is None:
        to = next((k for k in range(1, min(4, len(words) - 1)) if words[k] == "to"), None)
        if to is not None and all(w in ("down", "up", "back", "all", "the", "way") for w in words[1:to]):
            got = payload(to + 1)
            if got is not None:
                return step("scroll_to", description=got[0]), got[1]
        return None
    if verb is None:
        return None
    family, n = verb
    rest = words[n:]
    if not rest:
        return None
    start = n - 1 if words[n - 1] == "the" else n  # "press the submit button": keep "the" in his words
    made = None
    lead_word = rest[1] if rest[0] in ("the", "that", "my") and len(rest) > 1 else rest[0]
    if family == "press" and parse_keys(rest) is None and _ROLE_NOUNS & set(rest):
        made = "page_click", "description"
    elif family in ("open", "switch") and lead_word in _ORDINAL_WORDS and rest[-1] in _PAGE_THINGS:
        made = "page_click", "description"  # "open the second result", "go to the first link"
    elif family in ("open", "switch") and canvas_item(rest):
        made = "canvas_open_item", "item"
    elif family == "switch" and len(rest) > 1 and rest[-1] in ("section", "part"):
        made = "scroll_to", "description"
    if made is None:
        return None
    got = payload(start)
    return (step(made[0], **{made[1]: got[0]}), got[1]) if got is not None else None


def _canvas_question(text: str, toks: Sequence[Tok]) -> Step | None:
    """ "what's due this week", "do I have any homework due friday", "is anything overdue" -> canvas_due;
    "what does my homework for today say" -> canvas_item_text."""
    words = [t.word for t in toks if t.word]
    lead = _lead(words)
    words = words[lead:]
    if not words or words[0] not in _QUESTION_START or any(w in _NOT_QUESTIONS for w in words):
        return None
    found = set(words)
    if found & _READ_WORDS and canvas_item(words):  # "what does my homework for today say": its instructions
        said = [t for t in toks if t.word][lead:]
        item = _clean_payload(text[said[0].start : said[-1].end])
        return step("canvas_item_text", item=item) if item else None
    if not found & _DUE_WORDS and not (found & _COURSE_NOUNS and found & _RANGE_HINTS):
        return None
    said = [t for t in toks if t.word][lead:]
    asked = _clean_payload(text[said[0].start : said[-1].end])
    if canvas.parse_ref(asked, datetime.now().astimezone()).words:  # "anything for physics due this week"
        return step("canvas_due", range=_due_range(words), item=asked)
    return step("canvas_due", range=_due_range(words))


# --- payloads: type ... / search ... ----------------------------------------------------------------------------

_SUBMIT = re.compile(r"(?:(?:press|hit|tap|push)(?: the)? )?(?:enter|return)(?: key)?|submit(?: it)?|send(?: it)?")
_ENGINES = {"google": "google", "youtube": "youtube", "bing": "bing", "duckduckgo": "duckduckgo",
            "wikipedia": "wikipedia", "amazon": "amazon", "github": "github", "maps": "maps", "images": "images"}
_ENGINE_PHRASES = sorted(
    [(("google", "maps"), "maps"), (("google", "images"), "images"), (("duck", "duck", "go"), "duckduckgo"),
     *(((w,), e) for w, e in _ENGINES.items())],
    key=lambda p: -len(p[0]),
)
_ENGINE_VERBS = frozenset({"google", "youtube", "bing"})


def _engine_at(words: Sequence[str], at_end: bool) -> tuple[str, int] | None:
    for phrase, engine in _ENGINE_PHRASES:
        n = len(phrase)
        part = tuple(words[-n:]) if at_end else tuple(words[:n])
        if len(words) >= n and part == phrase:
            return engine, n
    return None


def _starts_command(words: tuple[str, ...], st: _State) -> bool:
    """Does this clause start a new command (so a payload stops before it)?"""
    if not words:
        return False
    probe = _State(st.catalog, st.strict, running=st.running)
    if _SUBMIT.fullmatch(" ".join(words)):
        return True
    if _verb(words) is None and _template(words) is None:
        return False
    made = _clause(words, probe)
    return made is not None or (_verb(words) or ("", 0))[0] in ("type", "search", "click")


def _payload(text: str, toks: Sequence[Tok], start_tok: int, clauses: Sequence[Clause], ci: int,
             st: _State) -> tuple[int, int, int]:
    """(start char, end char, next clause index) of a payload starting at token ``start_tok``, running on through
    clauses that don't start a new command."""
    end_clause = ci
    j = ci + 1
    while j < len(clauses):
        c = clauses[j]
        lead = _lead(c.words)
        if _starts_command(_trail(c.words[lead:]), st):
            break
        end_clause = j
        j += 1
    last = clauses[end_clause].last
    end = toks[last].end
    k = last + 1
    while k < len(toks) and toks[k].punct in ("!", "?"):
        end = toks[k].end
        k += 1
    return toks[start_tok].start, end, j


def _clean_payload(text: str) -> str:
    return text.strip().rstrip(" ,;:.").strip()


# --- the parser --------------------------------------------------------------------------------------------------

_SIDE_BY_SIDE = (("side", "by", "side"), ("next", "to", "each", "other"), ("split", "screen"))
_SPLIT_AREAS = {2: ("left", "right"), 3: ("left_third", "middle_third", "right_third"),
                4: ("top_left", "top_right", "bottom_left", "bottom_right")}


def _side_by_side(clauses: list[Clause], st: _State) -> tuple[int, list[object]] | None:
    """ "chrome, claude and fusion side by side": (first clause of the group, its layout item)."""
    if not clauses:
        return None
    last = clauses[-1].words[_lead(clauses[-1].words):]
    for suffix in _SIDE_BY_SIDE:
        if len(last) > len(suffix) and last[-len(suffix):] == suffix:
            last = last[: -len(suffix)]
            break
    else:
        return None
    groups = [*(c.words[_lead(c.words):] for c in clauses[:-1]), last]
    targets: list[Target] = []
    first = len(clauses)
    for index in range(len(groups) - 1, -1, -1):
        words = groups[index]
        verb = _verb(words)
        if verb is not None and verb[0] == "place":
            words = words[verb[1]:]
        found = _find(words, st.catalog, st.strict, apps_only=True) if words else None
        if not isinstance(found, Found):
            break
        targets.insert(0, found.target)
        first = index
        if verb is not None:
            break
    if len(targets) not in _SPLIT_AREAS:
        return None
    areas = _SPLIT_AREAS[len(targets)]
    slots = tuple({"app": t.name, "area": a} for t, a in zip(targets, areas, strict=True))
    return first, [step("layout", slots=slots)]


_SINGLE_AREA_OPS = {"left": "snap_left", "right": "snap_right", "top": "snap_top", "bottom": "snap_bottom",
                    "full": "maximize", "center": "center"}


def _merge_places(items: list[object]) -> list[Step] | None:
    out: list[Step] = []
    i = 0
    while i < len(items):
        item = items[i]
        if not isinstance(item, Place):
            out.append(item)  # type: ignore[arg-type]
            i += 1
            continue
        group = [item]
        while i + len(group) < len(items) and isinstance(items[i + len(group)], Place):
            group.append(items[i + len(group)])  # type: ignore[arg-type]
        i += len(group)
        if len(group) == 1:
            place = group[0]
            name = place.target.name if place.target else ACTIVE
            op = _SINGLE_AREA_OPS.get(place.area)
            if op is not None:
                out.append(step("window", op=op, target=name))
            else:
                out.append(step("layout", slots=({"app": name, "area": place.area},)))
            continue
        areas = [p.area for p in group]
        if len(group) == 3 and set(areas) <= {"left", "center", "right"} and len(set(areas)) == 3:
            thirds = {"left": "left_third", "center": "middle_third", "right": "right_third"}
            areas = [thirds[a] for a in areas]
        if any(a in ("center", "full") for a in areas) or len(group) > 4:
            return None
        out.append(step("layout", slots=tuple(
            {"app": p.target.name if p.target else ACTIVE, "area": a} for p, a in zip(group, areas, strict=True)
        )))
    return out


def parse(text: str, catalog: Catalog, *, strict: bool = False, running: Iterable[str] = ()) -> Plan | None:
    """A plan for ``text``, or None if Tier 0 doesn't understand it."""
    toks = tokenize(text)
    question = _canvas_question(text, toks)
    if question is not None:
        return _plan([question], 1.0)
    clauses = split_clauses(toks)
    st = _State(catalog, strict, running=frozenset(running))
    guessed: set[int] = set()  # clauses whose first word was a misheard verb
    tail: list[object] = []
    side = _side_by_side(clauses, st)
    if side is not None:
        clauses, tail = clauses[: side[0]], side[1]
    items: list[object] = []
    asks: list[tuple[int, Ask]] = []
    i = 0
    while i < len(clauses):
        clause = clauses[i]
        lead = _lead(clause.words)
        words = _trail(clause.words[lead:])
        if not words:
            i += 1
            continue
        verb = _verb(words)
        if _engine_search(words) and _find(words, catalog, True, bare=True) is None:  # "google how tall is ..."
            begin, end, i = _payload(text, toks, clause.first + lead + 1, clauses, i, st)
            query = _clean_payload(text[begin:end])
            if not query:
                return None
            items.append(step("web_search", query=query, engine=words[0]))
            st.family = None
            continue
        page = _page_step(words, verb, text, toks, clause, lead, clauses, i, st)
        if page is not None:
            made, i = page
            items.append(made)
            st.family = None
            continue
        if verb is not None and verb[0] in ("type", "search", "click"):
            start_tok = clause.first + lead + verb[1]
            if start_tok > clause.last:
                return None
            if words[0] == "write" and len(words) > verb[1] and words[verb[1]] in _NEW_WORK:
                return None  # "write me a poem": work for Claude, not typing (Tier 1 decides)
            begin, end, i = _payload(text, toks, start_tok, clauses, i, st)
            made = _payload_step(verb[0], text, toks, begin, end, st)
            if made is None:
                return None
            if made.action in ("type_text", "page_fill") and i < len(clauses):
                nxt = clauses[i]
                nwords = _trail(nxt.words[_lead(nxt.words):])
                if _SUBMIT.fullmatch(" ".join(nwords)):
                    made = Step(made.action, {**made.args, "submit": True})
                    i += 1
            items.append(made)
            st.family = None
            continue
        result = _clause(words, st)
        if result is None:
            k = _inner_split(words, st)
            if k is None:
                guess = None if strict or i in guessed else _guess_verb(words, st)
                if guess is None:
                    return None
                guessed.add(i)  # "Clothes notepad": try it as "close notepad", then ask before doing it
                clauses[i] = Clause(clause.first, clause.last,
                                    clause.words[:lead] + (guess,) + clause.words[lead + 1 :])
                continue
            cut = clause.first + lead + k  # "close claude open chrome": two commands with no "and" between
            clauses[i : i + 1] = [
                Clause(clause.first, cut - 1, clause.words[: lead + k]),
                Clause(cut, clause.last, clause.words[lead + k :]),
            ]
            continue
        if isinstance(result, Ask):
            asks.append((len(items), result))
            items.append(result)
        else:
            items.extend(result)
        i += 1
    items.extend(tail)
    if not items:
        return None
    if asks:
        if strict or len(asks) > 1:
            return None
        if len(clauses) > 1 and _weak(asks[0][1], st.running):
            return None  # "open the clock, I need a timer": the second clause is talk, not a name to ask about
        return _clarify(items, asks[0][0], asks[0][1], st.running)
    plan = _plan(items, st.confidence)
    if plan is not None and guessed:
        said = describe(plan)
        return Plan("clarify", question=f"{said}?", choices=(said,), options=(plan,), confidence=0.5)
    return plan


_NEW_WORK = frozenset({"me", "us", "him", "her", "them", "a", "an", "some", "up", "my"})
_GUESS_FAMILIES = frozenset({"open", "switch", "close", "maximize", "minimize", "restore", "center", "place", "type",
                             "press"})  # free-text verbs other than "type" would turn any mishearing into a question
_GUESSABLE = tuple(sorted({p[0] for p, family in _VERBS if len(p) == 1 and family in _GUESS_FAMILIES}))


def _guess_verb(words: tuple[str, ...], st: _State) -> str | None:
    """The command verb a misheard first word sounds like ("clothes" -> close, "skype" -> type) that makes the
    clause a command, or None. Closest-sounding first."""
    first = words[0] if words else ""
    if len(words) < 2 or len(first) < 3 or not first.isalpha() or first in _FILLERS or _verb(words) is not None:
        return None
    scored = sorted(((sounds_like(first, v), v) for v in _GUESSABLE), reverse=True)
    for score, verb in scored:
        if score < 45.0:
            break
        family = _verb((verb,))[0]
        if family == "type":  # the rest is free text: it always parses
            return verb
        made = _clause((verb, *words[1:]), _State(st.catalog, st.strict, running=st.running))
        if made is None or (isinstance(made, Ask) and not _familiar(made, st.running)):
            continue  # a guessed verb + a sound-alike of something not even open is two guesses: Tier 1 decides
        return verb
    return None


def _familiar(ask: Ask, running: frozenset[str]) -> bool:
    return all(m.target.name in running for m in ask.matches)


def _weak(ask: Ask, running: frozenset[str]) -> bool:
    """Only sound-alikes of things that aren't open ("timer" ~ Teams / Drive)."""
    return all(m.how == "sound" and m.target.name not in running for m in ask.matches)


def describe(plan: Plan) -> str:
    """One line for a yes / no question: "Close Notepad", "Type “hello world”"."""
    s = plan.steps[0]
    a = s.args
    match s.action:
        case "open_app":
            said = f"Open {a['app']}"
        case "switch_app":
            said = f"Switch to {a['app']}"
        case "close_app":
            said = f"Close {a['app']}"
        case "type_text":
            said = f"Type “{a['text']}”" + (" and press Enter" if a.get("submit") else "")
        case "press":
            said = f"Press {a['keys']}"
        case "web_search":
            said = f"Search for “{a['query']}”"
        case "open_url":
            said = f"Open {a['url'].split('//', 1)[-1].removeprefix('www.').rstrip('/')}"
        case "window":
            target = a.get("target", ACTIVE)
            said = f"{_FAMILY_VERB.get(a['op'], a['op'].replace('_', ' ').capitalize())}" + (
                f" {target}" if target != ACTIVE else " this window")
        case "page_click":
            said = f"Click “{a.get('label') or a['description']}”"
        case "page_fill":
            said = f"Type “{a['text']}” in {a.get('label') or a['description']}"
        case "scroll_to":
            said = f"Scroll to {a.get('label') or a['description']}"
        case "canvas_due":
            said = "Show what's due on Canvas"
        case "canvas_open_item":
            said = f"Open “{a['item']}” on Canvas"
        case "canvas_item_text":
            said = f"Read “{a['item']}” on Canvas"
        case "tab" if a.get("op") == "goto":
            said = f"Switch to the {a['match']} tab"
        case _:
            said = s.action.replace("_", " ").capitalize()
    return said + (" …" if len(plan.steps) > 1 else "")


def _inner_split(words: tuple[str, ...], st: _State) -> int | None:
    """Where a second command starts inside a clause that doesn't parse whole, if the part before it parses."""
    for k in range(1, len(words)):
        right = words[k:]
        if _verb(right) is None and _template(right) is None:
            continue
        if _clause(words[:k], _State(st.catalog, st.strict, st.family, running=st.running)) is not None:
            return k
    return None


def _payload_step(kind: str, text: str, toks: Sequence[Tok], begin: int, end: int, st: _State) -> Step | None:
    payload = _clean_payload(text[begin:end])
    if not payload:
        return None
    if kind == "type":
        into = _INTO_BOX.match(payload)  # "type thermo into the search box": that box on the page
        if into is not None:
            return step("page_fill", description=into["target"], text=into["text"], submit=False)
        return step("type_text", text=payload, submit=False)
    if kind == "click":
        said = [t.word for t in toks if begin <= t.start and t.end <= end and t.word]
        if canvas_item(said):
            return step("canvas_open_item", item=payload)
        return step("page_click", description=payload)
    inner = [t for t in toks if begin <= t.start and t.end <= end and t.word]
    words = [t.word for t in inner]
    engine = "google"
    for lead in (["the", "web", "for"], ["the", "internet", "for"], ["online", "for"], ["the", "web"]):
        if words[: len(lead)] == lead and len(words) > len(lead):  # "search the web for X"
            inner, words = inner[len(lead) - (lead[-1] == "for") :], words[len(lead) - (lead[-1] == "for") :]
            break
    if words[:1] == ["for"]:  # "search for X"
        inner, words = inner[1:], words[1:]
    head = _engine_at(words, at_end=False)
    if head is not None and len(words) > head[1] + 1 and words[head[1]] == "for":  # "search youtube for X"
        engine = head[0]
        inner, words = inner[head[1] + 1 :], words[head[1] + 1 :]
    else:
        tailing = _engine_at(words, at_end=True)
        if tailing is not None and len(words) > tailing[1] + 1 and words[-tailing[1] - 1] in ("on", "in", "using", "with"):
            engine = tailing[0]
            cut = tailing[1] + 1
            inner, words = inner[:-cut], words[:-cut]
    if not inner:
        return None
    query = _clean_payload(text[inner[0].start : inner[-1].end])
    return step("web_search", query=query, engine=engine) if query else None


def _plan(items: list[object], confidence: float) -> Plan | None:
    steps = _merge_places(items)
    if not steps:
        return None
    try:
        return check(Plan("act", tuple(steps), confidence=confidence))
    except PlanError as error:
        _log.debug("tier 0 made a bad plan: %s", error)
        return None


def _clarify(items: list[object], at: int, ask: Ask, running: frozenset[str] = frozenset()) -> Plan | None:
    options: list[Plan] = []
    choices: list[str] = []
    for match in ask.matches:
        plan = _plan([*items[:at], *ask.make(match.target), *items[at + 1 :]], 1.0)
        if plan is not None and match.target.name not in choices:
            options.append(plan)
            choices.append(match.target.name)
    if not options:
        return None
    question = f"{ask.verb} {choices[0]}?" if len(choices) == 1 else f"{ask.verb} which one?"
    # Only sound-alikes or partial names of things that aren't open: a guess ("new dab" ~ YouTube, "wikipedia" ~
    # ChatGPT). The router lets Tier 1 try first and asks this only if Tier 1 doesn't act.
    guess = all(m.how in ("sound", "words") and m.target.name not in running for m in ask.matches)
    return Plan("clarify", question=question, choices=tuple(choices), options=tuple(options),
                confidence=max(m.score for m in ask.matches) / 100.0, guess=guess)


_STATEMENT = frozenset({"is", "was", "has", "had", "keeps", "kept", "just", "always", "never", "can", "cant", "wont",
                        "does", "did", "says", "said"})


def _engine_search(words: tuple[str, ...]) -> bool:
    """ "youtube how to tie a tie" searches; "youtube recommended me ..." / "google is down" is talk."""
    return (len(words) > 1 and words[0] in _ENGINE_VERBS and words[1] not in _STATEMENT
            and not (words[1].endswith("ed") and len(words[1]) > 4))


# --- always-listening: the gate's resolver ------------------------------------------------------------------------

_SAFE = frozenset({"open_app", "switch_app", "open_url", "layout", "nav", "scroll", "scroll_to", "media", "volume",
                   "listening"})
_FAMILIES = {"open_app": "open", "switch_app": "open", "open_url": "open", "close_app": "close", "window": "window",
             "layout": "window", "type_text": "type", "press": "press", "web_search": "search", "page_click": "click",
             "page_fill": "type", "scroll_to": "scroll", "canvas_due": "canvas", "canvas_open_item": "click"}


class Tier0Resolver:
    """The gate's always-listening resolver (SPEC §5): strict Tier 0 against the real catalog."""

    def __init__(self, catalog: Catalog) -> None:
        self._catalog = catalog

    @property
    def count(self) -> int:
        return len(self._catalog.apps)

    def resolve(self, words: tuple[str, ...]) -> Resolved | None:
        plan = parse(" ".join(words), self._catalog, strict=True)
        if plan is None or plan.kind != "act":
            return None
        safe = all(_safe(s, words) for s in plan.steps)
        first = plan.steps[0]
        target = first.get("app") or first.get("target") or _site_name(first)
        return Resolved(_FAMILIES.get(first.action, first.action), target, safe)


def safe_plan(plan: Plan, text: str) -> bool:
    """May this plan run from always-listening, without the name or the key? (SPEC §5)"""
    words = tuple(t.word for t in tokenize(text) if t.word)
    return plan.kind == "act" and all(_safe(s, words) for s in plan.steps)


def _safe(s: Step, words: tuple[str, ...]) -> bool:
    if s.action == "window":
        return s.get("op") != "close"
    if s.action == "tab":
        return s.get("op") != "close"
    if s.action == "web_search":
        return any(_engine_at(words[i:], at_end=False) for i in range(len(words)))  # he named the site
    return s.action in _SAFE


def _site_name(s: Step) -> str | None:
    url = s.get("url")
    return url.split("//", 1)[-1].split("/", 1)[0] if isinstance(url, str) else None
