"""Nova's native-messaging host (SPEC §9): Chrome's stdio frames <-> Nova's named pipe. Standard library only.

Chrome starts this through ``nova_host.bat`` when the extension connects. It reads the pipe name and key from
``data/chrome-bridge.json`` (written by ``nova chrome --install``; ``NOVA_BRIDGE_FILE`` overrides it for tests),
connects with ``multiprocessing.connection`` (HMAC challenge both ways), and relays raw JSON both ways:
Chrome sends ``<4-byte little-endian length><UTF-8 JSON>`` on stdin and reads the same on stdout. Nova not
running -> exit at once (the extension tries again later). Either side closing -> exit.
"""

from __future__ import annotations

import json
import os
import struct
import sys
import threading
import time
from multiprocessing.connection import Client
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONNECT_FOR_S = 1.5  # Nova may be starting: keep trying this long
MAX_TO_CHROME = 1024 * 1024  # Chrome refuses bigger messages from a host
_LOG = ROOT / "logs" / "chrome-host.log"


def frame(body: bytes) -> bytes:
    """One native message: little-endian length + the bytes."""
    return struct.pack("<I", len(body)) + body


def read_exact(stream, n: int) -> bytes | None:
    data = b""
    while len(data) < n:
        chunk = stream.read(n - len(data))
        if not chunk:
            return None
        data += chunk
    return data


def read_frame(stream) -> bytes | None:
    """The next native message from Chrome, or None at the end of input."""
    head = read_exact(stream, 4)
    if head is None:
        return None
    (size,) = struct.unpack("<I", head)
    return read_exact(stream, size) if size else b""


def too_big(body: bytes) -> bytes:
    """Chrome drops a host message over 1 MB (and the port with it): answer the request with an error instead."""
    try:
        request_id = json.loads(body.decode("utf-8")).get("id")
    except (ValueError, AttributeError, UnicodeDecodeError):
        request_id = None
    return json.dumps({"id": request_id, "ok": False, "error": "answer too big for Chrome"}).encode("utf-8")


def _log(text: str) -> None:
    try:
        if _LOG.exists() and _LOG.stat().st_size > 256_000:
            _LOG.unlink()
        with _LOG.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n")
    except OSError:
        pass


def _connect(info: dict):
    deadline = time.monotonic() + CONNECT_FOR_S
    key = bytes.fromhex(info["key"])
    while True:
        try:
            return Client(info["pipe"], family="AF_PIPE", authkey=key)
        except FileNotFoundError:  # no pipe: Nova isn't running (yet)
            if time.monotonic() > deadline:
                return None
            time.sleep(0.15)
        except OSError as error:  # busy (another host is connecting): try again
            if time.monotonic() > deadline:
                _log(f"can't connect to Nova: {error}")
                return None
            time.sleep(0.05)


def main() -> int:
    if sys.platform == "win32":
        import msvcrt

        msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
        msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
    source = Path(os.environ.get("NOVA_BRIDGE_FILE") or ROOT / "data" / "chrome-bridge.json")
    try:
        info = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        _log(f"no bridge settings ({source}): {error} - run: nova chrome --install")
        return 1
    try:
        conn = _connect(info)
    except Exception as error:  # noqa: BLE001 - wrong key etc.: say why in the log, exit
        _log(f"Nova refused the connection: {error!r}")
        return 1
    if conn is None:
        return 0
    stdin, stdout = sys.stdin.buffer, sys.stdout.buffer

    def chrome_to_nova() -> None:
        try:
            while True:
                body = read_frame(stdin)
                if body is None:
                    break
                conn.send_bytes(body)
        except (OSError, EOFError):
            pass
        os._exit(0)  # Chrome closed the port (or Nova went away): the other thread goes too

    threading.Thread(target=chrome_to_nova, name="chrome-to-nova", daemon=True).start()
    try:
        while True:
            body = conn.recv_bytes()
            if len(body) > MAX_TO_CHROME:
                body = too_big(body)
            stdout.write(frame(body))
            stdout.flush()
    except (OSError, EOFError):
        pass
    os._exit(0)


if __name__ == "__main__":
    sys.exit(main())
