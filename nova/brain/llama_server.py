"""llama.cpp's server as Tier 1's runtime (SPEC §7.4, Stage 07a). Local only: a child process on 127.0.0.1.

Why: Ollama 0.30 itself runs models through the ``llama-server.exe`` it ships (``lib/ollama``), but adds ~0.3 s to
every request and re-reads the whole prompt each time. Talking to that same binary directly, on the same GGUF with
the same prompt and schema, measured 437 ms vs 716 ms warm p50 on the RTX (2026-11-13, `nova bench-llm --runtime`).

How:
- ``LlamaServer``: the one child process. Started on demand for the model the governor picked (GPU = every layer,
  CPU = none, no vision projector), health-checked, stopped after ``keep_alive`` unused, on unload and when Nova
  ends — however it ends (a Windows job object). One model at a time; a start in flight is joined, never repeated.
- ``LlamaClient``: the same surface as ``nova.brain.llm.Ollama``. Raw completions go to the child; which files a
  model has, chat-template models (Gemma) and everything else stay with Ollama, which is also the fallback when the
  child can't run here (``llm.runtime: auto``).
"""

from __future__ import annotations

import atexit
import contextlib
import ctypes
import http.client
import json
import logging
import os
import re
import socket
import subprocess
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from ctypes import wintypes
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .. import paths
from ..governor import ALL_LAYERS, GB
from .llm import DEFAULT_URL, NUM_CTX, SAMPLING, LLMCancelled, LLMError, Loaded, Ollama, Reply

_log = logging.getLogger(__name__)

EXE = "llama-server.exe"
START_TIMEOUT_S = 180.0  # a cold load (7-8 s on the RTX, longer on the CPU); after this the child is hung
RETRY_S = 300.0  # a child that couldn't start is tried again after this long (the GPU may have been full)
DEFAULT_KEEP_ALIVE_S = 600.0
_POLL_S = 0.1
_OVERHEAD = 0.4 * GB  # KV cache + compute buffers on top of the weights (as the governor estimates)
_SAMPLING_KEYS = (*SAMPLING, "top_p", "min_p", "seed")


# --- pure parts ------------------------------------------------------------------------------------------------------


def find_exe() -> Path | None:
    """The llama-server Ollama ships (no download: the binary Ollama itself runs models with)."""
    exe = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "lib" / "ollama" / EXE
    return exe if exe.is_file() else None


def gguf_path(modelfile: str) -> str | None:
    """The model's own GGUF from `ollama show --modelfile` (a second FROM is the vision projector)."""
    for line in modelfile.splitlines():
        if line.startswith("FROM "):
            path = line[5:].strip()
            if Path(path).is_absolute():
                return path
    return None


def server_args(exe: Path, gguf: str, *, port: int, ctx: int = NUM_CTX, layers: int = ALL_LAYERS) -> list[str]:
    """One slot, loopback only, no web UI, no network, no vision projector. Nova renders the chat template itself."""
    return [str(exe), "-m", gguf, "--host", "127.0.0.1", "--port", str(port), "--no-webui", "--offline",
            "-c", str(ctx), "-np", "1", "-ngl", str(layers), "-fa", "auto", "--no-jinja", "--chat-template", "chatml"]


def server_env(lib: Path, base: Mapping[str, str] | None = None, *, gpu: bool = True) -> dict[str, str]:
    """Ollama's llama-server finds its CUDA backend only with lib/ollama/cuda_v13 on the DLL path. The CPU choice
    leaves it out, so a busy GPU (Fusion) isn't asked for a CUDA context at all."""
    env = dict(os.environ if base is None else base)
    if not gpu:
        return {**env, "PATH": f"{lib};{env.get('PATH', '')}"}
    cuda = lib / "cuda_v13"
    return {**env, "PATH": f"{cuda};{lib};{env.get('PATH', '')}", "GGML_BACKEND_PATH": str(cuda / "ggml-cuda.dll")}


_KEEP = re.compile(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?")


def keep_alive_s(text: str) -> float | None:
    """``llm.keep_alive`` in seconds: "10m" -> 600, "0" -> 0 (unload after every request), "-1" -> None (never)."""
    text = text.strip()
    if text == "-1":
        return None
    if text == "0":
        return 0.0
    match = _KEEP.fullmatch(text)
    if not text or match is None:
        return DEFAULT_KEEP_ALIVE_S
    hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return float(hours * 3600 + minutes * 60 + seconds)


def completion_body(prompt: str, *, schema: Mapping[str, Any] | None, options: Mapping[str, Any],
                    num_predict: int | None) -> dict[str, Any]:
    """llama-server's /completion request for one raw prompt. ``cache_prompt``: the static prefix read at load is
    kept, so a request only reads its own part."""
    body: dict[str, Any] = {"prompt": prompt, "cache_prompt": True, **SAMPLING}
    body.update({key: options[key] for key in _SAMPLING_KEYS if key in options})
    if num_predict is not None:
        body["n_predict"] = num_predict
    if schema is not None:
        body["json_schema"] = dict(schema)
    if options.get("stop"):
        body["stop"] = list(options["stop"])
    return body


def reply_of_completion(data: Any, ms: float, *, load_ms: float = 0.0) -> Reply:
    text = data.get("content") if isinstance(data, Mapping) else None
    if not isinstance(text, str):
        raise LLMError("The local model sent an answer Nova can't read.")
    timings = data.get("timings") if isinstance(data.get("timings"), Mapping) else {}

    def number(key: str) -> float:
        value = timings.get(key)
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0

    return Reply(text, round(ms, 1), round(load_ms, 1), int(number("prompt_n")), round(number("prompt_ms"), 1),
                 int(number("predicted_n")), round(number("predicted_ms"), 1))


# --- the child process -----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Serving:
    model: str
    gguf: str
    layers: int  # on the GPU (0 = the CPU choice)
    ctx: int
    port: int
    size: int  # the GGUF's bytes
    started_ms: float  # how long it took to come up (0 while it is still loading)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _health(port: int) -> bool:
    """200 once the model is loaded (503 while it loads, nothing at all before the port is open)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1.0)
    try:
        conn.request("GET", "/health")
        return conn.getresponse().status == 200
    except (OSError, http.client.HTTPException):
        return False
    finally:
        conn.close()


def _spawn(args: list[str], env: Mapping[str, str]) -> subprocess.Popen:
    paths.LOGS.mkdir(parents=True, exist_ok=True)
    with (paths.LOGS / "llama-server.log").open("w", encoding="utf-8") as log:  # the last start's output
        proc = subprocess.Popen(args, env=dict(env), stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    _die_with_nova(proc)
    return proc


def _timer(seconds: float, callback: Callable[[], None]) -> Callable[[], None]:
    timer = threading.Timer(seconds, callback)
    timer.daemon = True
    timer.start()
    return timer.cancel


class _BasicLimits(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BasicLimits), ("IoInfo", ctypes.c_uint64 * 6),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


_JOB_EXTENDED_LIMIT_INFORMATION = 9
_KILL_ON_JOB_CLOSE = 0x2000
_job: int | None = None
_job_lock = threading.Lock()


def _die_with_nova(proc: subprocess.Popen) -> None:
    """The child must not outlive Nova holding 3 GB of VRAM. A job object that kills its processes when its last
    handle closes — and Windows closes Nova's handle when Nova ends, cleanly or not (taskkill, a crash)."""
    global _job
    if os.name != "nt":
        return
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        kernel32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
        kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        with _job_lock:
            if _job is None:
                job = kernel32.CreateJobObjectW(None, None)
                limits = _ExtendedLimits()
                limits.BasicLimitInformation.LimitFlags = _KILL_ON_JOB_CLOSE
                if not job or not kernel32.SetInformationJobObject(job, _JOB_EXTENDED_LIMIT_INFORMATION,
                                                                   ctypes.byref(limits), ctypes.sizeof(limits)):
                    raise ctypes.WinError(ctypes.get_last_error())
                _job = job
        if not kernel32.AssignProcessToJobObject(_job, int(proc._handle)):  # noqa: SLF001 - the process handle
            raise ctypes.WinError(ctypes.get_last_error())
    except Exception:  # noqa: BLE001 - without the job, stop() and atexit still end the child
        _log.warning("llama-server isn't tied to Nova's life (job object failed)", exc_info=True)


class LlamaServer:
    """The llama-server child: at most one, serving one model. Thread-safe."""

    def __init__(self, *, exe: Path | None = None, spawn: Callable[[list[str], Mapping[str, str]], Any] = _spawn,
                 health: Callable[[int], bool] = _health, free_port: Callable[[], int] = _free_port,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                 timer: Callable[[float, Callable[[], None]], Callable[[], None]] = _timer) -> None:
        self._exe = exe
        self._spawn = spawn
        self._health = health
        self._free_port = free_port
        self._clock = clock
        self._sleep = sleep
        self._timer = timer
        self._lock = threading.Lock()
        self._proc: Any = None
        self._serving: Serving | None = None  # what the child was started for
        self._ready = False
        self._started_at = 0.0
        self._broken = ""
        self._broken_at = 0.0
        self._busy = 0  # requests in flight
        self._idle_at: float | None = None  # stop when unused until then
        self._keep = DEFAULT_KEEP_ALIVE_S
        self._cancel_timer: Callable[[], None] | None = None

    # --- state ----------------------------------------------------------------------------------------------------

    @property
    def usable(self) -> bool:
        """The binary is here and didn't just fail to start."""
        if self._exe is None:
            return False
        return not self._broken or self._clock() - self._broken_at > RETRY_S

    @property
    def broken(self) -> str:
        """Why it can't run ("" = it can)."""
        if self._exe is None:
            return f"{EXE} isn't on this laptop (it comes with Ollama 0.30)"
        return self._broken

    def serves(self, model: str) -> bool:
        """The child is running — or starting — for ``model``."""
        with self._lock:
            alive = self._proc is not None and self._proc.poll() is None
            return alive and self._serving is not None and self._serving.model == model

    def rows(self) -> tuple[Loaded, ...]:
        """What is loaded and ready, in Ollama's /api/ps shape (for the governor and the loader)."""
        with self._lock:
            s = self._serving
            if s is None or not self._ready or self._proc is None or self._proc.poll() is not None:
                return ()
            size = int(s.size * 1.1 + _OVERHEAD)
            return (Loaded(s.model, size, size if s.layers > 0 else 0),)

    # --- start / stop -----------------------------------------------------------------------------------------------

    def ensure(self, model: str, gguf: str, *, size: int, layers: int, ctx: int, timeout: float,
               cancelled: Callable[[], bool] = lambda: False, hold: bool = False) -> Serving:
        """The child serving ``model``, ready: as it runs, joined while it starts, or started now (replacing another
        model). Waits at most ``timeout`` and stops waiting when ``cancelled`` — the start itself goes on, so the
        next caller finds the model warm. Raises LLMError when it can't run. ``hold``: return with a request already
        counted (in the same step as "ready", so the idle timer can't stop the child in between); the caller then
        runs it in ``request(held=True)``, which releases it."""
        deadline = self._clock() + timeout
        want = (model, gguf, layers, ctx)
        with self._lock:
            if self._exe is None:
                raise LLMError(self.broken)
            s = self._serving
            alive = self._proc is not None and self._proc.poll() is None
            same = alive and s is not None and (s.model, s.gguf, s.layers, s.ctx) == want
            if same and self._ready and s is not None:
                if hold:
                    self._busy += 1
                return s
            if not same:
                if not self.usable:
                    raise LLMError(self._broken)
                self._kill()
                port = self._free_port()
                try:
                    self._proc = self._spawn(server_args(self._exe, gguf, port=port, ctx=ctx, layers=layers),
                                             server_env(self._exe.parent, gpu=layers > 0))
                except OSError as error:  # broken like a child that died: auto falls back to Ollama
                    self._broken = f"llama-server couldn't start: {error}"
                    self._broken_at = self._clock()
                    _log.warning("%s", self._broken)
                    raise LLMError(self._broken) from error
                self._serving = Serving(model, gguf, layers, ctx, port, size, 0.0)
                self._ready = False
                self._started_at = self._clock()
                self._broken = ""
                _log.info("llama-server starting: %s, %s", model, "GPU" if layers > 0 else "CPU")
            proc, serving, started = self._proc, self._serving, self._started_at
        assert serving is not None
        while True:
            code = proc.poll()
            if code is not None:
                raise LLMError(self._fail(proc, f"llama-server stopped (exit {code}) — see logs/llama-server.log"))
            if self._health(serving.port):
                with self._lock:
                    if self._proc is not proc:
                        raise LLMError("The local model was switched while it loaded.")
                    if not self._ready:
                        self._ready = True
                        self._serving = replace(serving, started_ms=round((self._clock() - started) * 1000, 1))
                        _log.info("llama-server ready: %s in %.0f ms", model, self._serving.started_ms)
                    if hold:
                        self._busy += 1
                    return self._serving  # type: ignore[return-value]
            if cancelled():
                raise LLMCancelled("Cancelled")
            now = self._clock()
            if now - started > START_TIMEOUT_S:
                raise LLMError(self._fail(proc, f"llama-server didn't come up in {START_TIMEOUT_S:.0f} s"))
            if now >= deadline:
                raise LLMError("The local model took too long.")
            self._sleep(_POLL_S)

    def _fail(self, proc: Any, why: str) -> str:
        with self._lock:
            if self._proc is proc:
                self._kill()
                self._broken = why
                self._broken_at = self._clock()
                _log.warning("%s", why)
        return why

    def _kill(self) -> None:
        """Lock held: end the child (if any) and forget it."""
        proc, self._proc, self._serving, self._ready = self._proc, None, None, False
        if self._cancel_timer is not None:
            self._cancel_timer()
            self._cancel_timer = None
        self._idle_at = None
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001 - already gone, or the job object ends it with Nova
            _log.debug("llama-server didn't stop cleanly", exc_info=True)

    def stop(self, model: str | None = None) -> bool:
        """Stop the child (only if it serves — or is loading — ``model``, when given). True if it was stopped."""
        with self._lock:
            if self._proc is None or (model is not None and (self._serving is None or self._serving.model != model)):
                return False
            _log.info("llama-server stopped: %s", self._serving.model if self._serving else "")
            self._kill()
            return True

    # --- keep_alive -------------------------------------------------------------------------------------------------

    @contextlib.contextmanager
    def request(self, held: bool = False) -> Iterator[None]:
        """Around every request: a child that is answering is never stopped as idle. ``held``: ``ensure(hold=True)``
        already counted this one."""
        if not held:
            with self._lock:
                self._busy += 1
        try:
            yield
        finally:
            with self._lock:
                self._busy -= 1

    def used(self, keep_alive: float | None) -> None:
        """A request ended: stop the child when it stays unused for ``keep_alive`` s (None = never, 0 = now)."""
        with self._lock:
            if self._proc is None:
                return
            if self._cancel_timer is not None:
                self._cancel_timer()
                self._cancel_timer = None
            if keep_alive is None:
                self._idle_at = None
                return
            if keep_alive <= 0 and self._busy == 0:
                self._kill()
                return
            self._keep = max(keep_alive, 1.0)
            self._idle_at = self._clock() + keep_alive
            self._cancel_timer = self._timer(self._keep, self._expire)

    def _expire(self) -> None:
        with self._lock:
            if self._proc is None or self._idle_at is None or self._clock() < self._idle_at - 0.01:
                return  # stopped, or used since this timer was set (a newer one is running)
            if self._busy:
                self._idle_at = self._clock() + self._keep
                self._cancel_timer = self._timer(self._keep, self._expire)
                return
            _log.info("llama-server idle: unloading %s", self._serving.model if self._serving else "")
            self._kill()


_shared: LlamaServer | None = None
_shared_lock = threading.Lock()
_FILES: dict[str, tuple[str | None, int]] = {}  # model -> (its GGUF, bytes), as Ollama's store has it
_VIA_OLLAMA: set[str] = set()  # models this process has had Ollama serve (chat templates, loads, fallbacks)
_UNLOAD_S = 2.0  # asking Ollama to drop its copy before the child starts: best effort, never longer than this


def shared() -> LlamaServer:
    """This process's one llama-server (every client talks to the same child)."""
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = LlamaServer(exe=find_exe())
            atexit.register(_shared.stop)
        return _shared


# --- the client ------------------------------------------------------------------------------------------------------


class LlamaClient(Ollama):
    """``Ollama``'s surface with llama-server behind the raw completions. ``runtime``: ``llm.runtime`` as it is now
    (auto = llama-server, Ollama when it can't run · ollama · llama-server = no fallback)."""

    def __init__(self, url: str = DEFAULT_URL, server: Any = None, runtime: Callable[[], str] = lambda: "auto", *,
                 files: dict[str, tuple[str | None, int]] | None = None, via_ollama: set[str] | None = None) -> None:
        """``via_ollama``: the models Ollama has served for this client and its siblings — the only rows of
        Ollama's list that count as loaded (outside ``ollama`` mode), so a model Ollama holds for someone else
        never passes for the child's."""
        super().__init__(url)
        self._server = server if server is not None else shared()
        self._runtime = runtime
        self._files = files if files is not None else {}
        self._via_ollama = via_ollama if via_ollama is not None else set()

    @property
    def server(self) -> Any:
        return self._server

    def sibling(self) -> LlamaClient:
        return LlamaClient(self.url, self._server, self._runtime, files=self._files, via_ollama=self._via_ollama)

    def _file(self, model: str) -> tuple[str | None, int]:
        known = self._files.get(model)
        if known is None:
            gguf = gguf_path(str(self.show(model).get("modelfile", "")))
            try:
                size = os.path.getsize(gguf) if gguf else 0
            except OSError:
                gguf, size = None, 0
            known = self._files[model] = (gguf, size)
        return known

    def _serve(self, model: str, options: Mapping[str, Any] | None, timeout: float) -> Serving | None:
        """The child, ready for ``model`` and already counted as in use (run the request in ``request(held=True)``)
        — or None when this call is Ollama's."""
        mode = self._runtime()
        if mode == "ollama":
            self._server.stop()  # left from before the setting changed: one copy of the model at a time
            return None
        fallback = mode != "llama-server"
        if not self._server.usable:
            if fallback:
                return None
            raise LLMError(self._server.broken)
        with self._lock:
            self._aborted = False
        opts = options or {}
        try:
            gguf, size = self._file(model)
            if gguf is None:
                if fallback:
                    return None
                raise LLMError(f"{model} has no GGUF file llama-server can open")
            if not self._server.serves(model):  # never the same model on the GPU twice
                self._drop_from_ollama(model)
            return self._server.ensure(model, gguf, size=size, layers=int(opts.get("num_gpu", ALL_LAYERS)),
                                       ctx=int(opts.get("num_ctx", NUM_CTX)), timeout=timeout,
                                       cancelled=lambda: self._aborted, hold=True)
        except LLMCancelled:
            raise
        except LLMError as error:
            if fallback and not self._server.usable:  # it can't run here: Ollama answers until it can
                self._files.pop(model, None)
                _log.warning("llama-server can't run (%s): using Ollama", error)
                return None
            raise

    def generate(self, model: str, prompt: str, *, schema: Mapping[str, Any] | None = None,
                 options: Mapping[str, Any] | None = None, keep_alive: str = "10m", timeout: float = 8.0,
                 num_predict: int | None = None) -> Reply:
        self._check(model)
        began = time.perf_counter()
        serving = self._serve(model, options, timeout)
        if serving is None:
            self._via_ollama.add(model)
            return super().generate(model, prompt, schema=schema, options=options, keep_alive=keep_alive,
                                    timeout=timeout, num_predict=num_predict)
        try:
            with self._server.request(held=True):  # counted by ensure: released here, whatever happens
                load_ms = (time.perf_counter() - began) * 1000
                body = completion_body(prompt, schema=schema, options=options or {}, num_predict=num_predict)
                status, data = self._send("127.0.0.1", serving.port, "POST", "/completion", body,
                                          max(0.5, timeout - load_ms / 1000), who="llama-server",
                                          down="The local model stopped answering.")
        finally:
            self._server.used(keep_alive_s(keep_alive))
        if status >= 400:
            detail = data.get("error") if isinstance(data, Mapping) else None
            message = detail.get("message") if isinstance(detail, Mapping) else detail
            raise LLMError(f"llama-server: {message or f'HTTP {status}'}")
        return reply_of_completion(data, (time.perf_counter() - began) * 1000, load_ms=load_ms)

    def chat(self, model: str, messages: Sequence[Mapping[str, str]], *, schema: Mapping[str, Any] | None = None,
             options: Mapping[str, Any] | None = None, keep_alive: str = "10m", timeout: float = 8.0,
             num_predict: int | None = None) -> Reply:
        """Chat-template models stay with Ollama."""
        self._via_ollama.add(model)
        return super().chat(model, messages, schema=schema, options=options, keep_alive=keep_alive, timeout=timeout,
                            num_predict=num_predict)

    def load(self, model: str, *, options: Mapping[str, Any] | None = None, keep_alive: str = "10m",
             timeout: float = 120.0) -> float:
        """A load without a prompt is the chat-template path (raw models are warmed by reading their prefix through
        ``generate``): Ollama's, as their requests are."""
        self._check(model)
        self._via_ollama.add(model)
        return super().load(model, options=options, keep_alive=keep_alive, timeout=timeout)

    def unload(self, model: str, timeout: float = 10.0) -> None:
        if not self._server.stop(model):
            super().unload(model, timeout)

    def _drop_from_ollama(self, model: str) -> None:
        """Before the child loads ``model``: Ollama's copy goes (best effort — it may hold none, or not be running)."""
        self._via_ollama.discard(model)
        conn = http.client.HTTPConnection(self._host, self._port, timeout=_UNLOAD_S)
        try:
            conn.request("POST", "/api/generate", body=json.dumps({"model": model, "keep_alive": 0}).encode("utf-8"),
                         headers={"Content-Type": "application/json"})
            conn.getresponse().read()
        except (OSError, http.client.HTTPException) as error:
            _log.debug("Ollama didn't unload %s before llama-server: %s", model, error)
        finally:
            conn.close()

    def loaded(self, timeout: float = 2.0) -> tuple[Loaded, ...]:
        """The child's model, plus Ollama's rows only for models Ollama served here (``ollama`` mode: Ollama's list).
        Nothing served through Ollama = no HTTP at all: this is on every request's path."""
        if self._runtime() == "ollama":
            return super().loaded(timeout)
        rows = self._server.rows()
        ours = frozenset(self._via_ollama)
        if not ours:
            return rows
        try:
            theirs = super().loaded(timeout)
        except LLMError:
            if rows:
                return rows
            raise
        child = {row.name for row in rows}
        return (*rows, *(row for row in theirs if row.name in ours and row.name not in child))


def connect(url: str = DEFAULT_URL, runtime: Callable[[], str] = lambda: "auto") -> LlamaClient:
    """The LLM client for ``llm.runtime`` (read on every call, so a settings change needs no restart)."""
    return LlamaClient(url, shared(), runtime, files=_FILES, via_ollama=_VIA_OLLAMA)
