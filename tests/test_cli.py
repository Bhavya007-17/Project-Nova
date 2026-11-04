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


def test_eval_and_bench_dispatch(monkeypatch):
    seen = []
    monkeypatch.setattr("nova.evals.main", lambda **kw: seen.append(("eval", kw)) or 0)
    monkeypatch.setattr("nova.bench_llm.main", lambda models, **kw: seen.append(("bench", models, kw)) or 0)
    assert cli.main(["eval", "--tier1", "--model", "qwen3.5:4b", "--device", "cpu", "--limit", "5"]) == 0
    assert cli.main(["bench-llm", "a:1b", "b:2b", "--devices", "gpu,arc", "--save"]) == 0
    assert seen[0] == ("eval", {"model": "qwen3.5:4b", "device": "cpu", "tier1_only": True, "limit": 5,
                                "misses": True})
    assert seen[1] == ("bench", ("a:1b", "b:2b"), {"devices": ("gpu", "arc"), "cpu_cases": 40, "save": True})


def test_say_and_latency_dispatch(monkeypatch):
    seen = []
    monkeypatch.setattr("nova.say.main", lambda text, dry=False: seen.append((text, dry)) or 0)
    monkeypatch.setattr("nova.latency.main", lambda: seen.append("latency") or 0)
    assert cli.main(["say", "open chrome"]) == 0
    assert cli.main(["say", "--dry", "new tab"]) == 0
    assert cli.main(["latency"]) == 0
    assert seen == [("open chrome", False), ("new tab", True), "latency"]


def test_say_needs_text():
    with pytest.raises(SystemExit):
        cli.main(["say"])


def test_listen_dispatch(monkeypatch, tmp_path):
    seen = {}

    def fake(device=None, wavs=(), seconds=None, gate=False):
        seen.update(device=device, wavs=wavs, seconds=seconds, gate=gate)
        return 0

    monkeypatch.setattr("nova.listen.main", fake)
    assert cli.main(["listen"]) == 0
    assert seen == {"device": None, "wavs": (), "seconds": None, "gate": False}
    wav = tmp_path / "a.wav"
    assert cli.main(["listen", "--device", "Jabra", "--wav", str(wav), "--seconds", "30", "--gate"]) == 0
    assert seen == {"device": "Jabra", "wavs": [wav], "seconds": 30.0, "gate": True}


def test_keys_probe_dispatch(monkeypatch):
    seen = []
    monkeypatch.setattr("nova.keyprobe.main", lambda seconds=20.0, raw=False: seen.append((seconds, raw)) or 0)
    assert cli.main(["keys", "--probe"]) == 0
    assert cli.main(["keys", "--probe", "--seconds", "5", "--raw"]) == 0
    assert seen == [(20.0, False), (5.0, True)]
    with pytest.raises(SystemExit):
        cli.main(["keys"])  # --probe is the only thing it does


def test_bench_stt_dispatch(monkeypatch):
    seen = []
    monkeypatch.setattr("nova.stt.bench.run", lambda live=0: seen.append(live) or 0)
    assert cli.main(["bench-stt"]) == 0
    assert cli.main(["bench-stt", "--live", "5"]) == 0
    assert seen == [0, 5]
