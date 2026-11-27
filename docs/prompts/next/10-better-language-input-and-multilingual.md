# Prompt 10: Better language input and multilingual

> Builds on: stage 02 second-opinion decoding (reuse it for Hindi routing), stage 03 phonetic aliases (add Devanagari keys), stage 07 dataset (multilingual source), stage 02b budgets (routed utterances get their own span).


```
Goal: understand looser phrasing and more languages, all locally.

Facts: Parakeet v3 covers 25 European languages with automatic language detection but no Hindi/Asian/Arabic. Free Hindi options: AI4Bharat IndicConformer 600M (MIT, gated download) and Qwen3-ASR 0.6B/1.7B (Apache-2.0; ONNX export is a community build). Planners mostly fail on non-English commands by copying foreign words into English-valued slots; keeping slot values in English helps.

Do:
1. Looser English first: Tier 0 grows from data, not new regexes. Move phrase patterns into config/data where the current code allows, and let learned skills (Prompt 9) become Tier 0 entries automatically. Fillers ("can you", "go ahead and", "uh") come from a per-language data file, applied before matching without dropping content words.
2. Language detection: log Parakeet's detected language per utterance; add text language ID (fastText lid or lingua, both local) as a second signal.
3. Plan contract: schema keys and slot values (app ids, verbs, setting names) stay English; free text (search query, dictation, message text, contact names) stays in the spoken language. Enforce in validate.py.
4. Per-language data packs (config/lang/es.yaml, fr.yaml, de.yaml, hi.yaml): verb synonyms, fillers, and "scratch that"/"new paragraph" equivalents, as data read by the same generic code that handles English. Adding a language means adding a file, never code. Seed from native speakers; extend from confirmed use. Commands outside the pack go to nova-brain, which handles them through the multilingual training data.
5. App names in mixed sentences ("Chrome खोलो", "abre Spotify"): aliases stored in Latin and Devanagari plus phoneme keys; the wake word "Nova" accepted in any script.
6. Hindi: add IndicConformer or Qwen3-ASR as a second recognizer used when Parakeet's confidence is low or the app match fails; measure both on my recordings and pick one.
7. Hinglish: decode with both recognizers in parallel when routed, reconcile against the app catalog (the key noun is usually English).
8. Eval: hand-written (not translated) command sets per language and for Hinglish, 50-100 rows each; measure intent accuracy, slot match, WER, misroute rate and added latency.
9. Feed the native rows into Prompt 7's multilingual source for nova-brain.

Done when: per-language intent accuracy and Hindi WER are reported, English accuracy does not regress, and added latency only applies to routed utterances.
```
