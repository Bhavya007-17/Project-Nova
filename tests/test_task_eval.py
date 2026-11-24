"""`nova eval --tasks` (Stage 07d): the task file loads into numbered rows and wanted rows, picks are scored per step
and per task, the history each step sees is the labels of the steps before it."""

import math

import pytest

from nova import task_eval as te
from nova.brain.task import Choice

NOTEPAD = ['menu item "File"', 'menu item "Edit"', 'menu item "View"', 'button "Settings"', 'edit "Text editor"']
VIEW_OPEN = ['menu item "Zoom" closed (View menu)', 'menu item "Status bar" on (View menu)',
             'menu item "Word wrap" off (View menu)', *NOTEPAD]


def load(text, tmp_path):
    path = tmp_path / "tasks.yaml"
    path.write_text(text, encoding="utf-8")
    return te.load_tasks(path)


WORD_WRAP = """
screens:
  notepad: &notepad
    - menu item "File"
    - menu item "Edit"
    - menu item "View"
    - button "Settings"
    - edit "Text editor"
tasks:
  - goal: turn on word wrap
    app: Notepad
    title: Untitled - Notepad
    steps:
      - ui: *notepad
        want: [{click: View}, {press: alt+v}]
      - ui: [menu item "Zoom" closed (View menu), menu item "Status bar" on (View menu),
             menu item "Word wrap" off (View menu), *notepad]
        want: {click: Word wrap}
      - ui: *notepad
        want: done
"""


def test_rows_are_numbered_in_order_and_nested_screens_flatten(tmp_path):
    (task,) = load(WORD_WRAP, tmp_path)
    first, second, last = task.steps
    assert first.ui == ('1 menu item "File"', '2 menu item "Edit"', '3 menu item "View"', '4 button "Settings"',
                        '5 edit "Text editor"')
    assert second.ui[2] == '3 menu item "Word wrap" off (View menu)' and second.ui[3] == '4 menu item "File"'
    assert (task.app, first.title) == ("Notepad", "Untitled - Notepad")
    assert last.wants == (te.Want("done"),)


def test_wanted_rows_are_found_by_name(tmp_path):
    (task,) = load(WORD_WRAP, tmp_path)
    assert task.steps[0].wants == (te.Want("click", rows=(3,), name="View"), te.Want("press", keys="alt+v"))
    assert task.steps[1].wants == (te.Want("click", rows=(3,), name="Word wrap"),)


def test_recorded_lines_keep_their_text_but_get_renumbered(tmp_path):
    (task,) = load("""
tasks:
  - goal: save it
    steps:
      - ui: ['7 menu item "File"', '8 button "Save"']
        want: {click: Save}
""", tmp_path)
    assert task.steps[0].ui == ('1 menu item "File"', '2 button "Save"') and task.steps[0].wants[0].rows == (2,)


@pytest.mark.parametrize("want, error", [
    ("{click: Nowhere}", "no row"),
    ("{click: Close}", "2 rows"),
    ("{hover: View}", "want"),
])
def test_a_want_that_does_not_match_the_rows_is_an_authoring_error(tmp_path, want, error):
    with pytest.raises(ValueError, match=error):
        load(f"""
tasks:
  - goal: close it
    steps:
      - ui: [button "Close", button "Close", menu item "View"]
        want: {want}
""", tmp_path)


def test_a_name_shared_by_two_rows_is_told_apart_by_its_area(tmp_path):
    (task,) = load("""
tasks:
  - goal: close the tab
    steps:
      - ui: [button "Close" (Tabs tab), button "Close"]
        want: {click: Close, area: Tabs tab}
""", tmp_path)
    assert task.steps[0].wants[0].rows == (1,)


def test_typing_and_keys_and_scrolling(tmp_path):
    (task,) = load("""
tasks:
  - goal: find budget
    steps:
      - ui: [edit "Find", button "Find next"]
        want: {type: budget, into: Find}
      - ui: [edit "Find", button "Find next"]
        want: [{press: Enter}, {click: Find next}]
      - ui: [list item "a.txt"]
        want: {scroll: down}
""", tmp_path)
    first, second, third = task.steps
    assert first.wants == (te.Want("type", rows=(1,), text="budget", name="Find"),)
    assert second.wants == (te.Want("press", keys="enter"), te.Want("click", rows=(2,), name="Find next"))
    assert third.wants == (te.Want("scroll", text="down"),)


def test_each_step_sees_the_labels_of_the_wanted_steps_before_it(tmp_path):
    (task,) = load(WORD_WRAP, tmp_path)
    assert te.history(task) == ((), ("Click “View”",), ("Click “View”", "Click “Word wrap”"))
    (typed,) = load("""
tasks:
  - goal: new folder receipts
    steps:
      - ui: [button "New"]
        want: {click: New}
      - ui: [edit "Name"]
        want: {type: receipts}
      - ui: [edit "Name"]
        want: {press: enter}
      - ui: [button "New"]
        want: done
""", tmp_path)
    assert te.history(typed)[-1] == ("Click “New”", "Type “receipts”", "Press Enter")


def test_a_click_is_right_only_on_a_wanted_row_with_the_wanted_verb():
    want = (te.Want("click", rows=(3,)), te.Want("press", keys="alt+v"))
    assert te.right(Choice(row=3), want)
    assert not te.right(Choice(row=2), want)
    assert not te.right(Choice(row=3, verb="double"), want)
    assert te.right(Choice(verb="press", keys="alt+v"), want)
    assert te.right(Choice(verb="press", keys="v+alt"), want)
    assert te.right(Choice(verb="press", keys="control+s"), (te.Want("press", keys="ctrl+s"),))
    assert not te.right(Choice(verb="press", keys="alt"), want)
    assert not te.right(None, want)
    assert not te.right(Choice(done=True), want)


def test_typing_is_right_with_the_words_on_the_field_or_where_the_focus_is():
    want = (te.Want("type", rows=(1,), text="budget"),)
    assert te.right(Choice(row=1, verb="type", text="budget"), want)
    assert te.right(Choice(row=None, verb="type", text="Budget."), want)
    assert not te.right(Choice(row=2, verb="type", text="budget"), want)
    assert not te.right(Choice(row=1, verb="type", text="budget report"), want)


def test_done_ask_and_scroll():
    assert te.right(Choice(done=True), (te.Want("done"),))
    assert not te.right(Choice(row=1), (te.Want("done"),))
    assert te.right(Choice(ask="Which file?"), (te.Want("ask"),))
    assert not te.right(Choice(done=True), (te.Want("ask"),))
    assert te.right(Choice(verb="scroll", text="down"), (te.Want("scroll", text="down"),))
    assert te.right(Choice(verb="scroll"), (te.Want("scroll", text="down"),))  # no direction = down
    assert not te.right(Choice(verb="scroll", text="up"), (te.Want("scroll", text="down"),))


def test_run_feeds_each_step_its_rows_and_history_and_scores_it(tmp_path):
    (task,) = load(WORD_WRAP, tmp_path)
    seen = []
    answers = iter([({"step": {"row": 3, "verb": "click"}}, 210.0, ""),
                    ({"step": {"row": 1, "verb": "click"}}, 300.0, ""),
                    (None, 5000.0, "timed out")])

    def pick(goal, lines, did, app, title):
        seen.append((goal, lines, did, app, title))
        return next(answers)

    items = te.run([task], pick)
    assert [i.ok for i in items] == [True, False, False]
    assert seen[1][1] == task.steps[1].ui and seen[1][2] == ("Click “View”",)
    assert seen[0][3:] == ("Notepad", "Untitled - Notepad")
    assert items[1].got == 'click 1 "Zoom"' and items[1].want == 'click 3 "Word wrap"'
    assert items[2].got == "nothing (timed out)" and items[2].ms == 5000.0


def test_summary_scores_steps_and_whole_tasks_and_the_pick_time():
    items = [te.Item(0, 0, "a", "", "", True, 100.0), te.Item(0, 1, "a", "", "", True, 300.0),
             te.Item(1, 0, "b", "", "", True, 200.0), te.Item(1, 1, "b", "", "", False, 400.0)]
    summary = te.summarize(items)
    assert (summary.steps, summary.steps_right, summary.tasks, summary.tasks_right) == (4, 3, 2, 1)
    assert summary.step_rate == 0.75 and summary.task_rate == 0.5
    assert summary.pick_p50 == 250.0 and math.isclose(summary.pick_p95, 385.0)


def test_the_shipped_task_file_loads_with_twenty_goals_or_more():
    tasks = te.load_tasks()
    assert len(tasks) >= 20
    assert sum(len(t.steps) for t in tasks) >= 50
    assert all(t.steps[-1].wants[0].verb in ("done", "ask") for t in tasks)
    for t in tasks:
        for s in t.steps:
            assert 0 < len(s.ui) <= 60
