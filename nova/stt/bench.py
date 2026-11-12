"""`nova bench-stt`: WER and decode time on Bhavya's voicebar recordings (SPEC §4, §18).

Reference text is each prompt's ``expected_text`` (what the recorder showed him to read).
Three numbers per set: "STT alone" decodes the whole file; "Nova path" runs the live
gate + Silero + endpoint rules and decodes the segments it finds; "+ vocab" adds the
vocab fix-up. ``--live N`` also plays N files through the real threaded worker in real
time and measures how long after the end of speech the final text is ready.

``--set fixtures/command-set`` scores the 30 commands from `nova record-set` instead: app names
heard wrong, wake names missed, empty outputs, WER and decode time, per ``--backend``; with
``--backend both`` it also applies Stage 07a's adoption rule for the name-biasing candidate.
"""

from __future__ import annotations

import gc
import json
import re
import statistics
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .. import paths
from ..actions.catalog import key_of, parse_aliases, short_name
from ..audio import WavFeed, read_wav
from ..bus import Bus
from ..events import FinalTranscript, SpeechEnded, SpeechStarted
from ..gate import WakeName, strip_wake
from ..recordset import MANIFEST, Line, read_manifest
from ..vad import FRAME, SAMPLE_RATE, EndpointRules, Endpointer, EnergyGate, Silero
from ..vocab import Vocab, VocabFile
from .wer import corpus_wer, word_errors

CLIPPED_S = 0.25  # speech this close to the start of a file: the recorder began mid-word
BACKENDS = ("parakeet", "sherpa")


def load_backend(name: str):
    """A warmed-up transcriber: ``parakeet`` (onnx-asr, greedy; Nova's default) or ``sherpa`` (the candidate)."""
    if name == "parakeet":
        from . import parakeet

        return parakeet.load(paths.MODELS)
    if name == "sherpa":
        from . import sherpa

        return sherpa.load(paths.MODELS)
    raise ValueError(f"unknown backend {name!r} (parakeet or sherpa)")


@dataclass(frozen=True)
class Item:
    session: str
    label: str
    prompt: str
    expected: str
    wav: Path


@dataclass
class Row:
    item: Item
    clipped: bool = False
    full: str = ""
    path: str = ""
    vocab: str = ""
    segments: int = 0
    stt_ms: list[float] = field(default_factory=list)
    audio_s: list[float] = field(default_factory=list)


def find_items(root: Path) -> tuple[list[Item], list[Path]]:
    """Recordings with expected text, plus WAVs in folders without a manifest (timed only)."""
    items: list[Item] = []
    sessions: set[Path] = set()
    for manifest in sorted(Path(root).glob("*/manifest.json")):
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        sessions.add(manifest.parent)
        label = str(data.get("session_label") or manifest.parent.name)
        for prompt in data.get("prompts", []):
            wav, text = prompt.get("selected_wav"), prompt.get("expected_text")
            if isinstance(wav, str) and isinstance(text, str) and (manifest.parent / wav).is_file():
                items.append(Item(manifest.parent.name, label, str(prompt.get("id", "?")), text, manifest.parent / wav))
    others = sorted(p for p in Path(root).rglob("*.wav") if p.parent not in sessions)
    return items, others


def segment(audio: np.ndarray, vad: Silero, rules: EndpointRules | None = None) -> tuple[list[np.ndarray], float | None]:
    """The live VAD path on a whole file: speech segments, and when speech first started (s)."""
    vad.reset()
    gate, ep = EnergyGate(), Endpointer(rules)
    pad = (-len(audio)) % FRAME + SAMPLE_RATE
    audio = np.concatenate((audio.astype(np.float32), np.zeros(pad, dtype=np.float32)))
    out: list[np.ndarray] = []
    first: float | None = None
    for pos in range(0, len(audio), FRAME):
        frame = audio[pos : pos + FRAME]
        if gate.check(float(np.dot(frame, frame)) / FRAME, ep.listening):
            p = vad.probability(frame)
        else:
            vad.skip(frame)
            p = 0.0
        event = ep.push(pos, frame, p)
        if isinstance(event, SpeechStarted) and first is None:
            first = event.at
        elif isinstance(event, SpeechEnded):
            out.append(event.pcm)
    last = ep.end_now()
    if last is not None:
        out.append(last.pcm)
    return out, first


def _pct(values: Sequence[float], q: int) -> float:
    if not values:
        return float("nan")
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=100, method="inclusive")[q - 1]


def _wer_line(name: str, rows: Sequence[Row]) -> str:
    def wer(attr: str) -> str:
        return f"{100 * corpus_wer((r.item.expected, getattr(r, attr)) for r in rows):6.1f} %"

    return f"  {name:<22}{len(rows):>5}   {wer('full')}   {wer('path')}   {wer('vocab')}"


def run(live: int = 0, out=print, backend: str = "parakeet") -> int:
    items, others = find_items(paths.RECORDINGS)
    began = time.perf_counter()
    stt = load_backend(backend)
    vad = Silero(paths.MODELS / "silero" / "silero_vad.onnx")
    out(f"nova bench-stt · {stt.name} · loaded in {(time.perf_counter() - began):.1f} s (+ warm-up)")
    vocab_file = VocabFile(paths.VOCAB)
    vocab: Vocab = vocab_file.current()
    if not items:
        out(f"No recordings with expected text under {paths.RECORDINGS}: timings only.")
    sessions = sorted({i.label for i in items})
    out(f"{len(items)} recordings with expected text ({', '.join(sessions) or 'none'}); {len(others)} more without text (timed only)\n")

    rows: list[Row] = []
    for item in items:
        audio = read_wav(item.wav)
        row = Row(item)
        segments, first = segment(audio, vad)
        row.clipped = first is not None and first < CLIPPED_S
        row.segments = len(segments)
        texts = []
        for pcm in segments:
            t = time.perf_counter()
            texts.append(stt.transcribe(pcm))
            row.stt_ms.append((time.perf_counter() - t) * 1000)
            row.audio_s.append(len(pcm) / SAMPLE_RATE)
        row.path = " ".join(texts)
        row.vocab = vocab.correct(row.path).text
        row.full = stt.transcribe(audio)
        rows.append(row)
    extra_ms: list[float] = []
    extra_audio: list[float] = []
    for wav in others:
        segments, _ = segment(read_wav(wav), vad)
        for pcm in segments:
            t = time.perf_counter()
            stt.transcribe(pcm)
            extra_ms.append((time.perf_counter() - t) * 1000)
            extra_audio.append(len(pcm) / SAMPLE_RATE)

    if rows:
        out("WER, case / punctuation / numbers normalized")
        out(f"  {'set':<22}{'files':>5}   STT alone   Nova path    + vocab")
        for label in sessions:
            out(_wer_line(label, [r for r in rows if r.item.label == label]))
        out(_wer_line("all", rows))
        clean = [r for r in rows if not r.clipped]
        out(_wer_line("clean starts only", clean))
        clipped = len(rows) - len(clean)
        if clipped:
            by = {label: sum(r.clipped for r in rows if r.item.label == label) for label in sessions}
            detail = ", ".join(f"{label} {n}/{sum(r.item.label == label for r in rows)}" for label, n in by.items() if n)
            out(f"  {clipped} recordings begin mid-word (speech within {CLIPPED_S * 1000:.0f} ms of the start: "
                f"the old recorder started late), so their first words are missing: {detail}")
        split = sum(r.segments > 1 for r in rows)
        empty = sum(r.segments == 0 for r in rows)
        out(f"  Nova path: {split} recordings split into 2+ utterances at a pause; {empty} had no speech found")

    stt_ms = [ms for r in rows for ms in r.stt_ms] + extra_ms
    audio_s = [s for r in rows for s in r.audio_s] + extra_audio
    out("\nDecode time per utterance (Nova path)")
    out(f"  stt_ms p50 {_pct(stt_ms, 50):.0f} · p95 {_pct(stt_ms, 95):.0f} · max {max(stt_ms, default=float('nan')):.0f}"
        f"   (n={len(stt_ms)}, audio p50 {_pct(audio_s, 50):.1f} s, max {max(audio_s, default=0):.1f} s)")
    for low, high in ((0, 2), (2, 4), (4, 60)):
        sel = [ms for ms, s in zip(stt_ms, audio_s, strict=True) if low <= s < high]
        if sel:
            out(f"  {low}-{high if high < 60 else '+'} s audio: p50 {_pct(sel, 50):.0f} ms (n={len(sel)})")

    if rows:
        worst = sorted(rows, key=lambda r: -word_errors(r.item.expected, r.vocab)[0])[:10]
        out("\nMost word errors (Nova path + vocab)")
        for r in worst:
            errors, n = word_errors(r.item.expected, r.vocab)
            if errors:
                flag = " (begins mid-word)" if r.clipped else ""
                out(f"  {r.item.label}/{r.item.prompt} {errors}/{n}{flag}: \"{r.item.expected}\" -> \"{r.vocab}\"")

    if live and backend != "parakeet":
        out("\n(--live replays through Nova's own worker, which uses Parakeet: skipped for this backend)")
    elif live:
        clean = [r.item for r in rows if not r.clipped] or [r.item for r in rows]
        picked = clean[:: max(1, len(clean) // live)][:live]
        if picked:
            _live(picked, out)
    return 0


def _live(items: Sequence[Item], out) -> None:
    from ..speech import standard_worker

    out(f"\nLive replay: {len(items)} recordings in real time through the live worker (gate, Silero, endpoints, lanes)")
    bus = Bus()
    finals: list[FinalTranscript] = []
    lock = threading.Lock()

    def on_final(event: FinalTranscript) -> None:
        if event.text:
            with lock:
                finals.append(event)

    bus.subscribe(FinalTranscript, on_final)
    feeds: list[WavFeed] = []

    def source(ring, wake):
        feed = WavFeed(ring, wake, [item.wav for item in items], gap_s=1.5)
        feeds.append(feed)
        return feed

    worker = standard_worker(bus, vocab=VocabFile(paths.VOCAB), trace=None, source=source)
    worker.start()
    try:
        while not worker.ready.wait(0.1):
            pass
        while not (feeds and feeds[0].done.is_set() and worker.idle):
            time.sleep(0.1)
        time.sleep(0.3)
    finally:
        worker.close()
    final_ms = [f.final_ms for f in finals]
    stt_ms = [f.stt_ms for f in finals]
    early = sum(f.early for f in finals)
    out(f"  final text after end of speech: p50 {_pct(final_ms, 50):.0f} ms · p95 {_pct(final_ms, 95):.0f} ms · max {max(final_ms, default=float('nan')):.0f}"
        f"   (n={len(finals)}; includes the 300/700 ms endpoint wait)")
    out(f"  decode p50 {_pct(stt_ms, 50):.0f} ms; decode started early in the pause for {early}/{len(finals)}")


# --- the recorded command set (`nova record-set`) ------------------------------------------------------------

_TOKEN = re.compile(r"[^\W_]+(?:['’][^\W_]+)*")


def _name_words(text: str) -> list[str]:
    """Words as the gate reads them: lowercase, no possessive "'s", no apostrophes."""
    words = []
    for match in _TOKEN.finditer(text):
        word = match.group().lower().replace("’", "'")
        if word.endswith("'s"):
            word = word[:-2]
        words.append(word.replace("'", ""))
    return words


def _is_url(target: str) -> bool:
    return target.startswith(("http://", "https://"))


def entity_keys(entity: str, aliases: Mapping[str, str]) -> frozenset[str]:
    """Squashed spellings that make Nova act on the same thing as ``entity``: the name itself, every alias with
    the same target, and the target app's full and vendor-less names ("Chrome" ~ "Google Chrome")."""
    key = key_of(entity)
    targets = {t for p, t in aliases.items() if key_of(p) == key}
    targets |= {t for t in aliases.values() if not _is_url(t) and key in (key_of(t), key_of(short_name(t)))}
    keys = {key} | {key_of(p) for p, t in aliases.items() if t in targets}
    for target in targets:
        if not _is_url(target):
            keys |= {key_of(target), key_of(short_name(target))}
    return frozenset(k for k in keys if k)


def heard_entity(keys: Iterable[str], text: str) -> bool:
    """Does a run of whole words in ``text`` squash to one of ``keys`` ("chat GPT" -> "chatgpt")?"""
    keys = frozenset(keys)
    words = _name_words(text)
    for i in range(len(words)):
        joined = ""
        for word in words[i : i + 6]:
            joined += word
            if joined in keys:
                return True
    return False


@dataclass(frozen=True)
class SetResult:
    line: Line
    heard: str  # Nova path + vocab fix-up: the text the gate and Tier 0 would get
    segments: tuple[str, ...]  # raw text of each decoded speech segment
    ms: tuple[float, ...]  # decode time of each segment


@dataclass(frozen=True)
class SetScore:
    items: int
    entities: int
    entity_errors: int
    wake_items: int
    wake_misses: int
    empty: int  # decodes that returned no text
    decodes: int
    silent_items: int  # commands that ended with no text at all
    wer: float
    p50: float
    p95: float
    entity_misses: tuple[SetResult, ...]


def score_set(results: Sequence[SetResult], aliases: Mapping[str, str], wake: WakeName) -> SetScore:
    entities = errors = 0
    misses: list[SetResult] = []
    for result in results:
        wrong = [e for e in result.line.entities if not heard_entity(entity_keys(e, aliases), result.heard)]
        entities += len(result.line.entities)
        errors += len(wrong)
        if wrong:
            misses.append(result)
    ms = [m for r in results for m in r.ms]
    return SetScore(
        items=len(results),
        entities=entities,
        entity_errors=errors,
        wake_items=sum(r.line.has_wake for r in results),
        wake_misses=sum(r.line.has_wake and strip_wake(r.heard, wake) is None for r in results),
        empty=sum(not text.strip() for r in results for text in r.segments),
        decodes=sum(len(r.segments) for r in results),
        silent_items=sum(not r.heard.strip() for r in results),
        wer=corpus_wer((r.line.text, r.heard) for r in results),
        p50=_pct(ms, 50),
        p95=_pct(ms, 95),
        entity_misses=tuple(misses),
    )


def adopt(base: SetScore, candidate: SetScore) -> tuple[bool, str]:
    """Stage 07a's fixed rule: app-name errors at least halve and drop by >= 3, p95 rises <= 100 ms,
    empty outputs don't rise."""
    fixed = base.entity_errors - candidate.entity_errors
    names = candidate.entity_errors * 2 <= base.entity_errors and fixed >= 3
    slower = candidate.p95 - base.p95
    ok = names and slower <= 100 and candidate.empty <= base.empty
    why = (f"app-name errors {base.entity_errors} -> {candidate.entity_errors} (needs half and at least 3 fewer), "
           f"p95 {base.p95:.0f} -> {candidate.p95:.0f} ms ({slower:+.0f}, needs <= +100), "
           f"empty outputs {base.empty} -> {candidate.empty} (must not rise)")
    return ok, why


def wake_verdict(score: SetScore) -> str:
    if not score.wake_items:
        return "No recorded line starts with the wake name."
    said = f"Wake name missed {score.wake_misses} of {score.wake_items}"
    if score.wake_misses * 15 > score.wake_items:
        return f"{said}: more than 1 in 15, so revisit the wake-name design (SPEC §4)."
    return f"{said}: within 1 in 15."


def find_set(folder: Path) -> list[tuple[Line, Path]]:
    """Recorded lines of a `nova record-set` folder whose WAV is on disk."""
    folder = Path(folder)
    lines, recorded = read_manifest(folder / MANIFEST)
    return [(line, folder / recorded[line.id]) for line in lines
            if line.id in recorded and (folder / recorded[line.id]).is_file()]


def _print_score(score: SetScore, out) -> None:
    out(f"  app names wrong    {score.entity_errors} of {score.entities}")
    out(f"  wake name missed   {score.wake_misses} of {score.wake_items}")
    out(f"  empty outputs      {score.empty} of {score.decodes} decodes ({score.silent_items} commands with no text)")
    out(f"  WER                {100 * score.wer:.1f} %  (Nova path + vocab fix-up, case / punctuation normalized)")
    out(f"  decode             p50 {score.p50:.0f} ms · p95 {score.p95:.0f} ms  (n={score.decodes})")
    if score.entity_misses:
        out("  app names heard wrong:")
        for r in score.entity_misses:
            out(f"    {r.line.id} \"{r.line.text}\" -> \"{r.heard}\"")


def run_set(folder: Path, backends: Sequence[str] = ("parakeet",), out=print) -> int:
    """Score the recorded command set with each backend on the same speech segments."""
    from ..config import ConfigStore

    items = find_set(folder)
    if not items:
        out(f"No recorded commands in {folder}. Record them first: uv run nova record-set")
        return 1
    vad = Silero(paths.MODELS / "silero" / "silero_vad.onnx")
    prepared = [(line, segment(read_wav(wav), vad)[0]) for line, wav in items]
    vocab = VocabFile(paths.VOCAB).current()
    aliases = parse_aliases(paths.ALIASES.read_text(encoding="utf-8-sig"))
    settings = ConfigStore(paths.SETTINGS).load()
    wake = WakeName.of(settings.wake_name, settings.wake_spellings)
    out(f"nova bench-stt --set · {len(items)} recorded commands in {folder} "
        f"({sum(line.has_wake for line, _ in items)} start with \"{settings.wake_name}\")")
    scores: dict[str, SetScore] = {}
    for name in backends:
        began = time.perf_counter()
        stt = load_backend(name)
        out(f"\n{stt.name} · loaded in {time.perf_counter() - began:.1f} s (+ warm-up)")
        results = []
        for line, segments in prepared:
            texts, ms = [], []
            for pcm in segments:
                t = time.perf_counter()
                texts.append(stt.transcribe(pcm))
                ms.append((time.perf_counter() - t) * 1000)
            heard = vocab.correct(" ".join(t for t in texts if t)).text
            results.append(SetResult(line, heard, tuple(texts), tuple(ms)))
        scores[name] = score_set(results, aliases, wake)
        _print_score(scores[name], out)
        del stt
        gc.collect()
    if "parakeet" in scores and "sherpa" in scores:
        ok, why = adopt(scores["parakeet"], scores["sherpa"])
        out(f"\nAdopt sherpa + hotwords? {'YES' if ok else 'no'}: {why}")
    out("\n" + wake_verdict(scores[backends[0]]))
    return 0
