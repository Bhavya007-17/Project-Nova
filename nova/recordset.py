"""`nova record-set`: 30 short commands built from his own names, read aloud once (SPEC §4, Stage 07a).

The set measures what synthetic voices can't: how speech-to-text hears *his* app names and the wake name.
Lines come from ``config/aliases.yaml`` (every alias at least once), ``config/vocab.yaml`` and the installed
apps (Start-menu catalog), with varied verbs; half start with the wake name. The same config always gives the
same lines (seeded). Each take is saved as ``fixtures/command-set/NN.wav`` (16 kHz mono, 16-bit) with
``manifest.json`` next to it; ``nova bench-stt --set fixtures/command-set`` scores them.

Nothing is transcribed while recording, so nothing he says lands in ``logs/``. The folder is git-ignored.
"""

from __future__ import annotations

import json
import random
import re
import threading
import time
import wave
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import paths
from .actions.catalog import key_of, short_name, words_of

SEED = 7  # fixed: the same config gives the same lines
COUNT = 30
SECONDS = 4.0  # each take; commands are ~1-3 s
PREROLL_S = 0.5  # audio kept from just before "speak now", so an early start isn't clipped
SAMPLE_RATE = 16_000
COMMAND_SET = paths.ROOT / "fixtures" / "command-set"
MANIFEST = "manifest.json"
NEXT_STEP = "uv run nova bench-stt --set fixtures/command-set --backend both"

_APP_VERBS = ("open {}", "switch to {}", "close {}")
_SITE_VERBS = ("go to {}", "open {}")
_TERM_VERBS = ("search for {}", "search {site} for {}")
_VERBS = {"app": _APP_VERBS, "site": _SITE_VERBS, "term": _TERM_VERBS}
_VOWELS = frozenset("aeiouy")
_WAV_NAME = re.compile(r"\d{2}\.wav")
_QUIET = 0.01  # peak below -40 dBFS
_CLIPPED = 0.99


@dataclass(frozen=True)
class Line:
    id: str  # "01"
    text: str  # what he reads (and the WER reference)
    entities: tuple[str, ...]  # the names in it, spelled as in ``text``
    has_wake: bool


@dataclass(frozen=True)
class _Name:
    text: str  # as shown: "Chrome", "VS Code", "MuJoCo"
    kind: str  # app | site | term


# --- the lines (pure) ------------------------------------------------------------------------------------------


def display_name(phrase: str, known: Mapping[str, str]) -> str:
    """An alias as he'd write it: a known spelling (vocab term, app or short app name), else Title Case
    with vowel-less words in capitals ("vs code" -> "VS Code")."""
    found = known.get(key_of(phrase))
    if found:
        return found
    return " ".join(w.upper() if not _VOWELS & set(w.lower()) else w.capitalize() for w in phrase.split())


def _names(aliases: Mapping[str, str], terms: Sequence[str],
           apps: Sequence[str]) -> tuple[list[_Name], list[_Name], list[_Name]]:
    """(names the set must say: every alias, then every vocab term; the full names of the apps his aliases point
    at; other installed apps that may fill it up)."""
    known: dict[str, str] = {}
    for name in [*apps, *(short_name(a) for a in apps), *terms]:
        known.setdefault(key_of(name), name)
    app_keys = {key_of(a) for a in apps} | {key_of(short_name(a)) for a in apps}
    groups: dict[str, list[tuple[str, str]]] = {}
    for phrase, target in aliases.items():  # "vs code" and "vscode" sound the same: one line covers both
        groups.setdefault(key_of(phrase), []).append((phrase, target))
    required: list[_Name] = []
    for group in groups.values():
        phrase, target = max(group, key=lambda pt: len(pt[0].split()))
        kind = "site" if target.startswith(("http://", "https://")) else "app"
        required.append(_Name(display_name(phrase, known), kind))
    said = set(groups)
    for term in terms:
        key = key_of(term)
        if key and key not in said:
            said.add(key)
            required.append(_Name(term, "app" if key in app_keys else "term"))
    targets = {target.casefold() for target in aliases.values()}
    preferred: list[_Name] = []
    for target in aliases.values():  # "Google Chrome", "Visual Studio Code": his apps, by their full name
        key = key_of(target)
        if not target.startswith(("http://", "https://")) and key and key not in said:
            said.add(key)
            preferred.append(_Name(target, "app"))
    extras: list[_Name] = []
    for app in sorted(set(apps)):
        words = words_of(app)
        if not 1 <= len(words) <= 3 or not all(w.isalpha() for w in words) or len(app) > 24:
            continue  # "Python 3.12 (64-bit)", long names: not how anyone says them
        if "uninstall" in app.casefold() or app.casefold() in targets:
            continue
        if key_of(app) in said or key_of(short_name(app)) in said:
            continue
        said.add(key_of(app))
        extras.append(_Name(app, "app"))
    return required, preferred, extras


def build_set(aliases: Mapping[str, str], terms: Sequence[str], apps: Sequence[str], *, count: int = COUNT,
              seed: int = SEED, wake: str = "Nova") -> tuple[Line, ...]:
    """``count`` distinct lines: every alias, then vocab terms, then his aliased apps' full names, then other
    installed apps, then the same names with other verbs."""
    rng = random.Random(seed)
    required, preferred, extras = _names(aliases, terms, apps)
    items = (required + preferred)[:count]
    room = count - len(items)
    items += rng.sample(extras, min(room, len(extras)))
    sites = [n for n in required if n.kind == "site"]
    pool = [n for n in required if n.kind == "app"] + [n for n in required if n.kind != "app"]
    bodies: list[tuple[str, tuple[str, ...]]] = []
    used: set[str] = set()
    turn = {kind: rng.randrange(len(verbs)) for kind, verbs in _VERBS.items()}

    def phrase(name: _Name) -> tuple[str, tuple[str, ...]] | None:
        verbs = _VERBS[name.kind]
        for k in range(len(verbs)):
            verb = verbs[(turn[name.kind] + k) % len(verbs)]
            if "{site}" in verb:
                if not sites:
                    continue
                site = sites[rng.randrange(len(sites))].text
                body, entities = verb.format(name.text, site=site), (site, name.text)
            else:
                body, entities = verb.format(name.text), (name.text,)
            if body.casefold() not in used:
                turn[name.kind] += 1
                return body, entities
        return None

    rng.shuffle(items)
    for name in items:
        made = phrase(name)
        if made is not None:
            used.add(made[0].casefold())
            bodies.append(made)
    repeats = rng.sample(pool, len(pool)) if pool else []
    misses = 0
    while len(bodies) < count and repeats and misses < len(repeats):
        made = phrase(repeats[len(bodies) % len(repeats)])
        if made is None:
            misses += 1
            repeats = repeats[1:] + repeats[:1]
            continue
        used.add(made[0].casefold())
        bodies.append(made)
    rng.shuffle(bodies)
    woke = set(rng.sample(range(len(bodies)), len(bodies) // 2))
    lines = []
    for i, (body, entities) in enumerate(bodies):
        text = f"{wake}, {body}" if i in woke else body[:1].upper() + body[1:]
        lines.append(Line(f"{i + 1:02d}", text, entities, i in woke))
    return tuple(lines)


# --- manifest ----------------------------------------------------------------------------------------------------


def manifest(lines: Sequence[Line], recorded: Mapping[str, str]) -> dict:
    return {
        "version": 1,
        "sample_rate": SAMPLE_RATE,
        "commands": [
            {"id": line.id, "wav": recorded.get(line.id), "expected_text": line.text,
             "entities": list(line.entities), "has_wake": line.has_wake}
            for line in lines
        ],
    }


def read_manifest(path: Path) -> tuple[tuple[Line, ...], dict[str, str]]:
    """(lines, {id: wav name} for the recorded ones). Anything malformed is left out; never raises."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return (), {}
    commands = data.get("commands") if isinstance(data, dict) else None
    lines: list[Line] = []
    recorded: dict[str, str] = {}
    for item in commands if isinstance(commands, list) else []:
        if not isinstance(item, dict):
            continue
        cid, text, entities = item.get("id"), item.get("expected_text"), item.get("entities")
        if not (isinstance(cid, str) and isinstance(text, str) and isinstance(entities, list)):
            continue
        if not all(isinstance(e, str) for e in entities):
            continue
        lines.append(Line(cid, text, tuple(entities), bool(item.get("has_wake"))))
        wav = item.get("wav")
        if isinstance(wav, str) and _WAV_NAME.fullmatch(wav):
            recorded[cid] = wav
    return tuple(lines), recorded


def _write_json(path: Path, data: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def save_wav(path: Path, pcm: np.ndarray) -> None:
    """float32 [-1, 1] -> 16 kHz mono 16-bit PCM (what ``audio.read_wav`` reads)."""
    samples = np.round(np.clip(np.asarray(pcm, dtype=np.float32), -1.0, 1.0) * 32767).astype("<i2")
    temporary = path.with_name(path.name + ".tmp")
    with wave.open(str(temporary), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(samples.tobytes())
    temporary.replace(path)


# --- recording ---------------------------------------------------------------------------------------------------

_KEYS = {"\r": "keep", "\n": "keep", "r": "again", "s": "skip", "q": "quit"}


def key_action(key: str) -> str | None:
    return _KEYS.get(key.lower()) if key else None


def record(lines: Sequence[Line], folder: Path, capture: Callable[[float], np.ndarray], read_key: Callable[[], str],
           *, seconds: float = SECONDS, tick: Callable[[float], None] = time.sleep,
           out: Callable[..., None] = print) -> dict[str, str]:
    """Show each line, count down, record ``seconds``, then Enter keep / R again / S skip / Q quit.

    The manifest is rewritten after every kept take, so quitting (or Ctrl+C) keeps what's there; a second run
    of the same set records only what's missing. Returns {id: wav name} of what's recorded.
    """
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    lines = tuple(lines)
    old_lines, old = read_manifest(folder / MANIFEST)
    recorded = {i: w for i, w in old.items() if (folder / w).is_file()} if old_lines == lines else {}
    _write_json(folder / MANIFEST, manifest(lines, recorded))
    left = sum(line.id not in recorded for line in lines)
    if recorded:
        out(f"{len(recorded)} of {len(lines)} already recorded in {folder}; recording the other {left}.")
    out(f"Read each line the way you'd say it to Nova, from where you usually sit, when it says \"speak now\". "
        f"Each take is {seconds:.0f} s.")
    out("After each take: Enter = keep · R = record it again · S = skip · Q = quit (keeps what's recorded)")
    for n, line in enumerate(lines, 1):
        if line.id in recorded:
            continue
        while True:
            out(f"\n[{n}/{len(lines)}]   {line.text}")
            for k in (3, 2, 1):
                out(f"  {k}…", end="", flush=True)
                tick(0.4)
            out("  speak now")
            pcm = capture(seconds)
            peak = float(np.max(np.abs(pcm))) if len(pcm) else 0.0
            if peak < _QUIET:
                out("  (very quiet: R to record it again?)")
            elif peak >= _CLIPPED:
                out("  (clipped: R to record it again a little further from the mic?)")
            out("  Enter keep · R again · S skip · Q quit")
            action = None
            while action is None:
                action = key_action(read_key())
            if action == "again":
                continue
            if action == "keep":
                name = f"{line.id}.wav"
                save_wav(folder / name, pcm)
                recorded = {**recorded, line.id: name}
                _write_json(folder / MANIFEST, manifest(lines, recorded))
            break
        if action == "quit":
            break
    out(f"\n{len(recorded)} of {len(lines)} recorded in {folder}.")
    if len(recorded) < len(lines):
        out("Run `uv run nova record-set` again to record the rest.")
    out(f"Then: {NEXT_STEP}")
    return recorded


class MicCapture:
    """The same capture path as Nova (``audio.Mic``: the settings' device, 16 kHz float32 blocks) into a ring.

    Opened once for the whole set; each call returns ``PREROLL_S`` from before the call plus ``seconds`` after.
    """

    def __init__(self, device: str = "default", seconds: float = SECONDS) -> None:
        from .audio import Mic, Ring

        wake = threading.Event()
        self._ring = Ring(max(1 << 17, int((seconds + PREROLL_S + 3) * SAMPLE_RATE)), wake)
        self._mic = Mic(self._ring, wake, device)

    @property
    def name(self) -> str:
        return self._mic.name

    def open(self) -> None:
        self._mic.open()

    def close(self) -> None:
        self._mic.close()

    def __call__(self, seconds: float) -> np.ndarray:
        from .audio import MicError

        go = self._ring.end
        start = max(self._ring.oldest, go - int(PREROLL_S * SAMPLE_RATE))
        end = go + int(seconds * SAMPLE_RATE)
        deadline = time.perf_counter() + seconds + 2.0
        while self._ring.end < end:
            if time.perf_counter() > deadline:
                raise MicError("the mic stopped sending audio")
            time.sleep(0.05)
        pcm = self._ring.read(start, end)
        if pcm is None:
            raise MicError("lost audio from the ring")
        return pcm


def _console_key() -> str:
    import msvcrt

    while msvcrt.kbhit():  # keys pressed while recording don't count
        msvcrt.getwch()
    key = msvcrt.getwch()
    if key in ("\x00", "\xe0"):  # arrows / function keys come as two characters
        msvcrt.getwch()
        return ""
    if key == "\x03":
        raise KeyboardInterrupt
    return key


def current_set(count: int = COUNT, out: Callable[..., None] = print) -> tuple[Line, ...]:
    """The lines for this laptop's aliases.yaml, vocab.yaml, app list and wake name."""
    from .actions.catalog import CatalogStore
    from .config import ConfigStore
    from .vocab import VocabFile

    store = CatalogStore(paths.CATALOG_CACHE, paths.ALIASES)
    store.load_cached()
    if not store.loaded.is_set():
        out("Reading the Start menu (first run, ~1 s)…")
        try:
            store.refresh()
        except Exception as error:  # noqa: BLE001 - the set still works from aliases + vocab
            out(f"  couldn't read the app list ({error}); using aliases and vocab only")
    from .actions.catalog import parse_aliases

    aliases = parse_aliases(paths.ALIASES.read_text(encoding="utf-8-sig"))
    terms = VocabFile(paths.VOCAB).current().terms
    wake = ConfigStore(paths.SETTINGS).load().wake_name
    return build_set(aliases, terms, [app.name for app in store.current().apps], count=count, wake=wake)


def main(*, list_only: bool = False, seconds: float = SECONDS, device: str | None = None) -> int:
    from .audio import MicError
    from .config import ConfigStore

    try:
        lines = current_set()
    except (OSError, ValueError) as error:
        print(f"Can't build the set: {error}")
        return 1
    if list_only:
        for line in lines:
            print(f"{line.id}  {line.text}")
        return 0
    capture = MicCapture(device or ConfigStore(paths.SETTINGS).load().audio.device, seconds)
    try:
        capture.open()
    except MicError as error:
        print(f"Can't open the mic: {error}")
        return 1
    print(f"Mic: {capture.name} · {len(lines)} lines, about {len(lines) * (seconds + 2.5) / 60:.0f} minutes")
    try:
        record(lines, COMMAND_SET, capture, _console_key, seconds=seconds)
    except KeyboardInterrupt:
        print("\nStopped. What you kept is saved; run it again to finish.")
        return 1
    except MicError as error:
        print(f"\nThe mic stopped ({error}). What you kept is saved; run it again to finish.")
        return 1
    finally:
        capture.close()
    return 0
