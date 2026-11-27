# Prompt 8: Training module B: train nova-brain and adapt Ollama

> Builds on: stage 07 dataset; stage 02b latency replay (report cold/warm p50/p95 for nova-brain vs stock); stage 01 nova eval --compare.


```
Goal: fine-tune nova-brain (and optionally nova-fix), export to GGUF, serve through Ollama/llama-server, and prove it beats the prompt-only baseline.

Facts: Ollama only imports and serves; it does not train. Training happens in Unsloth (Windows/Linux, CUDA) or MLX (Mac). Unsloth lists bf16 LoRA at about 5 GB for Qwen3.5-2B and 10 GB for 4B and advises against QLoRA on Qwen3.5; use the same chat template in training and inference. GGUF export from MLX works from non-quantized base models only.

Do:
1. nova/training/train.py with three backends behind one config (config/training.yaml):
   - local-cuda: Unsloth LoRA on the RTX 5070 Laptop for Qwen3.5-2B (close the tray app first; Nova's llama-server holds ~3 GB VRAM).
   - kaggle / colab: a notebook in nova/training/notebooks/ for Qwen3.5-4B on a free 16 GB GPU. Upload only the built dataset (synthetic/scrubbed), never raw logs or audio. Use the notebook's precision defaults (T4 has no bf16).
   - mac-mlx: mlx-lm / mlx-tune LoRA on Apple Silicon, from a non-quantized base so GGUF export works.
   Hyperparameters start: LoRA rank 16, alpha 16-32, lr 1e-4 to 2e-4, 1-3 epochs, completion-only loss, max length 1024-2048. Log loss and dev-set accuracy each epoch.
2. Export: merge LoRA -> 16-bit -> llama.cpp convert_hf_to_gguf.py -> llama-quantize Q4_K_M (also Q5_K_M and Q6_K for the sweep).
3. Ollama adaptation: create a Modelfile per model:
     FROM ./nova-brain-4b-Q4_K_M.gguf
     TEMPLATE <the exact ChatML template Nova renders, with empty think block>
     PARAMETER temperature 0
     PARAMETER num_ctx <one value shared by every caller>
     PARAMETER presence_penalty 0
     PARAMETER repeat_penalty 1
   ollama create nova-brain:4b -f Modelfile. Make llm.model in settings.yaml point at it; llama_server.py must find the GGUF the same way it finds the stock one (first absolute FROM in ollama show --modelfile, per GOTCHAS). Test the ADAPTER route only as an experiment; ship the merged GGUF.
4. Shrink the prompt: nova-brain gets a short system prompt (rules + schema, no long few-shot list). Measure cold start, warm p50/p95, and prompt tokens vs the 4,300-token baseline.
5. Evaluate the QUANTIZED file with nova eval --compare against main on intents, negatives, sessions and tasks; also run a general-chat regression (talk sessions) to catch forgetting. Keep schema-forced decoding on.
6. Keep the stock model one config switch away (llm.model) for instant rollback.
7. Later, optional: preference or RL step using logs (prefer the plan I confirmed over the one I corrected; reward = schema-valid + dry-run success). Only after SFT shows a gain.

Done when: nova-brain beats or matches the baseline on intents with fewer false actions on negatives (significance reported), latency is reported for cold/warm, rollback works, and docs/TRAINING.md explains how to retrain from scratch on each backend.
```
