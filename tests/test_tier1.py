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
from nova.brain.llm import LLMCancelled, LLMError, Ollama, Reply, is_cloud, reply_of
from nova.brain.tier1 import Dialect, Seen, Thought, Tier1, chatml, chatml_turn, dialect_for
from nova.brain.validate import check_llm
from nova.config import Llm
from nova.governor import Choice

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
    url = "http://127.0.0.1:1"

    def __init__(self, family="qwen35", text='{"kind":"ignore","steps":[],"confidence":0.9}', error=None):
        self.family = family
        self.text = text
        self.error = error
        self.calls = []

    def show(self, model, timeout=5.0):
        caps = ["completion", "thinking"] if self.family == "qwen35" else ["completion"]
        return {"details": {"family": self.family}, "capabilities": caps}

    def generate(self, model, prompt_, **kw):
        self.calls.append(("generate", model, prompt_, kw))
        if self.error:
            raise self.error
        return Reply(self.text, 400.0)

    def chat(self, model, messages, **kw):
        self.calls.append(("chat", model, messages, kw))
        if self.error:
            raise self.error
        return Reply(self.text, 400.0)

    def load(self, model, **kw):
        self.calls.append(("load", model, kw))
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


def tier1(client, governor=None, monkeypatch=None):
    t = Tier1(client, governor or FakeGovernor(), lambda: Llm(keep_alive="5m", timeout_s=6.0), lambda: CATALOG)
    t._background = client  # warm-ups go to the same fake
    return t


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
    assert kw["options"] == {"num_ctx": 4096} and kw["num_predict"] == 200


def test_other_families_use_chat_and_cpu_means_no_gpu_layers():
    client = FakeOllama("gemma4")
    t = tier1(client, FakeGovernor(Choice("gemma4:e2b", "cpu")))
    t.think("open it", Seen())
    kind, _model, messages, kw = client.calls[0]
    assert kind == "chat" and messages[-1] == {"role": "user", "content": '{"said":"open it"}'}
    assert kw["options"] == {"num_ctx": 4096, "num_gpu": 0}


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
