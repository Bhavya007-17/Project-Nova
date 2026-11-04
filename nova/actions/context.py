"""Context snapshot (SPEC §7.1): what's on screen when a command arrives, in ~10 ms.

Foreground window (process, title, class, which app), the top 12 app windows, monitors,
Chrome's active tab (title + address from the extension's last report, no round trip; the
window title when the extension isn't connected). The focused control (UI Automation) is asked
separately and only when typing, see ``nova.actions.uia``; the page map only when he points at
the page (``nova.actions.chrome``).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from . import native
from .catalog import Catalog
from .native import Monitor, Win

TOP_WINDOWS = 12
_CHROME_SUFFIX = " - Google Chrome"


@dataclass(frozen=True)
class AppWindow:
    win: Win
    app: str  # catalog name, "" if unknown


@dataclass(frozen=True)
class Tab:
    title: str
    url: str = ""  # from the extension


@dataclass(frozen=True)
class Context:
    foreground: AppWindow | None
    windows: tuple[AppWindow, ...]  # frontmost first
    monitors: tuple[Monitor, ...]
    tab: Tab | None
    locked: bool
    ms: float

    def compact(self) -> dict:
        """Small JSON for logs and (Stage 05) the LLM: apps and states, no window titles except the tab's."""
        fg = self.foreground
        return {
            "foreground": {"app": fg.app, "exe": fg.win.exe_name, "class": fg.win.cls} if fg else None,
            "windows": [{"app": w.app or w.win.exe_name, "min": w.win.minimized, "max": w.win.maximized}
                        for w in self.windows],
            "monitors": len(self.monitors),
            "tab": self.tab.title if self.tab else None,
            "site": (urlsplit(self.tab.url).hostname or None) if self.tab and self.tab.url else None,
        }


def snapshot(catalog: Catalog, chrome: Any = None) -> Context:
    """``chrome``: the extension bridge (anything with ``active_tab()``), or None."""
    started = time.perf_counter()
    wins = native.app_windows(limit=TOP_WINDOWS)
    named = tuple(AppWindow(w, _name(catalog, w)) for w in wins)
    fg_hwnd = native.foreground()
    fg = next((w for w in named if w.win.hwnd == fg_hwnd), None)
    if fg is None and fg_hwnd:
        win = native.describe(fg_hwnd)
        fg = AppWindow(win, _name(catalog, win)) if win is not None else None
    live = chrome.active_tab() if chrome is not None else None
    window = next((w for w in named if w.app == "Google Chrome"), None)
    if live is not None:
        tab: Tab | None = Tab(live.title, live.url)
    else:
        tab = Tab(window.win.title.removesuffix(_CHROME_SUFFIX)) if window else None
    return Context(fg, named, tuple(native.monitors()), tab, native.workstation_locked(),
                   (time.perf_counter() - started) * 1000)


def _name(catalog: Catalog, win: Win) -> str:
    app = catalog.app_for(win.exe, win.aumids)
    return app.name if app else ""
