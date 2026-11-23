"""Rows (Stage 07d): the window in front as numbered lines for the planner model — filter, order, cap, text."""

from nova.actions import rows
from nova.actions.marks import Box, Target
from nova.actions.uitree import Element

BUTTON, CHECKBOX, EDIT, MENU, MENU_BAR, MENU_ITEM, TEXT, GROUP, PANE, TAB_ITEM = (
    50000, 50002, 50004, 50009, 50010, 50011, 50020, 50026, 50033, 50019)


def el(i, name, ct, rect, patterns=("invoke",), **kw):
    return Element(i, name, ct, rect, frozenset(patterns), **kw)


def test_only_named_controls_become_rows_and_labels_are_quoted():
    out = rows.build([
        el(0, "Save", BUTTON, (10, 10, 50, 30)),
        el(1, "", BUTTON, (60, 10, 90, 30)),                        # no name, no help, no id: dropped
        el(2, "", BUTTON, (100, 10, 130, 30), help="Bold (Ctrl+B)"),  # help text names it
        el(3, "", BUTTON, (140, 10, 170, 30), auto_id="NewTabButton"),
        el(4, "Untitled - Notepad", TEXT, (0, 40, 300, 60), ()),    # plain text is not a control
    ])
    assert [r.text for r in out] == ['1 button "Save"', '2 button "Bold (Ctrl+B)"', '3 button "New Tab Button"']
    assert [r.number for r in out] == [1, 2, 3]
    assert out[0].target.ref == (0, 0, frozenset({"invoke"}))


def test_state_and_area_show_in_the_row():
    out = rows.build([
        el(0, "View", MENU, (100, 40, 300, 200), ()),
        el(1, "Word wrap", MENU_ITEM, (110, 50, 290, 70), ("toggle",), state="off"),
        el(2, "Status bar", MENU_ITEM, (110, 70, 290, 90), ("toggle",), state="on"),
    ])
    assert [r.text for r in out] == ['1 menu item "Word wrap" off (View menu)', '2 menu item "Status bar" on (View menu)']
    assert out[0].area == "View menu" and out[0].state == "off"


def test_an_open_menu_comes_first_then_reading_order():
    out = rows.build([
        el(0, "File", MENU_ITEM, (0, 0, 40, 20), ("expand",)),
        el(1, "View", MENU_ITEM, (50, 0, 90, 20), ("expand",)),
        el(2, "OK", BUTTON, (200, 300, 260, 320)),
        el(3, "Cancel", BUTTON, (100, 300, 160, 320)),
        el(4, "View", MENU, (50, 20, 250, 120), ()),
        el(5, "Zoom", MENU_ITEM, (55, 25, 245, 45), ("expand",)),
    ])
    assert [r.target.name for r in out] == ["Zoom", "File", "View", "Cancel", "OK"]


def test_the_focus_area_comes_first():
    out = rows.build([
        el(0, "Home", TAB_ITEM, (0, 0, 60, 20), ("select",)),
        el(1, "Font", GROUP, (0, 100, 200, 200), ()),
        el(2, "Bold", BUTTON, (10, 110, 40, 130)),
    ], first=Box(0, 100, 200, 200))
    assert [r.target.name for r in out] == ["Bold", "Home"]
    assert out[0].area == "Font group"


def test_capped_and_duplicates_dropped():
    many = [el(i, f"Item {i}", BUTTON, (0, i * 20, 50, i * 20 + 18)) for i in range(80)]
    many.append(el(80, "Item 0", BUTTON, (1, 1, 50, 18)))  # the same control twice in the tree
    out = rows.build(many)
    assert len(out) == rows.ROW_CAP == 60
    assert [r.target.name for r in out].count("Item 0") == 1


def test_password_fields_never_become_rows():
    out = rows.build([el(0, "Password", EDIT, (0, 0, 100, 20), ("focusable",), secret=True),
                      el(1, "User name", EDIT, (0, 30, 100, 50), ("focusable",))])
    assert [r.target.name for r in out] == ["User name"]


def test_ocr_lines_join_only_when_uia_has_few_rows():
    few = [el(0, "OK", BUTTON, (0, 0, 40, 20))]
    ocr = [Target(Box(0, 0, 40, 20), "OK", "text", "ocr"), Target(Box(0, 50, 120, 70), "Export as PDF", "text", "ocr")]
    out = rows.build(few, ocr=ocr)
    assert [r.text for r in out] == ['1 button "OK"', '2 text "Export as PDF"']  # the OCR "OK" sits on the button
    many = [el(i, f"B{i}", BUTTON, (0, i * 30, 40, i * 30 + 20)) for i in range(rows.OCR_BELOW)]
    assert all(r.target.source == "uia" for r in rows.build(many, ocr=ocr))


def test_long_names_are_cut_and_quotes_kept_safe():
    out = rows.build([el(0, 'Say "hi" ' + "x" * 80, BUTTON, (0, 0, 40, 20))])
    assert out[0].text.startswith('1 button "Say \'hi\' xxx')
    assert len(out[0].text) < 70


def test_rows_can_be_found_by_number_and_text_lines_for_the_model():
    out = rows.build([el(0, "Save", BUTTON, (0, 0, 40, 20)), el(1, "Open", BUTTON, (50, 0, 90, 20))])
    assert rows.by_number(out, 2).target.name == "Open"
    assert rows.by_number(out, 3) is None and rows.by_number(out, 0) is None
    assert rows.lines(out) == ['1 button "Save"', '2 button "Open"']
    assert rows.lines(out, limit=1) == ['1 button "Save"']
