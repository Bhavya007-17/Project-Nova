# Prompt 3: Names and fuzzy matching


```
Goal: "Claire" -> Claude, "foam" -> Chrome, "clothes the stab" -> close the tab, without phrase lists and without acting on the wrong thing.

Do:
1. Phonetic keys: add a G2P step (CMUdict lookup + espeak-ng/phonemizer fallback, both free and local) for every name in config/aliases.yaml, the Start-menu catalog and open window titles. Store phoneme strings.
2. Matcher: for a candidate span in the transcript, compute weighted phoneme edit distance to a SHORT context-ranked list: foreground app, running processes, recently used apps/sites, aliases. Do not match against all 180 apps at once (research and our own sherpa test show big lists cause false accepts).
3. Decide with two thresholds tuned on the speech set: one clear match + reversible action -> act; two close candidates or a risky action -> ask in the island with numbered choices (existing UI); nothing close -> pass to Tier 1 unchanged. Keep always-listening strict, as GOTCHAS says.
4. Learned aliases: when I confirm a choice ("Claire" -> Claude), append it to config/learned_aliases.yaml in a plain `spoken: written` format (Talon-style), with count and last-used date. Never learn from a single unconfirmed guess. Provide `nova aliases list/forget`.
5. Fuzz tests from Prompt 1 must pass: wrong-app actions = 0.

Done when: command-resolution accuracy on the speech set rises, ask rate is reported, wrong-app actions stay at 0.
```
