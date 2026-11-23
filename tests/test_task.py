"""The do-mode loop (Stage 07d): look -> pick -> act -> settle -> check, on fakes."""

from __future__ import annotations

from nova.actions.marks import Box, Target
from nova.actions.native import ActionError
from nova.actions.rows import Row
from nova.brain.plan import Step
from nova.brain.risk import Scene
from nova.brain.task import Choice, Loop, View, keys_allowed, kept_plan, model_picker, parse, said


def _row(number: int, name: str, role: str = "menu item") -> Row:
    return Row(number, Target(Box(0, number * 20, 100, number * 20 + 18), name, role, "uia", None))


MENU_BAR = (_row(1, "File", "menu"), _row(2, "Edit", "menu"), _row(3, "View", "menu"))
VIEW_MENU = (_row(1, "Zoom"), _row(2, "Status bar"), _row(3, "Word wrap"))


def _sig(value: int) -> tuple[int, ...]:
    return (value,) * 10  # a screenful of pixel rows


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


class World:
    """Screens in order: each action moves to the next one (unless ``stuck``)."""

    def __init__(self, screens: list[tuple[Row, ...]], *, exe: str = "notepad.exe", stuck: bool = False,
                 password: bool = False, fails: bool = False, exe_after: str = "") -> None:
        self.screens = screens
        self.at = 0
        self.exe = exe
        self.stuck = stuck
        self.password = password
        self.fails = fails
        self.exe_after = exe_after
        self.acted: list[tuple[str, str, str, str]] = []
        self.flicker = 0  # signature reads that still differ after an action (settle must wait them out)

    def look(self) -> View:
        return View(self.screens[self.at], _sig(self.at))

    def signature(self) -> tuple[int, ...]:
        if self.flicker:
            self.flicker -= 1
            return _sig(100 + self.flicker)
        return _sig(self.at)

    def where(self, s: Step | None = None) -> Scene:
        exe = self.exe_after if self.exe_after and self.at else self.exe
        return Scene(app="Notepad", exe=exe, title="Untitled - Notepad", password=self.password)

    def act(self, row: Row | None, choice: Choice) -> None:
        if self.fails:
            raise ActionError("I clicked “View” but nothing changed.")
        self.acted.append((row.target.name if row else "", choice.verb, choice.text, choice.keys))
        if not self.stuck:
            self.at = min(self.at + 1, len(self.screens) - 1)


def picker(*answers: Choice | None):
    calls: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = []
    queue = list(answers)

    def pick(goal, lines, did, app, title):
        calls.append((goal, tuple(lines), tuple(did)))
        return queue.pop(0) if queue else None

    pick.calls = calls  # type: ignore[attr-defined]
    return pick


def _loop(world: World, pick, *, answer: bool | None = True, clock: Clock | None = None, stop_after: int = 999,
          **kw) -> tuple[Loop, list[str]]:
    clock = clock or Clock()
    asked: list[str] = []

    def confirm(question: str) -> bool | None:
        asked.append(question)
        return answer

    loop = Loop(world, pick, confirm, clock=clock.now, sleep=clock.sleep,
                stopped=lambda: len(world.acted) >= stop_after, **kw)
    return loop, asked


WRAP = "turn on word wrap"


def test_done_after_two_steps_keeps_a_replayable_plan():
    world = World([MENU_BAR, VIEW_MENU, MENU_BAR])
    pick = picker(Choice(row=3), Choice(row=3), Choice(done=True, say="Word wrap is on"))
    result = _loop(world, pick)[0].run(WRAP)
    assert result.exit == "done" and result.ok
    assert result.detail == "Word wrap is on"
    assert [a[0] for a in world.acted] == ["View", "Word wrap"]
    assert result.did == ("Click “View”", "Click “Word wrap”")
    assert kept_plan(result).steps == (Step("ui_click", {"name": "View", "role": "menu"}),
                                       Step("ui_click", {"name": "Word wrap", "role": "menu item"}))
    # the model saw the goal, the rows fenced as lines and the steps done so far
    assert pick.calls[1][0] == WRAP and pick.calls[1][1][2] == '3 menu item "Word wrap"'
    assert pick.calls[1][2] == ("Click “View”",)


def test_stuck_when_nothing_changes():
    world = World([MENU_BAR, VIEW_MENU], stuck=True)
    result = _loop(world, picker(Choice(row=3)))[0].run(WRAP)
    assert result.exit == "stuck"
    assert result.detail == "That didn't change anything — stopped at step 1: Click “View”."


def test_limit_after_max_steps():
    world = World([MENU_BAR, VIEW_MENU] * 10)
    result = _loop(world, picker(*[Choice(row=1)] * 20), max_steps=3)[0].run(WRAP)
    assert result.exit == "limit"
    assert len(world.acted) == 3
    assert result.detail.startswith("I stopped after 3 steps: ")


def test_limit_on_time():
    clock = Clock()
    world = World([MENU_BAR, VIEW_MENU] * 10)

    def slow(goal, lines, did, app, title):
        clock.t += 25.0
        return Choice(row=1)

    result = _loop(world, slow, clock=clock)[0].run(WRAP)
    assert result.exit == "limit" and len(world.acted) == 2  # 25, 50, 75 s: the third pick ran past 60 s


def test_stopped_by_the_user():
    world = World([MENU_BAR, VIEW_MENU, MENU_BAR])
    result = _loop(world, picker(Choice(row=3), Choice(row=3)), stop_after=1)[0].run(WRAP)
    assert result.exit == "stopped" and len(world.acted) == 1


def test_failed_when_the_model_has_no_answer():
    result = _loop(World([MENU_BAR]), picker(None))[0].run(WRAP)
    assert result.exit == "failed"
    assert result.detail == "I couldn't finish — show me?"


def test_failed_on_a_row_that_is_not_there():
    result = _loop(World([MENU_BAR]), picker(Choice(row=9)))[0].run(WRAP)
    assert result.exit == "failed"


def test_failed_when_the_action_fails():
    world = World([MENU_BAR, VIEW_MENU], fails=True)
    result = _loop(world, picker(Choice(row=3)))[0].run(WRAP)
    assert result.exit == "failed" and "nothing changed" in result.detail


def test_asked_passes_the_question_on():
    result = _loop(World([MENU_BAR]), picker(Choice(ask="Which file?")))[0].run("open the report")
    assert result.exit == "asked" and result.detail == "Which file?"


def test_risky_step_confirms_and_declined_stops():
    rows = (_row(1, "Send", "button"),)
    world = World([rows, MENU_BAR])
    loop, asked = _loop(world, picker(Choice(row=1)), answer=False)
    result = loop.run("send it")
    assert asked == ["Click “Send”?"]
    assert result.exit == "declined" and world.acted == []


def test_risky_step_confirmed_goes_ahead():
    world = World([(_row(1, "Send", "button"),), MENU_BAR])
    loop, asked = _loop(world, picker(Choice(row=1), Choice(done=True)), answer=True)
    assert loop.run("send it").exit == "done" and asked and world.acted


def test_settings_apps_are_guide_only():
    world = World([MENU_BAR], exe="SystemSettings.exe")
    pick = picker(Choice(row=1))
    result = _loop(world, pick)[0].run("turn on dark mode")
    assert result.exit == "guide_only" and world.acted == [] and pick.calls == []
    assert "show" in result.detail


def test_a_step_that_lands_in_settings_stops_there():
    world = World([MENU_BAR, VIEW_MENU], exe_after="SystemSettings.exe")
    result = _loop(world, picker(Choice(row=1), Choice(row=1)))[0].run("open the display settings")
    assert result.exit == "guide_only" and len(world.acted) == 1


def test_never_types_words_the_user_did_not_say():
    world = World([(_row(1, "Search", "field"),), MENU_BAR])
    result = _loop(world, picker(Choice(row=1, verb="type", text="rm -rf")))[0].run("search for cats")
    assert result.exit == "asked" and world.acted == []


def test_types_words_from_the_goal():
    world = World([(_row(1, "Search", "field"),), MENU_BAR])
    result = _loop(world, picker(Choice(row=1, verb="type", text="Cats"), Choice(done=True)))[0].run(
        "search for cats")
    assert result.exit == "done" and world.acted == [("Search", "type", "Cats", "")]


def test_never_types_into_a_password_field():
    world = World([(_row(1, "Password", "field"),), MENU_BAR], password=True)
    result = _loop(world, picker(Choice(row=1, verb="type", text="hunter2")))[0].run("type hunter2")
    assert result.exit == "failed" and world.acted == []


def test_typing_matches_whole_words_only():
    assert said("cats", "search for cats") and said("for cats", "Search for  cats!")
    assert not said("at", "search for cats")  # a piece of a word is not something he said
    assert not said("cats search", "search for cats")


def test_keys_he_said_must_be_a_whole_word_too():
    assert keys_allowed("ctrl+s", "save it with ctrl + s")
    assert not keys_allowed("s", "save it with ctrl+s")  # "s" is inside "save" and "ctrl+s", never said alone
    assert keys_allowed("enter", "anything")


def test_enter_with_an_unknown_focus_is_confirmed():
    class Unknown(World):
        def where(self, s=None):
            return Scene(app="Notepad", exe="notepad.exe", title="Untitled", focus_known=s is None)

    world = Unknown([MENU_BAR, VIEW_MENU])
    loop, asked = _loop(world, picker(Choice(verb="press", keys="enter")), answer=False)
    assert loop.run(WRAP).exit == "declined" and world.acted == []
    assert asked == ["Press Enter here? (I couldn't check what it presses)"]


def test_only_plain_keys_unless_said():
    world = World([MENU_BAR, VIEW_MENU, MENU_BAR])
    result = _loop(world, picker(Choice(verb="press", keys="ctrl+a")))[0].run(WRAP)
    assert result.exit == "asked" and world.acted == []
    world = World([MENU_BAR, VIEW_MENU, MENU_BAR])
    result = _loop(world, picker(Choice(verb="press", keys="alt"), Choice(verb="press", keys="ctrl+s"),
                                 Choice(done=True)))[0].run("save it with ctrl+s")
    assert result.exit == "done" and [a[3] for a in world.acted] == ["alt", "ctrl+s"]


def test_settle_waits_for_two_equal_reads():
    clock = Clock()
    world = World([MENU_BAR, VIEW_MENU, MENU_BAR])
    loop, _ = _loop(world, picker(Choice(row=3), Choice(done=True)), clock=clock)
    world.flicker = 3
    loop.run(WRAP)
    assert 0.35 <= clock.t <= 0.55  # three changing reads, then two equal ones, 100 ms apart


def test_settle_gives_up_after_two_seconds():
    clock = Clock()

    class Busy(World):
        def signature(self):
            return _sig(int(clock.t * 1000))  # a video: never two equal reads

    world = Busy([MENU_BAR, VIEW_MENU, MENU_BAR])
    loop, _ = _loop(world, picker(Choice(row=3), Choice(done=True)), clock=clock)
    assert loop.run(WRAP).exit == "done"
    assert 2.0 <= clock.t <= 2.2


def test_parse_the_models_answer():
    assert parse({"step": {"row": 3, "verb": "click"}, "done": False, "ask": "", "say": ""}) == Choice(row=3)
    assert parse({"step": None, "done": True, "ask": "", "say": "Done"}) == Choice(done=True, say="Done")
    assert parse({"done": False, "ask": "Which one?"}) == Choice(ask="Which one?")
    assert parse({"step": {"row": "3"}, "done": False}) is None
    assert parse({"step": {"row": 2, "verb": "fly"}, "done": False}) is None
    assert parse({"step": {"verb": "press", "keys": "enter"}}) == Choice(verb="press", keys="enter")
    assert parse("nonsense") is None
    assert parse({"done": False, "ask": "", "step": None}) is None
    # a usable step beats a question written alongside it (the model fills every string it's given)
    assert parse({"step": {"row": 3, "verb": "click"}, "done": False, "ask": "Click View?"}) == Choice(row=3)
    assert parse({"step": {"row": "x"}, "ask": "Which one?"}) == Choice(ask="Which one?")
    # the schema spells them out ("right" alone read as "correct")
    assert parse({"step": {"row": 2, "verb": "right-click"}}) == Choice(row=2, verb="right")
    assert parse({"step": {"row": 2, "verb": "double-click"}}) == Choice(row=2, verb="double")


def test_model_picker_parses_the_thought_and_maps_errors_to_none():
    class Thought:
        def __init__(self, data=None, error=None):
            self.data, self.error, self.ms = data, error, 120.0

    class Tier1:
        def __init__(self, thought):
            self.thought, self.calls = thought, []

        def pick(self, goal, lines, *, did, front, title):
            self.calls.append((goal, tuple(lines), tuple(did), front, title))
            return self.thought

    good = Tier1(Thought({"step": {"row": 3, "verb": "click"}, "done": False, "ask": "", "say": ""}))
    assert model_picker(good)(WRAP, ["3 menu \"View\""], ("x",), "Notepad", "") == Choice(row=3)
    assert good.calls == [(WRAP, ('3 menu "View"',), ("x",), "Notepad", None)]
    assert model_picker(Tier1(Thought(error="down")))(WRAP, [], (), "", "") is None


def test_progress_reports_each_step():
    seen: list[tuple[str, int]] = []
    world = World([MENU_BAR, VIEW_MENU, MENU_BAR])
    loop, _ = _loop(world, picker(Choice(row=3), Choice(row=3), Choice(done=True)),
                    progress=lambda label, n: seen.append((label, n)))
    loop.run(WRAP)
    assert seen == [("Click “View”", 1), ("Click “Word wrap”", 2)]
