"""Ollama client for Tier 1 (SPEC §7.4). Local only: plain HTTP to 127.0.0.1 (no system proxy).

Every call has a timeout, can be aborted from another thread (Esc while the model thinks),
and fails with ``LLMError`` in plain words for the island ("Ollama isn't running", "the
model isn't pulled", "took too long"). Cloud tags (``*:cloud``, ``*-cloud``) are refused.

Measured on this laptop (Ollama 0.30.10): every /api/chat and /api/generate re-reads the
model file's header before it is scheduled (~0.4-0.7 s for Qwen 3.5 / Gemma 4, shown as
``load_ms`` even when the model is resident); and /api/chat with ``think: false`` ignores
``format`` for Qwen 3.5. Tier 1 therefore renders Qwen prompts itself and calls
/api/generate with ``raw`` (see ``nova.brain.tier1``).
"""

from __future__ import annotations

import http.client
import json
import socket
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

DEFAULT_URL = "http://127.0.0.1:11434"
CONNECT_S = 3.0
# One set of load options for every caller (SPEC §7.4): Ollama reloads the model (10-27 s) whenever a request's
# num_ctx / num_gpu differ from the loaded copy's, so the planner and dictation cleanup must send the same ones.
NUM_CTX = 4096
LOAD_TIMEOUT_S = 180.0  # a cold load (15-20 s on the RTX, longer on the CPU) + reading the prompt prefix
_PS_TIMEOUT_S = 1.0
# Greedy and unpenalized: some models ship presence/repeat penalties (Qwen 3.5: 1.5 / 1.1) that bend JSON at temp 0.
SAMPLING = {"temperature": 0, "top_k": 1, "repeat_penalty": 1.0, "presence_penalty": 0.0, "frequency_penalty": 0.0}


class LLMError(Exception):
    """Tier 1 couldn't answer; the message is shown in the island as-is."""


class LLMCancelled(LLMError):
    """Aborted on purpose (Esc / superseded)."""


def is_cloud(name: str) -> bool:
    """`gemma4:31b-cloud`, `qwen3.5:cloud`: run on Ollama's servers. Nova never uses them."""
    tag = name.strip().rpartition(":")[2]
    return tag == "cloud" or tag.endswith("-cloud")


def load_options(device_options: Mapping[str, Any]) -> dict[str, Any]:
    """The load options every request for a model sends (``device_options``: the governor's GPU / CPU choice)."""
    return {"num_ctx": NUM_CTX, **device_options}


@dataclass(frozen=True)
class Reply:
    text: str
    ms: float  # wall time of the request
    load_ms: float = 0.0  # Ollama getting the model ready (its per-request overhead included)
    prompt_tokens: int = 0
    prompt_ms: float = 0.0  # prompt reading (cached prefix not re-read)
    tokens: int = 0
    eval_ms: float = 0.0


@dataclass(frozen=True)
class Loaded:
    name: str
    size: int  # bytes
    vram: int  # bytes on the GPU (0 = CPU)


def _ms(data: Mapping[str, Any], key: str) -> float:
    value = data.get(key)
    return round(value / 1e6, 1) if isinstance(value, (int, float)) else 0.0


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def reply_of(data: Mapping[str, Any], ms: float) -> Reply:
    """A non-streamed /api/generate or /api/chat answer."""
    if "response" in data:
        text = data.get("response")
    else:
        message = data.get("message")
        text = message.get("content") if isinstance(message, Mapping) else None
    if not isinstance(text, str):
        raise LLMError("The local model sent an answer Nova can't read.")
    return Reply(text, round(ms, 1), _ms(data, "load_duration"), _int(data.get("prompt_eval_count")),
                 _ms(data, "prompt_eval_duration"), _int(data.get("eval_count")), _ms(data, "eval_duration"))


class Ollama:
    def __init__(self, url: str = DEFAULT_URL) -> None:
        parts = urlsplit(url)
        self.url = url.rstrip("/")
        self._host = parts.hostname or "127.0.0.1"
        self._port = parts.port or 11434
        self._lock = threading.Lock()
        self._active: set[http.client.HTTPConnection] = set()
        self._aborted = False

    # --- plumbing -------------------------------------------------------------------------------------------

    def abort(self) -> None:
        """Any thread: drop the requests in flight (Ollama stops working on them)."""
        with self._lock:
            self._aborted = True
            active = list(self._active)
        for conn in active:
            sock = conn.sock
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def sibling(self) -> Ollama:
        """Another client for the same runtime: its requests aren't dropped by this one's ``abort``."""
        return Ollama(self.url)

    def _send(self, host: str, port: int, method: str, path: str, body: Mapping[str, Any] | None, timeout: float,
              *, who: str, down: str) -> tuple[int, Any]:
        """One JSON request to a local server -> (status, answer). Abortable; ``down``: what to say when nobody
        answers."""
        conn = http.client.HTTPConnection(host, port, timeout=CONNECT_S)
        with self._lock:
            self._aborted = False
            self._active.add(conn)
        try:
            try:
                conn.connect()  # its own deadline: Windows takes ~2 s to refuse a loopback port nobody listens on
            except OSError as error:
                raise LLMError(down) from error
            if conn.sock is not None:
                conn.sock.settimeout(timeout)
            payload = json.dumps(body).encode("utf-8") if body is not None else None
            conn.request(method, path, body=payload, headers={"Content-Type": "application/json"})
            response = conn.getresponse()
            raw = response.read()
            status = response.status
        except TimeoutError as error:
            raise LLMError("The local model took too long.") from error
        except (OSError, http.client.HTTPException) as error:
            if self._aborted:
                raise LLMCancelled("Cancelled") from error
            raise LLMError(down) from error
        finally:
            with self._lock:
                self._active.discard(conn)
            conn.close()
        try:
            return status, (json.loads(raw.decode("utf-8")) if raw else {})
        except (UnicodeDecodeError, ValueError) as error:
            raise LLMError(f"{who} sent something Nova can't read.") from error

    def _call(self, method: str, path: str, body: Mapping[str, Any] | None = None, timeout: float = 10.0,
              model: str = "") -> Any:
        status, data = self._send(self._host, self._port, method, path, body, timeout, who="Ollama",
                                  down=f"Ollama isn't answering at {self.url} — is it running?")
        if status == 404 and model:
            raise LLMError(f"The model {model} isn't on this laptop — run: ollama pull {model}")
        if status >= 400:
            detail = data.get("error") if isinstance(data, Mapping) else None
            raise LLMError(f"Ollama: {detail or f'HTTP {status}'}")
        return data

    @staticmethod
    def _check(model: str) -> None:
        if is_cloud(model):
            raise LLMError(f"{model} is a cloud model; Nova only runs models on this laptop.")

    # --- calls ----------------------------------------------------------------------------------------------

    def generate(self, model: str, prompt: str, *, schema: Mapping[str, Any] | None = None,
                 options: Mapping[str, Any] | None = None, keep_alive: str = "10m", timeout: float = 8.0,
                 num_predict: int | None = None) -> Reply:
        """Raw completion: ``prompt`` is sent exactly as given (Nova rendered the chat template)."""
        self._check(model)
        opts = {**SAMPLING, **(options or {})}
        if num_predict is not None:
            opts["num_predict"] = num_predict
        body: dict[str, Any] = {"model": model, "prompt": prompt, "raw": True, "stream": False,
                                "keep_alive": keep_alive, "options": opts}
        if schema is not None:
            body["format"] = schema
        began = time.perf_counter()
        data = self._call("POST", "/api/generate", body, timeout, model)
        return reply_of(data, (time.perf_counter() - began) * 1000)

    def chat(self, model: str, messages: Sequence[Mapping[str, str]], *, schema: Mapping[str, Any] | None = None,
             options: Mapping[str, Any] | None = None, keep_alive: str = "10m", timeout: float = 8.0,
             num_predict: int | None = None) -> Reply:
        self._check(model)
        opts = {**SAMPLING, **(options or {})}
        if num_predict is not None:
            opts["num_predict"] = num_predict
        body: dict[str, Any] = {"model": model, "messages": [dict(m) for m in messages], "stream": False,
                                "think": False, "keep_alive": keep_alive, "options": opts}
        if schema is not None:
            body["format"] = schema
        began = time.perf_counter()
        data = self._call("POST", "/api/chat", body, timeout, model)
        return reply_of(data, (time.perf_counter() - began) * 1000)

    def load(self, model: str, *, options: Mapping[str, Any] | None = None, keep_alive: str = "10m",
             timeout: float = 120.0) -> float:
        """Load ``model`` (no-op if it's resident with the same options). Returns ms."""
        self._check(model)
        began = time.perf_counter()
        self._call("POST", "/api/generate", {"model": model, "keep_alive": keep_alive, "options": dict(options or {})},
                   timeout, model)
        return (time.perf_counter() - began) * 1000

    def unload(self, model: str, timeout: float = 10.0) -> None:
        self._call("POST", "/api/generate", {"model": model, "keep_alive": 0}, timeout, model)

    def loaded(self, timeout: float = 2.0) -> tuple[Loaded, ...]:
        data = self._call("GET", "/api/ps", timeout=timeout)
        out = []
        for row in data.get("models") or []:
            if isinstance(row, Mapping) and isinstance(row.get("name"), str):
                out.append(Loaded(row["name"], _int(row.get("size")), _int(row.get("size_vram"))))
        return tuple(out)

    def models(self, timeout: float = 3.0) -> dict[str, int]:
        """Pulled local models -> size in bytes (cloud tags left out)."""
        data = self._call("GET", "/api/tags", timeout=timeout)
        return {row["name"]: _int(row.get("size")) for row in data.get("models") or []
                if isinstance(row, Mapping) and isinstance(row.get("name"), str) and not is_cloud(row["name"])}

    def show(self, model: str, timeout: float = 5.0) -> dict[str, Any]:
        self._check(model)
        data = self._call("POST", "/api/show", {"model": model}, timeout, model)
        return data if isinstance(data, dict) else {}


# --- loading, apart from answering (SPEC §7.4) ------------------------------------------------------------------------


class Load:
    """One model load: ``wait`` for it; ``error`` says why it failed ("" = loaded)."""

    def __init__(self, model: str) -> None:
        self.model = model
        self.error = ""
        self.ms = 0.0
        self._done = threading.Event()

    @property
    def done(self) -> bool:
        return self._done.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._done.wait(timeout)


class Loader:
    """Loads a model once for every caller (Tier 1, dictation cleanup, preloads) on its own client, so an Esc that
    aborts a command never aborts a load: a cold load (15 s+) outlasts any answer's timeout, and Ollama drops a load
    whose request went away — the next request then starts from zero. While a model loads, callers wait on the same
    ``Load``; a second load of it is never started."""

    def __init__(self, client: Any, *, timeout: float = LOAD_TIMEOUT_S) -> None:
        self._client = client
        self._timeout = timeout
        self._lock = threading.Lock()
        self._loads: dict[str, Load] = {}  # in flight

    @property
    def url(self) -> str:
        return str(getattr(self._client, "url", ""))

    def resident(self, model: str) -> bool:
        """``model`` is loaded and not being (re)loaded by Nova right now (Ollama's /api/ps: ~3 ms)."""
        with self._lock:
            if model in self._loads:
                return False
        try:
            rows = self._client.loaded(timeout=_PS_TIMEOUT_S)
        except LLMError:
            return False
        return any(row.name == model for row in rows)

    def loading(self, model: str) -> Load | None:
        with self._lock:
            return self._loads.get(model)

    def load(self, model: str, *, options: Mapping[str, Any], keep_alive: str, prompt: str | None = None) -> Load:
        """Start loading ``model`` — reading ``prompt`` once, so a hybrid model like Qwen 3.5 keeps a resume point at
        its end — or join the load of it already under way. Never blocks."""
        with self._lock:
            current = self._loads.get(model)
            if current is not None:
                return current
            job = Load(model)
            self._loads[model] = job
        threading.Thread(target=self._run, args=(job, dict(options), keep_alive, prompt), name="nova-llm-load",
                         daemon=True).start()
        return job

    def _run(self, job: Load, options: dict[str, Any], keep_alive: str, prompt: str | None) -> None:
        began = time.perf_counter()
        try:
            if prompt is not None:
                self._client.generate(job.model, prompt, options=options, keep_alive=keep_alive,
                                      timeout=self._timeout, num_predict=1)
            else:
                self._client.load(job.model, options=options, keep_alive=keep_alive, timeout=self._timeout)
        except LLMError as error:
            job.error = str(error)
        except Exception as error:  # noqa: BLE001 - a load must never take a thread down silently
            job.error = f"Loading {job.model} failed: {type(error).__name__}"
        finally:
            job.ms = (time.perf_counter() - began) * 1000
            with self._lock:
                self._loads.pop(job.model, None)
            job._done.set()
