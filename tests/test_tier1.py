"""Tier 1's prompt and plumbing (SPEC §7.4): a cacheable static prefix, a compact request, per-family dialects."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from catalog_fixture import CATALOG

from nova.actions.context import AppWindow, Context, Tab
from nova.actions.native import Win
from nova.brain import prompt
from nova.brain.llm import NUM_CTX, LLMCancelled, LLMError, Loaded, Loader, Ollama, Reply, is_cloud, load_options, reply_of
from nova.brain.tier1 import Dialect, Seen, Thought, Tier1, chatml, chatml_turn, dialect_for
from nova.brain.validate import check_llm
from nova.config import Llm
from nova.governor import ALL_LAYERS, Choice

# --- the prompt ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("request_", "plan"), prompt.EXAMPLES)
def test_every_worked_example_is_a_valid_plan(request_, plan):
    checked = check_llm(plan, request_["said"], CATALOG, running=frozenset(request_.get("open", ())))
    if plan["kind"] in ("teach", "ignore"):
        assert checked.plan is None and checked.kind == plan["kind"]
    else:
        assert checked.plan is not None, checked.problem
    assert list(plan)[:3] == ["kind", "steps", "confidence"]  # the order the schema's grammar writes


def test_examples_are_not_eval_items():
    from nova.evals import EVAL_FILE

    eval_text = EVAL_FILE.read_text(encoding="utf-8").lower()
    for request_, _ in prompt.EXAMPLES:
        assert f"say: {request_['said'].lower()}\n" not in eval_text


def test_prefix_is_byte_identical_for_the_same_aliases():
    assert prompt.prefix(CATALOG.aliases) == prompt.prefix(CATALOG.aliases)
    system = prompt.prefix(CATALOG.aliases)[0]["content"]
    assert "canvas = https://canvas.vt.edu" in system and "fusion = Autodesk Fusion" in system
    assert prompt.prefix(())[0]["content"] != system


def test_schema_puts_kind_steps_confidence_first():
    assert prompt.SCHEMA["required"] == ["kind", "steps", "confidence"]
    actions = [s["properties"]["action"]["const"] for s in prompt.STEP_SCHEMAS]
    assert "open_app" in actions and "layout" in actions and len(actions) == len(set(actions))


def test_request_is_compact():
    data = prompt.request("open the thing", front="Google Chrome", open_apps=("Google Chrome", "Claude"), tab=None,
                          apps=())
    assert data == {"said": "open the thing", "front": "Google Chrome", "open": ["Google Chrome", "Claude"]}
    assert prompt.dumps(data) == '{"said":"open the thing","front":"Google Chrome","open":["Google Chrome","Claude"]}'
    again = prompt.request("open it", front=None, open_apps=(), tab=None, apps=(), asked="Open which one?",
                           answer="Notepad")
    assert again == {"said": "open it", "asked": "Open which one?", "answer": "Notepad"}


def test_relevant_apps_match_his_words_only():
    assert prompt.relevant_apps("yo throw spotify on for me", CATALOG)[:1] == ["Spotify"]
    assert "Canvas" in prompt.relevant_apps("can you get canvas up", CATALOG)
    assert prompt.relevant_apps("make it bigger", CATALOG) == []


# --- dialects and rendering -----------------------------------------------------------------------------------------


def test_dialects_by_family():
    assert dialect_for({"details": {"family": "qwen35"}, "capabilities": ["completion", "thinking"]}) == Dialect(True, True)
    assert dialect_for({"details": {"family": "qwen2"}, "capabilities": ["completion"]}) == Dialect(True, False)
    assert dialect_for({"details": {"family": "gemma4"}, "capabilities": ["thinking"]}) == Dialect(False, False)
    assert dialect_for({}) == Dialect(False, False)


def test_chatml_static_part_ends_where_the_request_starts():
    static = chatml([{"role": "system", "content": "rules"}, {"role": "user", "content": "a"},
                     {"role": "assistant", "content": "b"}])
    assert static == ("<|im_start|>system\nrules<|im_end|>\n<|im_start|>user\na<|im_end|>\n"
                      "<|im_start|>assistant\nb<|im_end|>\n<|im_start|>user\n")
    assert chatml_turn("{}", True) == "{}<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
    assert chatml_turn("{}", False) == "{}<|im_end|>\n<|im_start|>assistant\n"


def test_seen_from_the_context():
    def win(n, title=""):
        return Win(n, 0, 0, title, "C", "C:\\x\\app.exe", ())

    ctx = Context(AppWindow(win(1), "Google Chrome"),
                  (AppWindow(win(1), "Google Chrome"), AppWindow(win(2), "Claude"), AppWindow(win(3), "Google Chrome"),
                   AppWindow(win(4), "")), (), Tab("Inbox - Gmail"), False, 3.0)
    assert Seen.of(ctx) == Seen("Google Chrome", ("Google Chrome", "Claude"), "Inbox - Gmail")
    unknown = Context(AppWindow(win(5), ""), (), (), None, False, 1.0)
    assert Seen.of(unknown) == Seen("app", (), None)


def test_thought_trace():
    thought = Thought({"kind": "act"}, choice=Choice("m:2b", "cpu"), reply=Reply("{}", 900.0, 600.0, 1800, 30.0, 25, 210.0),
                      ms=905.4)
    assert thought.trace() == {"llm_ms": 905.4, "model": "m:2b", "device": "cpu", "load_ms": 600.0,
                               "prompt_tokens": 1800, "prompt_ms": 30.0, "tokens": 25, "eval_ms": 210.0}


# --- Tier1 with a fake Ollama ---------------------------------------------------------------------------------------


class FakeOllama:
    """One fake Ollama. ``resident``: what /api/ps lists; a warm-up / load (num_predict 1) makes its model resident
    and waits for ``load_gate`` when one is given (a cold load in flight)."""

    url = "http://127.0.0.1:1"

    def __init__(self, family="qwen35", text='{"kind":"ignore","steps":[],"confidence":0.9}', error=None,
                 resident=("qwen3.5:2b-q4_K_M", "qwen3.5:4b"), load_gate=None):
        self.family = family
        self.text = text
        self.error = error
        self.calls = []
        self.resident = set(resident)
        self.load_gate = load_gate
        self.load_error = None

    def show(self, model, timeout=5.0):
        caps = ["completion", "thinking"] if self.family == "qwen35" else ["completion"]
        return {"details": {"family": self.family}, "capabilities": caps}

    def loaded(self, timeout=2.0):
        return tuple(Loaded(name, 3 * 1024**3, 3 * 1024**3) for name in sorted(self.resident))

    def _loading(self, model):
        if self.load_gate is not None:
            self.load_gate.wait(5)
        if self.load_error:
            raise self.load_error
        self.resident.add(model)

    def generate(self, model, prompt_, **kw):
        self.calls.append(("generate", model, prompt_, kw))
        if kw.get("num_predict") == 1:
            self._loading(model)
            return Reply("", 10.0)
        if self.error:
            raise self.error
        if model not in self.resident:  # a request that has to load first (what timed out before)
            self._loading(model)
        return Reply(self.text, 400.0)

    def chat(self, model, messages, **kw):
        self.calls.append(("chat", model, messages, kw))
        if self.error:
            raise self.error
        return Reply(self.text, 400.0)

    def load(self, model, **kw):
        self.calls.append(("load", model, kw))
        self._loading(model)
        return 10.0

    def abort(self):
        self.calls.append(("abort",))


class FakeGovernor:
    def __init__(self, choice=Choice("qwen3.5:2b-q4_K_M", "gpu")):
        self.choice = choice
        self.used_count = 0

    def choose(self, *, fresh=False):
        return self.choice

    def used(self):
        self.used_count += 1

    def close(self):
        pass


def tier1(client, governor=None, warming=None, warmed=None, loader=None):
    """Tier 1 on a fake: its loader (warm-ups, loads) talks to the same fake."""
    return Tier1(client, governor or FakeGovernor(), lambda: Llm(keep_alive="5m", timeout_s=6.0), lambda: CATALOG,
                 loader=loader or Loader(client), warming=warming or (lambda: None), warmed=warmed or (lambda: None))


def requests(client):
    """The answering calls (not warm-ups / loads)."""
    return [c for c in client.calls if c[0] in ("generate", "chat") and c[3].get("num_predict") != 1]


def loads(client):
    return [c for c in client.calls if c[0] == "load" or (c[0] == "generate" and c[3].get("num_predict") == 1)]


def wait_for(condition, seconds=2.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def test_qwen_is_rendered_here_and_sent_raw():
    client = FakeOllama("qwen35")
    t = tier1(client)
    thought = t.think("my roommate opened the window", Seen("Google Chrome", ("Google Chrome",)))
    assert thought.data == {"kind": "ignore", "steps": [], "confidence": 0.9}
    kind, model, text, kw = client.calls[0]
    assert kind == "generate" and model == "qwen3.5:2b-q4_K_M"
    assert text.startswith("<|im_start|>system\n") and text.endswith("<think>\n\n</think>\n\n")
    assert '"said":"my roommate opened the window"' in text
    assert kw["schema"] == prompt.SCHEMA and kw["keep_alive"] == "5m" and kw["timeout"] == 6.0
    assert kw["options"] == {"num_ctx": 4096, "num_gpu": ALL_LAYERS} and kw["num_predict"] == 200


def test_other_families_use_chat_and_cpu_means_no_gpu_layers():
    client = FakeOllama("gemma4", resident=("gemma4:e2b",))
    t = tier1(client, FakeGovernor(Choice("gemma4:e2b", "cpu")))
    t.think("open it", Seen())
    kind, _model, messages, kw = client.calls[0]
    assert kind == "chat" and messages[-1] == {"role": "user", "content": '{"said":"open it"}'}
    assert kw["options"] == {"num_ctx": 4096, "num_gpu": 0}


# --- one model, one set of load options (Stage 07a item 1) -------------------------------------------------------------


def test_load_options_are_the_same_for_every_caller():
    # GPU = every layer on the GPU: left to itself, Ollama 0.30 (llama-server's fit) reserved room for the vision
    # projector and put 0 of 33 layers on an RTX with 6.9 GB free (2026-11-11, Fusion open) — a "GPU" model on the CPU.
    assert load_options(Choice("qwen3.5:4b", "gpu").options) == {"num_ctx": NUM_CTX, "num_gpu": ALL_LAYERS}
    assert load_options(Choice("qwen3.5:2b", "cpu").options) == {"num_ctx": NUM_CTX, "num_gpu": 0}
    assert NUM_CTX == 4096


def test_warm_ups_and_requests_send_the_same_load_options():
    client = FakeOllama("qwen35", resident=())
    t = tier1(client)
    t.warm()
    t.think("x", Seen())
    assert {json.dumps(c[3]["options"], sort_keys=True) for c in client.calls} == {json.dumps({"num_ctx": 4096, "num_gpu": ALL_LAYERS})}


# --- a cold start never fails (Stage 07a item 2) ---------------------------------------------------------------------


def test_a_cold_request_waits_for_the_load_then_answers():
    gate = threading.Event()
    client = FakeOllama("qwen35", resident=(), load_gate=gate)
    warmed = []
    t = tier1(client, warming=lambda: warmed.append(time.monotonic()))
    began = time.monotonic()
    out = []
    thread = threading.Thread(target=lambda: out.append(t.think("x", Seen())))
    thread.start()
    assert wait_for(lambda: warmed)
    assert warmed[0] - began < 0.3  # the island says "Warming up…" at once
    time.sleep(0.1)
    assert requests(client) == [] and len(loads(client)) == 1  # no answering request while it loads
    gate.set()
    thread.join(2)
    assert out and out[0].data == {"kind": "ignore", "steps": [], "confidence": 0.9} and not out[0].error
    assert requests(client)[0][3]["timeout"] == 6.0  # the timeout covers the answer only
    assert out[0].warm_ms >= 100 and out[0].trace()["warm_ms"] == round(out[0].warm_ms, 1)


def test_a_warm_request_doesnt_wait_or_say_warming():
    client = FakeOllama("qwen35")
    warmed = []
    t = tier1(client, warming=lambda: warmed.append(1))
    assert t.think("x", Seen()).data is not None
    assert warmed == [] and loads(client) == []


def test_a_request_during_a_preload_joins_it_instead_of_loading_again():
    gate = threading.Event()
    client = FakeOllama("qwen35", resident=(), load_gate=gate)
    t = tier1(client)
    t.preload("key")
    assert wait_for(lambda: loads(client))
    out = []
    threads = [threading.Thread(target=lambda: out.append(t.think("x", Seen()))) for _ in range(2)]
    for thread in threads:
        thread.start()
    time.sleep(0.1)
    gate.set()
    for thread in threads:
        thread.join(2)
    assert len(out) == 2 and all(o.data is not None for o in out)
    assert len(loads(client)) == 1  # exactly one load, no abort
    assert ("abort",) not in client.calls


def test_abort_while_warming_cancels_the_command_and_leaves_the_load_running():
    gate = threading.Event()
    client = FakeOllama("qwen35", resident=(), load_gate=gate)
    loader = Loader(client)
    t = Tier1(client, FakeGovernor(), lambda: Llm(), lambda: CATALOG, loader=loader)
    out = []
    thread = threading.Thread(target=lambda: out.append(t.think("x", Seen())))
    thread.start()
    assert wait_for(lambda: loads(client))
    t.abort()
    thread.join(2)
    assert out and out[0].cancelled and out[0].data is None
    assert requests(client) == []
    assert not loader.resident("qwen3.5:2b-q4_K_M")  # still loading...
    gate.set()
    assert wait_for(lambda: loader.resident("qwen3.5:2b-q4_K_M"))  # ...and it finished: the next one is warm
    assert len(loads(client)) == 1


def test_a_load_that_fails_says_why():
    client = FakeOllama("qwen35", resident=())
    client.load_error = LLMError("The model qwen3.5:2b-q4_K_M isn't on this laptop — run: ollama pull qwen3.5:2b-q4_K_M")
    thought = tier1(client).think("x", Seen())
    assert thought.data is None and "ollama pull" in thought.error and requests(client) == []


def test_warmed_is_said_once_when_the_load_is_done_before_the_answer():
    gate = threading.Event()
    client = FakeOllama("qwen35", resident=(), load_gate=gate)
    said = []
    t = tier1(client, warming=lambda: said.append("warming"),
              warmed=lambda: said.append(("warmed", len(requests(client)))))
    out = []
    thread = threading.Thread(target=lambda: out.append(t.think("x", Seen())))
    thread.start()
    assert wait_for(lambda: said)
    time.sleep(0.1)
    assert said == ["warming"]  # still loading
    gate.set()
    thread.join(2)
    assert out and out[0].data is not None
    assert said == ["warming", ("warmed", 0)]  # once, after the load and before the answering request


def test_warmed_is_not_said_for_a_warm_model():
    client = FakeOllama("qwen35")
    said = []
    t = tier1(client, warming=lambda: said.append("warming"), warmed=lambda: said.append("warmed"))
    assert t.think("x", Seen()).data is not None
    assert said == []


def test_warmed_is_not_said_after_esc():
    gate = threading.Event()
    client = FakeOllama("qwen35", resident=(), load_gate=gate)
    loader = Loader(client)
    said = []
    t = tier1(client, warming=lambda: said.append("warming"), warmed=lambda: said.append("warmed"), loader=loader)
    out = []
    thread = threading.Thread(target=lambda: out.append(t.think("x", Seen())))
    thread.start()
    assert wait_for(lambda: loads(client))
    t.abort()
    thread.join(2)
    gate.set()
    assert wait_for(lambda: loader.resident("qwen3.5:2b-q4_K_M"))  # the load itself finished
    assert out and out[0].cancelled and said == ["warming"]


def test_warmed_is_not_said_after_a_failed_load():
    client = FakeOllama("qwen35", resident=())
    client.load_error = LLMError("The model qwen3.5:2b-q4_K_M isn't on this laptop — run: ollama pull qwen3.5:2b-q4_K_M")
    said = []
    thought = tier1(client, warming=lambda: said.append("warming"), warmed=lambda: said.append("warmed")).think(
        "x", Seen())
    assert thought.error and said == ["warming"]


def test_loader_loads_each_model_once_at_a_time():
    gate = threading.Event()
    client = FakeOllama("qwen35", resident=(), load_gate=gate)
    loader = Loader(client)
    first = loader.load("m:1b", options={"num_ctx": 4096}, keep_alive="10m", prompt="prefix")
    second = loader.load("m:1b", options={"num_ctx": 4096}, keep_alive="10m", prompt="prefix")
    assert first is second and not first.done and not loader.resident("m:1b")
    gate.set()
    assert first.wait(2) and first.error == "" and loader.resident("m:1b")
    again = loader.load("m:1b", options={"num_ctx": 4096}, keep_alive="10m")
    assert again is not first and again.wait(2)
    assert [c[0] for c in client.calls] == ["generate", "load"]
    assert client.calls[0][3]["timeout"] >= 60  # a cold load can take 15-20 s: its own long deadline


def test_resident_is_false_when_ollama_doesnt_answer():
    class Down(FakeOllama):
        def loaded(self, timeout=2.0):
            raise LLMError("Ollama isn't answering")

    assert not Loader(Down()).resident("qwen3.5:4b")


def test_errors_and_garbage_become_thoughts_without_data():
    assert tier1(FakeOllama(error=LLMError("Ollama isn't answering"))).think("x", Seen()).error == "Ollama isn't answering"
    cancelled = tier1(FakeOllama(error=LLMCancelled("Cancelled"))).think("x", Seen())
    assert cancelled.cancelled and cancelled.data is None
    garbage = tier1(FakeOllama(text="```json oops")).think("x", Seen())
    assert garbage.data is None and "wasn't a plan" in garbage.error


def test_warm_reads_the_static_prefix_once():
    client = FakeOllama("qwen35")
    t = tier1(client)
    t.warm()
    kind, _model, text, kw = client.calls[-1]
    assert kind == "generate" and kw["num_predict"] == 1 and text.endswith("<|im_start|>user\n")
    t.think("x", Seen())
    assert client.calls[-1][2].startswith(text)  # every request continues the warmed prefix


def test_preload_ignores_always_listening_chatter():
    client = FakeOllama("qwen35")
    t = tier1(client)
    t.preload("always")
    time.sleep(0.05)
    assert client.calls == []
    t.preload("key")
    for _ in range(100):
        if client.calls:
            break
        time.sleep(0.01)
    assert client.calls and client.calls[0][3]["num_predict"] == 1


def test_nova_say_preload_loads_only_a_cold_model():
    warm = FakeOllama("qwen35")
    tier1(warm).preload("say")
    time.sleep(0.1)
    assert loads(warm) == []  # resident: a warm-up would only queue in front of his request
    cold = FakeOllama("qwen35", resident=())
    tier1(cold).preload("say")
    assert wait_for(lambda: loads(cold))


def test_pinned_choice_wins():
    client = FakeOllama("qwen35")
    t = tier1(client)
    t.pin(Choice("qwen3.5:4b", "gpu"))
    t.think("x", Seen())
    assert client.calls[0][1] == "qwen3.5:4b"


# --- the Ollama client ----------------------------------------------------------------------------------------------


def test_cloud_tags():
    assert is_cloud("gemma4:31b-cloud") and is_cloud("qwen3.5:cloud")
    assert not is_cloud("qwen3.5:4b") and not is_cloud("cloudy:1b")


def test_reply_parsing():
    r = reply_of({"response": "{}", "load_duration": 5e8, "prompt_eval_count": 10, "prompt_eval_duration": 2e7,
                  "eval_count": 30, "eval_duration": 2.5e8}, 812.34)
    assert r == Reply("{}", 812.3, 500.0, 10, 20.0, 30, 250.0)
    assert reply_of({"message": {"content": "x"}}, 1.0).text == "x"
    with pytest.raises(LLMError):
        reply_of({"message": None}, 1.0)


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if body.get("model") == "missing:1b":
            self._send(404, {"error": "model 'missing:1b' not found"})
        elif body.get("model") == "slow:1b":
            time.sleep(2.0)
            self._send(200, {"response": "{}"})
        else:
            self._send(200, {"response": json.dumps({"echo": body.get("prompt")}), "load_duration": 1e6})

    def _send(self, code, data):
        raw = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except OSError:
            pass


@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_client_round_trip_and_errors(server):
    client = Ollama(server)
    assert json.loads(client.generate("ok:1b", "hi").text) == {"echo": "hi"}
    with pytest.raises(LLMError, match="ollama pull missing:1b"):
        client.generate("missing:1b", "hi")
    with pytest.raises(LLMError, match="cloud"):
        client.generate("big:cloud", "hi")
    with pytest.raises(LLMError, match="took too long"):
        client.generate("slow:1b", "hi", timeout=0.3)


def test_nothing_listening_is_said_plainly():
    with pytest.raises(LLMError, match="isn't answering"):
        Ollama("http://127.0.0.1:9").generate("ok:1b", "hi", timeout=1.0)


def test_abort_drops_the_call(server):
    client = Ollama(server)
    errors = []

    def call():
        try:
            client.generate("slow:1b", "hi", timeout=5.0)
        except LLMError as error:
            errors.append(error)

    thread = threading.Thread(target=call)
    thread.start()
    time.sleep(0.3)
    client.abort()
    thread.join(3.0)
    assert errors and isinstance(errors[0], LLMCancelled)
