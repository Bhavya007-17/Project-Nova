"""Vocab fix-up: respell names Bhavya uses after speech-to-text (SPEC §4).

"crow labs" -> "CroLabs", "chat GPT" -> "ChatGPT", "Ross 2" -> "ROS2". A run of
1-4 words (never across punctuation) is joined and compared with each name:
same letters -> replace; otherwise the sound (metaphone + digits) must match AND
the spelling must be close (rapidfuzz ratio >= 85 for one word, >= 80 when
words were joined). Strong matches only: "cloud" is not turned into "Claude"
here; Tier 0 can decide that in context. Plain one-word names (Canvas, Fusion)
are never just re-cased, since "canvas" and "fusion" are ordinary words too.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import jellyfish
import yaml
from rapidfuzz import fuzz, process

_log = logging.getLogger(__name__)

DEFAULT_VOCAB_YAML = """\
# Names Nova should spell your way. After speech-to-text, words that sound and
# look close to one of these are respelled ("crow labs" -> "CroLabs"). Only
# strong matches change. App names from the Start menu are added on their own.
# Save the file; Nova uses it from the next thing you say.
terms:
  - CroLabs
  - HIVE
  - ROS2
  - MuJoCo
  - Canvas
  - Fusion
  - Claude
  - ChatGPT
  - Notion
  - Todoist
  - Virginia Tech
  # Professors (write them the way you want them spelled):
  # - Professor Lastname
  # Course codes:
  # - ME 4524
"""

_TOKEN = re.compile(r"[^\W_]+(?:['’][^\W_]+)?")
_JOINABLE = re.compile(r"[ \t-]+")  # what may sit between the words of one name
_POSSESSIVE = re.compile(r"['’]s$", re.IGNORECASE)
_NUMBER_WORDS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
}
_MAX_WORDS = 4
_JOINED_MIN_SCORE = 80.0
_SINGLE_MIN_SCORE = 85.0
_FUZZY_MIN_LETTERS = 4


class VocabError(ValueError):
    """vocab.yaml content Nova cannot use. The message is shown as-is."""


@dataclass(frozen=True)
class Correction:
    text: str
    changes: tuple[tuple[str, str], ...] = ()  # (what was heard, what it became)


@dataclass(frozen=True)
class _Term:
    text: str
    key: str  # lowercase letters and digits
    sound: str
    plain: bool  # one ordinary word ("Canvas"): never just re-cased


def _key(words: Iterable[str]) -> str:
    joined = "".join(_NUMBER_WORDS.get(word.lower(), word.lower()) for word in words)
    return "".join(ch for ch in joined if ch.isalnum())


@lru_cache(maxsize=4096)
def _sound(key: str) -> str:
    letters = "".join(ch for ch in key if ch.isalpha())
    digits = "".join(ch for ch in key if ch.isdigit())
    return (jellyfish.metaphone(letters) if letters else "") + digits


def _is_plain(term: str) -> bool:
    return term.isalpha() and (term.islower() or (term[0].isupper() and term[1:].islower()))


class Vocab:
    def __init__(self, terms: Iterable[str]) -> None:
        by_key: dict[str, _Term] = {}
        for raw in terms:
            text = " ".join(str(raw).split())
            key = _key(text.split())
            if key and key not in by_key:
                by_key[key] = _Term(text, key, _sound(key), _is_plain(text))
        self._exact = by_key
        self._terms = list(by_key.values())
        self._fuzzy = [t for t in self._terms if len(t.key) >= _FUZZY_MIN_LETTERS]
        self._fuzzy_keys = [t.key for t in self._fuzzy]

    @property
    def terms(self) -> tuple[str, ...]:
        return tuple(t.text for t in self._terms)

    def correct(self, text: str) -> Correction:
        if not self._terms or not text.strip():
            return Correction(text)
        tokens = [(m.start(), m.end(), m.group()) for m in _TOKEN.finditer(text)]
        candidates = []  # (score, words, first, last, term, possessive)
        for i in range(len(tokens)):
            for n in range(1, _MAX_WORDS + 1):
                j = i + n - 1
                if j >= len(tokens):
                    break
                if n > 1 and not _JOINABLE.fullmatch(text[tokens[j - 1][1] : tokens[j][0]]):
                    break
                words = [tok[2] for tok in tokens[i : j + 1]]
                possessive = bool(_POSSESSIVE.search(words[-1]))
                if possessive:
                    words[-1] = words[-1][:-2]
                hit = self._match(words)
                if hit is not None:
                    term, score = hit
                    candidates.append((score, n, i, j, term, possessive))
        if not candidates:
            return Correction(text)
        # Best score first; on ties prefer more words, then earlier.
        candidates.sort(key=lambda c: (-c[0], -c[1], c[2]))
        taken: set[int] = set()
        chosen = []
        for score, n, i, j, term, possessive in candidates:
            span = set(range(i, j + 1))
            if span & taken:
                continue
            taken |= span
            chosen.append((i, j, term, possessive))
        chosen.sort()
        out, changes, cursor = [], [], 0
        for i, j, term, possessive in chosen:
            start, end = tokens[i][0], tokens[j][1]
            replacement = term.text + (tokens[j][2][-2:] if possessive else "")
            out.append(text[cursor:start])
            out.append(replacement)
            heard = text[start : end - 2] if possessive else text[start:end]
            changes.append((heard, term.text))
            cursor = end
        out.append(text[cursor:])
        return Correction("".join(out), tuple(changes))

    def _match(self, words: list[str]) -> tuple[_Term, float] | None:
        key = _key(words)
        if len(key) < 3:
            return None
        heard = " ".join(words)
        term = self._exact.get(key)
        if term is not None:
            if heard == term.text:
                return None  # already right
            if term.plain and len(words) == 1 and heard.lower() == term.text.lower():
                return None  # "canvas" stays as Parakeet wrote it
            return term, 100.0
        if len(key) < _FUZZY_MIN_LETTERS or not self._fuzzy:
            return None
        floor = _SINGLE_MIN_SCORE if len(words) == 1 else _JOINED_MIN_SCORE
        sound = _sound(key)
        best = None
        for _choice, score, index in process.extract(
            key, self._fuzzy_keys, scorer=fuzz.ratio, score_cutoff=floor, limit=None
        ):
            candidate = self._fuzzy[index]
            if candidate.sound == sound and (best is None or score > best[1]):
                best = (candidate, score)
        return best


def parse_vocab(text: str) -> list[str]:
    """vocab.yaml text -> terms. Raises VocabError."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise VocabError(f"not valid YAML: {error}") from error
    if data is None:
        return []
    if not isinstance(data, dict):
        raise VocabError("vocab.yaml must start with `terms:` followed by `  - Name` lines")
    unknown = [str(key) for key in data if key != "terms"]
    if unknown:
        raise VocabError(f"unknown entry {unknown[0]!r} (vocab.yaml has only `terms:`)")
    terms = data.get("terms")
    if terms is None:
        return []
    if not isinstance(terms, list):
        raise VocabError("terms must be a list of `  - Name` lines")
    out = []
    for item in terms:
        if not isinstance(item, str):
            raise VocabError(f"each term must be text, not {item!r}")
        if not item.strip():
            raise VocabError("a term is empty")
        out.append(item.strip())
    return out


class VocabFile:
    """config/vocab.yaml plus extra names (app catalog). Re-reads the file when it changes.

    A broken save keeps the last good terms; ``error`` says what's wrong. Not thread-safe:
    the speech worker is the only user.
    """

    def __init__(self, path: Path, extra: Iterable[str] = ()) -> None:
        self.path = Path(path)
        self._extra = tuple(extra)
        self._terms: list[str] = []
        self._signature: tuple[int, int] | None = None
        self._vocab = Vocab(self._extra)
        self.error: str | None = None

    def set_extra(self, names: Iterable[str]) -> None:
        self._extra = tuple(names)
        self._vocab = Vocab([*self._terms, *self._extra])

    def current(self) -> Vocab:
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            self._write_default()
            return self._vocab
        except OSError:
            return self._vocab
        signature = (stat.st_mtime_ns, stat.st_size)
        if signature != self._signature:
            self._signature = signature
            self._load()
        return self._vocab

    def _write_default(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("x", encoding="utf-8") as file:
                file.write(DEFAULT_VOCAB_YAML)
        except FileExistsError:
            pass
        except OSError as error:
            self.error = f"{self.path.name}: cannot create it ({error})"
            return
        self._signature = None
        self.current()

    def _load(self) -> None:
        try:
            text = self.path.read_bytes().decode("utf-8-sig")
            terms = parse_vocab(text)
        except (OSError, UnicodeDecodeError, VocabError) as error:
            message = f"{self.path.name}: {error}"
            if message != self.error:
                _log.warning("%s; keeping the last good vocab", message)
            self.error = message
            return
        self._terms = terms
        self._vocab = Vocab([*terms, *self._extra])
        self.error = None
