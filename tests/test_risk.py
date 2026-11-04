"""Risk detection (SPEC §14): what needs a Yes in the island, and what Nova never does."""

import pytest

from nova.brain.plan import step
from nova.brain.risk import Scene, assess, risky_label

NOTEPAD = Scene(app="Notepad", exe="notepad.exe", window_class="Notepad", title="notes.txt - Notepad")
TEAMS = Scene(app="Microsoft Teams", exe="ms-teams.exe", title="Chat | Lab group | Microsoft Teams")
GMAIL = Scene(app="Google Chrome", exe="chrome.exe", title="Inbox (3) - b@vt.edu - Gmail - Google Chrome")
CLAUDE = Scene(app="Claude", exe="claude.exe", title="Claude")
TERMINAL = Scene(app="Terminal", exe="WindowsTerminal.exe", window_class="CASCADIA_HOSTING_WINDOW_CLASS")
EXPLORER = Scene(app="File Explorer", exe="explorer.exe", window_class="CabinetWClass", title="Downloads")


def test_everyday_actions_need_no_confirmation():
    assert assess(step("type_text", text="hello world", submit=True), NOTEPAD) is None
    assert assess(step("press", keys="enter"), NOTEPAD) is None
    assert assess(step("open_app", app="Google Chrome"), NOTEPAD) is None
    assert assess(step("window", op="close", target="active"), NOTEPAD) is None
    assert assess(step("close_app", app="Notepad"), Scene(windows=1)) is None
    assert assess(step("type_text", text="Explain entropy simply", submit=True), CLAUDE) is None  # his own words to Claude


@pytest.mark.parametrize("scene", [TEAMS, GMAIL, Scene(app="Discord", exe="Discord.exe", title="#general - Discord")])
def test_sending_a_message_is_confirmed(scene):
    risk = assess(step("type_text", text="running late", submit=True), scene)
    assert risk is not None and risk.confirm and "running late" in risk.question
    assert assess(step("press", keys="enter"), scene).confirm
    assert assess(step("type_text", text="running late", submit=False), scene) is None


def test_closing_several_windows_is_confirmed():
    risk = assess(step("close_app", app="Google Chrome"), Scene(windows=3))
    assert risk.confirm and risk.question == "Close all 3 Google Chrome windows?"


def test_never_types_into_password_fields():
    risk = assess(step("type_text", text="hunter2"), Scene(app="Google Chrome", password=True))
    assert risk.refuse and "password" in risk.reason


@pytest.mark.parametrize(
    "text",
    ["4111 1111 1111 1111", "my card is 4111-1111-1111-1111", "123-45-6789", "routing 021000021 account 12345678901"],
)
def test_never_enters_card_bank_or_id_numbers(text):
    assert assess(step("type_text", text=text), NOTEPAD).refuse


def test_ordinary_numbers_are_fine():
    assert assess(step("type_text", text="Room 1234, call 540-555-0100"), NOTEPAD) is None
    assert assess(step("type_text", text="ME 4524 homework 5"), NOTEPAD) is None


def test_no_shell_commands_and_no_file_deletion():
    assert assess(step("type_text", text="dir"), TERMINAL).refuse
    assert assess(step("press", keys="enter"), TERMINAL).refuse
    assert assess(step("press", keys="win+r"), NOTEPAD).refuse
    assert assess(step("press", keys="delete"), EXPLORER).refuse
    assert assess(step("press", keys="shift+delete"), EXPLORER).refuse
    assert assess(step("press", keys="delete"), NOTEPAD) is None  # deleting text is fine


def test_nothing_runs_while_locked():
    assert assess(step("open_app", app="Google Chrome"), Scene(locked=True)).refuse


@pytest.mark.parametrize(
    "label",
    ["Submit Assignment", "Turn in", "Send", "Post", "Publish", "Delete", "Remove from cart", "Move to trash", "Buy now",
     "Place order", "Pay $20", "Checkout", "Reply all", "Comment"],
)
def test_risky_labels(label):
    assert risky_label(label)
    assert assess(step("page_click", description=label), GMAIL).confirm


@pytest.mark.parametrize("label", ["HW 5 – Thermo", "Next page", "Grades", "Inbox", "Sent folder", "Show more"])
def test_ordinary_labels(label):
    assert not risky_label(label)
