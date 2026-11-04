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

    def _call(self, method: str, path: str, body: Mapping[str, Any] | None = None, timeout: float = 10.0,
              model: str = "") -> Any:
        conn = http.client.HTTPConnection(self._host, self._port, timeout=CONNECT_S)
        with self._lock:
            self._aborted = False
            self._active.add(conn)
        try:
            try:
                conn.connect()  # its own deadline: Windows takes ~2 s to refuse a loopback port nobody listens on
            except OSError as error:
                raise LLMError(f"Ollama isn't answering at {self.url} — is it running?") from error
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
            raise LLMError(f"Ollama isn't answering at {self.url} — is it running?") from error
        finally:
            with self._lock:
                self._active.discard(conn)
            conn.close()
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, ValueError) as error:
            raise LLMError("Ollama sent something Nova can't read.") from error
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
