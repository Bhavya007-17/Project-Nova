"""Canvas adapter (SPEC §10): planner JSON -> items, date ranges, which item he means, island text. Pure."""

import json
from datetime import datetime, timedelta, timezone

from nova.chrome import canvas
from nova.chrome.canvas import Item

EST = timezone(timedelta(hours=-5))
NOW = datetime(2026, 11, 4, 15, 0, tzinfo=EST)  # Wednesday afternoon
ORIGIN = "https://canvas.vt.edu"


def planner_row(kind, pid, title, due, course="ME 3124 Thermodynamics", cid=11, submitted=False, missing=False,
                url=None, done=False):
    return {
        "plannable_type": kind, "plannable_id": pid, "course_id": cid, "context_name": course,
        "html_url": url if url is not None else f"/courses/{cid}/{'quizzes' if kind == 'quiz' else 'assignments'}/{pid}",
        "plannable_date": due, "plannable": {"id": pid, "title": title, "due_at": due},
        "submissions": {"submitted": submitted, "missing": missing},
        "planner_override": {"marked_complete": True} if done else None,
    }


ROWS = [
    planner_row("assignment", 501, "HW 5", "2026-11-05T04:59:00Z"),  # today 11:59 PM
    planner_row("assignment", 302, "Lab 3 report", "2026-11-06T22:00:00Z", "PHYS 2306 Foundations of Physics", 22),
    planner_row("quiz", 77, "Quiz 2", "2026-11-10T14:00:00Z", "MATH 2214 Differential Equations", 33),  # next Tue
    planner_row("discussion_topic", 9, "Reading response", "2026-11-04T17:00:00Z", "ENGL 1106", 44, submitted=True,
                url="/courses/44/discussion_topics/9"),
    {"plannable_type": "announcement", "plannable_id": 1, "context_name": "ME 3124", "html_url": "/courses/11/announcements/1",
     "plannable_date": "2026-11-04T14:00:00Z", "plannable": {"title": "Midterm moved"}},
    {"plannable_type": "calendar_event", "plannable_id": 2, "context_name": "ME 3124", "html_url": "/calendar",
     "plannable_date": "2026-11-05T14:00:00Z", "plannable": {"title": "Office hours"}},
    {"plannable_type": "planner_note", "plannable_id": 3, "plannable_date": "2026-11-05T14:00:00Z",
     "plannable": {"title": "Email advisor", "todo_date": "2026-11-05T14:00:00Z"}},
    planner_row("assignment", 502, "HW 6", "2026-11-13T04:59:00Z"),  # next Thu
    planner_row("assignment", 201, "Lab 2 report", "2026-11-01T22:00:00Z", "PHYS 2306 Foundations of Physics", 22,
                missing=True),  # overdue
]
ITEMS = canvas.parse_planner(ROWS, ORIGIN)


def titles(items):
    return [i.title for i in items]


def test_guard_prefix_is_stripped():
    assert canvas.strip_guard("while(1);[1]") == "[1]"
    assert canvas.strip_guard("  [1]") == "  [1]"
    assert json.loads(canvas.strip_guard("while(1);" + json.dumps(ROWS)))[0]["plannable_id"] == 501


def test_planner_rows_become_items():
    assert titles(ITEMS) == ["Lab 2 report", "Reading response", "HW 5", "Lab 3 report", "Quiz 2", "HW 6"]
    hw5 = next(i for i in ITEMS if i.title == "HW 5")
    assert hw5.url == "https://canvas.vt.edu/courses/11/assignments/501" and hw5.course == "ME 3124 Thermodynamics"
    assert hw5.due.astimezone(EST) == datetime(2026, 11, 4, 23, 59, tzinfo=EST) and not hw5.done
    assert next(i for i in ITEMS if i.title == "Reading response").done
    assert next(i for i in ITEMS if i.title == "Lab 2 report").missing
    assert next(i for i in ITEMS if i.title == "Quiz 2").kind == "quiz"


def test_completed_by_override_counts_as_done():
    rows = [planner_row("assignment", 1, "X", "2026-11-05T04:59:00Z", done=True)]
    assert canvas.parse_planner(rows, ORIGIN)[0].done


def test_windows():
    day = lambda d, h=0: datetime(2026, 11, d, h, tzinfo=EST)  # noqa: E731
    assert canvas.window("today", NOW) == (day(4), day(5))
    assert canvas.window("tomorrow", NOW) == (day(5), day(6))
    assert canvas.window("week", NOW) == (day(4), day(9))  # through Sunday
    assert canvas.window("next_week", NOW) == (day(9), day(16))
    assert canvas.window("weekend", NOW) == (day(7), day(9))
    assert canvas.window("upcoming", NOW) == (NOW, NOW + timedelta(days=7))
    assert canvas.window("overdue", NOW) == (NOW - timedelta(days=21), NOW)


def test_what_is_due():
    assert titles(canvas.due(ITEMS, "today", NOW)) == ["HW 5", "Reading response"]  # outstanding first
    assert titles(canvas.due(ITEMS, "week", NOW)) == ["HW 5", "Lab 3 report", "Reading response"]
    assert titles(canvas.due(ITEMS, "next_week", NOW)) == ["Quiz 2", "HW 6"]
    assert titles(canvas.due(ITEMS, "overdue", NOW)) == ["Lab 2 report"]
    assert canvas.due(ITEMS, "weekend", NOW) == []


def test_references():
    ref = canvas.parse_ref("my assignment for today", NOW)
    assert ref.range == "today" and ref.words == ()
    assert canvas.parse_ref("the thermo homework", NOW).words == ("thermo",)
    assert canvas.parse_ref("HW 5", NOW).words == ("hw", "5")
    ref = canvas.parse_ref("my lab report due friday", NOW)
    assert ref.day == datetime(2026, 11, 6).date() and ref.words == ("lab", "report")
    assert canvas.parse_ref("my next assignment", NOW).next
    ref = canvas.parse_ref("my quiz for next week", NOW)
    assert ref.range == "next_week" and ref.kinds == frozenset({"quiz"})
    assert canvas.parse_ref("today's homework", NOW).range == "today"


def pick(text):
    return titles(canvas.select(ITEMS, canvas.parse_ref(text, NOW), NOW))


def test_which_item_he_means():
    assert pick("my assignment for today") == ["HW 5"]  # the submitted one drops out
    assert pick("the thermo homework") == ["HW 5"]  # the soonest of the thermo ones, well before the next
    assert pick("hw 6") == ["HW 6"]
    assert pick("HW 5") == ["HW 5"]
    assert pick("my quiz for next week") == ["Quiz 2"]
    assert pick("my lab report due friday") == ["Lab 3 report"]
    assert pick("my next assignment") == ["HW 5"]
    assert pick("my overdue assignment") == ["Lab 2 report"]
    assert pick("the physics lab") == ["Lab 2 report", "Lab 3 report"]  # two close ones: ask
    assert pick("my assignment for saturday") == []
    assert pick("the reading response") == ["Reading response"]  # done, but it's the only match


def test_several_due_today_are_all_offered():
    rows = ROWS + [planner_row("assignment", 503, "Pre-lab", "2026-11-04T23:00:00Z", "PHYS 2306", 22)]
    items = canvas.parse_planner(rows, ORIGIN)
    assert titles(canvas.select(items, canvas.parse_ref("my assignment for today", NOW), NOW)) == ["Pre-lab", "HW 5"]


def test_next_due():
    assert canvas.next_due(ITEMS, NOW).title == "HW 5"
    assert canvas.next_due(ITEMS, datetime(2026, 11, 7, tzinfo=EST)).title == "Quiz 2"
    assert canvas.next_due(ITEMS, datetime(2026, 12, 1, tzinfo=EST)) is None


def test_island_text():
    hw5, lab3, quiz2 = (next(i for i in ITEMS if i.title == t) for t in ("HW 5", "Lab 3 report", "Quiz 2"))
    assert canvas.when(hw5, NOW) == "today 11:59 PM"
    assert canvas.when(lab3, NOW) == "Fri 5 PM"
    assert canvas.when(quiz2, NOW) == "Nov 10"
    assert canvas.course_short("ME 3124 Thermodynamics") == "Thermodynamics"
    assert canvas.course_short("2026FA-ME-3124 (Fall 2026)") == "ME 3124"
    assert len(canvas.label(lab3, NOW)) <= 32 and canvas.label(hw5, NOW).startswith("HW 5")
    assert canvas.nothing_due("weekend", quiz2, NOW) == "Nothing due this weekend — next: Quiz 2 (Differential Equations), Nov 10"
    assert canvas.nothing_due("today", None, NOW) == "Nothing due today, and nothing coming up"
    assert canvas.range_words("next_week") == "next week"


def test_description_html_to_text():
    html = "<p>Read <b>chapter 5</b>&nbsp;and answer:</p><ul><li>Problem 1</li><li>Problem 2</li></ul><script>x()</script>"
    assert canvas.html_text(html) == "Read chapter 5 and answer:\nProblem 1\nProblem 2"


def test_dom_fallback_rows():
    rows = [
        {"title": "HW 5", "url": "https://canvas.vt.edu/courses/11/assignments/501", "row": "HW 5 ME 3124 Due Nov 4 at 11:59pm"},
        {"title": "Quiz 2", "url": "https://canvas.vt.edu/courses/33/quizzes/77", "row": "Quiz 2 MATH 2214 Due: Nov 10, 2026 9:00 AM"},
        {"title": "Lab", "url": "https://canvas.vt.edu/courses/22/assignments/1", "row": "Lab Due Tomorrow at 5pm"},
        {"title": "No date", "url": "https://canvas.vt.edu/courses/22/assignments/2", "row": "No date here"},
    ]
    items = canvas.dom_items(rows, NOW)
    assert [(i.title, i.due) for i in items] == [
        ("HW 5", datetime(2026, 11, 4, 23, 59, tzinfo=EST)),
        ("Lab", datetime(2026, 11, 5, 17, 0, tzinfo=EST)),
        ("Quiz 2", datetime(2026, 11, 10, 9, 0, tzinfo=EST)),
    ]
    assert items[2].kind == "quiz"


def test_api_paths():
    start, end = canvas.window("today", NOW)
    path = canvas.planner_path(start, end)
    assert path.startswith("/api/v1/planner/items?") and "per_page=100" in path and "2026-11-04T00%3A00%3A00-05%3A00" in path
    hw5 = next(i for i in ITEMS if i.title == "HW 5")
    assert canvas.item_api(hw5) == "/api/v1/courses/11/assignments/501"
    assert canvas.item_api(next(i for i in ITEMS if i.title == "Quiz 2")) == "/api/v1/courses/33/quizzes/77"
    assert canvas.item_api(next(i for i in ITEMS if i.kind == "discussion")) == "/api/v1/courses/44/discussion_topics/9"
    link = '<https://canvas.vt.edu/api/v1/planner/items?page=2&per_page=100>; rel="next", <https://x/first>; rel="first"'
    assert canvas.next_page(link, ORIGIN) == "/api/v1/planner/items?page=2&per_page=100"
    assert canvas.next_page('<https://evil.example/x>; rel="next"', ORIGIN) is None
    assert canvas.next_page("", ORIGIN) is None


def test_description_of_an_item_from_api_json():
    assert canvas.description({"description": "<p>Do it</p>"}) == "Do it"
    assert canvas.description({"message": "<p>Discuss</p>"}) == "Discuss"
    assert canvas.description({}) == ""


def test_item_is_frozen():
    item = Item("T", "C", NOW, "https://x", "assignment")
    try:
        item.title = "U"
    except AttributeError:
        return
    raise AssertionError("Item must be immutable")


def test_weekday_ranges():
    assert canvas.window("friday", NOW) == (datetime(2026, 11, 6, tzinfo=EST), datetime(2026, 11, 7, tzinfo=EST))
    assert canvas.window("wednesday", NOW)[0] == datetime(2026, 11, 4, tzinfo=EST)  # today is Wednesday
    assert titles(canvas.due(ITEMS, "friday", NOW)) == ["Lab 3 report"]
    assert canvas.range_words("friday") == "on Friday"


def test_done_items_are_marked_in_choices():
    reading = next(i for i in ITEMS if i.title == "Reading response")
    assert canvas.label(reading, NOW).startswith("✓ Reading")
    lab3 = next(i for i in ITEMS if i.title == "Lab 3 report")
    assert canvas.label(lab3, NOW) == "Lab 3 report · Foundations · Fri"


def test_question_words_dont_count_as_item_words():
    ref = canvas.parse_ref("what does my homework for today say", NOW)
    assert ref.range == "today" and ref.words == ()
    assert canvas.parse_ref("what's the thermo homework about", NOW).words == ("thermo",)


def test_course_codes_answer_to_the_subject():
    lab3 = next(i for i in ITEMS if i.title == "Lab 3 report")
    phys = canvas.Item("Pre-lab", "PHYS 2306", NOW, "https://x", "assignment")
    assert canvas.only([lab3, phys], ("physics",)) == [lab3, phys]
    assert canvas.only([phys], ("chemistry",)) == []
