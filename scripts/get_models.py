"""Download Nova's speech models into models/ (one time, ~670 MB), each checked by size and SHA-256.

Nova itself never downloads anything at runtime; this setup script is the one place that does.
Files that are already there and match are skipped; an interrupted download resumes.

    uv run python scripts/get_models.py           # download what's missing
    uv run python scripts/get_models.py --check   # only verify (exit code 1 if anything is missing)

Sources (pinned):
- Parakeet TDT 0.6B v3, int8 ONNX for onnx-asr (speech-to-text, CC-BY-4.0):
  huggingface.co/istupakov/parakeet-tdt-0.6b-v3-onnx at commit 8f23f0c
- Silero VAD v6.2 (speech start / end, MIT): github.com/snakers4/silero-vad, tag v6.2
"""

from __future__ import annotations

import argparse
import hashlib
import sys
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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Download / verify Nova's speech models.")
    parser.add_argument("--check", action="store_true", help="only verify; download nothing")
    parser.add_argument("--dir", type=Path, default=ROOT / "models", help="models folder (default: ./models)")
    args = parser.parse_args(argv)
    todo = missing(args.dir)
    if not todo:
        print(f"Models OK in {args.dir}")
        return 0
    if args.check:
        for model in todo:
            print(f"missing or different: {model.path}")
        return 1
    print(f"Downloading {len(todo)} file(s), {sum(m.size for m in todo) / 1e6:.0f} MB, into {args.dir}")
    for model in todo:
        download(model, args.dir / model.path)
    print("Models OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
