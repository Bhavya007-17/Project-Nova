"""Start with Windows: the command line written to the Run key."""

from nova.startup import command


def test_prefers_the_windowless_interpreter(tmp_path):
    python = tmp_path / "python.exe"
    python.touch()
    assert command(python) == f'"{python}" -m nova'  # no pythonw next to it: keep what runs
    (tmp_path / "pythonw.exe").touch()
    assert command(python) == f'"{tmp_path / "pythonw.exe"}" -m nova'
    assert command(tmp_path / "pythonw.exe").endswith('pythonw.exe" -m nova')
