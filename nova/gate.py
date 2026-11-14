"""The trigger gate (SPEC §5): may this final act, and on what text? Pure: text + context in, Decision out.

Rules, in order:
1. Empty -> ignored.
2. A key session (Copilot key / chord) or a wake session ("Nova" alone, then this) -> accepted;
   the name is stripped if he said it anyway.
3. Addressed to the wake name ("Nova, ..." / "um, hey Nova ..." / "please Nova, open ...", spellings from
   settings; see ``strip_wake``) -> accepted, name stripped.
4. A session waiting for its own words, Passthrough or asleep -> nothing else counts. (While Nova thinks or acts
   on a command, 6 and 7 still apply; the state machine queues what passes and "stop" / "cancel" aborts.)
5. Another app is using the mic (a call) -> name or key required.
6. Follow-up window (8 s after a command ran) -> anything that starts like a command.
7. Always-listening -> starts like a command, resolves in strict Tier 0 (a page / media / volume
   command, or a command verb + a known app or site), and is safe without the name. Typing,
   clicking, closing and searching an unnamed engine need the name or the key.
While the speakers play, 6 and 7 are narrower (a video must not act, but "pause" has to work): the follow-up
window takes only what resolves and is safe, always-listening only media and volume commands.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

_TOKEN = re.compile(r"[^\W_]+(?:['’][^\W_]+)*")
_SEPARATORS = " \t\r\n,.:;!?-–—…\"'“”"
_GREETINGS = frozenset({"hey", "hi", "hello", "ok", "okay", "yo", "oh"})
_WAKE_LEADS = _GREETINGS | {"um", "uh", "uhm", "er", "erm", "hmm", "mm", "so", "well", "and"}  # skipped before the name
_MAX_WAKE_LEADS = 4
_WAKE_WITHIN = 3  # the name counts within the first three words after them


def _norm(token: str) -> str:
    """Lowercase, no possessive "'s", no apostrophes: "Nova's" -> "nova", "I'm" -> "im"."""
    word = token.lower().replace("’", "'")
    if word.endswith("'s"):
        word = word[:-2]
    return word.replace("'", "")


def _words(text: str) -> list[str]:
    return [_norm(match.group()) for match in _TOKEN.finditer(text)]


# --- wake name -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class WakeName:
    name: str
    words: frozenset[str]  # one-word spellings, normalized
    phrases: tuple[tuple[str, ...], ...] = ()  # several-word spellings ("no va")
    enabled: bool = True

    @classmethod
    def of(cls, name: str, spellings: Iterable[str] = (), enabled: bool = True) -> WakeName:
        words: set[str] = set()
        phrases: list[tuple[str, ...]] = []
        for text in (name, *spellings):
            tokens = tuple(_words(text))
            if len(tokens) == 1:
                words.add(tokens[0])
            elif tokens:
                phrases.append(tokens)
        return cls(name, frozenset(words), tuple(sorted(phrases, key=len, reverse=True)), enabled)

    def _is_word(self, word: str) -> bool:
        # "Novas" / "Novah": speech-to-text often hangs an s or h on a name.
        return word in self.words or (len(word) > 2 and word[-1] in "sh" and word[:-1] in self.words)


def _name_end(text: str, tokens: list[re.Match[str]], i: int, wake: WakeName) -> int | None:
    """Where the wake name ends if it sits at token ``i`` (a one-word or several-word spelling), else None."""
    for phrase in wake.phrases:
        span = tokens[i : i + len(phrase)]
        if (
            len(span) == len(phrase)
            and tuple(_norm(t.group()) for t in span) == phrase
            and all(not text[a.end() : b.start()].strip() for a, b in zip(span, span[1:], strict=False))
        ):
            return span[-1].end()
    if i < len(tokens) and wake._is_word(_norm(tokens[i].group())):
        return tokens[i].end()
    return None


def strip_wake(text: str, wake: WakeName) -> str | None:
    """The rest of ``text`` if it is addressed to the wake name (SPEC §5), else None.

    Greetings and fillers in front are skipped ("um, Nova, open Chrome"). After them the name is the first word -
    then anything may follow, or nothing ("Nova." alone) - or the second or third word, and then only when what
    follows starts like a command ("please Nova, open Chrome"): chatter that merely mentions the name is no wake.
    """
    tokens = list(_TOKEN.finditer(text))
    i = 0
    while i < min(_MAX_WAKE_LEADS, len(tokens)) and _norm(tokens[i].group()) in _WAKE_LEADS:
        i += 1
    for at in range(i, min(i + _WAKE_WITHIN, len(tokens))):
        end = _name_end(text, tokens, at, wake)
        if end is None:
            continue
        rest = text[end:].lstrip(_SEPARATORS).strip()
        if at == i or command_words(rest) is not None:
            return rest
        return None
    return None


# --- does it start like a command? ------------------------------------------------------------------

_FILLERS = frozenset({
    "um", "uh", "uhm", "er", "erm", "hmm", "mm", "okay", "ok", "so", "and", "now", "then", "also",
    "alright", "right", "well", "oh", "yeah", "hey",
})
_POLITE = sorted(
    [("can", "you"), ("could", "you"), ("would", "you"), ("will", "you"), ("can", "u"), ("go", "ahead", "and"),
     ("i", "need", "you", "to"), ("i", "want", "you", "to")],
    key=len, reverse=True,
)
_VERBS = frozenset({
    "open", "launch", "start", "run", "close", "quit", "exit", "kill", "shut", "switch", "go", "bring", "pull",
    "fire", "boot", "focus", "navigate", "jump", "take", "get", "show", "minimize", "minimise", "maximize",
    "maximise", "restore", "snap", "move", "put", "make", "click", "press", "hit", "type", "write", "scroll",
    "search", "play", "pause", "resume", "unpause", "stop", "skip", "next", "previous", "mute", "unmute", "turn",
    "volume", "new", "reload", "refresh", "reopen",
})


def command_words(text: str) -> tuple[str, ...] | None:
    """Normalized words from the command verb on, if ``text`` starts like a command (SPEC §5), else None.

    Skips fillers ("um", "and then"), one polite frame ("can you", "could you") and "please" / "just".
    """
    words = _words(text)
    i = 0
    while i < len(words) and i < 3 and words[i] in _FILLERS:
        i += 1
    for frame in _POLITE:
        if tuple(words[i : i + len(frame)]) == frame:
            i += len(frame)
            break
    while i < len(words) and words[i] in ("please", "just"):
        i += 1
    if i < len(words) and words[i] in _VERBS:
        return tuple(words[i:])
    return None


_STOPS = frozenset({
    ("stop",), ("cancel",), ("abort",), ("wait",), ("stop", "it"), ("stop", "that"), ("cancel", "it"),
    ("cancel", "that"), ("never", "mind"), ("nevermind",), ("hold", "on"),
})


def is_stop(text: str, wake: WakeName) -> bool:
    """Is this only "stop" / "cancel" / "wait" (the name or fillers in front, "please" behind)? While Nova is
    busy that aborts the running command (SPEC §5); a sentence with "stop" in it doesn't."""
    rest = strip_wake(text, wake) if wake.enabled else None
    words = _words(text if rest is None else rest)
    while words and words[0] in _FILLERS:
        words = words[1:]
    while words and words[-1] in ("please", "now"):
        words = words[:-1]
    return tuple(words) in _STOPS


# --- the always-listening resolver ---------------------------------------------------------------------------------
#
# What un-addressed speech may do is decided by strict Tier 0 (``nova.brain.tier0.Tier0Resolver``): the same parser
# that will plan the command, on the real app catalog. The gate only needs this much of it, and stays pure: the
# resolver is handed its catalog and running-apps snapshot, it never fetches one.


@dataclass(frozen=True)
class Resolved:
    action: str  # open | close | window | type | press | click | dictation | search | nav | tab | scroll | media | volume | listening
    target: str | None  # the known app / site, "active" for "this window", None for page / media commands
    safe: bool  # may run from always-listening, without the name or the key
    media: bool = False  # nothing in it but media and volume: runs even while the speakers play
    text: str | None = None  # what to act on instead of his words (a bare "next" while music plays = "next track")


class Resolver(Protocol):
    @property
    def count(self) -> int: ...

    def resolve(self, words: tuple[str, ...], *, media: bool = False) -> Resolved | None:
        """``media``: something is playing, so a bare "next" / "stop" means the player."""
        ...


class NoResolver:
    """Until the app catalog is read: nothing resolves, so nothing runs without the name or the key."""

    count = 0

    def resolve(self, words: tuple[str, ...], *, media: bool = False) -> Resolved | None:
        return None


# --- the decision -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Addressing:
    """What the state machine knows about this utterance."""

    session: str | None = None  # "copilot" | "chord" | "wake": he addressed Nova before saying it
    follow_up: bool = False  # within 8 s of a command that ran
    passthrough: bool = False  # Claude / ChatGPT voice mode is listening to him
    asleep: bool = False  # "stop listening": only the name or the key count
    busy: bool = False  # a key / wake session is waiting for its own words: this isn't them
    working: bool = False  # thinking / acting on a command: what passes now runs next (the state machine queues it)


@dataclass(frozen=True)
class Env:
    """Settings and detectors at decision time."""

    wake: WakeName
    always_listening: bool
    resolver: Resolver
    mic_busy: str | None = None  # another app recording (a call): its name
    speakers: bool = False  # the speakers played audio while this was said


@dataclass(frozen=True)
class Decision:
    accepted: bool
    via: str  # accepted: key | wake | follow-up | always; ignored: the reason (see decide)
    text: str  # what to act on: greeting and wake name stripped
    heard: str  # the whole final
    why: str  # plain words for logs and `nova listen --gate`
    safe_only: bool = False  # always-listening: only safe actions may run

    @property
    def wake_only(self) -> bool:
        """He only said the name: open a listening session for what comes next."""
        return self.accepted and not self.text


def decide(text: str, addressing: Addressing, env: Env) -> Decision:
    heard = text.strip()
    name = env.wake.name

    def ignored(via: str, why: str) -> Decision:
        return Decision(False, via, heard, heard, why)

    if not heard:
        return ignored("empty", "nothing heard")
    rest = strip_wake(heard, env.wake) if env.wake.enabled else None
    if addressing.session is not None:
        via = "wake" if addressing.session == "wake" else "key"
        why = f"you said {name} first" if via == "wake" else ("Copilot key" if addressing.session == "copilot" else "chord")
        return Decision(True, via, rest if rest is not None else heard, heard, why)
    if rest is not None:
        return Decision(True, "wake", rest, heard, f"starts with {name}")
    if addressing.busy:
        return ignored("busy", f"listening for something else: say {name} or use the key")
    if addressing.passthrough:
        return ignored("passthrough", f"voice mode is on: say {name} or use the key")
    if addressing.asleep:
        return ignored("asleep", f"asleep: say {name} or use the key")
    if env.mic_busy:
        return ignored("mic-busy", f"{env.mic_busy} is using the mic: say {name} or use the key")
    over_speakers = ignored("speakers", f"the speakers are playing: say {name} or use the key")
    words = command_words(heard)
    # While Nova works on his command he is still talking to it: the follow-up window is already open.
    if (addressing.follow_up or addressing.working) and words is not None:
        if not env.speakers:
            return Decision(True, "follow-up", heard, heard, "follow-up window")
        # What the speakers say can't type, click or close: only what strict Tier 0 calls safe gets through.
        resolved = env.resolver.resolve(words, media=True)
        if resolved is None or not resolved.safe:
            return over_speakers
        return Decision(True, "follow-up", resolved.text or heard, heard,
                        "follow-up window, speakers playing: safe actions only", safe_only=True)
    if not env.always_listening:
        return ignored("off", "always-listening is off")
    if words is None:
        return ignored("not-command", "doesn't start like a command")
    resolved = env.resolver.resolve(words, media=env.speakers)
    if resolved is None:
        return ignored("unresolved", "no known app, site or page command in it")
    if not resolved.safe:
        return ignored("needs-name", f"{resolved.action} needs {name} or the key")
    if env.speakers and not resolved.media:
        return over_speakers  # "pause" and "volume down" have to work over a video; a video can't open apps
    target = f" {resolved.target}" if resolved.target and resolved.target != "active" else ""
    return Decision(True, "always", resolved.text or heard, heard, f"{resolved.action}{target}", safe_only=True)
