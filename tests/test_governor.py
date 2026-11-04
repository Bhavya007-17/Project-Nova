"""The resource governor (SPEC §15): GPU or CPU model from free VRAM, one Nova model at a time, unload on exit."""

from nova.config import Llm
from nova.governor import Choice, Governor, decide

GB = 1024**3
LLM = Llm(gpu_model="big:4b", cpu_model="small:2b", device="auto", headroom_gb=1.0)


def test_room_on_the_gpu_runs_the_gpu_model():
    assert decide(LLM, free_gb=6.0, need_gb=3.5) == Choice("big:4b", "gpu", "6.0 GB free ≥ 4.5 GB")


def test_tight_vram_runs_the_cpu_model():
    choice = decide(LLM, free_gb=3.0, need_gb=3.5)
    assert choice.model == "small:2b" and choice.device == "cpu" and choice.options == {"num_gpu": 0}
    assert "3.0 GB free" in choice.why


def test_no_nvidia_means_cpu():
    assert decide(LLM, free_gb=None, need_gb=3.5).device == "cpu"


def test_model_already_on_the_gpu_keeps_running_there():
    assert decide(LLM, free_gb=0.5, need_gb=3.5, resident=True).device == "gpu"


def test_device_setting_wins():
    assert decide(Llm(gpu_model="big:4b", cpu_model="small:2b", device="cpu"), free_gb=7.0, need_gb=1.0).device == "cpu"
    forced = decide(Llm(gpu_model="big:4b", cpu_model="small:2b", device="gpu"), free_gb=0.1, need_gb=3.5)
    assert forced.device == "gpu" and forced.model == "big:4b" and forced.options == {}


def test_simulated_low_vram():
    llm = Llm(gpu_model="big:4b", cpu_model="small:2b", simulate_free_vram_gb=0.5)
    assert decide(llm, free_gb=7.5, need_gb=3.5).device == "cpu"


class FakeClient:
    def __init__(self, loaded=(), sizes=None):
        self.unloaded = []
        self._loaded = list(loaded)
        self._sizes = sizes or {"big:4b": int(3.3 * GB), "small:2b": int(1.9 * GB)}

    def loaded(self, timeout=2.0):
        from nova.brain.llm import Loaded

        return tuple(Loaded(name, size, vram) for name, size, vram in self._loaded)

    def models(self, timeout=3.0):
        return dict(self._sizes)

    def unload(self, model, timeout=10.0):
        self.unloaded.append(model)
        self._loaded = [row for row in self._loaded if row[0] != model]


def governor(free=6.0, client=None, llm=LLM, clock=None):
    times = iter(clock or [0.0] * 100)
    return Governor(client or FakeClient(), lambda: llm, vram=lambda: free, clock=lambda: next(times))


def test_switching_models_unloads_the_last_one():
    client = FakeClient()
    free = [6.0]
    g = Governor(client, lambda: LLM, vram=lambda: free[0], clock=lambda: 0.0)
    assert g.choose().device == "gpu"
    free[0] = 0.5  # Fusion took the GPU
    g.forget()
    assert g.choose().device == "cpu"
    assert client.unloaded == ["big:4b"]


def test_simulated_low_vram_beats_a_resident_gpu_model():
    llm = Llm(gpu_model="big:4b", cpu_model="small:2b", simulate_free_vram_gb=0.5)
    assert decide(llm, free_gb=7.5, need_gb=3.5, resident=True).device == "cpu"


def test_need_comes_from_the_model_size_and_what_ollama_measured():
    client = FakeClient(loaded=[("big:4b", int(4.0 * GB), int(4.0 * GB))])
    g = governor(free=4.4, client=client)
    assert g.choose().why == "already on the GPU"
    assert g.need_gb("big:4b") == 4.0  # measured beats the estimate
    assert governor(client=FakeClient()).need_gb("big:4b") > 3.3  # file size + KV / buffers


def test_a_recent_choice_is_reused_without_asking_again():
    calls = []
    client = FakeClient()
    g = Governor(client, lambda: LLM, vram=lambda: calls.append(1) or 6.0, clock=iter([0.0, 10.0, 20.0]).__next__)
    first = g.choose()
    g.used()
    assert g.choose() == first and len(calls) == 1


def test_close_unloads_novas_model():
    client = FakeClient()
    g = governor(client=client)
    g.choose()
    g.close()
    assert client.unloaded == ["big:4b"]
