# Prompt 4: Dictation misheard-word fix (#18)


```
Goal: fix "meat" -> "meet" after a sentence lands, without breaking correct text and without limiting dictation length.

Research to apply: free-form LLM correction often damages correct transcripts (a 2026 preprint measured 12.8-31.9% of correct drafts broken), while giving the corrector phonetic information and restricting it to choosing among candidates helps.

Builds on earlier stages:
- Stage 02: per-word recognizer confidence decides which words are flagged.
- Stage 03: reuse its G2P, phoneme distance and learned_aliases.yaml for candidates. Do not write a second phonetic matcher.
- Stage 02b: add a `dictation_fix` timing span and a budget in config/latency_budgets.yaml. The fix runs after the text lands and never delays landing.

Do:
1. Diagnose first: run nova/dictation/cleanup.py on 50+ recorded/synthesized sentences with known errors; log why each attempt fails (guard rejection, thinking tokens, schema, timeout, wrong candidate). Write the findings in the issue.
2. Redesign as a picker, not a rewriter:
   - Trigger only on words with low recognizer confidence (Prompt 2) or words not in the dictionary/context.
   - For each flagged word build 2-5 candidates: homophones/near-phonemes (G2P), names from the active window (UIA text), my learned aliases.
   - Prompt: the sentence with the flagged word marked, its phoneme form, the candidate list. Output schema: {"pick": <index or "keep">}. Thinking off, temperature 0.
   - Accept only if the pick is phonetically close; else keep the original.
   - Run any number of sentences; each correction is local to its word, so length is unlimited.
3. Swap in place using the existing Target.erase landing check (GOTCHAS). Google Docs (canvas) stays uncorrected.
4. Optional later: train `nova-fix` (Prompt 8) on synthetic (noisy, clean) pairs.

Done when: on the dictation set, repairs > breaks by a clear margin, break rate reported, p95 time-to-fix under ~1 s on GPU, and live attempts land (issue #18 closes with numbers).
```
