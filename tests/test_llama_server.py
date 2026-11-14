"""llama-server as Tier 1's runtime (Stage 07a): the child process's life and the client in front of it.

No real llama-server here: a fake process, a fake clock, and one small HTTP server that plays both Ollama and
llama-server."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from nova.brain.llama_server import (
    RETRY_S,
    START_TIMEOUT_S,
    LlamaClient,
    LlamaServer,
    Serving,
    completion_body,
    connect,
    keep_alive_s,
    reply_of_completion,
    server_args,
    server_env,
)
from nova.brain.llm import SAMPLING, LLMCancelled, LLMError, Loaded, Ollama, Reply

EXE = Path("C:/o/llama-server.exe")
GGUF = "C:/blobs/sha256-aaa"


# --- pure parts ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("text", "seconds"), [("10m", 600.0), ("30s", 30.0), ("1h", 3600.0), ("1h30m", 5400.0),
                                               ("2m5s", 125.0), ("0", 0.0), ("-1", None), ("", 600.0), ("soon", 600.0)])
def test_keep_alive_in_seconds(text, seconds):
    assert keep_alive_s(text) == seconds  # -1 = never unload; nonsense = the default 10 minutes


def test_cpu_choice_starts_the_server_without_gpu_layers_or_the_cuda_backend():
    joined = " ".join(server_args(EXE, GGUF, port=5001, layers=0))
    assert "-ngl 0" in joined and "--port 5001" in joined and "--host 127.0.0.1" in joined
    env = server_env(Path("C:/o"), {"PATH": "C:/Windows"}, gpu=False)
    assert "GGML_BACKEND_PATH" not in env and "cuda" not in env["PATH"]
    assert env["PATH"].split(";")[0].replace("\\", "/") == "C:/o"


def test_completion_body_is_greedy_cached_and_schema_bound():
    body = completion_body("PROMPT", schema={"type": "object"}, options={"num_ctx": 4096, "num_gpu": 99},
                           num_predict=200)
    assert body["prompt"] == "PROMPT" and body["n_predict"] == 200 and body["cache_prompt"] is True
    assert body["json_schema"] == {"type": "object"} and "stop" not in body
    assert {key: body[key] for key in SAMPLING} == SAMPLING
    assert "num_ctx" not in body and "num_gpu" not in body  # load options belong to the process, not the request


def test_completion_body_without_a_schema_passes_stop_words():
    body = completion_body("P", schema=None, options={"stop": ["<|im_end|>"], "temperature": 0.2}, num_predict=None)
    assert body["stop"] == ["<|im_end|>"] and "json_schema" not in body and "n_predict" not in body
    assert body["temperature"] == 0.2


def test_reply_of_completion_reads_llama_servers_timings():
    data = {"content": "{}", "timings": {"prompt_n": 50, "prompt_ms": 31.26, "predicted_n": 26, "predicted_ms": 322.0}}
    assert reply_of_completion(data, 440.12, load_ms=7.0) == Reply("{}", 440.1, 7.0, 50, 31.3, 26, 322.0)
    with pytest.raises(LLMError):
        reply_of_completion({"error": {"message": "x"}}, 1.0)


# --- the child process -----------------------------------------------------------------------------------------------


class FakeProc:
    def __init__(self, pid):
        self.pid = pid
        self.code = None
        self.killed = False

    def poll(self):
        return self.code

    def terminate(self):
        self.killed = True
        self.code = 1

    def kill(self):
        self.terminate()

    def wait(self, timeout=None):
        return self.code


class Rig:
    """A LlamaServer whose process, health check, clock and idle timer are scripted."""

    def __init__(self, *, healthy_after=0, exe=EXE):
        self.now = 100.0
        self.procs: list[FakeProc] = []
        self.spawned: list[list[str]] = []
        self.healthy_after = healthy_after  # health checks that fail before the model is ready
        self.checks = 0
        self.timers: list[tuple[float, object]] = []
        self.server = LlamaServer(exe=exe, spawn=self._spawn, health=self._health, free_port=lambda: 5000 + len(self.procs),
                                  clock=lambda: self.now, sleep=self._sleep, timer=self._timer)

    def _spawn(self, args, env):
        self.spawned.append(args)
        self.procs.append(FakeProc(len(self.procs) + 1))
        self.checks = 0
        return self.procs[-1]

    def _health(self, port):
        self.checks += 1
        return self.checks > self.healthy_after

    def _sleep(self, seconds):
        self.now += seconds

    def _timer(self, seconds, callback):
        self.timers.append((seconds, callback))
        return lambda: None

    def ensure(self, model="qwen:4b", gguf=GGUF, layers=99, timeout=60.0, cancelled=lambda: False):
        return self.server.ensure(model, gguf, size=3 * 1024**3, layers=layers, ctx=4096, timeout=timeout,
                                  cancelled=cancelled)


def test_the_server_starts_on_demand_and_only_once():
    rig = Rig(healthy_after=3)
    assert rig.server.rows() == ()
    first = rig.ensure()
    assert isinstance(first, Serving) and first.port == 5000 and first.started_ms > 0
    assert rig.ensure() == first and len(rig.spawned) == 1  # resident: nothing to start
    row = rig.server.rows()[0]
    assert row.name == "qwen:4b" and row.vram > 0


def test_cpu_model_reports_no_vram():
    rig = Rig()
    rig.ensure("qwen:2b", layers=0)
    assert rig.server.rows()[0].vram == 0 and "-ngl 0" in " ".join(rig.spawned[0])


def test_another_model_replaces_the_one_running():
    rig = Rig()
    rig.ensure("qwen:4b")
    rig.ensure("qwen:2b", gguf="C:/blobs/sha256-bbb", layers=0)
    assert rig.procs[0].killed and not rig.procs[1].killed and len(rig.spawned) == 2
    assert [row.name for row in rig.server.rows()] == ["qwen:2b"]  # one Nova model at a time


def test_a_server_that_dies_while_starting_is_broken_for_a_while_then_tried_again():
    rig = Rig(healthy_after=10**9)
    rig.server  # noqa: B018
    original = rig._health

    def dying(port):
        rig.procs[-1].code = 3221225781  # a missing DLL
        return original(port)

    rig.server._health = dying
    with pytest.raises(LLMError, match="llama-server stopped"):
        rig.ensure()
    assert not rig.server.usable and "stopped" in rig.server.broken
    with pytest.raises(LLMError):
        rig.ensure()
    assert len(rig.spawned) == 1  # not respawned on every request
    rig.now += RETRY_S + 1
    assert rig.server.usable


def test_a_server_that_never_gets_healthy_is_killed_and_marked_broken():
    rig = Rig(healthy_after=10**9)
    with pytest.raises(LLMError):
        rig.ensure(timeout=START_TIMEOUT_S + 50)
    assert rig.procs[0].killed and not rig.server.usable


def test_a_short_timeout_gives_up_waiting_but_never_stops_the_load():
    rig = Rig(healthy_after=40)  # ready after ~4 s of 0.1 s checks
    with pytest.raises(LLMError, match="took too long"):
        rig.ensure(timeout=1.0)
    assert not rig.procs[0].killed and rig.server.usable and rig.server.rows() == ()  # still loading
    serving = rig.ensure(timeout=60.0)  # the next caller joins the same start
    assert len(rig.spawned) == 1 and serving.port == 5000 and rig.server.rows() != ()


def test_esc_while_the_server_starts_stops_the_wait_not_the_load():
    rig = Rig(healthy_after=40)
    asked = []

    def cancelled():
        asked.append(1)
        return len(asked) > 5

    with pytest.raises(LLMCancelled):
        rig.ensure(cancelled=cancelled)
    assert not rig.procs[0].killed
    rig.ensure()
    assert len(rig.spawned) == 1


def test_a_crashed_server_is_started_again():
    rig = Rig()
    rig.ensure()
    rig.procs[0].code = 1  # it died after it was ready
    assert rig.server.rows() == ()
    rig.ensure()
    assert len(rig.spawned) == 2 and rig.server.rows() != ()


def test_the_server_stops_after_keep_alive_unused():
    rig = Rig()
    rig.ensure()
    rig.server.used(600.0)
    seconds, expire = rig.timers[-1]
    assert seconds == 600.0
    rig.now += 300
    rig.server.used(600.0)  # used again: the first timer finds it still fresh
    expire()
    assert not rig.procs[0].killed
    rig.now += 600
    rig.timers[-1][1]()
    assert rig.procs[0].killed and rig.server.rows() == ()


def test_keep_alive_never_and_at_once():
    rig = Rig()
    rig.ensure()
    rig.server.used(None)  # -1: stays until Nova quits
    assert rig.timers == []
    rig.server.used(0.0)  # 0: unload after every request, as Ollama does
    assert rig.procs[0].killed


def test_a_request_in_flight_keeps_the_server_past_keep_alive():
    rig = Rig()
    rig.ensure()
    with rig.server.request():
        rig.server.used(10.0)
        rig.now += 60
        rig.timers[-1][1]()
        assert not rig.procs[0].killed
    assert len(rig.timers) == 2  # checked again later


def test_stop_only_the_named_model():
    rig = Rig()
    rig.ensure("qwen:4b")
    rig.server.stop("gemma:2b")
    assert not rig.procs[0].killed
    rig.server.stop("qwen:4b")
    assert rig.procs[0].killed


def test_no_binary_means_not_usable():
    rig = Rig(exe=None)
    assert not rig.server.usable and "llama-server.exe" in rig.server.broken
    with pytest.raises(LLMError):
        rig.ensure()


# --- the client ------------------------------------------------------------------------------------------------------


MODELFILE = "# Modelfile\nFROM C:\\blobs\\sha256-aaa\nFROM C:\\blobs\\sha256-bbb\n"


class _Both(BaseHTTPRequestHandler):
    """Ollama's API and llama-server's on one port; every request is noted."""

    seen: list[tuple[str, dict]] = []
    ps: list[str] = ["gemma:2b"]  # what Ollama holds

    def log_message(self, *args):
        pass

    def do_GET(self):
        type(self).seen.append((self.path, {}))
        if self.path == "/api/ps":
            self._send(200, {"models": [{"name": name, "size": 5, "size_vram": 5} for name in type(self).ps]})
        else:
            self._send(200, {"status": "ok"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).seen.append((self.path, body))
        if self.path == "/api/show":
            self._send(200, {"modelfile": MODELFILE, "details": {"family": "qwen35"}})
        elif self.path == "/completion":
            self._send(200, {"content": '{"via": "llama-server"}',
                             "timings": {"prompt_n": 50, "prompt_ms": 30.0, "predicted_n": 5, "predicted_ms": 60.0}})
        elif self.path == "/api/chat":
            self._send(200, {"message": {"content": '{"via": "ollama chat"}'}})
        else:
            self._send(200, {"response": '{"via": "ollama"}'})

    def _send(self, code, data):
        raw = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class FakeServer:
    """Stands in for LlamaServer: 'serves' on the test HTTP server's port."""

    def __init__(self, port, *, usable=True, fail=""):
        self.port = port
        self.usable = usable
        self.broken = "" if usable else "llama-server.exe isn't installed"
        self.fail = fail
        self.serving: Serving | None = None
        self.ensured: list[tuple] = []
        self.stopped: list[str | None] = []
        self.kept: list[float | None] = []

    def ensure(self, model, gguf, *, size, layers, ctx, timeout, cancelled, hold=False):
        self.ensured.append((model, gguf, layers, ctx))
        if self.fail:
            self.usable = False
            raise LLMError(self.fail)
        self.serving = Serving(model, gguf, layers, ctx, self.port, 3 * 1024**3, 0.0)
        return self.serving

    def serves(self, model):
        return self.serving is not None and self.serving.model == model

    def rows(self):
        s = self.serving
        return () if s is None else (Loaded(s.model, s.size, s.size if s.layers else 0),)

    def request(self, held=False):
        return threading.Lock()  # any context manager

    def used(self, seconds):
        self.kept.append(seconds)

    def stop(self, model=None):
        self.stopped.append(model)
        if self.serving is not None and (model is None or self.serving.model == model):
            self.serving = None
            return True
        return False


@pytest.fixture
def both(monkeypatch):
    monkeypatch.setattr("nova.brain.llama_server.os.path.getsize", lambda path: 3 * 1024**3)
    _Both.seen = []
    _Both.ps = ["gemma:2b"]
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Both)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()


def _paths():
    return [path for path, _ in _Both.seen]


def _unloads(model):
    return [body for path, body in _Both.seen if path == "/api/generate" and body == {"model": model, "keep_alive": 0}]


def _ollama_generates():
    """Ollama asked to answer or load (not to unload)."""
    return [body for path, body in _Both.seen if path == "/api/generate" and body.get("keep_alive") != 0]


def test_raw_completions_go_to_llama_server(both):
    server = FakeServer(both)
    client = LlamaClient(f"http://127.0.0.1:{both}", server)
    reply = client.generate("qwen:4b", "PROMPT", schema={"type": "object"}, options={"num_ctx": 4096, "num_gpu": 99},
                            keep_alive="5m", num_predict=200)
    assert json.loads(reply.text) == {"via": "llama-server"} and reply.tokens == 5 and reply.prompt_tokens == 50
    assert server.ensured == [("qwen:4b", "C:\\blobs\\sha256-aaa", 99, 4096)] and server.kept == [300.0]
    sent = dict(_Both.seen)["/completion"]
    assert sent["prompt"] == "PROMPT" and sent["json_schema"] == {"type": "object"} and sent["n_predict"] == 200
    client.generate("qwen:4b", "AGAIN", options={"num_gpu": 0})
    assert _paths().count("/api/show") == 1  # the model's file is looked up once
    assert server.ensured[-1][2] == 0  # num_gpu 0 = the CPU choice


def test_runtime_ollama_never_touches_llama_server(both):
    server = FakeServer(both)
    client = LlamaClient(f"http://127.0.0.1:{both}", server, lambda: "ollama")
    assert json.loads(client.generate("qwen:4b", "P").text) == {"via": "ollama"}
    assert server.ensured == [] and server.stopped == [None]  # and a server left from before the setting changed stops


def test_auto_falls_back_to_ollama_when_the_server_cannot_run(both):
    missing = LlamaClient(f"http://127.0.0.1:{both}", FakeServer(both, usable=False))
    assert json.loads(missing.generate("qwen:4b", "P").text) == {"via": "ollama"}
    failing = FakeServer(both, fail="llama-server stopped (exit 1)")
    client = LlamaClient(f"http://127.0.0.1:{both}", failing)
    assert json.loads(client.generate("qwen:4b", "P").text) == {"via": "ollama"}
    assert json.loads(client.generate("qwen:4b", "P").text) == {"via": "ollama"}
    assert len(failing.ensured) == 1  # broken: not tried again on every request


def test_runtime_llama_server_says_why_instead_of_falling_back(both):
    client = LlamaClient(f"http://127.0.0.1:{both}", FakeServer(both, usable=False), lambda: "llama-server")
    with pytest.raises(LLMError, match="llama-server.exe"):
        client.generate("qwen:4b", "P")
    assert "/api/generate" not in _paths()


def test_cancelled_is_never_a_reason_to_fall_back(both):
    class Cancelling(FakeServer):
        def ensure(self, *args, **kwargs):
            raise LLMCancelled("Cancelled")

    client = LlamaClient(f"http://127.0.0.1:{both}", Cancelling(both))
    with pytest.raises(LLMCancelled):
        client.generate("qwen:4b", "P")
    assert _ollama_generates() == []  # (asking Ollama to drop its own copy first is no fallback)


def test_loaded_is_the_servers_model_without_asking_ollama(both):
    server = FakeServer(both)
    client = LlamaClient(f"http://127.0.0.1:{both}", server)
    assert client.loaded() == () and _paths() == []  # nothing served here, nothing through Ollama: no HTTP at all
    client.generate("qwen:4b", "P")
    _Both.seen.clear()
    assert [row.name for row in client.loaded()] == ["qwen:4b"] and _paths() == []


def test_load_without_a_prompt_goes_to_ollama(both):
    server = FakeServer(both)
    client = LlamaClient(f"http://127.0.0.1:{both}", server)
    client.load("gemma:2b", options={"num_ctx": 4096, "num_gpu": 99}, keep_alive="10m")  # the chat-template path
    assert server.ensured == [] and server.serving is None
    assert _ollama_generates() == [{"model": "gemma:2b", "keep_alive": "10m",
                                    "options": {"num_ctx": 4096, "num_gpu": 99}}]


def test_unload_stops_the_server_or_asks_ollama(both):
    server = FakeServer(both)
    client = LlamaClient(f"http://127.0.0.1:{both}", server)
    client.generate("qwen:4b", "P", options={"num_ctx": 4096, "num_gpu": 99})
    assert server.serving is not None
    client.unload("qwen:4b")
    assert server.serving is None
    client.unload("gemma:2b")  # not ours: Ollama's to unload
    assert _Both.seen[-1] == ("/api/generate", {"model": "gemma:2b", "keep_alive": 0})


def test_chat_template_models_stay_on_ollama(both):
    server = FakeServer(both)
    client = LlamaClient(f"http://127.0.0.1:{both}", server)
    assert json.loads(client.chat("gemma:2b", [{"role": "user", "content": "hi"}]).text) == {"via": "ollama chat"}
    assert server.ensured == []


def test_a_sibling_shares_the_server_but_not_the_abort(both):
    server = FakeServer(both)
    client = LlamaClient(f"http://127.0.0.1:{both}", server)
    twin = client.sibling()
    assert isinstance(twin, LlamaClient) and twin is not client
    twin.generate("qwen:4b", "P")
    assert server.ensured  # same server
    assert isinstance(Ollama("http://127.0.0.1:1").sibling(), Ollama)


def test_connect_picks_the_client_for_the_setting(monkeypatch):
    monkeypatch.setattr("nova.brain.llama_server._shared", None)
    monkeypatch.setattr("nova.brain.llama_server.find_exe", lambda: None)
    assert type(connect("http://127.0.0.1:1", lambda: "ollama")) is LlamaClient  # the setting can change while it runs
    assert not connect("http://127.0.0.1:1").server.usable


def test_nova_say_keeps_ollama_unless_llama_server_is_forced():
    from nova.say import one_shot_runtime

    assert one_shot_runtime("auto") == "ollama"  # a child server can't stay warm between one-command runs
    assert one_shot_runtime("llama-server") == "llama-server" and one_shot_runtime("ollama") == "ollama"


# --- the child's model is never counted from Ollama's list (Stage 07a review) ------------------------------------------


def test_a_cold_child_is_not_resident_because_ollama_holds_the_model(both):
    from nova.brain.llm import Loader

    _Both.ps = ["qwen:4b"]  # `nova say` (Ollama) left it loaded
    server = FakeServer(both)
    client = LlamaClient(f"http://127.0.0.1:{both}", server)
    assert client.loaded() == () and _paths() == []  # nothing went through Ollama here: no HTTP
    assert not Loader(client.sibling()).resident("qwen:4b")  # so the request warms up instead of timing out


def test_a_cold_start_asks_ollama_to_drop_its_copy_once(both):
    _Both.ps = ["qwen:4b"]
    server = FakeServer(both)
    order = []
    ensure = server.ensure

    def noting(*args, **kwargs):
        order.append(("ensure", len(_unloads("qwen:4b"))))
        return ensure(*args, **kwargs)

    server.ensure = noting
    client = LlamaClient(f"http://127.0.0.1:{both}", server)
    client.generate("qwen:4b", "P", options={"num_ctx": 4096, "num_gpu": 99})
    assert len(_unloads("qwen:4b")) == 1 and order == [("ensure", 1)]  # first Ollama's copy goes, then the child starts
    client.generate("qwen:4b", "AGAIN", options={"num_ctx": 4096, "num_gpu": 99})
    assert len(_unloads("qwen:4b")) == 1  # warm: never again
    assert json.loads(client.generate("qwen:4b", "P").text) == {"via": "llama-server"}


def test_a_failing_unload_never_stops_the_child_from_starting(both):
    server = FakeServer(both)
    client = LlamaClient("http://127.0.0.1:9", server, files={"qwen:4b": (GGUF, 3 * 1024**3)})  # Ollama isn't there
    client.generate("qwen:4b", "P")
    assert server.ensured and server.serving is not None


def test_chat_template_models_are_reported_from_ollamas_list(both):
    _Both.ps = ["gemma:2b", "qwen:4b"]
    server = FakeServer(both)
    client = LlamaClient(f"http://127.0.0.1:{both}", server)
    client.chat("gemma:2b", [{"role": "user", "content": "hi"}])
    assert [row.name for row in client.loaded()] == ["gemma:2b"]  # not qwen:4b: nothing here put it there
    twin = LlamaClient(f"http://127.0.0.1:{both}", FakeServer(both))
    twin.load("gemma:2b", keep_alive="10m")
    assert [row.name for row in twin.sibling().loaded()] == ["gemma:2b"]  # siblings share what went through Ollama


def test_mode_ollama_reports_ollamas_list(both):
    _Both.ps = ["gemma:2b", "qwen:4b"]
    client = LlamaClient(f"http://127.0.0.1:{both}", FakeServer(both), lambda: "ollama")
    assert [row.name for row in client.loaded()] == ["gemma:2b", "qwen:4b"]


def test_a_fallback_answer_counts_ollamas_copy_until_the_child_takes_over(both):
    _Both.ps = ["qwen:4b"]
    runtime = ["ollama"]
    server = FakeServer(both)
    client = LlamaClient(f"http://127.0.0.1:{both}", server, lambda: runtime[0])
    assert json.loads(client.generate("qwen:4b", "P").text) == {"via": "ollama"}
    runtime[0] = "auto"
    assert [row.name for row in client.loaded()] == ["qwen:4b"]  # Ollama answered: its copy is the model
    client.generate("qwen:4b", "P")  # now the child: Ollama's copy is dropped first
    assert len(_unloads("qwen:4b")) == 1
    _Both.seen.clear()
    assert [row.name for row in client.loaded()] == ["qwen:4b"] and _paths() == []  # the child's row, no HTTP


def test_auto_fallback_counts_ollamas_copy(both):
    _Both.ps = ["qwen:4b"]
    client = LlamaClient(f"http://127.0.0.1:{both}", FakeServer(both, usable=False))
    client.generate("qwen:4b", "P")
    assert [row.name for row in client.loaded()] == ["qwen:4b"]


def test_every_connected_client_shares_what_went_through_ollama(both, monkeypatch):
    monkeypatch.setattr("nova.brain.llama_server._shared", FakeServer(both))
    monkeypatch.setattr("nova.brain.llama_server._VIA_OLLAMA", set())
    _Both.ps = ["gemma:2b"]
    connect(f"http://127.0.0.1:{both}").chat("gemma:2b", [{"role": "user", "content": "hi"}])
    assert [row.name for row in connect(f"http://127.0.0.1:{both}").loaded()] == ["gemma:2b"]


def test_serves_is_the_model_running_or_starting():
    rig = Rig(healthy_after=40)
    assert not rig.server.serves("qwen:4b")
    with pytest.raises(LLMError):
        rig.ensure(timeout=1.0)  # still starting
    assert rig.server.serves("qwen:4b") and not rig.server.serves("qwen:2b")
    rig.server.stop()
    assert not rig.server.serves("qwen:4b")


# --- two small holes (Stage 07a review N3) ------------------------------------------------------------------------------


def test_a_request_that_got_the_child_is_counted_before_the_idle_timer_can_stop_it():
    rig = Rig()
    rig.ensure()
    rig.server.used(10.0)
    rig.now += 60  # idle long enough: the timer is due
    rig.server.ensure("qwen:4b", GGUF, size=3 * 1024**3, layers=99, ctx=4096, timeout=60.0, hold=True)
    rig.timers[-1][1]()  # the timer fires between ensure and the request
    assert not rig.procs[0].killed
    with rig.server.request(held=True):
        pass
    rig.server.used(0.0)  # released: nothing in flight, so keep_alive 0 stops it
    assert rig.procs[0].killed


def test_a_cold_start_held_is_counted_too_and_a_failed_one_is_not():
    rig = Rig(healthy_after=3)
    rig.server.ensure("qwen:4b", GGUF, size=3 * 1024**3, layers=99, ctx=4096, timeout=60.0, hold=True)
    rig.server.used(0.0)
    assert not rig.procs[0].killed  # in flight
    with rig.server.request(held=True):
        pass
    rig.server.used(0.0)
    assert rig.procs[0].killed
    slow = Rig(healthy_after=40)
    with pytest.raises(LLMError):
        slow.server.ensure("qwen:4b", GGUF, size=3 * 1024**3, layers=99, ctx=4096, timeout=1.0, hold=True)
    slow.ensure()
    slow.server.used(0.0)
    assert slow.procs[0].killed  # the failed wait counted nothing


def test_a_child_that_cannot_be_spawned_is_broken_and_says_so():
    rig = Rig()

    def refuse(args, env):
        rig.spawned.append(args)
        raise OSError(22, "Invalid argument")

    rig.server._spawn = refuse
    with pytest.raises(LLMError, match="couldn't start"):
        rig.ensure()
    assert not rig.server.usable and "couldn't start" in rig.server.broken and rig.server.rows() == ()
    with pytest.raises(LLMError):
        rig.ensure()
    assert len(rig.spawned) == 1  # not tried on every request
    rig.now += RETRY_S + 1
    assert rig.server.usable


def test_auto_falls_back_to_ollama_when_the_child_cannot_be_spawned(both):
    rig = Rig()

    def refuse(args, env):
        raise OSError(22, "Invalid argument")

    rig.server._spawn = refuse
    client = LlamaClient(f"http://127.0.0.1:{both}", rig.server)
    assert json.loads(client.generate("qwen:4b", "P").text) == {"via": "ollama"}
