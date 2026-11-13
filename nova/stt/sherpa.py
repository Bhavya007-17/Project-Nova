"""Stage 07a candidate: the same Parakeet TDT 0.6B v3 int8 through sherpa-onnx, beam search + hotwords.

Not on the live path: ``nova bench-stt --backend sherpa|both`` only, until the recorded command set shows it
halves app-name errors (SPEC §4 "Name biasing"). Hotwords are his own names, in the casing the model writes
names (cased, as he spells them): aliases, the apps they point at (catalog spelling), vocab terms and the wake name.

Checked on sherpa-onnx 2.0.1 (2026-11-12): the NeMo transducer decoder takes ``modified_beam_search`` with a
hotwords file on this TDT export. It splits hotwords into the model's pieces only with ``modeling_unit="bpe"``
plus a SentencePiece-style vocab, which the export lacks, so one is written from ``tokens.txt`` (score = -id).
With the default unit ("cjkchar") names don't map to the model's pieces: they fail to encode and are skipped.
On synthetic speech, boost 2.0 turned "OpenCrow Labs" / "Mujoco" / "toist" / "OpenChat GPT" into CroLabs, MuJoCo,
Todoist and ChatGPT; greedy and beam search without hotwords gave the same text as each other.
"""

from __future__ import annotations

import logging
import re
import tempfile
import time
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

import numpy as np

_log = logging.getLogger(__name__)

DIR = "sherpa-parakeet"
FILES = ("encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx", "tokens.txt")
SAMPLE_RATE = 16_000
HOTWORD_SCORE = 2.0  # per piece; 1.5 (the default) fixed fewer names on synthetic speech
BEAM = 4
_MIN_SAMPLES = SAMPLE_RATE // 10
_RETRY_MIN = SAMPLE_RATE // 2  # same padded retry as nova/stt/parakeet.py
_RETRY_PAD = np.zeros(int(0.3 * SAMPLE_RATE), dtype=np.float32)
_WORD = re.compile(r"[^\W_]+")


def hotword_lines(names: Iterable[str], score: float = HOTWORD_SCORE) -> list[str]:
    """Names -> hotwords-file lines ("Visual Studio Code :2"): words only, spelled as given, each name once."""
    seen: set[str] = set()
    lines = []
    for name in names:
        words = _WORD.findall(str(name))
        if not words:
            continue
        phrase = " ".join(words)
        if phrase.casefold() in seen:
            continue
        seen.add(phrase.casefold())
        lines.append(f"{phrase} :{score:g}")
    return lines


def bpe_vocab(tokens: str) -> str:
    """tokens.txt ("piece id" lines) -> a SentencePiece vocab ("piece<TAB>score"), score = -id (lower id = more
    common piece). Control tokens ("<unk>", "<|endoftext|>", "<blk>") are left out."""
    out = []
    for line in tokens.splitlines():
        piece, _, index = line.rpartition(" ")
        if piece and index.isdigit() and not piece.startswith("<"):
            out.append(f"{piece}\t{-int(index)}")
    return "\n".join(out) + "\n"


def names_for_hotwords(aliases: Mapping[str, str], apps: Sequence[str], terms: Sequence[str], wake: str) -> list[str]:
    """His names, written the way he spells them: wake name, vocab terms, aliases and the apps they point at.

    The installed-app list only lends its spelling ("chrome" -> "Chrome"). Boosting every installed app too
    (180 names here) raised WER on the clean recordings from 14.4 % to 19.0 %: common words pulled into names.
    """
    from ..actions.catalog import key_of, short_name
    from ..recordset import display_name

    known: dict[str, str] = {}
    for name in [*apps, *(short_name(a) for a in apps), *terms]:
        known.setdefault(key_of(name), name)
    targets = [t for t in aliases.values() if not t.startswith(("http://", "https://"))]
    return [
        wake,
        *terms,
        *(display_name(phrase, known) for phrase in aliases),
        *targets,
        *(short_name(t) for t in targets),
    ]


class SherpaTranscriber:
    """``transcribe(pcm, *, retry_empty=True) -> str`` like ``ParakeetTranscriber``; one stream per call."""

    def __init__(self, directory: Path, threads: int = 4, hotwords: Sequence[str] = (),
                 score: float = HOTWORD_SCORE, beam: int = BEAM) -> None:
        directory = Path(directory)
        missing = [name for name in FILES if not (directory / name).is_file()]
        if missing:
            raise FileNotFoundError(f"sherpa-onnx model files missing in {directory}: {', '.join(missing)} "
                                    "(uv run python scripts/get_models.py --sherpa)")
        import sherpa_onnx

        lines = hotword_lines(hotwords, score)
        began = time.perf_counter()
        with tempfile.TemporaryDirectory(prefix="nova-sherpa-") as tmp:  # read while the recognizer is built
            vocab = Path(tmp) / "bpe.vocab"
            vocab.write_text(bpe_vocab((directory / "tokens.txt").read_text(encoding="utf-8")), encoding="utf-8")
            hot = Path(tmp) / "hotwords.txt"
            hot.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
            self._recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(
                encoder=str(directory / "encoder.int8.onnx"),
                decoder=str(directory / "decoder.int8.onnx"),
                joiner=str(directory / "joiner.int8.onnx"),
                tokens=str(directory / "tokens.txt"),
                num_threads=threads,
                sample_rate=SAMPLE_RATE,
                decoding_method="modified_beam_search",
                max_active_paths=beam,
                hotwords_file=str(hot) if lines else "",
                hotwords_score=score,
                model_type="nemo_transducer",
                modeling_unit="bpe",
                bpe_vocab=str(vocab),
            )
        self.load_ms = (time.perf_counter() - began) * 1000
        self.hotwords = len(lines)
        self.name = f"parakeet-tdt-0.6b-v3 int8 sherpa-onnx beam {beam} + {len(lines)} hotwords cpu x{threads}"
        _log.info("loaded %s in %.1f s", self.name, self.load_ms / 1000)

    def warm_up(self, runs: int = 3) -> None:
        rng = np.random.default_rng(0)
        for i in range(runs):
            self.transcribe((rng.standard_normal(SAMPLE_RATE * (1 + i % 2)) * 0.003).astype(np.float32))

    def transcribe(self, pcm: np.ndarray, *, retry_empty: bool = True) -> str:
        audio = np.asarray(pcm, dtype=np.float32)
        if len(audio) < _MIN_SAMPLES:
            audio = np.pad(audio, (0, _MIN_SAMPLES - len(audio)))
        text = self._decode(audio)
        if not text and retry_empty and len(audio) >= _RETRY_MIN:
            text = self._decode(np.concatenate((_RETRY_PAD, audio, _RETRY_PAD)))
        return text

    def _decode(self, audio: np.ndarray) -> str:
        stream = self._recognizer.create_stream()
        stream.accept_waveform(SAMPLE_RATE, audio)
        self._recognizer.decode_stream(stream)
        return str(stream.result.text).strip()


def current_names() -> list[str]:
    """Hotword names from this checkout's aliases.yaml, cached app list, vocab.yaml and wake name."""
    from .. import paths
    from ..actions.catalog import CatalogStore, parse_aliases
    from ..config import ConfigStore
    from ..vocab import VocabFile

    store = CatalogStore(paths.CATALOG_CACHE, paths.ALIASES)
    store.load_cached()  # no Start-menu read here: the cached list, or none
    aliases = parse_aliases(paths.ALIASES.read_text(encoding="utf-8-sig"))
    terms = VocabFile(paths.VOCAB).current().terms
    wake = ConfigStore(paths.SETTINGS).load().wake_name
    return names_for_hotwords(aliases, [app.name for app in store.current().apps], terms, wake)


def load(models: Path, threads: int = 4) -> SherpaTranscriber:
    stt = SherpaTranscriber(Path(models) / DIR, threads, hotwords=current_names())
    stt.warm_up()
    return stt
