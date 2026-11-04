"""Page-map ranking (SPEC §9): which element on the page did he mean? Pure: elements + his words in, ranked out."""

from datetime import datetime

from nova.chrome.pagemap import Element, PageView, decide, llm_rows, points_at_page, rank, wants_sections

NOW = datetime(2026, 11, 4, 15, 0)  # a Wednesday


def el(id_, name, role="link", **kw):
    return Element(id=id_, role=role, name=name, **kw)


PAGE = (
    el("0:1", "Home"),
    el("0:2", "Courses", section="Global navigation"),
    el("0:3", "Sign in", role="button", section="header"),
    el("0:4", "Search the site", role="searchbox", editable=True, search=True, section="header"),
    el("0:5", "Username", role="textbox", editable=True, section="Log in"),
    el("0:6", "Password", role="textbox", editable=True, secret=True, section="Log in"),
    el("0:7", "Sign in", role="button", section="Log in"),
    el("0:8", "Forgot password?", section="Log in"),
    el("0:9", "HW 5", section="To Do", row="ME 3124 Thermodynamics Due Nov 4 at 11:59pm", href="https://c/courses/1/assignments/5"),
    el("0:10", "Lab 3 report", section="To Do", row="PHYS 2306 Due Nov 6 at 5pm", href="https://c/courses/2/assignments/3"),
    el("0:11", "Quiz 2", section="To Do", row="MATH 2214 Due Nov 10 at 9am", href="https://c/courses/3/quizzes/2"),
    el("0:12", "Thermodynamics", role="clickable", section="Cards"),
    el("0:13", "Sign in with Google", role="button", section="Log in"),
    el("0:14", "Submit Assignment", role="button", section="Submit", in_view=False, submits=True),
    el("0:15", "Reply", role="button", section="Comments (2)", in_view=False),
    el("0:16", "Reply", role="button", section="Comments (2)", in_view=False),
    el("0:17", "Comments (2)", role="heading", in_view=False),
    el("0:18", "Disabled button", role="button", disabled=True),
    el("0:19", "Next page", section="Comments (2)", in_view=False),
)


def top(description, elements=PAGE, **kw):
    return [s.element.id for s in rank(elements, description, now=NOW, **kw)]


def test_exact_name_beats_longer_names():
    ids = top("sign in")
    assert ids[0] in ("0:3", "0:7") and "0:13" in ids[:3]
    assert rank(PAGE, "sign in", now=NOW)[0].score > rank(PAGE, "sign in with google", now=NOW)[1].score


def test_same_name_twice_clicks_the_first_without_asking():
    decision = decide(rank(PAGE, "sign in", now=NOW))
    assert decision.kind == "one" and decision.best.id == "0:3"


def test_section_words_pick_between_twins():
    assert decide(rank(PAGE, "the sign in button in the log in form", now=NOW)).best.id == "0:7"


def test_ordinals_count_matching_elements_in_page_order():
    assert decide(rank(PAGE, "the second reply button", now=NOW)).best.id == "0:16"
    assert decide(rank(PAGE, "the first reply", now=NOW)).best.id == "0:15"
    assert decide(rank(PAGE, "the last reply", now=NOW)).best.id == "0:16"


def test_spelling_and_partial_words():
    assert top("signin")[0] in ("0:3", "0:7")
    assert top("thermo")[0] == "0:12"
    assert top("the forgot password link")[0] == "0:8"


def test_row_dates_answer_today_and_tomorrow():
    assert decide(rank(PAGE, "my assignment for today", now=NOW)).best.id == "0:9"
    assert top("the one due friday")[0] == "0:10"  # Friday = Nov 6
    friday = datetime(2026, 11, 6, 9, 0)
    assert rank(PAGE, "the lab due today", now=friday)[0].element.id == "0:10"


def test_role_words_steer():
    assert top("the search box")[0] == "0:4"
    assert top("courses link")[0] == "0:2"


def test_fill_only_considers_fields_and_never_secrets():
    assert top("username", kind="fill")[0] == "0:5"
    assert "0:6" not in top("password", kind="fill")
    assert all(s.element.editable for s in rank(PAGE, "search", now=NOW, kind="fill"))


def test_disabled_elements_sink():
    assert "0:18" not in top("disabled button")[:1] or rank(PAGE, "disabled button", now=NOW)[0].score < 60


def test_sections_for_scroll_to():
    assert top("the comments", kind="section")[0] == "0:17"


def test_nothing_matches_means_none():
    assert decide(rank(PAGE, "purple elephant", now=NOW)).kind == "none"


def test_different_close_names_ask():
    elements = (el("1", "Thermodynamics HW 5"), el("2", "Thermodynamics HW 6"), el("3", "Home"))
    decision = decide(rank(elements, "thermodynamics homework", now=NOW))
    assert decision.kind == "ask" and {e.id for e in decision.options} == {"1", "2"}


def test_risky_twins_ask_which():
    elements = (el("1", "Delete", role="button", row="Draft 1"), el("2", "Delete", role="button", row="Draft 2"))
    decision = decide(rank(elements, "delete", now=NOW))
    assert decision.kind == "ask" and len(decision.options) == 2


def test_a_weak_lone_match_is_a_question():
    elements = (el("1", "Account settings and privacy"), el("2", "Home"))
    decision = decide(rank(elements, "privacy stuff", now=NOW))
    assert decision.kind in ("one", "ask") and decision.options[0].id == "1"


def test_llm_rows_are_numbered_and_compact():
    rows = llm_rows(rank(PAGE, "sign in", now=NOW), limit=3)
    assert rows[0] == "1 button: Sign in [header]" and [r.split()[0] for r in rows] == ["1", "2", "3"]
    hw = llm_rows([s for s in rank(PAGE, "hw 5", now=NOW) if s.element.id == "0:9"])
    assert hw == ["1 link: HW 5 [To Do] ME 3124 Thermodynamics Due Nov 4 at 11:…"]
    many = llm_rows(rank(PAGE * 10, "sign in", now=NOW), limit=40, chars=200)
    assert sum(len(r) for r in many) <= 200 and len(many) < 40


def test_page_view_from_extension_json():
    view = PageView.of({"url": "https://x/", "title": "X", "tab": {"id": 7}, "elements": [
        {"id": "0:1", "role": "link", "name": "Home", "box": [1, 2, 3, 4], "inView": True},
        {"id": "0:2", "role": "textbox", "name": "Email", "editable": True, "secret": True},
        {"bad": True},
    ]}, ms=12.0)
    assert view.tab == 7 and len(view.elements) == 2 and view.elements[1].secret and view.ms == 12.0
    assert view.find("0:1").name == "Home" and view.find("nope") is None


def test_points_at_page():
    for text in ("click sign in", "tap the second link", "press the submit button", "hit the blue button",
                 "open the first result", "check the remember me box", "go to the comments section",
                 "fill in the username field", "type hello into the search box", "click on my assignment for today",
                 "select the third option", "the one at the bottom of this page"):
        assert points_at_page(text), text
    for text in ("open chrome", "new tab", "close this tab", "go back", "scroll down", "volume up",
                 "open canvas", "what's due this week", "maximize this window", "type hello world"):
        assert not points_at_page(text), text


def test_wants_sections():
    assert wants_sections("scroll to the comments") and wants_sections("go to the reviews section")
    assert not wants_sections("click sign in")


def test_sections_beat_fields_for_scroll_to():
    elements = (el("1", "Comment", role="textbox", editable=True, section="Submit"),
                el("2", "Comments", role="region", in_view=False), el("3", "Comments (2)", role="heading", in_view=False))
    decision = decide(rank(elements, "the comments", now=NOW, kind="section"))
    assert decision.kind == "one" and decision.best.id == "2"


def test_a_role_word_separates_a_button_from_a_link_of_the_same_name():
    elements = (el("1", "Frame button", role="button"), el("2", "Frame link", role="link"))
    assert decide(rank(elements, "the frame button", now=NOW)).best.id == "1"


def test_colours_and_places_dont_dilute_the_name():
    assert decide(rank(PAGE, "the blue submit button at the bottom", now=NOW)).best.id == "0:14"
    back = (el("1", "Back to top"), el("2", "Home"))
    assert decide(rank(back, "back to top", now=NOW)).best.id == "1"


def test_a_lone_odd_word_matches_nothing():
    elements = (*PAGE, el("0:30", "Delete draft", role="button"))
    assert decide(rank(elements, "purple elephant", now=NOW)).kind == "none"
    assert decide(rank(elements, "elephant", now=NOW)).kind == "none"


def test_where_on_screen_picks_between_twins():
    twins = (el("1", "Sign in", role="button", box=(1000, 10, 80, 30)), el("2", "Sign in", role="button", box=(400, 700, 80, 30)))
    screen = (1200, 800)
    assert decide(rank(twins, "sign in up top", now=NOW, viewport=screen)).best.id == "1"
    assert decide(rank(twins, "the sign in button at the bottom", now=NOW, viewport=screen)).best.id == "2"
    assert decide(rank(twins, "the sign in on the right", now=NOW, viewport=screen)).best.id == "1"
    back = (el("1", "Back to top", box=(10, 700, 80, 20)), el("2", "Home", box=(10, 10, 80, 20)))
    assert decide(rank(back, "back to top", now=NOW, viewport=screen)).best.id == "1"  # "top" is its name here
    assert PageView.of({"viewport": [1200, 800], "elements": []}).viewport == (1200, 800)
