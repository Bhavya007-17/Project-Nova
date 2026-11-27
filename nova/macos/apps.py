"""macOS stand-in for ``nova.actions.apps``: open / switch / quit apps, open web addresses.

Launching goes through ``open -b <bundle id>`` (LaunchServices), which also re-opens a window for an app that is
running with none (the Mac norm). "Close <app>" quits it, like Cmd+Q; an app asking about unsaved work stays.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass

from AppKit import NSRunningApplication

from ..actions.catalog import App, Catalog
from . import native
from .native import ActionError, Win

LAUNCH_WAIT_S = 20.0
QUIT_WAIT_S = 5.0  # Safari takes ~3 s to quit
REOPEN_AFTER_S = 2.5  # launched, finished, still no window: ask it to open one
CLOSE_WAIT_S = 1.5
_BROWSERS = ("Google Chrome",)


@dataclass(frozen=True)
class Outcome:
    detail: str
    method: str = ""
    win: Win | None = None
    hold: float = 0.0


def windows_of(catalog: Catalog, app: App, wins: list[Win] | None = None) -> list[Win]:
    wins = native.app_windows() if wins is None else wins
    out = []
    for win in wins:
        owner = catalog.app_for(win.exe, win.aumids)
        if (owner is not None and owner.aumid == app.aumid) or (app.aumid in win.aumids):
            out.append(win)
    return out


def app_of(catalog: Catalog, win: Win) -> App | None:
    return catalog.app_for(win.exe, win.aumids)


def _running(app: App) -> list:
    return list(NSRunningApplication.runningApplicationsWithBundleIdentifier_(app.aumid) or [])


def _alive(pid: int) -> bool:
    import os

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _launched(app: App) -> bool:
    running = _running(app)
    return bool(running) and all(r.isFinishedLaunching() for r in running)


def launch(app: App) -> str:
    target = ["-b", app.aumid] if app.aumid and "." in app.aumid else ["-a", app.exe or app.name]
    result = subprocess.run(["/usr/bin/open", *target], capture_output=True, text=True, timeout=10, check=False)
    if result.returncode != 0:
        raise ActionError(f"Couldn't open {app.name}.")
    return "open"


def open_app(catalog: Catalog, app: App) -> Outcome:
    existing = windows_of(catalog, app)
    if existing:
        win = existing[0]
        method = native.activate(win.hwnd)
        text = f"Already on {app.name}" if method == "already" else f"Switched to {app.name}"
        return Outcome(text, method, win)
    was_running = bool(_running(app))
    how = launch(app)
    first = lambda: next(iter(windows_of(catalog, app)), None)  # noqa: E731
    deadline = time.perf_counter() + LAUNCH_WAIT_S
    win = native.wait_for(first, REOPEN_AFTER_S, interval=0.05)
    if win is None and _launched(app):
        # Mac apps may finish launching with no window (Safari, Notes...): "reopen" them like a Dock click.
        launch(app)
    if win is None:
        win = native.wait_for(first, max(0.0, deadline - time.perf_counter()), interval=0.05)
    if win is None:
        if was_running or _running(app):
            for running in _running(app):
                native.activate_pid(int(running.processIdentifier()))
            return Outcome(f"Switched to {app.name}", how)
        return Outcome(f"Starting {app.name}…", how)
    try:
        method = native.activate(win.hwnd)
    except ActionError:
        method = "launched"
    return Outcome(f"Opened {app.name}", f"{how}/{method}", win)


def browser(catalog: Catalog) -> App | None:
    for name in _BROWSERS:
        app = catalog.app(name)
        if app is not None:
            return app
    return None


def is_browser(catalog: Catalog, win: Win | None) -> bool:
    if win is None:
        return False
    app = app_of(catalog, win)
    return app is not None and app.name in _BROWSERS


def open_url(catalog: Catalog, url: str, label: str) -> Outcome:
    chrome = browser(catalog)
    if chrome is None:
        subprocess.run(["/usr/bin/open", url], check=False, timeout=10)
        return Outcome(f"Opened {label}", "default-browser")
    before = {w.hwnd: w.title for w in windows_of(catalog, chrome)}
    subprocess.run(["/usr/bin/open", "-b", chrome.aumid, url], check=False, timeout=10)

    def landed() -> Win | None:
        fg = native.describe(native.foreground())
        if fg is not None and is_browser(catalog, fg) and (fg.hwnd not in before or before[fg.hwnd] != fg.title):
            return fg
        return None

    win = native.wait_for(landed, 3.0, interval=0.03)
    if win is None:
        current = windows_of(catalog, chrome)
        if not current:
            return Outcome(f"Opening {label}…", "chrome-start")
        return Outcome(f"Opened {label}", native.activate(current[0].hwnd), current[0])
    return Outcome(f"Opened {label}", "chrome", win)


def browser_window(catalog: Catalog) -> tuple[Win, str]:
    fg = native.describe(native.foreground())
    if fg is not None and is_browser(catalog, fg):
        return fg, "already"
    chrome = browser(catalog)
    wins = windows_of(catalog, chrome) if chrome else []
    if not wins:
        raise ActionError("Chrome isn't open.")
    return wins[0], native.activate(wins[0].hwnd)


def close_windows(wins: list[Win], name: str) -> Outcome:
    """Close just these windows (their close buttons)."""
    for win in wins:
        native.post_close(win.hwnd)
    if native.wait_for(lambda: all(not native.is_window(w.hwnd) for w in wins), CLOSE_WAIT_S, interval=0.03):
        return Outcome(f"Closed {name}")
    stayed = next((w for w in wins if native.is_window(w.hwnd)), None)
    if stayed is not None:
        try:
            native.activate(stayed.hwnd)
        except ActionError:
            pass
    return Outcome(f"{name} is asking about unsaved work — your call", "prompt", stayed)


def close_app(catalog: Catalog, app: App) -> Outcome:
    running = _running(app)
    if not running:
        raise ActionError(f"{app.name} isn't open.")
    pids = [int(item.processIdentifier()) for item in running]
    for item in running:
        item.terminate()
    # Poll the processes themselves: NSRunningApplication.isTerminated only updates while a run loop spins.
    if native.wait_for(lambda: not any(_alive(pid) for pid in pids), QUIT_WAIT_S, interval=0.05):
        return Outcome(f"Quit {app.name}")
    for item in running:
        native.activate_pid(int(item.processIdentifier()))
    return Outcome(f"{app.name} is asking about unsaved work — your call", "prompt")
