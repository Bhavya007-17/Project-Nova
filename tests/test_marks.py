from nova.actions.marks import (
    MAX_TAGS,
    Box,
    Mark,
    Target,
    grid_cells,
    grid_zoom,
    merge,
    name_score,
    normalize,
    number,
    parse_number,
    rank,
    reading_order,
    split_role,
)

SCREEN = Box(0, 0, 2880, 1800)


def t(name, x, y, w=80, h=30, role="button", source="uia"):
    return Target(Box(x, y, x + w, y + h), name, role, source)


def test_normalize_drops_apostrophes_and_punctuation():
    assert normalize("Don’t Save") == "dont save"
    assert normalize("  Save  As…") == "save as"


def test_split_role():
    assert split_role("the save button") == ("save", "button")
    assert split_role("OK") == ("ok", "")
    assert split_role("downloads folder") == ("downloads", "item")


def test_name_score_tiers():
    assert name_score("save", "Save") == 4
    assert name_score("save", "Save as") == 3
    assert name_score("save", "Auto save on") == 2
    assert name_score("save", "Unsaved") == 1
    assert name_score("ok", "Book") == 0  # short words never match inside others
    assert name_score("x", "") == 0


def test_rank_prefers_exact_over_starts_with():
    found = rank("save", [t("Save as", 0, 0), t("Save", 100, 0), t("Autosave", 200, 0)])
    assert [f.name for f in found] == ["Save"]


def test_rank_several_equal_matches_stay_ambiguous():
    found = rank("close", [t("Close", 0, 0), t("Close", 0, 500)])
    assert len(found) == 2


def test_rank_role_narrows():
    found = rank("downloads link", [t("Downloads", 0, 0, role="button"), t("Downloads", 0, 100, role="link")])
    assert [f.role for f in found] == ["link"]


def test_rank_role_falls_back_when_nothing_has_it():
    found = rank("save button", [t("Save", 0, 0, role="menu item")])
    assert len(found) == 1


def test_rank_near_picks_the_closest():
    targets = [t("Edit", 0, 0), t("Edit", 0, 600), t("Shipping address", 100, 600, role="text")]
    found = rank("edit", targets, near="shipping address")
    assert found == [targets[1]]


def test_rank_drops_inner_duplicate():
    outer, inner = t("OK", 100, 100, 80, 30), t("OK", 110, 105, 60, 20, role="text")
    assert rank("ok", [outer, inner]) == [outer]


def test_rank_nothing():
    assert rank("print", [t("Save", 0, 0)]) == []


def test_dont_save_matches_dialog_button():
    found = rank("don't save", [t("Save", 0, 0), t("Don't save", 100, 0), t("Cancel", 200, 0)])
    assert [f.name for f in found] == ["Don't save"]


def test_merge_dom_wins_over_uia_and_ocr_names_unnamed():
    dom = [t("Sign in", 100, 100, source="dom")]
    uia = [t("Sign in", 102, 101, source="uia"), t("", 500, 500, 40, 40)]
    ocr = [t("Settings", 505, 510, 30, 15, role="text", source="ocr"), t("Help", 900, 900, 40, 20, role="text",
                                                                        source="ocr")]
    merged = merge(uia, dom, ocr, screen=SCREEN)
    assert [(m.name, m.source) for m in merged] == [("Sign in", "dom"), ("Settings", "uia"), ("Help", "ocr")]


def test_merge_drops_tiny_and_huge():
    merged = merge([t("dot", 0, 0, 10, 10), t("page", 0, 0, 2000, 1500), t("ok", 0, 0, 40, 40)], [], [],
                   screen=SCREEN, scale=2.0)
    assert [m.name for m in merged] == ["ok"]


def test_merge_same_box_once():
    merged = merge([t("", 0, 0, 100, 40, role="item"), t("Readme.md", 1, 1, 99, 39, role="item")], [], [],
                   screen=SCREEN)
    assert [m.name for m in merged] == ["Readme.md"]


def test_reading_order_rows_then_left_to_right():
    a, b, c = t("a", 300, 10), t("b", 10, 14), t("c", 10, 200)
    assert [x.name for x in reading_order([a, b, c])] == ["b", "a", "c"]


def test_number_pages_at_99():
    targets = [t(str(i), (i % 20) * 100, (i // 20) * 50) for i in range(150)]
    first, second = number(targets), number(targets, page=1)
    assert len(first) == MAX_TAGS and first[0].number == 1 and first[-1].number == 99
    assert len(second) == 51


def test_number_keeps_tags_that_barely_moved():
    before = number([t("a", 0, 0), t("b", 200, 0)])
    after = number([t("new", 0, 0, 30, 30), t("b", 203, 2)], previous=[m for m in before if m.target.name == "b"])
    assert {m.target.name: m.number for m in after} == {"b": 2, "new": 1}


def test_parse_number():
    assert parse_number("12") == 12
    assert parse_number("twelve") == 12
    assert parse_number("twenty one") == 21
    assert parse_number("twenty-one") == 21
    assert parse_number("number 7") == 7
    assert parse_number("Seven.") == 7
    assert parse_number("to") == 2
    assert parse_number("100") is None
    assert parse_number("open chrome") is None
    assert parse_number("zero") is None
    assert parse_number("twenty") == 20


def test_grid_cells_and_zoom():
    cells = grid_cells(Box(0, 0, 900, 900))
    assert len(cells) == 9 and cells[0] == Box(0, 0, 300, 300) and cells[8] == Box(600, 600, 900, 900)
    assert grid_zoom(Box(0, 0, 900, 900), 5) == Box(300, 300, 600, 600)


def test_mark_is_frozen():
    m = Mark(1, t("a", 0, 0))
    try:
        m.number = 2  # type: ignore[misc]
    except AttributeError:
        return
    raise AssertionError("Mark must be frozen")
