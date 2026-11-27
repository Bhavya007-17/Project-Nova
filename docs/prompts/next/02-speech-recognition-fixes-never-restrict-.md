# Prompt 2: Speech recognition fixes (never restrict speech)


```
Goal: fewer wrong transcripts without limiting what I say.

Facts to respect (GOTCHAS): Parakeet int8 sometimes returns "" for clear speech (0.3 s padded retry recovers ~70%); it invents "Yeah." on tiny clips (12.6% of a week's finals); sherpa hotwords with 180 names raised WER 14.4% -> 19.0%. Research note: in a Whisper study, silence padding itself caused hallucinations, so our padding may be creating some "Yeah." outputs.

Do:
1. Expose recognizer confidence. Check whether onnx-asr returns token log-probs/timestamps for Parakeet TDT; if so, compute per-word confidence and utterance mean/min. If not, find the cheapest way (patch, or decode-time access). Log it in traces.
2. Spurious-word gate WITHOUT length or word rules: decide "is this real speech addressed to Nova" from confidence + energy (p80 + 15 dB rule already exists) + whether speech began after the question/window opened (existing rule). A confident one-word "yes" must still pass. Measure bare-ack rate and false-reject rate on real one-word answers.
3. Padding check: A/B the 0.3 s padded retry vs. no-pad retry vs. re-decode with the second model (step 5) and measure how many "Yeah." finals each produces.
4. Unlimited speech: keep the 10 s cap ONLY for unaddressed room chatter (already lifted by key or name). For long addressed speech and dictation, segment at VAD pauses and decode sentence by sentence with the previous sentence available as context; there must be no total-length cap. Add a test with a 3-minute SAPI dictation WAV.
5. Second opinion only when unsure: if utterance confidence is low (threshold tuned on the speech set), re-decode the same audio with a second free local model (try Whisper large-v3-turbo via faster-whisper int8 on CPU, and Qwen3-ASR 0.6B if its ONNX export works), then pick the hypothesis that matches the app catalog / aliases, else the higher-confidence one. Must run on CPU and never block the confident path.
6. Keep sherpa hotwords off by default; biasing happens after recognition (Prompt 3).

Done when: bare-ack rate, empty-final rate and WER on the speech set improve vs baseline, real one-word answers are not rejected more often, the 3-minute dictation works, and p95 latency on the confident path does not rise.
```
