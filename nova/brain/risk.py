"""Risk detection (SPEC §14). Pure: a step + what's on screen -> nothing, "ask first", or "never".

Confirm in the island before: sending messages, submit / turn in, post / publish / comment,
delete / remove / trash, buy / order / pay / checkout, closing several windows at once
(and any handoff with content he didn't speak — Stage 09). Never: type into password
fields, enter card / bank / ID numbers, run shell commands, delete files, act while the
workstation is locked. Detection = action type + label / URL / window title + app. In
doubt -> confirm. (A close that brings up an unsaved-work prompt is left to that prompt:
the executor shows it and never clicks it.)

Page clicks are judged on the element the page map resolved (its own name: "the blue button"
may be "Submit Assignment"), plus a form's submit button while paying or messaging; page
fills refuse password / card fields and ask before submitting anything but a search box.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .plan import Step

_MESSAGING = re.compile(
    r"\b(teams|slack|discord|whatsapp|telegram|signal|messenger|outlook|gmail|thunderbird|messages|skype|zoom"
    r"|groupme|imessage|inbox|mail|chat)\b",
    re.IGNORECASE,
)
_MESSAGING_URL = re.compile(
    r"mail\.google\.com|outlook\.(live|office)\.com|teams\.microsoft\.com|slack\.com|discord\.com|web\.whatsapp\.com"
    r"|messenger\.com|instagram\.com/direct|/conversations|linkedin\.com/messaging",
    re.IGNORECASE,
)
_AI_APPS = re.compile(r"\b(claude|chatgpt|openai|codex)\b", re.IGNORECASE)  # his own words to an AI: SPEC §13
_TERMINALS = frozenset({
    "windowsterminal.exe", "openconsole.exe", "conhost.exe", "cmd.exe", "powershell.exe", "pwsh.exe", "wsl.exe",
    "bash.exe", "mintty.exe", "wt.exe",
})
_TERMINAL_CLASSES = frozenset({"CASCADIA_HOSTING_WINDOW_CLASS", "ConsoleWindowClass", "mintty"})
_FILE_MANAGERS = re.compile(r"^(explorer\.exe)$", re.IGNORECASE)
_RISKY = re.compile(
    r"\b(submit|turn\s*in|send|post|publish|comment|reply|delete|remove|trash|discard|buy|order|pay|checkout"
    r"|check\s*out|purchase|place\s+order|unsubscribe|unenroll|drop\s+(the\s+)?course|sign\s+out\s+everywhere"
    r"|tweet|retweet|donate|transfer|withdraw|subscribe|book\s+now|reserve\s+now)\b",
    re.IGNORECASE,
)
_SAFE_LABELS = re.compile(
    r"^(sent|send\s*later|drafts?|outbox|sent\s+folder|sent\s+items|order\s+(history|details|status|summary|number)"
    r"|track\s+(your\s+)?order|your\s+orders?)\b",
    re.IGNORECASE,
)
_CHECKOUT_URL = re.compile(r"checkout|/cart\b|/basket|payment|/buy\b|/order|/purchase|billing", re.IGNORECASE)
_CANVAS_URL = re.compile(r"(^|//)canvas\.|instructure\.com", re.IGNORECASE)
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_DIGIT_RUN = re.compile(r"\d[\d -]{11,}\d")
_BANKING = re.compile(r"\b(routing|account|iban|swift|acct)\b", re.IGNORECASE)
_LONG_NUMBER = re.compile(r"\b\d{8,17}\b")


@dataclass(frozen=True)
class Scene:
    """What's on screen, as far as risk goes."""

    app: str = ""  # the foreground app's catalog name
    exe: str = ""  # its process file name
    window_class: str = ""
    title: str = ""
    url: str = ""  # Chrome's active tab (Stage 06)
    password: bool = False  # the focused control is a password field
    locked: bool = False
    windows: int = 0  # close_app: how many windows the app has open
    focus_known: bool = True  # type_text / keys that enter text: UI Automation said what the focused control is


@dataclass(frozen=True)
class Risk:
    kind: str  # confirm | refuse
    question: str = ""  # confirm: the exact action for the island ("Send “running late”?")
    reason: str = ""  # refuse: plain words why not

    @property
    def confirm(self) -> bool:
        return self.kind == "confirm"

    @property
    def refuse(self) -> bool:
        return self.kind == "refuse"


def _confirm(question: str) -> Risk:
    return Risk("confirm", question=question)


def _refuse(reason: str) -> Risk:
    return Risk("refuse", reason=reason)


def risky_label(label: str) -> bool:
    """A button / link label that sends, submits, posts, deletes or pays."""
    text = " ".join(label.split())
    return bool(text) and not _SAFE_LABELS.match(text) and bool(_RISKY.search(text))


def messaging(scene: Scene) -> bool:
    if _AI_APPS.search(scene.app) or _AI_APPS.search(scene.exe):
        return False
    if scene.url:
        return bool(_MESSAGING_URL.search(scene.url))
    return bool(_MESSAGING.search(f"{scene.app} {scene.exe.removesuffix('.exe')} {scene.title}"))


def terminal(scene: Scene) -> bool:
    return scene.exe.lower() in _TERMINALS or scene.window_class in _TERMINAL_CLASSES


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
    return total % 10 == 0


def sensitive_number(text: str) -> bool:
    """Card numbers (13-19 digits passing Luhn), US SSNs, and bank account / routing numbers."""
    if _SSN.search(text):
        return True
    for run in _DIGIT_RUN.findall(text):
        digits = re.sub(r"\D", "", run)
        if 13 <= len(digits) <= 19 and _luhn(digits):
            return True
    return bool(_BANKING.search(text) and _LONG_NUMBER.search(text))


_KEY_MODIFIERS = frozenset({"ctrl", "shift", "alt", "win"})
_PASTES = {"v": ({"ctrl"}, {"ctrl", "shift"}, {"win"}), "insert": ({"shift"},)}  # Win+V: the clipboard history
_CHARACTER_MODIFIERS = (frozenset(), {"shift"}, {"ctrl", "alt"}, {"ctrl", "alt", "shift"})  # Ctrl+Alt = AltGr


def _chord(keys: str) -> tuple[frozenset[str], str] | None:
    """(modifiers, key) of "ctrl+shift+v", in any order; None unless there is exactly one other key."""
    parts = [part.strip() for part in keys.lower().split("+") if part.strip()]
    rest = [part for part in parts if part not in _KEY_MODIFIERS]
    return (frozenset(parts) & _KEY_MODIFIERS, rest[0]) if len(rest) == 1 else None


def pastes(keys: str) -> bool:
    chord = _chord(keys)
    return chord is not None and chord[0] in _PASTES.get(chord[1], ())


def enters_text(keys: str) -> bool:
    """Does this chord put characters into the focused field: a paste, or a character key on its own / with
    Shift / with AltGr? (Enter, Tab, Backspace, arrows and shortcuts like Ctrl+T enter nothing.)"""
    chord = _chord(keys)
    if chord is None:
        return False
    mods, key = chord
    return pastes(keys) or ((len(key) == 1 or key == "space") and mods in _CHARACTER_MODIFIERS)


def needs_focus(s: Step) -> bool:
    """Must the hands look at the focused control (UI Automation) before this step: is it about to get text?"""
    return s.action == "type_text" or (s.action == "press" and enters_text(str(s.get("keys", ""))))


def _quote(text: str, limit: int = 40) -> str:
    text = " ".join(text.split())
    return f"“{text if len(text) <= limit else text[: limit - 1] + '…'}”"


def assess_dictation(text: str, scene: Scene) -> Risk | None:
    """Dictated text about to be pasted (SPEC §11, §14): never into a password field or a terminal (a line break
    there runs a command), never card / bank / ID numbers, never while locked. No questions: he is talking."""
    if scene.locked:
        return _refuse("The screen is locked.")
    if scene.password:
        return _refuse("That's a password field — I never type there.")
    if terminal(scene):
        return _refuse("I don't type into terminals — no shell commands from voice.")
    if sensitive_number(text):
        return _refuse("I don't enter card, bank or ID numbers.")
    return None


def assess(s: Step, scene: Scene) -> Risk | None:
    """None = go ahead. Risk.confirm = ask in the island first. Risk.refuse = never."""
    if scene.locked:
        return _refuse("The screen is locked.")
    action = s.action
    if action == "type_text":
        text = str(s.get("text", ""))
        if scene.password:
            return _refuse("That's a password field — I never type there.")
        if terminal(scene):
            return _refuse("I don't type into terminals — no shell commands from voice.")
        if sensitive_number(text):
            return _refuse("I don't enter card, bank or ID numbers.")
        if s.get("submit") and messaging(scene):
            return _confirm(f"Send {_quote(text)}?")
        if not scene.focus_known:
            return _confirm(f"Type {_quote(text)} here? (I couldn't check this field)")
        return None
    if action == "press":
        keys = str(s.get("keys", "")).lower()
        if keys == "win+r":
            return _refuse("I don't run commands — no Run box from voice.")
        if keys in ("delete", "shift+delete") and _FILE_MANAGERS.match(scene.exe):
            return _refuse("I don't delete files.")
        if keys in ("enter", "ctrl+enter"):
            if terminal(scene):
                return _refuse("I don't run commands in terminals.")
            if messaging(scene):
                return _confirm("Send it?")
        if enters_text(keys):  # keys are an input path too (SPEC §14): a paste would put the clipboard there
            if scene.password:
                return _refuse("That's a password field — I never type there.")
            if not scene.focus_known:
                what = "Paste" if pastes(keys) else f"Press {keys.replace('+', ' + ')}"
                return _confirm(f"{what} here? (I couldn't check this field)")
        return None
    if action == "close_app":
        if scene.windows > 1:
            return _confirm(f"Close all {scene.windows} {s.get('app')} windows?")
        return None
    if action in ("page_click", "ui_click"):
        # once the page map has resolved it, the element's own name is what gets clicked ("the blue button" may be
        # "Submit Assignment")
        label = str(s.get("label") or s.get("description") or s.get("element") or "")
        where = " on Canvas" if scene.url and _CANVAS_URL.search(scene.url) else ""
        if risky_label(label):
            return _confirm(f"Click {_quote(label)}{where}?")
        if s.get("submits") and scene.url and (_CHECKOUT_URL.search(scene.url) or messaging(scene)):
            return _confirm(f"Click {_quote(label)}{where}?")  # a form's submit button while paying / messaging
        return None
    if action == "page_fill":
        text = str(s.get("text", ""))
        if s.get("secret") or scene.password:
            return _refuse("That's a password field — I never type there.")
        if sensitive_number(text):
            return _refuse("I don't enter card, bank or ID numbers.")
        if s.get("submit") and not s.get("search"):
            return _confirm(f"Send {_quote(text)}?" if messaging(scene) else f"Submit {_quote(text)}?")
        return None
    return None
