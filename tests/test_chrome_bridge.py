"""The Chrome bridge (SPEC §9): request / reply by id, tab events, several profiles, the host's framing, install."""

import importlib.util
import io
import json
import struct
import threading
from multiprocessing import Pipe

import pytest

from nova import doctor
from nova.chrome import install
from nova.chrome.bridge import Bridge, BridgeInfo, ChromeError

ROOT = install.EXTENSION.parent


class FakeExtension:
    """The other end of the pipe: answers requests like the extension's worker would."""

    def __init__(self, conn, answers=None):
        self.conn = conn
        self.answers = answers or {}
        self.seen = []
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def send(self, message):
        self.conn.send_bytes(json.dumps(message).encode())

    def _serve(self):
        while True:
            try:
                message = json.loads(self.conn.recv_bytes())
            except (EOFError, OSError):
                return
            self.seen.append(message)
            answer = self.answers.get(message["cmd"])
            if answer is None:
                continue  # never answers: the request times out
            reply = answer(message.get("args") or {}) if callable(answer) else answer
            self.send({"id": message["id"], **reply})


def connect(bridge, answers=None):
    ours, theirs = Pipe()
    bridge.attach(ours)
    return FakeExtension(theirs, answers)


def test_requests_get_their_own_replies():
    bridge = Bridge(BridgeInfo("unused", "00"))
    ext = connect(bridge, {"ping": {"ok": True, "result": {"version": "0.6.0"}},
                           "echo": lambda args: {"ok": True, "result": args}})
    assert bridge.wait_connected(1.0)
    assert bridge.request("ping") == {"version": "0.6.0"}
    assert bridge.request("echo", {"x": 1}) == {"x": 1}
    assert [m["cmd"] for m in ext.seen] == ["ping", "echo"]


def test_errors_and_timeouts_are_plain_words():
    bridge = Bridge(BridgeInfo("unused", "00"))
    connect(bridge, {"page.map": {"ok": False, "error": "Cannot access a chrome:// URL"}})
    with pytest.raises(ChromeError, match="doesn't let extensions"):
        bridge.request("page.map")
    with pytest.raises(ChromeError, match="in time"):
        bridge.request("silent", timeout=0.1)


def test_not_connected():
    with pytest.raises(ChromeError, match="isn't connected"):
        Bridge(BridgeInfo("unused", "00")).request("ping")


def test_tab_events_keep_the_active_tab_without_a_round_trip():
    bridge = Bridge(BridgeInfo("unused", "00"))
    ext = connect(bridge)
    ext.send({"event": "hello", "version": "0.6.0", "focused": True,
              "tab": {"id": 5, "windowId": 1, "url": "https://canvas.vt.edu/", "title": "Dashboard"}})
    ext.send({"event": "tab", "focused": True, "tab": {"id": 6, "windowId": 1, "url": "https://x.org/", "title": "X"}})
    deadline = threading.Event()
    for _ in range(100):
        tab = bridge.active_tab()
        if tab is not None and tab.id == 6:
            break
        deadline.wait(0.01)
    assert bridge.active_tab().url == "https://x.org/" and bridge.chrome_focused()
    ext.send({"event": "focus", "focused": False})
    for _ in range(100):
        if not bridge.chrome_focused():
            break
        deadline.wait(0.01)
    assert not bridge.chrome_focused() and bridge.active_tab().id == 6


def test_the_focused_profile_answers():
    bridge = Bridge(BridgeInfo("unused", "00"))
    work = connect(bridge, {"ping": {"ok": True, "result": "work"}})
    home = connect(bridge, {"ping": {"ok": True, "result": "home"}})
    home.send({"event": "hello", "focused": False, "tab": {"id": 1, "url": "https://a/", "title": "A"}})
    work.send({"event": "hello", "focused": True, "tab": {"id": 2, "url": "https://b/", "title": "B"}})
    for _ in range(200):
        if bridge.active_tab() is not None and bridge.active_tab().id == 2:
            break
        threading.Event().wait(0.01)
    assert bridge.request("ping") == "work"


def test_a_closed_connection_fails_its_requests():
    bridge = Bridge(BridgeInfo("unused", "00"))
    ext = connect(bridge)
    result = {}

    def ask():
        try:
            bridge.request("never", timeout=3.0)
        except ChromeError as error:
            result["error"] = str(error)

    thread = threading.Thread(target=ask)
    thread.start()
    for _ in range(100):
        if ext.seen:
            break
        threading.Event().wait(0.01)
    ext.conn.close()
    thread.join(3.0)
    assert "disconnected" in result["error"].lower() or "connection" in result["error"].lower()


def test_bridge_info_is_made_once(tmp_path):
    path = tmp_path / "chrome-bridge.json"
    first = BridgeInfo.ensure(path)
    assert first.pipe.startswith("\\\\.\\pipe\\nova-chrome-") and len(first.key) == 64
    assert BridgeInfo.ensure(path) == first
    assert BridgeInfo.load(tmp_path / "missing.json") is None


# --- the native host's framing (extension/host/nova_host.py, standard library only) -------------------------------------


def _host():
    spec = importlib.util.spec_from_file_location("nova_host", ROOT / "extension" / "host" / "nova_host.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_native_message_framing():
    host = _host()
    body = json.dumps({"id": 1, "cmd": "ping"}).encode()
    framed = host.frame(body)
    assert framed[:4] == struct.pack("<I", len(body))
    stream = io.BytesIO(framed + host.frame(b"{}"))
    assert host.read_frame(stream) == body and host.read_frame(stream) == b"{}" and host.read_frame(stream) is None
    assert host.read_frame(io.BytesIO(framed[:6])) is None  # cut short: the end


def test_answers_too_big_for_chrome_become_errors():
    reply = json.loads(_host().too_big(json.dumps({"id": 7, "ok": True, "result": "x" * 10}).encode()))
    assert reply == {"id": 7, "ok": False, "error": "answer too big for Chrome"}


# --- install ------------------------------------------------------------------------------------------------------------


def test_extension_id_comes_from_the_manifest_key():
    assert install.extension_id(install.manifest_key()) == "dnhapjgbmkmjofkmgpjoaidnhadgnkhf"


def test_host_manifest_only_lets_novas_extension_in():
    data = install.host_manifest(install.HOST_BAT, "abc")
    assert data["name"] == "com.nova.bridge" and data["type"] == "stdio"
    assert data["allowed_origins"] == ["chrome-extension://abc/"] and data["path"].endswith("nova_host.bat")


def test_loaded_in_reads_chrome_profiles(tmp_path):
    (tmp_path / "Default").mkdir()
    (tmp_path / "Default" / "Secure Preferences").write_text(json.dumps({"extensions": {"settings": {"abc": {}}}}))
    (tmp_path / "Profile 1").mkdir()
    (tmp_path / "Profile 1" / "Preferences").write_text("{not json")
    (tmp_path / "System Profile").mkdir()
    assert install.loaded_in("abc", tmp_path) == ["Default"]
    assert install.loaded_in("zzz", tmp_path) == []


def test_doctor_row():
    ok = doctor.check_extension(lambda: install.Status(True, "loaded (Default)"))
    assert ok.ok and ok.name == "Chrome extension"
    bad = doctor.check_extension(lambda: install.Status(False, "not loaded", install.MANUAL_STEP))
    assert not bad.ok and "Load unpacked" in bad.fix
