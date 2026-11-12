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


# --- archives (the optional sherpa-onnx copy) -----------------------------------------------------------------


def _tarball(files: dict[str, bytes], extra: dict[str, bytes] | None = None) -> bytes:
    import io
    import tarfile

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:bz2") as tar:
        for name, data in {**files, **(extra or {})}.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _archive(files: dict[str, bytes], blob: bytes, pins: dict[str, bytes] | None = None):
    pins = pins or files
    members = tuple(
        gm.Member(f"s/{name.rsplit('/', 1)[-1]}", name, len(data), hashlib.sha256(data).hexdigest())
        for name, data in pins.items()
    )
    return gm.Archive("s.tar.bz2", "https://example.invalid/s.tar.bz2", len(blob), hashlib.sha256(blob).hexdigest(),
                      members)


def test_fetch_archive_extracts_only_the_pinned_files_then_deletes_the_archive(tmp_path):
    files = {"pkg/encoder.onnx": b"enc", "pkg/tokens.txt": b"a 0\nb 1\n"}
    blob = _tarball(files, extra={"pkg/test_wavs/0.wav": b"RIFF", "../evil.txt": b"no"})
    archive = _archive(files, blob)
    assert gm.missing_members(tmp_path, archive) == list(archive.files)
    gm.fetch_archive(archive, tmp_path, opener=lambda request, timeout: _Response(blob), say=lambda *_, **__: None)
    assert (tmp_path / "s" / "encoder.onnx").read_bytes() == b"enc"
    assert (tmp_path / "s" / "tokens.txt").read_bytes() == b"a 0\nb 1\n"
    assert not (tmp_path / "s.tar.bz2").exists()
    assert sorted(p.name for p in (tmp_path / "s").iterdir()) == ["encoder.onnx", "tokens.txt"]
    assert not (tmp_path.parent / "evil.txt").exists()
    assert gm.missing_members(tmp_path, archive) == []


def test_extract_rejects_a_member_with_other_bytes(tmp_path):
    files = {"pkg/encoder.onnx": b"enc"}
    blob = _tarball(files)
    archive = _archive(files, blob, pins={"pkg/encoder.onnx": b"ENC"})
    (tmp_path / "s.tar.bz2").write_bytes(blob)
    with pytest.raises(RuntimeError, match="SHA-256"):
        gm.extract(archive, tmp_path / "s.tar.bz2", tmp_path)
    assert not (tmp_path / "s" / "encoder.onnx").exists()
    assert not (tmp_path / "s" / "encoder.onnx.part").exists()


def test_extract_reports_a_missing_member(tmp_path):
    blob = _tarball({"pkg/encoder.onnx": b"enc"})
    archive = _archive({"pkg/encoder.onnx": b"enc", "pkg/joiner.onnx": b"j"}, blob)
    (tmp_path / "s.tar.bz2").write_bytes(blob)
    with pytest.raises(RuntimeError, match="lacks pkg/joiner.onnx"):
        gm.extract(archive, tmp_path / "s.tar.bz2", tmp_path)


def test_sherpa_flag_checks_its_files_too(tmp_path, capsys):
    assert gm.main(["--check", "--sherpa", "--dir", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "sherpa-parakeet/encoder.int8.onnx" in out
    assert "parakeet/encoder-model.int8.onnx" in out


def test_sherpa_archive_is_pinned():
    archive = gm.SHERPA
    assert archive.url.startswith("https://github.com/k2-fsa/sherpa-onnx/releases/download/")
    assert len(archive.sha256) == 64 and archive.size > 0
    assert {m.path.split("/")[-1] for m in archive.files} == {"encoder.int8.onnx", "decoder.int8.onnx",
                                                               "joiner.int8.onnx", "tokens.txt"}
    for member in archive.files:
        assert member.path.startswith("sherpa-parakeet/")
        assert len(member.sha256) == 64 and member.size > 0
