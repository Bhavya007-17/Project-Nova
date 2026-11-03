import pytest

from nova import cli


def test_default_command_runs_tray(monkeypatch):
    calls = []
    monkeypatch.setattr("nova.app.run", lambda: calls.append("run") or 0)
    assert cli.main([]) == 0
    assert cli.main(["run"]) == 0
    assert calls == ["run", "run"]


def test_doctor_dispatch(monkeypatch):
    monkeypatch.setattr("nova.doctor.main", lambda: 1)
    assert cli.main(["doctor"]) == 1


@pytest.mark.parametrize(
    ("argv", "stage"),
    [(["say", "open chrome"], "04"), (["eval"], "05"), (["latency"], "11")],
)
def test_stubs_name_their_stage(argv, stage, capsys):
    assert cli.main(argv) == 1
    assert f"Stage {stage}" in capsys.readouterr().out


def test_say_needs_text():
    with pytest.raises(SystemExit):
        cli.main(["say"])


def test_listen_dispatch(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(
        "nova.listen.main", lambda device=None, wavs=(), seconds=None: seen.update(device=device, wavs=wavs, seconds=seconds) or 0
    )
    assert cli.main(["listen"]) == 0
    assert seen == {"device": None, "wavs": (), "seconds": None}
    wav = tmp_path / "a.wav"
    assert cli.main(["listen", "--device", "Jabra", "--wav", str(wav), "--seconds", "30"]) == 0
    assert seen == {"device": "Jabra", "wavs": [wav], "seconds": 30.0}


def test_bench_stt_dispatch(monkeypatch):
    seen = []
    monkeypatch.setattr("nova.stt.bench.run", lambda live=0: seen.append(live) or 0)
    assert cli.main(["bench-stt"]) == 0
    assert cli.main(["bench-stt", "--live", "5"]) == 0
    assert seen == [0, 5]
