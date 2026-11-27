# Prompt 7: Training module A: datasets (no APIs)

> Builds on: stage 03 G2P table (sound-alike noise in noise.py), stage 05 schema (every row must use it), stage 06 discovered app maps (template slots), stage 01 dev/test splits (leak test).


```
Goal: build nova/training/, a reproducible pipeline that produces clean, verified training data for nova-brain and nova-fix with zero API use.

Principles from the research: verified data beats unverified (ToolACE); explicit "no tool applies" examples matter (Hammer used ~7,500); ~1,100 sentences let an 8B model beat a full-data classifier on intent (COLING 2025); TinyAgent's 1.1B went 12.7% -> 78.9% on synthetic data, but that data came from an API, which we replace with the sources below.

Layout:
  nova/training/
    sources/       # generators, one per source
    build.py       # assemble -> validate -> dedupe -> split -> stats
    validate.py    # schema check + executor dry run
    noise.py       # ASR-noise augmentation
    data/          # git-ignored outputs (jsonl)
    DATASET.md     # card: sources, counts, licenses, known gaps

Sources (each tagged with its source in every row):
1. templates: slot templates x the catalog Nova discovers at build time (installed apps, sites from history/aliases, known folders, discovered app maps and Settings pages), never a typed-in app list, so the data follows whatever machine it is built on. Many phrasings per intent ("open/launch/start/bring up/pull up X", "X on the left and Y on the right"). Labels correct by construction.
2. eval-adjacent: paraphrases of train-split rows only (never dev/test).
3. local paraphrase: run the largest open model that fits (e.g. a 7-14B instruct model through Ollama with CPU offload, or on a free Kaggle GPU) to paraphrase templates in casual, rambling, indirect, polite and terse styles. Keep a row only if validate.py accepts the plan AND the label matches the template's.
4. logs: scrubbed rows from logs/traces.jsonl where the outcome was confirmed or verified by postcondition; drop anything with names of people, emails or text I dictated. Opt-in flag, off by default.
5. negatives: 10-25% of rows: small talk, acks, thinking aloud, questions to other people, TV speech, partial sentences, "hold on", "never mind", label = ignore.
6. asks: ambiguous commands with a correct clarification (two apps match, missing slot), label = ask.
7. multi-clause and session rows: "open Spotify and play my liked songs", follow-ups that refer to "it/that".
8. multilingual (Prompt 10): native-written rows for es/fr/de/hi and Hinglish; slot values stay English, free text stays in the source language.
9. noise.py: copy 20-30% of rows with ASR-style errors (sound-alike name swaps from the G2P table, dropped words, merged words, "Yeah." prefixes, missing punctuation). The label stays the clean plan, so the model learns to read noisy transcripts.
10. corrector pairs for nova-fix: generate sentences locally, synthesize with SAPI/piper TTS, decode with Parakeet, align to get real (heard, meant) word pairs plus candidates.

Format: chat jsonl in the exact chat template Nova uses at inference (ChatML with empty <think></think>), system prompt = the SHORT version planned for nova-brain, assistant = the JSON plan. Keep a "prompt_version" field.

Checks: every row passes the JSON schema and the executor dry run (fake hands); dedupe on normalized text against dev/test; per-intent and per-language counts in DATASET.md; at least 30 rows per intent; license column for any public dataset used (MASSIVE, Common Voice, FLEURS: check each license before use).

Pilot target: 1,000-3,000 verified rows (my judgment, not a sourced number). Measure with the pilot before generating more.

Done when: python -m nova.training.build produces train/dev jsonl, DATASET.md has counts and licenses, validate.py rejects malformed rows in tests, and no dev/test row leaks (test enforces it).
```
