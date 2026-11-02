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
    [(["listen"], "01"), (["say", "open chrome"], "04"), (["eval"], "05"), (["latency"], "11")],
)
def test_stubs_name_their_stage(argv, stage, capsys):
    assert cli.main(argv) == 1
    assert f"Stage {stage}" in capsys.readouterr().out


def test_say_needs_text():
    with pytest.raises(SystemExit):
        cli.main(["say"])
