"""Folders and files by name (prompt 07c §7): "open downloads", "open the budget spreadsheet".

Known folders come from the shell (``SHGetKnownFolderPath``: they move with OneDrive / a redirected Documents);
a file or folder name is matched loosely against the top level of the usual folders. ``best`` is pure.
Executables and scripts never open from voice (``risky``): that would be running a command.
"""

from __future__ import annotations

import ctypes
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from rapidfuzz import fuzz

from . import native
from .native import ActionError

_KNOWN = {
    "downloads": "{374DE290-123F-4565-9164-39C4925E467B}",
    "documents": "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}",
    "desktop": "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}",
    "pictures": "{33E28130-4E1E-4676-835A-98395C3BC3BB}",
    "music": "{4BD8D571-6D19-48D3-BE97-422220080E43}",
    "videos": "{18989B1D-99B5-455B-841C-AB7C74E4DDFC}",
    "screenshots": "{B7BEDE81-DF94-4682-A7D8-57A52620B86F}",
    "camera roll": "{AB5FB87B-7CE2-4F83-915D-550846C9537B}",
    "home": "{5E6C858F-0E22-4760-9AFE-EA3317B67173}",
}
_ALIASES = {
    "download": "downloads", "docs": "documents", "document": "documents", "my documents": "documents",
    "photos": "pictures", "picture": "pictures", "images": "pictures", "my pictures": "pictures",
    "movies": "videos", "video": "videos", "my videos": "videos", "my music": "music",
    "camera": "camera roll", "screenshot": "screenshots", "screen shots": "screenshots",
    "user folder": "home", "my files": "home", "profile": "home", "user": "home",
}
SEARCHED = ("desktop", "downloads", "documents", "pictures", "music", "videos")
_TITLES = {"camera roll": "Camera Roll", "home": "your user folder"}
_KF_FLAG_CREATE = 0x8000
THRESHOLD = 80.0  # WRatio of the spoken name against a file's name
TIE = 5.0  # runners-up this close to the best are offered too
MAX_FOUND = 4  # the island shows four choices
RISKY_EXTENSIONS = frozenset({
    ".exe", ".bat", ".cmd", ".com", ".ps1", ".psm1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".msi", ".msp",
    ".scr", ".pif", ".hta", ".cpl", ".reg", ".lnk", ".url", ".appref-ms", ".jar", ".py", ".pyw",
})
# words that say what kind of file, not its name: "the budget spreadsheet" is budget.xlsx
_KINDS = {
    "pdf": (".pdf",), "spreadsheet": (".xlsx", ".xls", ".xlsm", ".csv", ".ods"), "excel": (".xlsx", ".xls", ".xlsm"),
    "csv": (".csv",), "word": (".docx", ".doc", ".rtf", ".odt"), "doc": (".docx", ".doc", ".rtf", ".odt"),
    "presentation": (".pptx", ".ppt", ".odp"), "slides": (".pptx", ".ppt", ".odp"), "powerpoint": (".pptx", ".ppt"),
    "photo": (".jpg", ".jpeg", ".png", ".heic", ".webp", ".gif", ".bmp"),
    "picture": (".jpg", ".jpeg", ".png", ".heic", ".webp", ".gif", ".bmp"),
    "image": (".jpg", ".jpeg", ".png", ".heic", ".webp", ".gif", ".bmp"),
    "video": (".mp4", ".mov", ".mkv", ".avi", ".wmv", ".webm"), "zip": (".zip", ".7z", ".rar"),
    "text": (".txt", ".md"), "song": (".mp3", ".m4a", ".wav", ".flac"),
}
_FILLER = frozenset({"the", "my", "a", "file", "folder", "called", "named", "that", "this"})
_HIDDEN = stat.FILE_ATTRIBUTE_HIDDEN | stat.FILE_ATTRIBUTE_SYSTEM


@dataclass(frozen=True)
class Entry:
    name: str  # as on disk: "Budget 2026.xlsx"
    path: str
    folder: str  # the known folder it's in: "downloads"
    is_dir: bool = False
    modified: float = 0.0

    @property
    def where(self) -> str:
        return f"{self.name} · {folder_title(self.folder)}"


def folder_title(key: str) -> str:
    return _TITLES.get(key, key.capitalize())


def _words(text: str) -> list[str]:
    return re.sub(r"[_\-.,()\[\]]+", " ", text.lower()).split()


def folder_key(spoken: str) -> str | None:
    """ "my downloads folder" -> "downloads"; None if it isn't a known folder."""
    words = [w for w in _words(spoken) if w not in ("the", "my", "folder")]
    text = " ".join(words)
    if text in _KNOWN:
        return text
    return _ALIASES.get(text) or _ALIASES.get(" ".join(_words(spoken)))


def stem(entry: Entry) -> str:
    """What a file is called out loud: no extension, separators as spaces."""
    name = entry.name if entry.is_dir else os.path.splitext(entry.name)[0]
    return " ".join(_words(name))


def query(spoken: str) -> tuple[str, tuple[str, ...]]:
    """ "the budget spreadsheet" -> ("budget", (".xlsx", …)): the name to match, and the kinds of file it may be."""
    words = [w for w in _words(spoken) if w not in _FILLER]
    kinds: list[str] = []
    named = []
    for word in words:
        singular = word.removesuffix("s") if word.endswith("s") and word.removesuffix("s") in _KINDS else word
        if singular in _KINDS and len(words) > 1:
            kinds.extend(_KINDS[singular])
        else:
            named.append(word)
    return " ".join(named), tuple(dict.fromkeys(kinds))


def best(spoken: str, entries: list[Entry]) -> list[Entry]:
    """The entries ``spoken`` most likely means, best first: one when it's clear, up to ``MAX_FOUND`` when a few
    are about as good (newest first among equals), none when nothing is close."""
    name, kinds = query(spoken)
    if not name:
        return []
    pool = entries
    if kinds:
        typed = [e for e in entries if not e.is_dir and os.path.splitext(e.name)[1].lower() in kinds]
        pool = typed or entries
    exact = [e for e in pool if stem(e) == name]
    if exact:
        return sorted(exact, key=lambda e: -e.modified)[:MAX_FOUND]
    scored = [(fuzz.WRatio(name, stem(e)), e) for e in pool if stem(e)]
    scored = [(score, e) for score, e in scored if score >= THRESHOLD]
    if not scored:
        return []
    scored.sort(key=lambda pair: (-pair[0], -pair[1].modified))
    top = scored[0][0]
    return [e for score, e in scored if score >= top - TIE][:MAX_FOUND]


def risky(path: str) -> bool:
    """Would opening this run something (a program, a script, a shortcut to one)?"""
    return os.path.splitext(path)[1].lower() in RISKY_EXTENSIONS


# --- the disk ----------------------------------------------------------------------------------------------------

native._bind(native.shell32, "SHGetKnownFolderPath", ctypes.c_long, ctypes.POINTER(native.GUID), ctypes.c_uint32,
             ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p))
native._bind(native.ole32, "CoTaskMemFree", None, ctypes.c_void_p)


def known_folder(key: str, *, create: bool = False) -> Path:
    out = ctypes.c_wchar_p()
    flags = _KF_FLAG_CREATE if create else 0
    hr = native.shell32.SHGetKnownFolderPath(ctypes.byref(native.guid(_KNOWN[key])), flags, None, ctypes.byref(out))
    try:
        if hr < 0 or not out.value:
            raise ActionError(f"Windows didn't say where {folder_title(key)} is.")
        return Path(out.value)
    finally:
        native.ole32.CoTaskMemFree(out)


def listing(key: str) -> list[Entry]:
    """The top level of a known folder, without hidden / system files."""
    try:
        folder = known_folder(key)
        items = list(os.scandir(folder))
    except (ActionError, OSError):
        return []
    entries = []
    for item in items:
        try:
            info = item.stat()
        except OSError:
            continue
        if getattr(info, "st_file_attributes", 0) & _HIDDEN or item.name.startswith(("~$", ".")):
            continue
        entries.append(Entry(item.name, item.path, key, item.is_dir(), info.st_mtime))
    return entries


def find(spoken: str, folder: str | None = None) -> list[Entry]:
    """A known folder by name, or files / folders called ``spoken`` in ``folder`` (or the usual folders)."""
    key = folder_key(spoken)
    if key is not None and not folder:
        return [Entry(folder_title(key), str(known_folder(key)), key, True)]
    where = folder_key(folder) if folder else None
    if folder and where is None:
        raise ActionError(f"I don't know a folder called “{folder}”.")
    entries = [entry for place in ((where,) if where else SEARCHED) for entry in listing(place)]
    return best(spoken, entries)


def open_path(path: str, title: str) -> str:
    if not os.path.exists(path):
        raise ActionError(f"{title} isn't there any more.")
    native.grant_foreground()
    try:
        os.startfile(path)  # noqa: S606 - his own file, opened with its default app (executables are refused first)
    except OSError as error:
        raise ActionError(f"Windows couldn't open {title}.") from error
    return f"Opened {title}"
