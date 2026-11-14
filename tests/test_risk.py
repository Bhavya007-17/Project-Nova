"""Risk detection (SPEC §14): what needs a Yes in the island, and what Nova never does."""

import pytest

from nova.brain.plan import step
from nova.brain.risk import Scene, assess, enters_text, needs_focus, risky_label

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


PASSWORD_BOX = Scene(app="Google Chrome", exe="chrome.exe", password=True)
UNCHECKED = Scene(app="Google Chrome", exe="chrome.exe", focus_known=False)


@pytest.mark.parametrize(
    "keys", ["ctrl+v", "shift+insert", "win+v", "ctrl+shift+v", "v+ctrl", "a", "shift+a", "7", "space", ".", "ctrl+alt+e"]
)
def test_keys_that_put_text_in_a_field(keys):
    assert enters_text(keys)


@pytest.mark.parametrize(
    "keys",
    ["enter", "tab", "esc", "backspace", "delete", "f5", "left", "ctrl+t", "ctrl+c", "ctrl+x", "ctrl+z", "ctrl+a",
     "ctrl+s", "ctrl+=", "ctrl+shift+t", "alt+left", "win+d", "alt+v", "insert", "ctrl+insert"],
)
def test_keys_that_do_not(keys):
    assert not enters_text(keys)


@pytest.mark.parametrize("keys", ["ctrl+v", "shift+insert", "win+v", "a", "shift+1", "space"])
def test_never_pastes_or_presses_characters_into_password_fields(keys):
    risk = assess(step("press", keys=keys), PASSWORD_BOX)
    assert risk.refuse and "password" in risk.reason


def test_keys_that_enter_nothing_still_work_in_a_password_field():
    for keys in ("enter", "tab", "backspace", "ctrl+a", "esc"):  # he typed it himself; Nova adds no characters
        assert assess(step("press", keys=keys), PASSWORD_BOX) is None, keys


def test_a_paste_asks_first_when_the_field_could_not_be_checked():
    risk = assess(step("press", keys="ctrl+v"), UNCHECKED)
    assert risk.confirm and risk.question == "Paste here? (I couldn't check this field)"
    risk = assess(step("press", keys="shift+a"), UNCHECKED)
    assert risk.confirm and "couldn't check" in risk.question
    assert assess(step("press", keys="ctrl+t"), UNCHECKED) is None
    assert assess(step("press", keys="ctrl+v"), NOTEPAD) is None


@pytest.mark.parametrize("keys", ["ctrl+v+v", "a+b", "shift+a+b"])
def test_a_chord_it_cannot_classify_counts_as_entering_text(keys):
    # Ctrl down, V down, V down... is a paste: fail closed rather than let it past the password check
    assert enters_text(keys)
    assert needs_focus(step("press", keys=keys))
    risk = assess(step("press", keys=keys), PASSWORD_BOX)
    assert risk is not None and risk.refuse and "password" in risk.reason
    assert assess(step("press", keys=keys), UNCHECKED).confirm


def test_ordinary_chords_are_judged_as_before():
    for keys in ("ctrl+c", "alt+tab", "ctrl+shift+t", "enter"):
        assert not enters_text(keys), keys
        assert assess(step("press", keys=keys), PASSWORD_BOX) is None, keys
        assert assess(step("press", keys=keys), UNCHECKED) is None, keys


def test_which_steps_need_the_focused_control_checked():
    assert needs_focus(step("type_text", text="hi"))
    assert needs_focus(step("press", keys="ctrl+v"))
    assert needs_focus(step("press", keys="b"))
    assert not needs_focus(step("press", keys="ctrl+t"))  # no UI Automation question for keys that enter nothing
    assert not needs_focus(step("open_app", app="Notepad"))


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


# --- Stage 06: page clicks and fills (the label is the element's own name once the page map resolved it) -------------

CANVAS = Scene(app="Google Chrome", exe="chrome.exe", url="https://canvas.vt.edu/courses/11/assignments/501")
SHOP = Scene(app="Google Chrome", exe="chrome.exe", url="https://www.example-shop.com/checkout/payment")
PAGE = Scene(app="Google Chrome", exe="chrome.exe", url="https://example.com/")


def test_page_clicks_confirm_on_the_resolved_label():
    risk = assess(step("page_click", description="the blue button", element="0:4", label="Submit Assignment"), CANVAS)
    assert risk.confirm and risk.question == "Click “Submit Assignment” on Canvas?"
    assert assess(step("page_click", description="submit", element="0:4", label="Sign in"), PAGE) is None
    assert assess(step("page_click", description="sign in"), PAGE) is None


@pytest.mark.parametrize("label", ["Tweet", "Donate $5", "Transfer money", "Withdraw", "Subscribe", "Book now"])
def test_more_risky_labels(label):
    assert risky_label(label)


@pytest.mark.parametrize("label", ["Sign in", "Submissions", "Comments (2)", "Posts", "Deleted items", "Order history"])
def test_lookalike_labels_are_fine(label):
    assert not risky_label(label)


def test_form_submit_buttons_on_checkout_pages_confirm():
    assert assess(step("page_click", description="continue", element="0:1", label="Continue", submits=True), SHOP).confirm
    assert assess(step("page_click", description="continue", element="0:1", label="Continue", submits=True), PAGE) is None


def test_page_fill_rules():
    assert assess(step("page_fill", description="search box", text="thermo", submit=True, search=True), PAGE) is None
    assert assess(step("page_fill", description="username", text="bhavya"), PAGE) is None
    assert assess(step("page_fill", description="password", text="x", secret=True), PAGE).refuse
    assert assess(step("page_fill", description="card", text="4111 1111 1111 1111"), PAGE).refuse
    risk = assess(step("page_fill", description="comment", text="great work", submit=True), PAGE)
    assert risk.confirm and "great work" in risk.question
    gmail = Scene(app="Google Chrome", exe="chrome.exe", url="https://mail.google.com/mail/u/0/#inbox")
    assert assess(step("page_fill", description="message", text="hi", submit=True), gmail).question == "Send “hi”?"


def test_canvas_and_reading_steps_are_never_risky():
    for s in (step("canvas_due", range="week"), step("canvas_open_item", item="my assignment for today"),
              step("canvas_open", url="https://canvas.vt.edu/courses/1/assignments/2", title="HW 5"),
              step("scroll_to", description="comments"), step("page_read", what="page"),
              step("tab", op="goto", match="youtube")):
        assert assess(s, CANVAS) is None, s


def test_dictation_never_lands_in_passwords_terminals_or_with_card_numbers():
    from nova.brain.risk import assess_dictation

    assert assess_dictation("hello there", Scene(app="Notepad", exe="notepad.exe")) is None
    assert assess_dictation("hello", Scene(password=True)).refuse
    assert assess_dictation("ls\n", Scene(exe="WindowsTerminal.exe")).refuse
    assert assess_dictation("my card is 4111 1111 1111 1111", Scene(app="Notepad")).refuse
    assert assess_dictation("hi", Scene(locked=True)).refuse
