"""`nova bench-llm` (Stage 05): candidate local models x devices, measured on this laptop.

For each model: on the GPU, every eval phrasing through Tier 1 (accuracy on its own and behind
Tier 0, false actions, p50 / p95), cold load (load + reading the prompt prefix) and VRAM. On the
CPU (``num_gpu: 0``) and the Arc iGPU, a spread of phrasings for latency and cold load (same
weights, same answers). The Arc runs on a temporary second Ollama (127.0.0.1:11435, Vulkan,
integrated GPU enabled, CUDA hidden) that is stopped afterwards; Bhavya's Ollama is untouched.
Picks the defaults from the numbers; ``--save`` writes them to settings.yaml.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from . import paths
from .brain.llm import LLMError, Ollama
from .brain.router import Router
from .brain.tier1 import Tier1
from .config import ConfigError, ConfigStore
from .evals import Case, Item, load_cases, run, summarize
from .governor import GB, Choice, Governor

CANDIDATES = ("qwen3.5:4b", "qwen3.5:2b-q4_K_M", "gemma4:e2b", "qwen2.5:7b")  # 7B: the accuracy baseline only
BASELINE = "qwen2.5:7b"
ARC_URL = "http://127.0.0.1:11435"
GPU_P95_MS = 1200.0
CPU_P95_MS = 2500.0


@dataclass(frozen=True)
class Row:
    model: str
    device: str
    cases: int
    accuracy: float  # Tier 1 alone, positives
    router: float  # Tier 0 first, then Tier 1 (what he gets)
    false_rate: float  # negatives, router
    p50: float
    p95: float
    load_p50: float  # Ollama's per-request overhead
    cold_ms: float  # unloaded -> loaded and the prompt prefix read
    mem_gb: float  # VRAM (GPU / Arc) or RAM (CPU) as Ollama reports it
    note: str = ""


def _spread(cases: Sequence[Case], n: int) -> list[Case]:
    """``n`` cases spread over the file (positives and negatives in proportion)."""
    if n <= 0 or n >= len(cases):
        return list(cases)
    step = len(cases) / n
    return [cases[int(i * step)] for i in range(n)]


def _combined(t0: Sequence[Item], t1: Sequence[Item], cases: Sequence[Case], catalog) -> tuple[float, float]:
    """Router accuracy and false rate: Tier 0's answer where it has one, else Tier 1's."""
    right = positives = wrong = negatives = 0
    for zero, one, case in zip(t0, t1, cases, strict=True):
        item = zero if zero.tier == "0" else one
        if case.group == "positive":
            positives += 1
            right += item.ok
        else:
            negatives += 1
            wrong += not item.ok
    return (right / positives if positives else math.nan, wrong / negatives if negatives else math.nan)


def _ollama_exe() -> str | None:
    found = shutil.which("ollama")
    if found:
        return found
    candidate = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
    return str(candidate) if candidate.is_file() else None


class ArcServer:
    """A temporary Ollama on the Arc iGPU (Vulkan): integrated GPU on, CUDA hidden, nothing pruned."""

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.log = paths.LOGS / "bench-arc-ollama.log"
        self.device = ""

    def __enter__(self) -> ArcServer:
        exe = _ollama_exe()
        if exe is None:
            raise RuntimeError("ollama.exe not found")
        env = {**os.environ, "OLLAMA_HOST": ARC_URL.removeprefix("http://"), "OLLAMA_IGPU_ENABLE": "1",
               "CUDA_VISIBLE_DEVICES": "-1", "OLLAMA_NOPRUNE": "1", "OLLAMA_VULKAN": "1"}
        self.log.parent.mkdir(parents=True, exist_ok=True)
        out = self.log.open("w", encoding="utf-8")
        self.proc = subprocess.Popen([exe, "serve"], env=env, stdout=out, stderr=subprocess.STDOUT,
                                     creationflags=subprocess.CREATE_NO_WINDOW)
        client = Ollama(ARC_URL)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                client.models(timeout=1.0)
                break
            except LLMError:
                time.sleep(0.5)
        else:
            raise RuntimeError("the Arc Ollama didn't start")
        time.sleep(1.0)
        for line in self.log.read_text(encoding="utf-8", errors="replace").splitlines():
            if "inference compute" in line:
                self.device = line.split("description=", 1)[-1].split(" libdirs")[0].strip('"')
        return self

    def __exit__(self, *exc: object) -> None:
        if self.proc is not None:
            subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"], capture_output=True,
                           creationflags=subprocess.CREATE_NO_WINDOW)
            self.proc.wait(timeout=10)


def _mem_gb(client: Ollama, model: str, device: str) -> float:
    try:
        for row in client.loaded():
            if row.name == model:
                return round((row.vram if device != "cpu" else row.size) / GB, 2)
    except LLMError:
        pass
    return math.nan


def _bench(tier1: Tier1, client: Ollama, router: Router, catalog, model: str, device: str, cases: Sequence[Case],
           t0_items: Sequence[Item] | None, note: str = "") -> Row:
    choice = Choice(model, "cpu" if device == "cpu" else "gpu", f"bench {device}")
    tier1.pin(choice)
    try:
        client.unload(model)
    except LLMError:
        pass
    time.sleep(1.0)
    cold = tier1.warm(choice)
    mem = _mem_gb(client, model, device)

    def progress(i: int, n: int, item: Item) -> None:
        if i % 50 == 0 or i == n:
            print(f"    {i}/{n}", flush=True)

    items = run(router, catalog, cases, tier1_only=True, progress=progress)
    summary = summarize(items)
    if t0_items is not None:
        router_acc, false_rate = _combined(t0_items, items, cases, catalog)
    else:
        router_acc, false_rate = math.nan, summary.false_rate
    try:
        client.unload(model)
    except LLMError:
        pass
    path = paths.LOGS / f"bench-{model.replace(':', '_')}-{device}.jsonl"
    with path.open("w", encoding="utf-8") as out:
        for item in items:
            out.write(json.dumps(item.__dict__, ensure_ascii=False) + "\n")
    return Row(model, device, len(cases), summary.accuracy, router_acc, false_rate, summary.tier1_p50,
               summary.tier1_p95, summary.load_p50, round(cold), mem, note)


def table(rows: Sequence[Row]) -> str:
    def pct(x: float) -> str:
        return "—" if math.isnan(x) else f"{x:.1%}"

    def num(x: float) -> str:
        return "—" if math.isnan(x) else f"{x:.0f}"

    lines = ["| model | device | n | Tier 1 alone | behind Tier 0 | false actions | p50 ms | p95 ms | "
             "Ollama overhead ms | cold load ms | memory GB |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        mem = "—" if math.isnan(r.mem_gb) else f"{r.mem_gb:.1f}"
        lines.append(f"| {r.model} | {r.device}{' ' + r.note if r.note else ''} | {r.cases} | {pct(r.accuracy)} | "
                     f"{pct(r.router)} | {pct(r.false_rate)} | {num(r.p50)} | {num(r.p95)} | {num(r.load_p50)} | "
                     f"{num(r.cold_ms)} | {mem} |")
    return "\n".join(lines)


def recommend(rows: Sequence[Row], sizes: dict[str, int]) -> tuple[str | None, str | None, str]:
    """GPU: the smallest candidate (not the baseline) that meets the targets behind Tier 0 within the GPU budget,
    else the most accurate one. CPU: the most accurate within the CPU budget, smaller first on ties."""
    gpu_rows = {r.model: r for r in rows if r.device == "gpu"}
    cpu_rows = [r for r in rows if r.device == "cpu"]
    small = [m for m in gpu_rows if m != BASELINE]
    good = [m for m in small if gpu_rows[m].router >= 0.9 and gpu_rows[m].false_rate < 0.01
            and gpu_rows[m].p95 <= GPU_P95_MS]
    why = []
    if good:
        gpu = min(good, key=lambda m: (sizes.get(m, 0), -gpu_rows[m].router))
        why.append(f"GPU: {gpu} is the smallest that meets ≥ 90 % / < 1 % within {GPU_P95_MS:.0f} ms p95")
    elif small:
        gpu = max(small, key=lambda m: (gpu_rows[m].router, -gpu_rows[m].p95))
        why.append(f"GPU: none met every target; {gpu} came closest")
    else:
        gpu = None
    fast = [r for r in cpu_rows if r.model != BASELINE and r.p95 <= CPU_P95_MS]
    pool = fast or [r for r in cpu_rows if r.model != BASELINE]
    cpu = None
    if pool:
        best = max(pool, key=lambda r: (gpu_rows[r.model].router if r.model in gpu_rows else 0.0,
                                        -sizes.get(r.model, 0)))
        cpu = best.model
        why.append(f"CPU: {cpu} ({'within' if fast else 'over'} {CPU_P95_MS:.0f} ms p95)")
    return gpu, cpu, "; ".join(why)


def main(models: Sequence[str] = (), *, devices: Sequence[str] = ("gpu", "cpu"), cpu_cases: int = 40,
         save: bool = False) -> int:
    from .say import _catalog_store

    store = _catalog_store()
    catalog = store.current()
    config = ConfigStore(paths.SETTINGS)
    config.load()
    client = Ollama(config.settings.ollama.url)
    try:
        installed = client.models()
    except LLMError as error:
        print(error)
        return 1
    models = tuple(models) or CANDIDATES
    missing = [m for m in models if m not in installed]
    if missing:
        print("Not pulled (skipped): " + ", ".join(missing) + "  — `ollama pull <name>` to include them")
    models = tuple(m for m in models if m in installed)
    cases = load_cases(catalog)
    few = _spread(cases, cpu_cases)
    t0_items = run(Router(), catalog, cases)  # Tier 0 alone, once
    t0_few = run(Router(), catalog, few)
    print(f"{len(cases)} cases ({sum(c.group == 'positive' for c in cases)} positives); "
          f"Tier 0 alone answers {sum(i.tier == '0' for i in t0_items)} of them.\n", flush=True)
    governor = Governor(client, lambda: config.settings.llm)
    tier1 = Tier1(client, governor, lambda: config.settings.llm, store.current)
    router = Router(tier1)
    rows: list[Row] = []
    for model in models:
        for device in devices:
            if device == "arc" or (device == "cpu" and model == BASELINE):
                continue
            label = f"{model} on the {device.upper()}"
            print(f"== {label}", flush=True)
            try:
                if device == "gpu":
                    rows.append(_bench(tier1, client, router, catalog, model, device, cases, t0_items))
                else:
                    rows.append(_bench(tier1, client, router, catalog, model, device, few, t0_few))
            except LLMError as error:
                print(f"   failed: {error}")
            if rows:
                r = rows[-1]
                print(f"   Tier 1 {r.accuracy:.1%} · behind Tier 0 {r.router:.1%} · false {r.false_rate:.1%} · "
                      f"p50 {r.p50:.0f} / p95 {r.p95:.0f} ms · cold {r.cold_ms:.0f} ms · {r.mem_gb} GB", flush=True)
    if "arc" in devices:
        try:
            with ArcServer() as arc:
                print(f"== Arc server up: {arc.device or 'device unknown — see logs/bench-arc-ollama.log'}", flush=True)
                arc_client = Ollama(ARC_URL)
                arc_tier1 = Tier1(arc_client, Governor(arc_client, lambda: config.settings.llm),
                                  lambda: config.settings.llm, store.current)
                arc_router = Router(arc_tier1)
                for model in models:
                    if model == BASELINE:
                        continue
                    print(f"== {model} on the Arc", flush=True)
                    try:
                        rows.append(_bench(arc_tier1, arc_client, arc_router, catalog, model, "arc", few, t0_few,
                                           note="(Vulkan)"))
                        r = rows[-1]
                        print(f"   p50 {r.p50:.0f} / p95 {r.p95:.0f} ms · cold {r.cold_ms:.0f} ms · {r.mem_gb} GB",
                              flush=True)
                    except LLMError as error:
                        print(f"   failed: {error}")
        except RuntimeError as error:
            print(f"Arc: {error}")
    print()
    print(table(rows))
    gpu, cpu, why = recommend(rows, installed)
    print(f"\nRecommended: gpu_model {gpu}, cpu_model {cpu}. {why}")
    (paths.LOGS / "bench-llm.json").write_text(
        json.dumps({"rows": [asdict(r) for r in rows], "gpu_model": gpu, "cpu_model": cpu, "why": why}, indent=1),
        encoding="utf-8")
    if save and gpu and cpu:
        try:
            config.save_value("llm.gpu_model", gpu)
            config.save_value("llm.cpu_model", cpu)
            print("Saved to settings.yaml.")
        except ConfigError as error:
            print(f"Couldn't save: {error}")
            return 1
    return 0
