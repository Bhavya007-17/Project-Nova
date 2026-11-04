"""The app catalog (SPEC §8): Start-menu apps (AUMIDs + shortcut targets) and config/aliases.yaml.

- ``Catalog`` is immutable: what a spoken name means (``resolve``) and which app owns a
  window (``app_for``). Pure, unit-tested.
- ``CatalogStore`` keeps the current one: the cached list (data/catalog.json) at start,
  then a fresh read of the Start menu in the background now and every 10 minutes, and
  aliases.yaml re-read when it changes.

Resolution never guesses silently: a whole name or an alias is taken; a vendor-less name
("teams" = Microsoft Teams) or a near spelling is taken when nothing else competes;
partial names ("code") and sound-alikes of the names in aliases.yaml ("foam" -> Chrome,
"Claire" -> Claude: how speech-to-text mishears him) are only offered, for the island to
ask about. Strict resolution (always-listening) takes whole names and aliases only.
"""

from __future__ import annotations

import json
import logging
import ntpath
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import jellyfish
import yaml
from rapidfuzz import fuzz

_log = logging.getLogger(__name__)

REFRESH_S = 600.0
STRONG = 88.0  # a match at least this good (and clearly ahead) is acted on
_OFFER = 45.0  # weaker matches down to this are offered in a question
_MARGIN = 8.0
_VENDORS = frozenset({
    "microsoft", "google", "autodesk", "adobe", "apple", "hp", "intel", "nvidia", "amd", "dell", "lenovo", "mozilla",
    "oracle", "ansys", "mathworks", "windows", "zoom", "meta", "amazon",
})
_SMALL_WORDS = frozenset({"the", "a", "an", "for", "of", "and", "to", "in", "on", "my", "app"})
_EXPLORER_AUMID = "Microsoft.Windows.Explorer"
# Shortcuts that point at these are launchers (a folder in Explorer, a script, a control panel), not the program
# whose windows show up: their targets never decide which app a window belongs to.
_HOSTS = frozenset({
    "explorer.exe", "cmd.exe", "rundll32.exe", "mmc.exe", "msiexec.exe", "wscript.exe", "cscript.exe",
    "powershell.exe", "pwsh.exe", "conhost.exe", "control.exe", "chrome_proxy.exe", "msedge_proxy.exe",
    "javaw.exe", "java.exe", "python.exe", "pythonw.exe", "wsl.exe", "systemsettings.exe",
})
_UNINSTALL = re.compile(r"\buninstall|^unins\d*\.exe$|uninst", re.IGNORECASE)
_WORD = re.compile(r"[a-z0-9]+")


def words_of(text: str) -> tuple[str, ...]:
    """Lowercase alphanumeric words; apostrophes vanish ("Bhavya's" -> "bhavyas")."""
    return tuple(_WORD.findall(text.lower().replace("'", "").replace("’", "")))


def key_of(text: str) -> str:
    return "".join(words_of(text))


def short_name(name: str) -> str:
    """ "Google Chrome" -> "Chrome", "Autodesk Fusion" -> "Fusion" (for the island)."""
    parts = name.split()
    return " ".join(parts[1:]) if len(parts) >= 2 and parts[0].lower() in _VENDORS else name


@dataclass(frozen=True)
class App:
    name: str  # as the Start menu shows it: "Google Chrome"
    aumid: str  # AppUserModelID: "Chrome", "Claude_pzs8sxrjxfjjc!Claude"
    exe: str = ""  # the shortcut's target file (normalized case), "" for packaged apps
    args: str = ""


@dataclass(frozen=True)
class Target:
    """What a spoken name means: an installed app or a web address."""

    kind: str  # app | site
    name: str  # "Google Chrome" / "Canvas"
    app: App | None = None
    url: str = ""


@dataclass(frozen=True)
class Match:
    target: Target
    score: float  # 0-100
    how: str  # alias | exact | short | spelling | words | sound


@dataclass(frozen=True)
class Resolution:
    matches: tuple[Match, ...] = ()  # best first

    @property
    def best(self) -> Match | None:
        """The match to act on, or None: nothing strong enough, or a close second (ask instead)."""
        if not self.matches or self.matches[0].score < STRONG:
            return None
        top = self.matches[0]
        if len(self.matches) > 1 and self.matches[1].score > top.score - _MARGIN:
            return None
        return top


def _norm_path(path: str) -> str:
    if not path or not ntpath.isabs(path):
        return ""
    return ntpath.normcase(ntpath.normpath(path))


def apps_from_rows(rows: Iterable[Mapping[str, Any]]) -> tuple[App, ...]:
    """Start-menu rows ({Name, AppID, Exe, Args}) -> apps. Uninstallers and nameless rows are left out."""
    apps: list[App] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        name = " ".join(str(row.get("Name") or "").split())
        aumid = str(row.get("AppID") or "").strip()
        exe = str(row.get("Exe") or "").strip()
        if not name or not aumid or aumid in seen or not key_of(name):
            continue
        if _UNINSTALL.search(name) or _UNINSTALL.search(ntpath.basename(exe)):
            continue
        seen.add(aumid)
        apps.append(App(name, aumid, _norm_path(exe), str(row.get("Args") or "").strip()))
    return tuple(apps)


# --- sound-alikes -------------------------------------------------------------------------------------------

_VOWELS = "aeiouy"


@lru_cache(maxsize=8192)
def _sound(word: str) -> tuple[str, str, str]:
    """(metaphone, onset, rime) of one word. Rime = last vowel's first letter + metaphone of what follows."""
    code = jellyfish.metaphone(word) if word.isalpha() else word
    letters = word[:-1] if len(word) > 3 and word.endswith("e") and word[-2] not in _VOWELS else word
    end = len(letters)
    while end > 0 and letters[end - 1] not in _VOWELS:
        end -= 1
    start = end
    while start > 0 and letters[start - 1] in _VOWELS:
        start -= 1
    coda = letters[end:]
    rime = (letters[start] if start < end else "") + "-" + (jellyfish.metaphone(coda) if coda.isalpha() and coda else coda)
    return code, code[:2], rime


def _sounds_like(heard: str, word: str) -> float:
    """0, or 45-60 when ``heard`` could be a mishearing of ``word``."""
    if heard == word or len(heard) < 3 or len(word) < 3 or not heard.isalpha() or not word.isalpha():
        return 0.0
    a, b = _sound(heard), _sound(word)
    if not a[0] or not b[0]:
        return 0.0
    if a[0] == b[0]:
        return 60.0
    if min(len(a[0]), len(b[0])) / max(len(a[0]), len(b[0])) < 0.6:
        return 0.0
    onset = a[1] == b[1] and len(a[1]) == 2
    rhyme = a[2] == b[2] and a[2] != "-" and _syllables(heard) == _syllables(word) == 1  # foam / home ~ chrome
    close = fuzz.ratio(a[0], b[0]) >= 65  # "know it bad" ~ notepad
    if onset or rhyme or close:
        return 45.0 + 15.0 * fuzz.ratio(a[0], b[0]) / 100.0
    return 0.0


def sounds_like(heard: str, word: str) -> float:
    """0, or 45-60 when ``heard`` could be speech-to-text's mishearing of ``word``."""
    return _sounds_like(heard, word)


def _syllables(word: str) -> int:
    letters = word[:-1] if len(word) > 3 and word.endswith("e") and word[-2] not in _VOWELS else word
    return len(re.findall(f"[{_VOWELS}]+", letters))


# --- the catalog --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Name:
    """One way to say a target: its words and squashed key."""

    words: tuple[str, ...]
    key: str
    target: Target
    how: str  # alias | exact | short


class Catalog:
    """Installed apps + aliases. Immutable; build a new one to change it."""

    def __init__(self, apps: Iterable[App] = (), aliases: Mapping[str, str] | None = None) -> None:
        self.apps: tuple[App, ...] = tuple(apps)
        self._by_name = {app.name.casefold(): app for app in self.apps}
        names: list[_Name] = []
        for app in self.apps:
            target = Target("app", app.name, app=app)
            words = words_of(app.name)
            names.append(_Name(words, "".join(words), target, "exact"))
            if len(words) >= 2 and words[0] in _VENDORS:
                short = words[1:]
                names.append(_Name(short, "".join(short), target, "short"))
            plain = _without_version(words)
            if plain != words:  # "SOLIDWORKS 2025" -> "solidworks", "MATLAB R2025a" -> "matlab"
                names.append(_Name(plain, "".join(plain), target, "short"))
        for phrase, value in (aliases or {}).items():
            words = words_of(phrase)
            if not words:
                continue
            if value.startswith(("http://", "https://")):
                target = Target("site", " ".join(w.capitalize() for w in str(phrase).split()) if str(phrase).islower()
                                else str(phrase), url=value)
            else:
                app = self._by_name.get(value.casefold())
                if app is None:
                    continue  # that app isn't installed here
                target = Target("app", app.name, app=app)
            names.append(_Name(words, "".join(words), target, "alias"))
        self._names = tuple(names)
        self._aliases = {n.key: n for n in names if n.how == "alias"}
        self._exact: dict[str, list[_Name]] = {}
        self._short: dict[str, list[_Name]] = {}
        for n in names:
            if n.how == "exact":
                self._exact.setdefault(n.key, []).append(n)
            elif n.how == "short":
                self._short.setdefault(n.key, []).append(n)
        self._index_paths()

    # --- names --------------------------------------------------------------------------------------------

    @property
    def names(self) -> tuple[str, ...]:
        """Every app and alias name, for the vocab fix-up and the LLM prompt."""
        seen: dict[str, None] = {}
        for n in self._names:
            seen.setdefault(n.target.name if n.how != "alias" else " ".join(n.words), None)
        return tuple(seen)

    @property
    def aliases(self) -> tuple[tuple[str, Target], ...]:
        """The short names from aliases.yaml whose app is installed: ("canvas", site), ("fusion", Autodesk Fusion)."""
        return tuple((" ".join(n.words), n.target) for n in self._names if n.how == "alias")

    def app(self, name: str) -> App | None:
        return self._by_name.get(name.casefold())

    def resolve(self, phrase: str, *, strict: bool = False, familiar: frozenset[str] = frozenset()) -> Resolution:
        """``familiar``: app names with a window open now; like aliases, they may be offered as sound-alikes."""
        words = words_of(phrase)
        if not words:
            return Resolution()
        key = "".join(words)
        alias = self._aliases.get(key)
        if alias is not None:
            return Resolution((Match(alias.target, 100.0, "alias"),))
        exact = self._exact.get(key, [])
        if exact:
            return Resolution(tuple(Match(n.target, 100.0, "exact") for n in exact))
        if strict:
            return Resolution()
        found: dict[Target, Match] = {}

        def offer(target: Target, score: float, how: str) -> None:
            old = found.get(target)
            if old is None or score > old.score:
                found[target] = Match(target, score, how)

        for n in self._short.get(key, []):
            offer(n.target, 95.0, "short")
        content = [w for w in words if w not in _SMALL_WORDS]
        for n in self._names:
            if content and _contains(n.words, tuple(content)) and len(n.words) > len(content):
                offer(n.target, 60.0 + 25.0 * len(content) / len(n.words), "words")
            if len(key) >= 5 and len(n.key) >= 5:
                ratio = fuzz.ratio(key, n.key)
                if ratio >= 80:
                    offer(n.target, ratio, "spelling")
        if not found and 1 <= len(content) <= 3:
            heard = "".join(content)
            for n in self._names:
                if n.how != "alias" and n.target.name not in familiar:
                    continue  # sound-alikes only for names he set up or has open (rhymes are everywhere: door / Store)
                score = max(
                    [_sounds_like(heard, n.key)] + [_sounds_like(heard, w) for w in n.words if w not in _VENDORS]
                )
                if score >= _OFFER:
                    offer(n.target, score, "sound")
        ranked = sorted(found.values(), key=lambda m: (-m.score, m.target.name))
        return Resolution(tuple(ranked))

    # --- windows ------------------------------------------------------------------------------------------

    def _index_paths(self) -> None:
        by_aumid: dict[str, list[App]] = {}
        by_exe: dict[str, list[App]] = {}
        by_dir: dict[str, list[App]] = {}
        by_stem: dict[str, list[App]] = {}
        for app in self.apps:
            by_aumid.setdefault(app.aumid.casefold(), []).append(app)
            if app.exe and ntpath.basename(app.exe) not in _HOSTS:
                by_exe.setdefault(app.exe, []).append(app)
                by_dir.setdefault(ntpath.dirname(app.exe), []).append(app)
            by_stem.setdefault(key_of(app.name), []).append(app)
        self._by_aumid, self._by_exe, self._by_dir, self._by_stem = by_aumid, by_exe, by_dir, by_stem

    def app_for(self, exe: str, aumids: Iterable[str] = ()) -> App | None:
        """The installed app a window belongs to (its process path and AUMIDs), or None when unsure."""
        for aumid in aumids:
            owners = self._by_aumid.get(aumid.casefold(), [])
            if len(owners) == 1:
                return owners[0]
        path = _norm_path(exe)
        if not path:
            return None
        stem = ntpath.splitext(ntpath.basename(path))[0]
        if stem == "explorer":
            owners = self._by_aumid.get(_EXPLORER_AUMID.casefold(), [])
            return owners[0] if len(owners) == 1 else None
        if ntpath.basename(path) in _HOSTS:
            return None
        for index in (self._by_exe.get(path, []), self._by_dir.get(ntpath.dirname(path), [])):
            if len(index) == 1:
                return index[0]
            if index:
                return None  # a folder several apps share (Chrome's): don't guess
        owners = self._by_stem.get(key_of(stem), [])
        return owners[0] if len(owners) == 1 else None


_VERSIONISH = re.compile(r"\d+|r?\d{4}[a-z]?|r\d|v\d+|x\d+|bit")


def _without_version(words: tuple[str, ...]) -> tuple[str, ...]:
    """The name without trailing version / build words ("python 3 12 64 bit" -> "python"); never empty."""
    end = len(words)
    while end > 1 and _VERSIONISH.fullmatch(words[end - 1]):
        end -= 1
    return words[:end]


def _contains(name: tuple[str, ...], part: tuple[str, ...]) -> bool:
    """``part`` appears in ``name`` as whole words, in order (not necessarily adjacent)."""
    position = 0
    for word in part:
        try:
            position = name.index(word, position) + 1
        except ValueError:
            return False
    return True


# --- aliases.yaml ---------------------------------------------------------------------------------------------

DEFAULT_ALIASES = """\
# Short names for apps and sites. Left: what you say. Right: a Start-menu app name or a web address.
# Save the file; Nova uses it from the next command.
canvas: https://canvas.vt.edu
fusion: Autodesk Fusion
chrome: Google Chrome
claude: Claude
chatgpt: ChatGPT
docs: https://docs.google.com
google docs: https://docs.google.com
gmail: https://mail.google.com
google drive: https://drive.google.com
google calendar: https://calendar.google.com
youtube: https://www.youtube.com
github: https://github.com
vs code: Visual Studio Code
vscode: Visual Studio Code
teams: Microsoft Teams
edge: Microsoft Edge
explorer: File Explorer
files: File Explorer
"""


class _UniqueKeys(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        seen = set()
        for key_node, _ in node.value:
            key = " ".join(words_of(str(self.construct_object(key_node, deep=deep))))
            if key in seen:
                raise ValueError(f"{key!r} appears twice (line {key_node.start_mark.line + 1})")
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def parse_aliases(text: str) -> dict[str, str]:
    """`phrase: target` lines -> {normalized phrase: target}. Raises ValueError."""
    try:
        data = yaml.load(text, Loader=_UniqueKeys)
    except yaml.YAMLError as error:
        raise ValueError(f"not valid YAML: {error}") from error
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError("aliases.yaml must be `name: app or address` lines")
    out: dict[str, str] = {}
    for phrase, target in data.items():
        words = " ".join(words_of(str(phrase)))
        if not words or not isinstance(target, str) or not target.strip():
            raise ValueError(f"alias {phrase!r} needs an app name or a web address")
        out[words] = target.strip()
    return out


# --- reading the Start menu -------------------------------------------------------------------------------------

_APPS_FOLDER = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$folder = (New-Object -ComObject Shell.Application).NameSpace('shell:AppsFolder')
$rows = @(foreach ($item in $folder.Items()) {
    [pscustomobject]@{
        Name = [string]$item.Name
        AppID = [string]$item.ExtendedProperty('System.AppUserModel.ID')
        Exe = [string]$item.ExtendedProperty('System.Link.TargetParsingPath')
        Args = [string]$item.ExtendedProperty('System.Link.Arguments')
    }
})
ConvertTo-Json -InputObject $rows -Depth 3 -Compress
"""
_BELOW_NORMAL = 0x00004000


def query_start_menu() -> list[dict[str, Any]]:
    """Start-menu apps via a fixed PowerShell command (~1 s, below-normal priority). Never takes input."""
    powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    result = subprocess.run(
        [str(powershell), "-NoProfile", "-NonInteractive", "-Command", _APPS_FOLDER],
        capture_output=True, encoding="utf-8-sig", errors="replace", timeout=60, check=True,
        creationflags=subprocess.CREATE_NO_WINDOW | _BELOW_NORMAL,
    )
    rows = json.loads(result.stdout or "[]")
    return [rows] if isinstance(rows, dict) else [row for row in rows if isinstance(row, dict)]


class CatalogStore:
    """The current catalog for the brain (any thread). Cached list first, live list in the background."""

    def __init__(
        self,
        cache: Path,
        aliases: Path,
        *,
        query: Callable[[], list[dict[str, Any]]] = query_start_menu,
        refresh_s: float = REFRESH_S,
    ) -> None:
        self._cache = Path(cache)
        self._aliases_path = Path(aliases)
        self._query = query
        self._refresh_s = refresh_s
        self._lock = threading.Lock()
        self._rows: list[dict[str, Any]] = []
        self._aliases: dict[str, str] = {}
        self._aliases_stamp: tuple[int, int] | None = None
        self._catalog = Catalog()
        self._listeners: list[Callable[[Catalog], None]] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.loaded = threading.Event()  # a live or cached list is in
        self.error: str | None = None

    def subscribe(self, listener: Callable[[Catalog], None]) -> None:
        """Called (on the refresh thread) with every new catalog."""
        self._listeners.append(listener)

    def current(self) -> Catalog:
        self._check_aliases()
        return self._catalog

    def start(self) -> None:
        self.load_cached()
        self._thread = threading.Thread(target=self._run, name="nova-catalog", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()

    def refresh(self) -> Catalog:
        rows = self._query()
        if rows:
            self._rows = rows
            self._save_cache(rows)
            self._rebuild()
            self.loaded.set()
        return self._catalog

    def _run(self) -> None:
        while not self._stop.is_set():
            started = time.perf_counter()
            try:
                catalog = self.refresh()
                self.error = None
                _log.info("app catalog: %d apps (%.0f ms)", len(catalog.apps), (time.perf_counter() - started) * 1000)
            except Exception as error:  # noqa: BLE001 - the cached list keeps working
                self.error = f"couldn't read the Start menu: {error}"
                _log.warning("%s", self.error)
            if self._stop.wait(self._refresh_s):
                return

    def load_cached(self) -> None:
        """The list saved by the last refresh (instant), and aliases.yaml."""
        self._check_aliases()
        try:
            rows = json.loads(self._cache.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(rows, list) and rows:
            self._rows = [row for row in rows if isinstance(row, dict)]
            self._rebuild()
            self.loaded.set()

    def _save_cache(self, rows: list[dict[str, Any]]) -> None:
        try:
            self._cache.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._cache.with_suffix(".tmp")
            temporary.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
            temporary.replace(self._cache)
        except OSError:
            _log.warning("couldn't save the app catalog cache", exc_info=True)

    def _check_aliases(self) -> None:
        path = self._aliases_path
        try:
            info = path.stat()
        except FileNotFoundError:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(DEFAULT_ALIASES, encoding="utf-8")
                info = path.stat()
            except OSError:
                return
        except OSError:
            return
        stamp = (info.st_mtime_ns, info.st_size)
        if stamp == self._aliases_stamp:
            return
        with self._lock:
            if stamp == self._aliases_stamp:
                return
            self._aliases_stamp = stamp
            try:
                self._aliases = parse_aliases(path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError) as error:
                _log.warning("aliases.yaml not used: %s", error)
                return
        self._rebuild()

    def _rebuild(self) -> None:
        catalog = Catalog(apps_from_rows(self._rows), self._aliases)
        self._catalog = catalog
        for listener in list(self._listeners):
            try:
                listener(catalog)
            except Exception:  # noqa: BLE001
                _log.exception("catalog listener failed")
