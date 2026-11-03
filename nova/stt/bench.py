"""`nova bench-stt`: WER and decode time on Bhavya's voicebar recordings (SPEC §4, §18).

Reference text is each prompt's ``expected_text`` (what the recorder showed him to read).
Three numbers per set: "STT alone" decodes the whole file; "Nova path" runs the live
gate + Silero + endpoint rules and decodes the segments it finds; "+ vocab" adds the
vocab fix-up. ``--live N`` also plays N files through the real threaded worker in real
time and measures how long after the end of speech the final text is ready.
"""

from __future__ import annotations

import json
import statistics
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .. import paths
from ..audio import WavFeed, read_wav
from ..bus import Bus
from ..events import FinalTranscript, SpeechEnded, SpeechStarted
from ..vad import FRAME, SAMPLE_RATE, EndpointRules, Endpointer, EnergyGate, Silero
from ..vocab import Vocab, VocabFile
from .wer import corpus_wer, word_errors

CLIPPED_S = 0.25  # speech this close to the start of a file: the recorder began mid-word


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


def run(live: int = 0, out=print) -> int:
    items, others = find_items(paths.RECORDINGS)
    from . import parakeet

    began = time.perf_counter()
    stt = parakeet.load(paths.MODELS)
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

    if live:
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
