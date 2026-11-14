"""The resource governor (SPEC §15): which local model runs Tier 1, and where.

- GPU model on the RTX when NVML says it has the model + ``headroom_gb`` free (Fusion is a heavy
  GPU user); otherwise the CPU model (``num_gpu: 0``). ``llm.device`` forces one;
  ``llm.simulate_free_vram_gb`` fakes the NVML number for testing.
- One Nova model at a time: switching unloads the last one. Ollama unloads it by itself after
  ``keep_alive`` unused (sent with every call), and Nova unloads it when it quits.
- Cheap on the hot path: a recent choice is reused without asking NVML or Ollama again;
  preload hints (key down / wake name) refresh it in the background.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from .config import Llm

_log = logging.getLogger(__name__)

GB = 1024**3
REUSE_S = 120.0  # a choice this fresh is reused as-is
# GPU = every layer on the GPU (Ollama passes num_gpu to llama-server as -ngl). Left to itself, Ollama 0.30's
# llama-server fit reserved ~5.3 GB for qwen3.5's vision projector and put 0 of 33 layers on an RTX with 6.9 GB
# free (2026-11-11, Fusion open): the "GPU" model ran on the CPU at 11-14 tokens/s. ``decide`` already checked room.
ALL_LAYERS = 99
_OVERHEAD_GB = 0.4  # KV cache (num_ctx 4096) + compute buffers on top of the weights


@dataclass(frozen=True)
class Choice:
    model: str
    device: str  # gpu | cpu
    why: str = field(default="", compare=False)

    @property
    def options(self) -> dict[str, Any]:
        """Ollama runner options: CPU = no layers on the GPU, GPU = all of them."""
        return {"num_gpu": 0 if self.device == "cpu" else ALL_LAYERS}


def decide(llm: Llm, *, free_gb: float | None, need_gb: float, resident: bool = False) -> Choice:
    """Pure: settings + free VRAM + what the GPU model needs -> the choice."""
    if llm.device == "cpu":
        return Choice(llm.cpu_model, "cpu", "device: cpu")
    if llm.device == "gpu":
        return Choice(llm.gpu_model, "gpu", "device: gpu")
    simulated = llm.simulate_free_vram_gb is not None
    if resident and not simulated:
        return Choice(llm.gpu_model, "gpu", "already on the GPU")
    free = llm.simulate_free_vram_gb if simulated else free_gb
    if free is None:
        return Choice(llm.cpu_model, "cpu", "no NVIDIA GPU answering")
    need = need_gb + llm.headroom_gb
    note = " (simulated)" if simulated else ""
    if free >= need:
        return Choice(llm.gpu_model, "gpu", f"{free:.1f} GB free ≥ {need:.1f} GB{note}")
    return Choice(llm.cpu_model, "cpu", f"{free:.1f} GB free < {need:.1f} GB{note}")


class Client(Protocol):
    def loaded(self, timeout: float = ...) -> Any: ...
    def models(self, timeout: float = ...) -> dict[str, int]: ...
    def unload(self, model: str, timeout: float = ...) -> None: ...


_nvml_lock = threading.Lock()
_nvml_ready: bool | None = None


def nvml_free_gb() -> float | None:
    """Free VRAM on the NVIDIA GPU, or None if there's no driver answering."""
    global _nvml_ready
    try:
        import pynvml

        with _nvml_lock:
            if _nvml_ready is None:
                try:
                    pynvml.nvmlInit()
                    _nvml_ready = True
                except pynvml.NVMLError:
                    _nvml_ready = False
            if not _nvml_ready:
                return None
            handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            return pynvml.nvmlDeviceGetMemoryInfo(handle).free / GB
    except Exception:  # noqa: BLE001 - no GPU info = CPU model
        return None


class Governor:
    def __init__(self, client: Client, settings: Callable[[], Llm], *,
                 vram: Callable[[], float | None] = nvml_free_gb, clock: Callable[[], float] = time.monotonic) -> None:
        self._client = client
        self._settings = settings
        self._vram = vram
        self._clock = clock
        self._lock = threading.Lock()
        self._current: Choice | None = None
        self._fresh_at = -1e9  # when the current choice was made or last used
        self._sizes: dict[str, int] = {}
        self._measured: dict[str, float] = {}

    @property
    def current(self) -> Choice | None:
        return self._current

    def forget(self) -> None:
        """Decide afresh next time (settings changed, a preload hint)."""
        with self._lock:
            self._fresh_at = -1e9

    def used(self) -> None:
        with self._lock:
            self._fresh_at = self._clock()

    def need_gb(self, model: str) -> float:
        """What ``model`` takes on the GPU: as Ollama measured it once loaded, else its file + KV and buffers."""
        if model in self._measured:
            return self._measured[model]
        if model not in self._sizes:
            try:
                self._sizes.update(self._client.models())
            except Exception:  # noqa: BLE001 - unknown size: assume a ~4B model
                pass
        size = self._sizes.get(model)
        return round(size / GB * 1.1 + _OVERHEAD_GB, 2) if size else 4.0

    def _resident(self, model: str) -> bool:
        try:
            rows = self._client.loaded()
        except Exception:  # noqa: BLE001
            return False
        for row in rows:
            if row.vram > 0:
                self._measured[row.name] = round(row.size / GB, 2)
            if row.name == model and row.vram > 0:
                return True
        return False

    def choose(self, *, fresh: bool = False) -> Choice:
        llm = self._settings()
        with self._lock:
            now = self._clock()
            current = self._current
            if (not fresh and current is not None and now - self._fresh_at < REUSE_S
                    and current.model in (llm.gpu_model, llm.cpu_model)):
                return current
            resident = self._resident(llm.gpu_model) if llm.device == "auto" else False
            choice = decide(llm, free_gb=self._vram(), need_gb=self.need_gb(llm.gpu_model), resident=resident)
            if current is not None and current.model != choice.model:
                self._unload(current.model)
            if choice != current:
                _log.info("LLM: %s on the %s (%s)", choice.model, choice.device.upper(), choice.why)
            self._current = choice
            self._fresh_at = now
            return choice

    def _unload(self, model: str) -> None:
        try:
            self._client.unload(model)
        except Exception as error:  # noqa: BLE001
            _log.warning("couldn't unload %s: %s", model, error)

    def close(self) -> None:
        """Nova is quitting: free the memory now instead of after keep_alive."""
        with self._lock:
            current, self._current = self._current, None
        if current is not None:
            self._unload(current.model)
