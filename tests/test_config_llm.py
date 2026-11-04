"""settings.yaml `llm:` section (Stage 05): models, device, keep-alive, VRAM headroom, simulated low VRAM."""

import pytest

from nova.config import DEFAULT_YAML, ConfigError, Settings, load_text, with_value


def test_llm_defaults():
    llm = Settings().llm
    assert llm.device == "auto" and llm.keep_alive == "10m" and llm.headroom_gb == 1.0
    assert llm.gpu_model and llm.cpu_model
    assert llm.simulate_free_vram_gb is None


def test_default_yaml_has_the_llm_section():
    assert load_text(DEFAULT_YAML).llm == Settings().llm
    assert "simulate_free_vram_gb" in DEFAULT_YAML  # shown, commented out


def test_llm_values_parse():
    s = load_text(
        "llm:\n  gpu_model: qwen3.5:4b\n  cpu_model: qwen3.5:2b-q4_K_M\n  device: cpu\n  keep_alive: 30m\n"
        "  headroom_gb: 2\n  timeout_s: 6\n  simulate_free_vram_gb: 0.5\n"
    )
    assert s.llm.gpu_model == "qwen3.5:4b" and s.llm.device == "cpu" and s.llm.keep_alive == "30m"
    assert s.llm.headroom_gb == 2.0 and s.llm.timeout_s == 6.0 and s.llm.simulate_free_vram_gb == 0.5


def test_simulated_vram_can_be_cleared_with_null():
    assert load_text("llm:\n  simulate_free_vram_gb: null\n").llm.simulate_free_vram_gb is None
    assert load_text("llm:\n  simulate_free_vram_gb: 0\n").llm.simulate_free_vram_gb == 0.0


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("llm:\n  device: arc\n", r"llm\.device must be auto, gpu or cpu"),
        ("llm:\n  gpu_model: gemma4:31b-cloud\n", r"llm\.gpu_model.*cloud"),
        ("llm:\n  cpu_model: qwen3.5:cloud\n", r"llm\.cpu_model.*cloud"),
        ("llm:\n  gpu_model: ''\n", r"llm\.gpu_model must not be empty"),
        ("llm:\n  keep_alive: ten minutes\n", r"llm\.keep_alive"),
        ("llm:\n  headroom_gb: -1\n", r"llm\.headroom_gb"),
        ("llm:\n  timeout_s: 0.1\n", r"llm\.timeout_s"),
        ("llm:\n  simulate_free_vram_gb: lots\n", r"llm\.simulate_free_vram_gb must be a number"),
        ("llm:\n  simulate_free_vram_gb: -2\n", r"llm\.simulate_free_vram_gb"),
    ],
)
def test_bad_llm_values(text, message):
    with pytest.raises(ConfigError, match=message):
        load_text(text)


@pytest.mark.parametrize("keep", ["10m", "30s", "1h", "0", "-1", "90m", "1h30m"])
def test_keep_alive_durations(keep):
    assert load_text(f"llm:\n  keep_alive: '{keep}'\n").llm.keep_alive == keep


def test_bench_can_save_the_models_in_place():
    text = with_value(DEFAULT_YAML, "llm.gpu_model", "qwen3.5:4b")
    assert load_text(text).llm.gpu_model == "qwen3.5:4b"
    assert "# Local LLM" in text  # comments kept
