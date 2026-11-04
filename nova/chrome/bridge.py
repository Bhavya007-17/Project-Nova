"""The Chrome bridge (SPEC §3, §9): Nova's end of the named pipe the extension's native host connects to.

``extension/host/nova_host.py`` (started by Chrome) relays the extension's JSON to this pipe
(``multiprocessing.connection``, HMAC challenge both ways with the key in ``data/chrome-bridge.json``).
One thread accepts hosts (one per Chrome profile that has the extension), one thread per host reads.
Requests carry an id and wait for the matching reply (with a deadline). Events from the extension
(hello / tab / focus) keep the focused window's active tab in mind, so the context snapshot reads the
tab's address without a round trip. Nova opens no TCP port: the pipe is local, per user, keyed.
"""

from __future__ import annotations

import itertools
import json
import logging
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from multiprocessing.connection import Client, Connection, Listener
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

REQUEST_S = 3.0


class ChromeError(Exception):
    """A Chrome step that couldn't be done; the message is shown in the island as-is."""


@dataclass(frozen=True)
class BridgeInfo:
    pipe: str
    key: str  # hex

    @classmethod
    def load(cls, path: Path) -> BridgeInfo | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls(str(data["pipe"]), str(data["key"]))
        except (OSError, ValueError, KeyError, TypeError):
            return None

    @classmethod
    def ensure(cls, path: Path) -> BridgeInfo:
        """The pipe name and key, made once (a fresh random pipe name and 256-bit key)."""
        info = cls.load(path)
        if info is not None:
            return info
        info = cls(rf"\\.\pipe\nova-chrome-{secrets.token_hex(6)}", secrets.token_hex(32))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"pipe": info.pipe, "key": info.key}, indent=2) + "\n", encoding="utf-8")
        return info


@dataclass(frozen=True)
class TabInfo:
    """A Chrome tab as the extension describes it."""

    id: int
    window: int
    url: str
    title: str
    active: bool = True
    status: str = ""
    last: float = 0.0  # lastAccessed, ms since 1970

    @classmethod
    def of(cls, data: Any) -> TabInfo | None:
        if not isinstance(data, Mapping) or not isinstance(data.get("id"), int):
            return None
        last = data.get("lastAccessed")
        return cls(int(data["id"]), int(data.get("windowId") or 0), str(data.get("url") or ""),
                   str(data.get("title") or ""), bool(data.get("active", True)), str(data.get("status") or ""),
                   float(last) if isinstance(last, (int, float)) else 0.0)


@dataclass
class _Link:
    """One connected Chrome profile."""

    conn: Connection
    lock: threading.Lock = field(default_factory=threading.Lock)
    tab: TabInfo | None = None
    focused: bool = False
    seen: float = 0.0  # when it last told us something (monotonic)
    focus_at: float = 0.0  # when one of its windows last took focus
    version: str = ""


@dataclass
class _Pending:
    link: _Link
    done: threading.Event = field(default_factory=threading.Event)
    reply: Mapping[str, Any] | None = None


class Bridge:
    def __init__(self, info: BridgeInfo, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._info = info
        self._clock = clock
        self._links: list[_Link] = []
        self._pending: dict[int, _Pending] = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)
        self._listener: Listener | None = None
        self._closed = False
        self.error = ""

    # --- life -----------------------------------------------------------------------------------------------

    def start(self) -> bool:
        """Open the pipe and accept hosts in the background. False (and ``error``) if the pipe is taken."""
        try:
            self._listener = Listener(self._info.pipe, family="AF_PIPE", authkey=bytes.fromhex(self._info.key))
        except OSError as error:
            self.error = f"the Chrome pipe is in use ({error.strerror or error}) — is another Nova running?"
            _log.warning("chrome bridge: %s", self.error)
            return False
        threading.Thread(target=self._accept, name="nova-chrome-bridge", daemon=True).start()
        return True

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            links = list(self._links)
        for link in links:
            _close(link.conn)
        if self._listener is not None:
            # Wake the blocked accept() so its thread can end — from a throwaway thread: the wake-up may land on the
            # listener's spare pipe instance, which nobody serves, and its handshake would wait forever. Never block
            # Nova's shutdown on it; leaving the process ends both threads.
            threading.Thread(target=self._wake, name="nova-chrome-bridge-wake", daemon=True).start()
            try:
                self._listener.close()
            except OSError:
                pass

    def _wake(self) -> None:
        try:
            _close(Client(self._info.pipe, family="AF_PIPE", authkey=bytes.fromhex(self._info.key)))
        except Exception:  # noqa: BLE001 - nothing listening any more: fine
            pass

    def _accept(self) -> None:
        assert self._listener is not None
        while not self._closed:
            try:
                conn = self._listener.accept()
            except Exception as error:  # noqa: BLE001 - a client with the wrong key, or one that hung up mid-hello
                if self._closed:
                    return
                _log.info("chrome bridge: a connection was refused (%r)", error)
                continue
            if self._closed:
                _close(conn)
                return
            self.attach(conn)

    def attach(self, conn: Connection) -> None:
        """Serve one host's connection (``start`` does this; tests hand in one end of a Pipe)."""
        link = _Link(conn, seen=self._clock())
        with self._changed:
            self._links.append(link)
            self._changed.notify_all()
        threading.Thread(target=self._read, args=(link,), name="nova-chrome-link", daemon=True).start()
        _log.info("chrome bridge: Chrome connected (%d connected)", len(self._links))

    def _read(self, link: _Link) -> None:
        try:
            while True:
                try:
                    body = link.conn.recv_bytes()
                except (EOFError, OSError):
                    return
                try:
                    message = json.loads(body.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    continue
                if isinstance(message, dict):
                    self._deliver(link, message)
        finally:
            self._drop(link)

    def _drop(self, link: _Link) -> None:
        with self._changed:
            if link in self._links:
                self._links.remove(link)
            orphans = [p for p in self._pending.values() if p.link is link]
            self._changed.notify_all()
        for pending in orphans:
            pending.reply = {"ok": False, "error": "Chrome disconnected"}
            pending.done.set()
        _close(link.conn)
        if not self._closed:
            _log.info("chrome bridge: a Chrome connection closed (%d left)", len(self._links))

    def _deliver(self, link: _Link, message: Mapping[str, Any]) -> None:
        if message.get("id") is not None:
            with self._lock:
                pending = self._pending.pop(message["id"], None) if isinstance(message["id"], int) else None
            if pending is not None:
                pending.reply = message
                pending.done.set()
            return
        event = message.get("event")
        now = self._clock()
        with self._changed:
            link.seen = now
            if event in ("hello", "tab"):
                if event == "hello":
                    link.version = str(message.get("version") or "")
                tab = TabInfo.of(message.get("tab"))
                if tab is not None:
                    link.tab = tab
                focused = bool(message.get("focused"))
                if focused and not link.focused:
                    link.focus_at = now
                if focused:
                    for other in self._links:
                        if other is not link:
                            other.focused = False
                link.focused = focused
            elif event == "focus":
                link.focused = bool(message.get("focused"))
            self._changed.notify_all()

    # --- state ----------------------------------------------------------------------------------------------

    def _best(self) -> _Link | None:
        """The profile he's using: the one with a focused window, else the one heard from last."""
        if not self._links:
            return None
        return max(self._links, key=lambda link: (link.focused, link.focus_at, link.seen))

    @property
    def connected(self) -> bool:
        with self._lock:
            return bool(self._links)

    def active_tab(self) -> TabInfo | None:
        """The focused (or last focused) Chrome window's active tab. No round trip."""
        with self._lock:
            link = self._best()
            return link.tab if link is not None else None

    def chrome_focused(self) -> bool:
        with self._lock:
            link = self._best()
            return bool(link and link.focused)

    def wait_connected(self, timeout: float) -> bool:
        deadline = self._clock() + timeout
        with self._changed:
            while not self._links:
                left = deadline - self._clock()
                if left <= 0 or self._closed:
                    return False
                self._changed.wait(left)
            return True

    # --- requests -------------------------------------------------------------------------------------------

    def request(self, cmd: str, args: Mapping[str, Any] | None = None, timeout: float = REQUEST_S) -> Any:
        """Send a command to the extension and wait for its answer (``result``). Raises ChromeError."""
        with self._lock:
            link = self._best()
            if link is None:
                raise ChromeError(_NOT_CONNECTED)
            request_id = next(self._ids)
            pending = _Pending(link)
            self._pending[request_id] = pending
        body = json.dumps({"id": request_id, "cmd": cmd, "args": dict(args or {})}).encode("utf-8")
        try:
            with link.lock:
                link.conn.send_bytes(body)
        except (OSError, EOFError) as error:
            with self._lock:
                self._pending.pop(request_id, None)
            raise ChromeError("Lost the connection to Chrome — try again.") from error
        if not pending.done.wait(timeout):
            with self._lock:
                self._pending.pop(request_id, None)
            raise ChromeError("Chrome didn't answer in time.")
        reply = pending.reply or {}
        if not reply.get("ok"):
            raise ChromeError(_plain(str(reply.get("error") or "Chrome couldn't do that.")))
        return reply.get("result")


_NOT_CONNECTED = "Nova's Chrome extension isn't connected — run `nova chrome --install` and load it once (see `nova doctor`)."


def _plain(error: str) -> str:
    """Chrome's error text -> island words."""
    low = error.lower()
    if "cannot access" in low or "chrome://" in low or "extensions gallery" in low or "cannot be scripted" in low:
        return "Chrome doesn't let extensions touch this page."
    if "no tab with id" in low or "frame gone" in low or "frame with id" in low:
        return "That tab or page went away."
    if "error page" in low:
        return "This page didn't load."
    return error[:1].upper() + error[1:]


def _close(conn: Connection) -> None:
    try:
        conn.close()
    except OSError:
        pass
