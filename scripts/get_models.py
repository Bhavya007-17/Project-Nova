"""Download Nova's speech models into models/ (one time, ~670 MB), each checked by size and SHA-256.

Nova itself never downloads anything at runtime; this setup script is the one place that does.
Files that are already there and match are skipped; an interrupted download resumes.

    uv run python scripts/get_models.py           # download what's missing
    uv run python scripts/get_models.py --check   # only verify (exit code 1 if anything is missing)
    uv run python scripts/get_models.py --sherpa  # also the Stage 07a candidate decoder's copy (~490 MB download)

Sources (pinned):
- Parakeet TDT 0.6B v3, int8 ONNX for onnx-asr (speech-to-text, CC-BY-4.0):
  huggingface.co/istupakov/parakeet-tdt-0.6b-v3-onnx at commit 8f23f0c
- Silero VAD v6.2 (speech start / end, MIT): github.com/snakers4/silero-vad, tag v6.2
- Optional (``--sherpa``): the same Parakeet v3 int8 in sherpa-onnx's layout (encoder / decoder / joiner +
  tokens), for the name-biasing candidate `nova bench-stt --backend sherpa`: the
  ``sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8.tar.bz2`` asset of github.com/k2-fsa/sherpa-onnx release
  ``asr-models``. That tag is re-used for new uploads, so the archive and every extracted file are pinned by SHA-256.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import tarfile
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_HF = "https://huggingface.co/istupakov/parakeet-tdt-0.6b-v3-onnx/resolve/8f23f0c03c8761650bdb5b40aaf3e40d2c15f1ce/"
_SILERO = "https://github.com/snakers4/silero-vad/raw/v6.2/src/silero_vad/data/silero_vad.onnx"


@dataclass(frozen=True)
class Model:
    path: str  # relative to models/
    url: str
    size: int
    sha256: str


MODELS = (
    Model("parakeet/encoder-model.int8.onnx", _HF + "encoder-model.int8.onnx", 652_183_999,
          "6139d2fa7e1b086097b277c7149725edbab89cc7c7ae64b23c741be4055aff09"),
    Model("parakeet/decoder_joint-model.int8.onnx", _HF + "decoder_joint-model.int8.onnx", 18_202_004,
          "eea7483ee3d1a30375daedc8ed83e3960c91b098812127a0d99d1c8977667a70"),
    Model("parakeet/vocab.txt", _HF + "vocab.txt", 93_939,
          "d58544679ea4bc6ac563d1f545eb7d474bd6cfa467f0a6e2c1dc1c7d37e3c35d"),
    Model("parakeet/config.json", _HF + "config.json", 97,
          "666903c76b9798caf2c210afd4f6cd60b08a8dbf9800ec8d7a3bc0d2148ac466"),
    Model("silero/silero_vad.onnx", _SILERO, 2_327_524,
          "1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3"),
)

@dataclass(frozen=True)
class Member:
    """One file taken out of a downloaded archive."""

    path: str  # relative to models/
    name: str  # its path inside the archive
    size: int
    sha256: str


@dataclass(frozen=True)
class Archive:
    path: str  # where the archive itself is kept while it's needed, relative to models/
    url: str
    size: int
    sha256: str
    files: tuple[Member, ...]


_SHERPA_DIR = "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8/"
SHERPA = Archive(
    "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8.tar.bz2",
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8.tar.bz2",
    487_211_443,
    "1495e91b59d7fc53ddca91f3431758ec99f8fa253fdb0e1e851be0f72b14b0f9",
    (
        Member("sherpa-parakeet/encoder.int8.onnx", _SHERPA_DIR + "encoder.int8.onnx", 652_282_294,
               "1afbe69c1ffdabee94755f42e4daf1d93549131a3da11356bda84fcef0f2c479"),
        Member("sherpa-parakeet/decoder.int8.onnx", _SHERPA_DIR + "decoder.int8.onnx", 11_845_274,
               "0222a86147299d4dee90edf95b6d13827edb42ad8b7c9cf0b2582f030937c0b5"),
        Member("sherpa-parakeet/joiner.int8.onnx", _SHERPA_DIR + "joiner.int8.onnx", 6_355_277,
               "cbd02f3696e9944970111fb439d9c3fd365a80374565f13f2b9c7007d253fc98"),
        Member("sherpa-parakeet/tokens.txt", _SHERPA_DIR + "tokens.txt", 93_939,
               "d58544679ea4bc6ac563d1f545eb7d474bd6cfa467f0a6e2c1dc1c7d37e3c35d"),
    ),
)

_CHUNK = 1 << 20


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(_CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def matches(path: Path, model: Model) -> bool:
    return path.is_file() and path.stat().st_size == model.size and sha256(path) == model.sha256


def missing(root: Path, models: Sequence[Model] = MODELS) -> list[Model]:
    return [model for model in models if not matches(root / model.path, model)]


def download(model: Model, dest: Path, *, opener: Callable = urllib.request.urlopen,
             say: Callable[..., None] = print) -> None:
    """Fetch into ``<dest>.part`` (resuming a partial one), verify, then move into place."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    have = part.stat().st_size if part.exists() else 0
    if have >= model.size:  # a finished-but-bad or oversized leftover: start over
        part.unlink()
        have = 0
    request = urllib.request.Request(model.url, headers={"User-Agent": "nova-setup"})
    if have:
        request.add_header("Range", f"bytes={have}-")
    with opener(request, timeout=60) as response:
        if have and getattr(response, "status", 200) != 206:  # the server ignored the range
            have = 0
        done, shown = have, -1
        with part.open("ab" if have else "wb") as out:
            while chunk := response.read(_CHUNK):
                out.write(chunk)
                done += len(chunk)
                percent = done * 100 // model.size
                if percent != shown and percent % 10 == 0:
                    say(f"  {model.path}: {percent}%", flush=True)
                    shown = percent
    if not matches(part, model):
        part.unlink(missing_ok=True)
        raise RuntimeError(f"{model.path}: the download didn't match the expected size / SHA-256. Run me again.")
    part.replace(dest)


def missing_members(root: Path, archive: Archive) -> list[Member]:
    return [m for m in archive.files if not matches(root / m.path, Model(m.path, "", m.size, m.sha256))]


def extract(archive: Archive, source: Path, root: Path) -> None:
    """Take the listed files (and nothing else) out of ``source``, each checked by size and SHA-256.

    Members are written to their fixed paths under ``root``; names inside the archive never pick a path.
    """
    wanted = {m.name: m for m in missing_members(root, archive)}
    with tarfile.open(source, "r|*") as tar:
        for info in tar:
            member = wanted.get(info.name)
            if member is None:
                continue
            if not info.isfile():
                raise RuntimeError(f"{info.name} in {archive.path} is not a plain file")
            dest = root / member.path
            dest.parent.mkdir(parents=True, exist_ok=True)
            part = dest.with_name(dest.name + ".part")
            reader = tar.extractfile(info)
            with part.open("wb") as out:
                while block := reader.read(_CHUNK):
                    out.write(block)
            if not matches(part, Model(member.path, "", member.size, member.sha256)):
                part.unlink(missing_ok=True)
                raise RuntimeError(f"{member.path}: the extracted file didn't match the expected size / SHA-256.")
            part.replace(dest)
            del wanted[info.name]
    if wanted:
        raise RuntimeError(f"{archive.path} lacks {', '.join(sorted(wanted))}")


def fetch_archive(archive: Archive, root: Path, *, opener: Callable = urllib.request.urlopen,
                  say: Callable[..., None] = print) -> None:
    """Download (resumable, verified), extract the listed files, then delete the archive."""
    whole = Model(archive.path, archive.url, archive.size, archive.sha256)
    source = root / archive.path
    if not matches(source, whole):
        download(whole, source, opener=opener, say=say)
    say(f"  {archive.path}: extracting {len(archive.files)} file(s)", flush=True)
    extract(archive, source, root)
    source.unlink()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Download / verify Nova's speech models.")
    parser.add_argument("--check", action="store_true", help="only verify; download nothing")
    parser.add_argument("--dir", type=Path, default=ROOT / "models", help="models folder (default: ./models)")
    parser.add_argument("--sherpa", action="store_true",
                        help="also the sherpa-onnx copy of Parakeet for `nova bench-stt --backend sherpa` (~490 MB)")
    args = parser.parse_args(argv)
    todo = missing(args.dir)
    archives = [SHERPA] if args.sherpa and missing_members(args.dir, SHERPA) else []
    if not todo and not archives:
        print(f"Models OK in {args.dir}")
        return 0
    if args.check:
        for model in todo:
            print(f"missing or different: {model.path}")
        for archive in archives:
            for member in missing_members(args.dir, archive):
                print(f"missing or different: {member.path}")
        return 1
    size = sum(m.size for m in todo) + sum(a.size for a in archives)
    print(f"Downloading {len(todo) + len(archives)} file(s), {size / 1e6:.0f} MB, into {args.dir}")
    for model in todo:
        download(model, args.dir / model.path)
    for archive in archives:
        fetch_archive(archive, args.dir)
    print("Models OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
