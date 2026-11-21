"""Folders and files by name (prompt 07c §7): the pure matching."""

from nova.actions.places import Entry, best, folder_key, query, risky, stem


def entry(name, folder="downloads", modified=0.0, is_dir=False):
    return Entry(name, f"C:/x/{name}", folder, is_dir, modified)


def test_known_folders_by_loose_name():
    assert folder_key("downloads") == "downloads"
    assert folder_key("my downloads folder") == "downloads"
    assert folder_key("the documents") == "documents"
    assert folder_key("photos") == "pictures"
    assert folder_key("screenshots") == "screenshots"
    assert folder_key("camera roll") == "camera roll"
    assert folder_key("budget") is None


def test_query_splits_name_from_kind():
    assert query("the budget spreadsheet") == ("budget", (".xlsx", ".xls", ".xlsm", ".csv", ".ods"))
    assert query("my resume pdf") == ("resume", (".pdf",))
    assert query("pdf") == ("pdf", ())  # a kind alone is the name
    assert query("the file called notes") == ("notes", ())


def test_stem_is_the_spoken_name():
    assert stem(entry("Budget_2026-final.xlsx")) == "budget 2026 final"
    assert stem(entry("Physics.Lab", is_dir=True)) == "physics lab"


def test_best_exact_beats_fuzzy_and_newest_first():
    files = [entry("budget.xlsx", modified=1), entry("budget.pdf", modified=5), entry("budgets 2025.xlsx")]
    assert [e.name for e in best("budget", files)] == ["budget.pdf", "budget.xlsx"]
    assert [e.name for e in best("the budget spreadsheet", files)] == ["budget.xlsx"]


def test_best_fuzzy_ties_and_misses():
    files = [entry("Resume Bhavya.pdf"), entry("resume old.pdf"), entry("taxes.pdf")]
    found = best("resume", files)
    assert {e.name for e in found} == {"Resume Bhavya.pdf", "resume old.pdf"}
    assert best("vacation photos", files) == []
    assert best("the", files) == []


def test_best_kind_filter_falls_back_when_nothing_matches_the_kind():
    files = [entry("notes.txt")]
    assert [e.name for e in best("notes pdf", files)] == ["notes.txt"]


def test_where_and_risky():
    assert entry("a.pdf", folder="camera roll").where == "a.pdf · Camera Roll"
    assert entry("a.pdf").where == "a.pdf · Downloads"
    assert risky("C:/x/setup.EXE") and risky("run.ps1") and risky("thing.lnk")
    assert not risky("budget.xlsx") and not risky("Physics")


def test_inside_follows_the_real_path(tmp_path):
    from nova.actions.places import inside

    home = tmp_path / "home"
    (home / "Downloads").mkdir(parents=True)
    (tmp_path / "elsewhere").mkdir()
    assert inside(str(home / "Downloads" / "a.pdf"), [str(home)])
    assert inside(str(home), [str(home) + "\\"])
    assert not inside(str(tmp_path / "elsewhere" / "a.pdf"), [str(home)])
    assert not inside(str(tmp_path / "home2" / "a.pdf"), [str(home)])  # a prefix isn't a parent
    assert not inside(str(home / ".." / "elsewhere" / "a.pdf"), [str(home)])
