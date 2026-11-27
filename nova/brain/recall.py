"""Recall (Tier 0.7): understand a sentence by what it means, not by its words.

A small sentence model (all-MiniLM-L6-v2, 23 MB, CPU, ``models/embed``) turns what he said into a
vector. ``config/examples.yaml`` holds a few hundred sentences with the plan each one means. The
nearest ones by meaning decide:

- they clearly agree on one plan (close enough, and clearly closer than any sentence that means
  something else) -> that plan runs at once, no LLM. It is handed over in the model's own JSON
  shape, so ``validate.check_llm`` and ``risk`` treat it exactly like a Tier 1 answer.
- otherwise -> the nearest ones (and his own learned skills) go to Tier 1 as worked examples, so
  the LLM sees cases like this one instead of only the fixed ones in ``prompt.EXAMPLES``.

``{app}`` in an example stands for whichever installed app the sentence names: the one app he
mentions is swapped for a stand-in before comparing, so "open {app}" covers every app there is.
No model files -> ``look`` returns None and Nova works as before.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from .. import paths
from ..actions.catalog import Catalog, words_of

_log = logging.getLogger(__name__)

MODEL_FILE = "model_quantized.onnx"
VOCAB_FILE = "vocab.txt"
STAND_IN = "Notepad"  # what {app} reads as when sentences are compared
THRESHOLD = 0.75  # cosine similarity to the nearest example before a plan runs without the LLM
MARGIN = 0.05  # ...and how much closer it must be than the nearest example that means something else
SHOTS = 3  # worked examples shown to Tier 1
SHOT_FLOOR = 0.35  # an example less alike than this teaches the model nothing about this sentence
MAX_TOKENS = 64
APP_SCORE = 95.0  # how sure the catalog must be that a run of words names an app
_PLAIN = frozenset(
    "the a an my me to on in for of and up it this that can you please open get put go make with at is be i now "
    "then just app one new tab window screen left right back next play stop start close".split())

Embed = Callable[[Sequence[str]], np.ndarray]  # sentences -> unit vectors, one row each


@dataclass(frozen=True)
class Example:
    say: str
    slot: bool  # the sentence has {app} in it
    plan: dict[str, Any]  # {"kind": "act", "steps": [...]} | {"kind": "handoff", "to": ...} | {"kind": "ignore"}
    family: str  # examples with the same family mean the same thing


@dataclass(frozen=True)
class Recalled:
    data: dict[str, Any] | None  # sure: the plan, in the shape Tier 1 answers in
    shots: tuple[tuple[dict[str, Any], dict[str, Any]], ...]  # (request, plan) pairs for Tier 1
    like: str = ""  # the nearest example
    score: float = 0.0
    margin: float = 0.0
    ms: float = 0.0

    def trace(self) -> dict[str, Any]:
        return {"recall_ms": round(self.ms, 1), "recall_like": self.like, "recall_score": round(self.score, 3),
                "recall_margin": round(self.margin, 3)}


# --- the example bank -------------------------------------------------------------------------------------------


def load_bank(path: Path) -> list[Example]:
    """config/examples.yaml -> examples. Raises ValueError on a malformed file."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out: list[Example] = []
    for entry in data.get("examples") or ():
        if "do" in entry:
            steps = entry["do"] if isinstance(entry["do"], list) else [entry["do"]]
            plan: dict[str, Any] = {"kind": "act", "steps": steps}
        elif entry.get("kind") == "handoff":
            plan = {"kind": "handoff", "to": entry["to"]}
        elif entry.get("kind") in ("ignore", "dictate"):
            plan = {"kind": entry["kind"]}
        else:
            raise ValueError(f"examples.yaml: an entry needs `do:` or `kind:` ({entry!r})")
        # opening an app and switching to it are one idea (validate.check_llm picks by what is running)
        family = json.dumps(plan, sort_keys=True).replace("switch_app", "open_app")
        for say in entry.get("say") or ():
            out.append(Example(str(say), "{app}" in str(say), plan, family))
    return out


def fill(plan: dict[str, Any], said: str, app: str | None) -> dict[str, Any]:
    """An example's plan as a Tier 1 answer for this sentence."""
    if plan["kind"] == "handoff":
        return {"kind": "handoff", "steps": [], "confidence": 0.9,
                "handoff": {"to": plan["to"], "mode": "text", "prompt": said}}
    if plan["kind"] != "act":
        return {"kind": plan["kind"], "steps": [], "confidence": 0.9}
    steps = json.loads(json.dumps(plan["steps"]).replace("{app}", json.dumps(app or "")[1:-1]))
    return {"kind": "act", "steps": steps, "confidence": 0.9}


def mentions(text: str, catalog: Catalog) -> list[tuple[int, int, str]]:
    """Runs of words that name an installed app or a short-named site: (first word, one past the last, name)."""
    spoken = text.split()
    found: list[tuple[int, int, str]] = []
    i = 0
    while i < len(spoken):
        hit = None
        for size in (3, 2, 1):
            run = spoken[i : i + size]
            if len(run) < size:
                continue
            key = words_of(" ".join(run))
            if not key or all(word in _PLAIN for word in key):
                continue
            matches = catalog.resolve(" ".join(run)).matches
            if matches and matches[0].score >= APP_SCORE:
                hit = (i, i + size, matches[0].target.name)
                break
        if hit is not None:
            found.append(hit)
            i = hit[1]
        else:
            i += 1
    return found


def decide(sims: np.ndarray, families: Sequence[str], threshold: float, margin: float) -> tuple[int, float, float, bool]:
    """(nearest example, its similarity, its lead over the nearest one that means something else, sure?)."""
    best = int(np.argmax(sims))
    score = float(sims[best])
    other = [float(s) for s, family in zip(sims, families) if family != families[best]]
    lead = score - max(other) if other else score
    return best, score, lead, score >= threshold and lead >= margin


# --- the sentence model -----------------------------------------------------------------------------------------


def _is_punctuation(char: str) -> bool:
    code = ord(char)
    return (33 <= code <= 47 or 58 <= code <= 64 or 91 <= code <= 96 or 123 <= code <= 126
            or unicodedata.category(char).startswith("P"))


class WordPiece:
    """BERT's uncased tokenizer (lower-case, strip accents, split punctuation, greedy longest piece). The
    ``tokenizers`` package does the same and would bring five more packages with it."""

    def __init__(self, vocab: dict[str, int]) -> None:
        self._vocab = vocab
        self._cls, self._sep, self._unk = vocab["[CLS]"], vocab["[SEP]"], vocab["[UNK]"]

    @classmethod
    def load(cls, path: Path) -> WordPiece:
        return cls({line: i for i, line in enumerate(path.read_text(encoding="utf-8").splitlines())})

    def words(self, text: str) -> list[str]:
        out: list[str] = []
        word = ""
        for char in unicodedata.normalize("NFD", text.lower()):
            if unicodedata.category(char) == "Mn":
                continue
            if char.isspace() or _is_punctuation(char):
                if word:
                    out.append(word)
                    word = ""
                if not char.isspace():
                    out.append(char)
            else:
                word += char
        if word:
            out.append(word)
        return out

    def ids(self, text: str) -> list[int]:
        ids = [self._cls]
        for word in self.words(text):
            pieces: list[int] = []
            start = 0
            while start < len(word):
                end = len(word)
                piece = None
                while end > start:
                    piece = self._vocab.get(("##" if start else "") + word[start:end])
                    if piece is not None:
                        break
                    end -= 1
                if piece is None:
                    pieces = [self._unk]
                    break
                pieces.append(piece)
                start = end
            ids += pieces
        return ids[: MAX_TOKENS - 1] + [self._sep]


class Embedder:
    """all-MiniLM-L6-v2 on onnxruntime: mean of the token vectors, unit length. ~3 ms a sentence on a laptop CPU."""

    def __init__(self, folder: Path) -> None:
        import onnxruntime as ort

        self._tokens = WordPiece.load(folder / VOCAB_FILE)
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2  # short sentences: more threads only cost wake-ups
        options.log_severity_level = 3
        self._session = ort.InferenceSession(str(folder / MODEL_FILE), options, providers=["CPUExecutionProvider"])
        self._inputs = {i.name for i in self._session.get_inputs()}

    def __call__(self, texts: Sequence[str]) -> np.ndarray:
        rows = [self._tokens.ids(text) for text in texts]
        width = max(map(len, rows))
        ids = np.zeros((len(rows), width), np.int64)
        mask = np.zeros_like(ids)
        for i, row in enumerate(rows):
            ids[i, : len(row)] = row
            mask[i, : len(row)] = 1
        feed = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self._inputs:
            feed["token_type_ids"] = np.zeros_like(ids)
        hidden = self._session.run(None, feed)[0]
        mean = (hidden * mask[..., None]).sum(1) / mask.sum(1, keepdims=True)
        return mean / np.linalg.norm(mean, axis=1, keepdims=True)


# --- recall -----------------------------------------------------------------------------------------------------


class Recall:
    def __init__(self, folder: Path, bank: Path, *, cache: Path | None = None, embed: Embed | None = None,
                 skills: Callable[[], Iterable[tuple[str, list[dict[str, Any]]]]] = tuple,
                 tune: Callable[[], Any] | None = None) -> None:
        """``skills``: his learned skills as (what he says, steps); they are shown to Tier 1, never run from here
        (``nova.skills`` runs the ones he says word for word). ``cache``: where the bank's vectors are kept.
        ``tune``: the live settings (``config.Recall``: enabled / threshold / margin / shots)."""
        self._folder = folder
        self._bank_path = bank
        self._cache = cache
        self._embed = embed
        self._skills = skills
        self._tune = tune
        self._lock = threading.Lock()
        self._bank: list[Example] | None = None
        self._vectors = np.zeros((0, 0))
        self._skill_vectors: dict[str, np.ndarray] = {}
        self._broken = False

    @property
    def available(self) -> bool:
        if self._broken:
            return False
        if self._embed is not None:
            return True
        return (self._folder / MODEL_FILE).is_file() and (self._folder / VOCAB_FILE).is_file()

    def warm(self) -> bool:
        """Load the model and the bank (once; ~0.2 s with the vector cache, ~1.5 s without). False = unavailable."""
        with self._lock:
            if self._bank is not None:
                return True
            if not self.available:
                return False
            try:
                began = time.perf_counter()
                if self._embed is None:
                    self._embed = Embedder(self._folder)
                bank = load_bank(self._bank_path)
                self._vectors = self._bank_vectors(bank)
                self._bank = bank
                _log.info("recall ready: %d examples in %.0f ms", len(bank), (time.perf_counter() - began) * 1000)
                return True
            except Exception as error:  # a broken model file or bank must never stop commands
                _log.warning("recall is off: %s", error)
                self._broken = True
                return False

    def _bank_vectors(self, bank: Sequence[Example]) -> np.ndarray:
        assert self._embed is not None
        texts = [example.say.replace("{app}", STAND_IN) for example in bank]
        if not texts:
            raise ValueError("examples.yaml has no examples")
        key = hashlib.sha256(json.dumps(texts).encode("utf-8")).hexdigest()
        if self._cache is not None:
            try:
                with np.load(self._cache) as saved:
                    if str(saved["key"]) == key:
                        return saved["vectors"]
            except (OSError, ValueError, KeyError):
                pass
        vectors = np.concatenate([self._embed(texts[i : i + 64]) for i in range(0, len(texts), 64)])
        if self._cache is not None:
            try:
                self._cache.parent.mkdir(parents=True, exist_ok=True)
                with self._cache.open("wb") as file:
                    np.savez(file, key=key, vectors=vectors)
            except OSError as error:
                _log.debug("recall cache not written: %s", error)
        return vectors

    def look(self, text: str, catalog: Catalog) -> Recalled | None:
        """What the nearest examples say about ``text``; None when recall isn't available."""
        tune = self._tune() if self._tune is not None else None
        if not text.strip() or (tune is not None and not tune.enabled) or not self.warm():
            return None
        threshold, margin, count = (tune.threshold, tune.margin, tune.shots) if tune is not None else (THRESHOLD, MARGIN, SHOTS)
        assert self._bank is not None and self._embed is not None
        began = time.perf_counter()
        bank = self._bank
        spoken = text.split()
        named = mentions(text, catalog)
        texts = [text]
        app = name = None
        if len(named) == 1:  # one app named: compare the {app} sentences with that name swapped out
            first, last, name = named[0]
            app = " ".join(spoken[first:last]).strip(",.?!")
            texts.append(" ".join([*spoken[:first], STAND_IN, *spoken[last:]]))
        vectors = self._embed(texts)
        plain = self._vectors @ vectors[0]
        slotted = self._vectors @ vectors[1] if app else np.full(len(bank), -1.0)
        sims = np.where([example.slot for example in bank], slotted, plain)
        best, score, lead, sure = decide(sims, [example.family for example in bank], threshold, margin)
        nearest = bank[best]
        data = fill(nearest.plan, text, app) if sure and nearest.plan["kind"] in ("act", "handoff") else None
        shots = self._shots(sims, vectors[0], name, count) if data is None and count > 0 else ()
        return Recalled(data, shots, nearest.say, score, lead, (time.perf_counter() - began) * 1000)

    def _shots(self, sims: np.ndarray, vector: np.ndarray, name: str | None,
               count: int) -> tuple[tuple[dict[str, Any], dict[str, Any]], ...]:
        assert self._bank is not None
        ranked: list[tuple[float, str, dict[str, Any], dict[str, Any]]] = []
        per_family: dict[str, int] = {}
        for index in np.argsort(-sims)[: count * 4]:
            example = self._bank[int(index)]
            if sims[index] < SHOT_FLOOR or per_family.get(example.family, 0) >= 2:
                continue
            per_family[example.family] = per_family.get(example.family, 0) + 1
            app = (name or STAND_IN) if example.slot else None
            say = example.say.replace("{app}", app or "")
            request: dict[str, Any] = {"said": say}
            if app:
                request["apps"] = [app]
            ranked.append((float(sims[index]), say, request, fill(example.plan, say, app)))
        for say, steps in self._skills():
            known = self._skill_vectors.get(say)
            if known is None:
                assert self._embed is not None
                known = self._skill_vectors[say] = self._embed([say])[0]
            alike = float(known @ vector)
            if alike >= SHOT_FLOOR:
                ranked.append((alike, say, {"said": say}, {"kind": "act", "steps": steps, "confidence": 0.95}))
        ranked.sort(key=lambda row: -row[0])
        # the most alike goes last: it sits right before his own sentence
        return tuple((request, plan) for _, _, request, plan in reversed(ranked[:count]))


def standard(tune: Callable[[], Any] | None = None, book: Any = None) -> Recall:
    """Recall as Nova runs it: the model in models/embed, config/examples.yaml, his skill book (``nova.skills``)."""

    def skills() -> list[tuple[str, list[dict[str, Any]]]]:
        if book is None:
            return []
        return [(trigger, skill.steps) for skill in book.all() if not skill.stale for trigger in skill.triggers]

    return Recall(paths.EMBED, paths.EXAMPLES, cache=paths.RECALL_CACHE, skills=skills, tune=tune)
