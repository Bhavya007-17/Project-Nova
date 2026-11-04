"""Apps (SPEC §8): open / switch / close by catalog identity, web addresses in Chrome.

- A window belongs to an app by AUMID or shortcut target (``Catalog.app_for``), never by its title.
- open = if a window is up, restore it and bring it to the front; else launch it
  (packaged apps through IApplicationActivationManager, others through the Start-menu
  entry via explorer shell:AppsFolder) and wait for its window.
- close = WM_CLOSE to each of its windows. If something stays (an unsaved-changes prompt,
  in its own dialog or inside the window), bring it forward and say so: Nova never clicks it.
"""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
from ctypes import byref, c_void_p, wintypes
from dataclasses import dataclass
from pathlib import Path

from . import native
from .catalog import App, Catalog
from .native import ActionError, Win

_log = logging.getLogger(__name__)

LAUNCH_WAIT_S = 20.0  # app start-up is whatever the app takes (Fusion is slow); this is how long Nova watches
CLOSE_WAIT_S = 1.5  # still there after this = it's asking (Win 11 Notepad asks inside its own window)
_BROWSERS = ("Google Chrome",)
_CLSID_ActivationManager = native.guid("{45BA127D-10A8-46EA-8AB7-56EA9078943C}")
_IID_IApplicationActivationManager = native.guid("{2E941141-7F97-4756-BA1D-9DECDE894A3D}")


@dataclass(frozen=True)
class Outcome:
    detail: str  # island text: "Opened Chrome"
    method: str = ""  # how the foreground was taken (for the latency log)
    win: Win | None = None


def windows_of(catalog: Catalog, app: App, wins: list[Win] | None = None) -> list[Win]:
    """The app's windows, frontmost first."""
    wins = native.app_windows() if wins is None else wins
    out = []
    for win in wins:
        owner = catalog.app_for(win.exe, win.aumids)
        if owner is not None and owner.aumid == app.aumid:
            out.append(win)
    return out


def app_of(catalog: Catalog, win: Win) -> App | None:
    return catalog.app_for(win.exe, win.aumids)


def _activate_packaged(aumid: str) -> int:
    """IApplicationActivationManager::ActivateApplication -> process id (raises OSError on failure)."""
    native.com_init()
    manager = c_void_p()
    hr = native.ole32.CoCreateInstance(byref(_CLSID_ActivationManager), None, 0x4,  # CLSCTX_LOCAL_SERVER
                                       byref(_IID_IApplicationActivationManager), byref(manager))
    if hr < 0:
        raise OSError(f"activation manager unavailable (0x{hr & 0xFFFFFFFF:08X})")
    try:
        pid = wintypes.DWORD()
        hr = native.com_call(manager, 3, aumid, None, 0, byref(pid),
                             types=(wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_int, ctypes.POINTER(wintypes.DWORD)))
        if hr < 0:
            raise OSError(f"activation failed (0x{hr & 0xFFFFFFFF:08X})")
        return int(pid.value)
    finally:
        native.com_release(manager)


def _explorer() -> str:
    return str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "explorer.exe")


def launch(app: App) -> str:
    """Start ``app`` the way the Start menu does. Returns how."""
    if "!" in app.aumid and not app.exe:
        try:
            _activate_packaged(app.aumid)
            return "aumid"
        except OSError as error:
            _log.info("packaged activation of %s failed (%s); using the Start-menu entry", app.name, error)
    native.grant_foreground()
    subprocess.Popen([_explorer(), f"shell:AppsFolder\\{app.aumid}"], close_fds=True,  # noqa: S603 - fixed program
                     creationflags=subprocess.CREATE_NO_WINDOW)
    return "shell"


def open_app(catalog: Catalog, app: App) -> Outcome:
    existing = windows_of(catalog, app)
    if existing:
        win = existing[0]
        if native.elevated(win.pid):
            raise ActionError(f"{app.name} is running as administrator — Windows won't let Nova switch to it.")
        method = native.activate(win.hwnd)
        text = f"Already on {app.name}" if method == "already" else f"Switched to {app.name}"
        return Outcome(text, method, win)
    how = launch(app)
    win = native.wait_for(lambda: next(iter(windows_of(catalog, app)), None), LAUNCH_WAIT_S, interval=0.05)
    if win is None:
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
    """Open ``url`` in a new Chrome tab (or the default browser if Chrome isn't installed)."""
    chrome = browser(catalog)
    if chrome is None or not chrome.exe:
        native.grant_foreground()
        os.startfile(url)  # noqa: S606 - an http(s) address checked by the plan
        return Outcome(f"Opened {label}", "default-browser")
    before = {w.hwnd: w.title for w in windows_of(catalog, chrome)}
    native.grant_foreground()
    subprocess.Popen([chrome.exe, url], close_fds=True)  # noqa: S603 - Chrome from the Start menu, a checked URL

    def landed() -> Win | None:
        fg = native.describe(native.foreground())
        if fg is not None and is_browser(catalog, fg) and (fg.hwnd not in before or before[fg.hwnd] != fg.title):
            return fg
        return None

    win = native.wait_for(landed, 3.0, interval=0.03)
    method = "chrome"
    if win is None:
        current = windows_of(catalog, chrome)
        if not current:
            return Outcome(f"Opening {label}…", "chrome-start")
        method = native.activate(current[0].hwnd)
        win = current[0]
    return Outcome(f"Opened {label}", method, win)


def browser_window(catalog: Catalog) -> tuple[Win, str]:
    """The browser window to send tab / back / forward keys to: the foreground one, else the last used Chrome."""
    fg = native.describe(native.foreground())
    if fg is not None and is_browser(catalog, fg):
        return fg, "already"
    chrome = browser(catalog)
    wins = windows_of(catalog, chrome) if chrome else []
    if not wins:
        raise ActionError("Chrome isn't open.")
    return wins[0], native.activate(wins[0].hwnd)


def _closed(hwnd: int) -> bool:
    return not native.is_window(hwnd) or not native.user32.IsWindowVisible(hwnd)


def close_windows(wins: list[Win], name: str) -> Outcome:
    for win in wins:
        if native.elevated(win.pid):
            raise ActionError(f"{name} is running as administrator — Windows won't let Nova close it.")
        if native.is_hung(win.hwnd):
            raise ActionError(f"{name} isn't responding — close it yourself (or wait).")
    before = {w.hwnd for w in native.app_windows(include_owned=True)}
    for win in wins:
        native.post_close(win.hwnd)
    if native.wait_for(lambda: all(_closed(w.hwnd) for w in wins), CLOSE_WAIT_S, interval=0.03):
        return Outcome(f"Closed {name}")
    pids = {w.pid for w in wins}
    prompt = next((w for w in native.app_windows(include_owned=True) if w.pid in pids and w.hwnd not in before), None)
    stayed = next((w for w in wins if not _closed(w.hwnd)), None)
    target = prompt or stayed
    if target is not None:
        try:
            native.activate(target.hwnd)
        except ActionError:
            pass
    return Outcome(f"{name} is asking about unsaved work — your call", "prompt", target)


def close_app(catalog: Catalog, app: App) -> Outcome:
    wins = windows_of(catalog, app)
    if not wins:
        raise ActionError(f"{app.name} isn't open.")
    return close_windows(wins, app.name)
