"""scripts/get_models.py: which files are missing, resumable download, SHA-256 check (no network)."""

import hashlib
import importlib.util
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "get_models", Path(__file__).resolve().parent.parent / "scripts" / "get_models.py"
)
gm = importlib.util.module_from_spec(_SPEC)
sys.modules["get_models"] = gm  # dataclasses look their module up while the class is being built
_SPEC.loader.exec_module(gm)


def _model(data: bytes = b"hello"):
    return gm.Model("x/a.bin", "https://example.invalid/a.bin", len(data), hashlib.sha256(data).hexdigest())


class _Response:
    def __init__(self, data: bytes, status: int = 200) -> None:
        self._data = data
        self.status = status

    def read(self, n: int) -> bytes:
        chunk, self._data = self._data[:n], self._data[n:]
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> bool:
        return False


def test_missing_lists_absent_wrong_hash_and_accepts_good(tmp_path):
    model = _model()
    assert gm.missing(tmp_path, (model,)) == [model]
    (tmp_path / "x").mkdir()
    (tmp_path / "x" / "a.bin").write_bytes(b"hellO")  # same size, wrong bytes
    assert gm.missing(tmp_path, (model,)) == [model]
    (tmp_path / "x" / "a.bin").write_bytes(b"hello")
    assert gm.missing(tmp_path, (model,)) == []


def test_download_verifies_then_renames(tmp_path):
    model = _model()
    gm.download(model, tmp_path / model.path, opener=lambda request, timeout: _Response(b"hello"), say=lambda *_, **__: None)
    assert (tmp_path / model.path).read_bytes() == b"hello"
    assert not (tmp_path / "x" / "a.bin.part").exists()


def test_download_resumes_a_partial_file(tmp_path):
    model = _model()
    part = tmp_path / "x" / "a.bin.part"
    part.parent.mkdir()
    part.write_bytes(b"he")
    seen = {}

    def opener(request, timeout):
        seen["range"] = request.get_header("Range")
        return _Response(b"llo", status=206)

    gm.download(model, tmp_path / model.path, opener=opener, say=lambda *_, **__: None)
    assert seen["range"] == "bytes=2-"
    assert (tmp_path / model.path).read_bytes() == b"hello"


def test_download_starts_over_when_the_server_ignores_the_range(tmp_path):
    model = _model()
    part = tmp_path / "x" / "a.bin.part"
    part.parent.mkdir()
    part.write_bytes(b"he")
    gm.download(model, tmp_path / model.path, opener=lambda request, timeout: _Response(b"hello"), say=lambda *_, **__: None)
    assert (tmp_path / model.path).read_bytes() == b"hello"


def test_download_rejects_wrong_bytes_and_keeps_nothing(tmp_path):
    model = _model()
    with pytest.raises(RuntimeError, match="SHA-256"):
        gm.download(model, tmp_path / model.path, opener=lambda request, timeout: _Response(b"jello"),
                    say=lambda *_, **__: None)
    assert not (tmp_path / model.path).exists()
    assert not (tmp_path / "x" / "a.bin.part").exists()


def test_check_mode_reports_without_downloading(tmp_path, capsys):
    assert gm.main(["--check", "--dir", str(tmp_path)]) == 1
    assert "parakeet/encoder-model.int8.onnx" in capsys.readouterr().out


def test_manifest_is_pinned_and_matches_doctor():
    from nova.doctor import REQUIRED_MODELS

    assert {m.path for m in gm.MODELS} == set(REQUIRED_MODELS)
    for model in gm.MODELS:
        assert len(model.sha256) == 64 and model.size > 0
        assert "/main/" not in model.url and "/master/" not in model.url  # a commit or a release tag
