"""Stage 06 self-test: the real brain (Tier 0 [+ Tier 1 with --llm] -> executor -> hands -> extension) on the local
test page and a fake Canvas, in Chrome for Testing with the extension loaded (a throwaway profile; your Chrome is
not touched). Prints each command's result and the page-map / click latencies.

    uv run python extension/test/selftest.py [--llm] [--chrome PATH] [--keep]

Needs: Chrome for Testing (Playwright's, found automatically) and the tray app not running (it holds the pipe).
Registers the native host for Chrome for Testing while it runs and removes it after. Serves the test files and the
fake Canvas on 127.0.0.1 (a random port) for the run only.
"""

from __future__ import annotations

import argparse
import functools
import http.server
import json
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from nova import paths  # noqa: E402
from nova.actions import native  # noqa: E402
from nova.actions.catalog import Catalog, CatalogStore  # noqa: E402
from nova.actions.chrome import Chrome, ChromeHands  # noqa: E402
from nova.actions.native import ActionError  # noqa: E402
from nova.actions.context import snapshot  # noqa: E402
from nova.actions.hands import NovaControls, WindowsHands  # noqa: E402
from nova.brain.brain import Command, handle  # noqa: E402
from nova.brain.executor import Executor  # noqa: E402
from nova.brain.router import Router  # noqa: E402
from nova.chrome import install  # noqa: E402
from nova.chrome.bridge import Bridge, BridgeInfo, TabInfo  # noqa: E402

TEST_DIR = Path(__file__).resolve().parent


def find_cft() -> Path | None:
    base = Path.home() / "AppData" / "Local" / "ms-playwright"
    found = sorted(base.glob("chromium-*/chrome-win64/chrome.exe"), key=lambda p: p.parent.parent.name, reverse=True)
    return found[0] if found else None


# --- a fake Canvas next to the test page ---------------------------------------------------------------------------


def planner_rows(now: datetime) -> list[dict]:
    def at(days: int, hour: int, minute: int = 0) -> str:
        local = (now + timedelta(days=days)).replace(hour=hour, minute=minute, second=0, microsecond=0)
        return local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def row(pid, title, due, course, submitted=False, kind="assignment"):
        folder = "quizzes" if kind == "quiz" else "assignments"
        return {"plannable_type": kind, "plannable_id": pid, "course_id": 11, "context_name": course,
                "html_url": f"/courses/11/{folder}/{pid}", "plannable_date": due,
                "plannable": {"id": pid, "title": title, "due_at": due}, "submissions": {"submitted": submitted}}

    return [row(501, "HW 5", at(0, 23, 59), "ME 3124 Thermodynamics"),
            row(9, "Reading response", at(0, 12), "ENGL 1106", submitted=True),
            row(302, "Lab 3 report", at(2, 17), "PHYS 2306 Foundations of Physics"),
            row(77, "Quiz 2", at(6, 9), "MATH 2214 Differential Equations", kind="quiz"),
            row(502, "HW 6", at(8, 23, 59), "ME 3124 Thermodynamics")]


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(TEST_DIR), **kwargs)

    def log_message(self, *args):  # quiet
        pass

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except ConnectionError:
            pass

    def do_GET(self):  # noqa: N802 - http.server's name
        if self.path.startswith("/api/v1/planner/items"):
            body = ("while(1);" + json.dumps(planner_rows(datetime.now().astimezone()))).encode()
            self._send(200, "application/json; charset=utf-8", body)
        elif self.path.startswith("/courses/"):
            title = self.path.rsplit("/", 1)[-1]
            self._send(200, "text/html", f"<!doctype html><title>Item {title}</title><h1>Item {title}</h1>".encode())
        elif self.path == "/canvas" or self.path == "/canvas/":
            self._send(200, "text/html", b"<!doctype html><title>Dashboard</title><h1>Dashboard</h1>")
        else:
            super().do_GET()

    def _send(self, status: int, kind: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# --- answers to the island's questions, scripted ---------------------------------------------------------------------


class Scripted:
    def __init__(self):
        self.confirms: list[bool | None] = []
        self.choices: list[int | None] = []
        self.asked: list[str] = []

    def confirm(self, question):
        self.asked.append(question)
        return self.confirms.pop(0) if self.confirms else None

    def choose(self, question, choices):
        self.asked.append(f"{question} {list(choices)}")
        return self.choices.pop(0) if self.choices else None

    def answer(self, question):
        self.asked.append(question)
        return None


def main() -> int:
    sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--llm", action="store_true", help="also loose phrasings through Tier 1 (Ollama)")
    parser.add_argument("--chrome", type=Path, help="Chrome for Testing / Chromium chrome.exe")
    parser.add_argument("--keep", action="store_true", help="leave Chrome open at the end")
    args = parser.parse_args()
    cft = args.chrome or find_cft()
    if cft is None or not cft.is_file():
        print("Chrome for Testing not found (pass --chrome PATH)")
        return 2
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    bridge = Bridge(BridgeInfo.ensure(paths.CHROME_BRIDGE))
    if not bridge.start():
        print("The pipe is taken — quit the tray app first.", bridge.error)
        return 2
    had_key = install._registered("testing") is not None
    install.install(("testing",))
    profile = Path(tempfile.mkdtemp(prefix="nova-selftest-"))
    chrome = subprocess.Popen([str(cft), f"--user-data-dir={profile}", f"--load-extension={ROOT / 'extension'}",
                               "--disable-features=DisableLoadExtensionCommandLineSwitch", "--no-first-run",
                               "--no-default-browser-check", "--window-size=1100,900", f"{base}/page.html"])
    failures = 0
    try:
        if not bridge.wait_connected(30):
            print("The extension never connected.")
            return 1
        native.com_init()
        store = CatalogStore(paths.CATALOG_CACHE, paths.ALIASES)
        store.load_cached()
        real = store.current()
        aliases = {phrase: target.url or target.name for phrase, target in real.aliases}
        catalog = Catalog(real.apps, {**aliases, "canvas": f"{base}/canvas"})  # the fake Canvas
        api = Chrome(bridge, lambda: catalog)
        hands_chrome = ChromeHands(api)
        hands = WindowsHands(lambda: catalog, nova=NovaControls(), chrome=hands_chrome)
        asker = Scripted()
        executor = Executor(hands, asker)
        if args.llm:
            from nova.say import brain_parts

            _, tier1 = brain_parts(store)
            router = Router(tier1, page=hands_chrome.page_rows)
        else:
            router = Router(page=hands_chrome.page_rows)
        tab = TabInfo.of(bridge.request("tabs.active")["tab"])  # the hello may not be in yet
        if not tab.url.startswith(base):  # Chrome for Testing sometimes shows a page of its own first
            bridge.request("tabs.navigate", {"tabId": tab.id, "url": f"{base}/page.html"})
        api.wait(tab, timeout=10, url_prefix=f"{base}/page.html")
        api.front(tab)
        time.sleep(0.5)

        maps = []
        for _ in range(15):
            t = time.perf_counter()
            api.page_map()
            maps.append((time.perf_counter() - t) * 1000)

        def log_shows(text: str) -> bool:
            try:
                return bool(api.call("wait", {"text": text, "timeout": 1500}, timeout=3).get("ok"))
            except ActionError as error:
                print(f"      (couldn't read the page: {error})")
                return False

        clicks: list[float] = []
        totals: list[float] = []

        def run(said, expect=None, *, confirms=(), choices=(), want_ok=True, check=None):
            nonlocal failures
            asker.confirms, asker.choices, asker.asked = list(confirms), list(choices), []
            began = time.perf_counter()
            result, record = handle(Command(said, via="say", at=began), catalog, executor, router=router, asker=asker,
                                    snap=lambda c: snapshot(c, bridge))
            ms = (time.perf_counter() - began) * 1000
            ok = bool(result and result.ok) == want_ok
            if expect is not None:
                ok = ok and log_shows(expect)
            if check is not None:
                ok = ok and check(record)
            failures += 0 if ok else 1
            if record["type"] == "page_click" and result and result.ok:
                clicks.append(ms)
            totals.append(ms)
            plan = " ; ".join(f"{s['action']}" + (f"<{s['element']}>" if s.get("element") else "") for s in record["plan"])
            asked = f"  asked: {asker.asked}" if asker.asked else ""
            print(f"{'PASS' if ok else 'FAIL'}  {said!r:52} {ms:6.0f} ms  [{record['tier']}] {plan} -> {record['detail']}{asked}")

        run("click sign in", "sign in (top)")
        run("click the sign in button in the log in form", "sign in (form)")
        run("click settings", "settings")
        run("click shadow action", "shadow")
        run("click the frame button", "frame button")
        run("click remember me", "remember true")
        run("type bhavya into the username field", "typed bhavya")
        run("type hunter2 into the password field", want_ok=False)
        run("type thermo into the search box and press enter", "searched thermo")
        run("click the second reply button", "reply 2", confirms=[True])  # "Reply" asks first
        run("click submit assignment", confirms=[False], want_ok=False,
            check=lambda r: r["cancelled"] and r["asked"] == "confirm")
        run("click submit assignment", "form submitted", confirms=[True])
        run("click delete draft", "deleted", confirms=[True])
        run("scroll down")
        run("scroll to the comments")
        run("scroll to the top")
        run("click purple elephant", want_ok=False, check=lambda r: r["detail"].startswith("Couldn't find"))
        run("go to canvas", check=lambda r: r["detail"].startswith("Opened"))
        run("what's due this week", choices=[None], check=lambda r: r["detail"].startswith("3 due this week"))
        run("click on my assignment for today", check=lambda r: r["detail"] == "Opened HW 5")
        run("open my next assignment", check=lambda r: r["detail"] == "Opened HW 5")
        run("open the thermo homework", check=lambda r: r["detail"] == "Opened HW 5")
        run("what's due next week", choices=[1], check=lambda r: r["detail"] == "Opened HW 6")
        run("open my assignment for saturday", check=lambda r: r["detail"].startswith("Nothing due"))
        run("switch to the nova test page tab", check=lambda r: r["detail"].startswith("Switched to"))
        failures += dictation_checks(api, catalog, base)
        if args.llm:
            run("scroll to the top")
            time.sleep(1.0)  # let the smooth scroll finish: "up top" means what's on screen at the top
            run("hit that sign in thing up top", "sign in (top)")
            run("could you take me down to where the comments are")
            run("do I have anything for physics due this week", choices=[0],
                check=lambda r: r["detail"] == "Opened Lab 3 report")
        print()
        print(f"page.map round trip (test page, {len(maps)}x): p50 {statistics.median(maps):.1f} ms, "
              f"max {max(maps):.1f} ms")
        if clicks:
            print(f"page click, whole command (Tier 0 + map + rank + 300 ms highlight + click + verify): "
                  f"p50 {statistics.median(clicks):.0f} ms, max {max(clicks):.0f} ms over {len(clicks)}")
        print(f"{failures} failed")
        return 1 if failures else 0
    finally:
        if not args.keep:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(chrome.pid)], capture_output=True)
        bridge.close()
        server.shutdown()
        if not had_key:
            install.uninstall(("testing",))
        time.sleep(0.5)
        shutil.rmtree(profile, ignore_errors=True)


def dictation_checks(api: Chrome, catalog: Catalog, base: str) -> int:
    """Stage 07: the caret in a contenteditable / password field / textarea through the extension, then dictation
    (rules only, a real clipboard paste into this Chrome) landing in the textarea, with "scratch that"."""
    from nova.config import Llm
    from nova.dictation.cleanup import Cleaner
    from nova.dictation.history import History
    from nova.dictation.session import Dictation
    from nova.dictation.target import Target

    failures = 0

    def check(name: str, ok: bool, detail: object = "") -> None:
        nonlocal failures
        failures += 0 if ok else 1
        print(f"{'PASS' if ok else 'FAIL'}  {name:52} {detail}")

    tab = api.active_tab()
    if tab is None or not tab.url.startswith(f"{base}/page.html"):
        tab, _ = api.open(f"{base}/page.html", reuse=True)
        tab = api.wait(tab, timeout=10)
    api.front(tab)
    view = api.page_map()
    named = {e.name: e for e in view.elements}
    notes, draft, password = named.get("Notes"), named.get("Draft"), named.get("Password")
    if notes is None or draft is None or password is None:
        check("the test page's fields are mapped", False, sorted(named)[:12])
        return failures
    api.call("fill", "$n", "Meeting notes: we tested it with", {}, element=notes.id)
    began = time.perf_counter()
    caret = api.caret()
    check("caret: contenteditable", caret.get("before") == "Meeting notes: we tested it with",
          f"{(time.perf_counter() - began) * 1000:.0f} ms {caret.get('before')!r}")
    api.click(password.id, "Password")
    caret = api.caret()
    check("caret: a password field says so", caret.get("secret") is True, caret.get("secret"))
    api.call("fill", "$n", "", {}, element=draft.id)
    caret = api.caret()
    check("caret: an empty textarea", caret.get("before") == "" and caret.get("editable"), caret)
    target = Target(lambda: catalog, api)
    spot = target.look()
    check("look: Chrome, the textarea", spot.kind == "chrome" and spot.before == "", f"{spot.ms:.0f} ms {spot.kind}")
    history = History(Path(tempfile.mkdtemp(prefix="nova-dictation-")) / "history.jsonl")
    dictation = Dictation(None, Cleaner(None, Llm, lambda: (), enabled=lambda: False), target, history)
    dictation.start()
    try:
        began = time.perf_counter()
        dictation.begin("selftest", "um, hello from nova. new paragraph. second line, scratch that, the second line.")
        dictation.release()
        dictation.drain(15)
        ms = (time.perf_counter() - began) * 1000
        caret = api.caret()
        want = "Hello from nova.\n\nThe second line."
        check("dictation lands in the textarea, cleaned", caret.get("before") == want,
              f"{ms:.0f} ms {caret.get('before')!r}")
        dictation.begin("selftest", "and that is all.")
        dictation.release()
        dictation.drain(15)
        caret = api.caret()
        check("the next segment joins with a space", caret.get("before") == want + " And that is all.",
              repr(caret.get("before")))
    finally:
        dictation.close()
    return failures


if __name__ == "__main__":
    sys.exit(main())
