"""Guide mode (Stage 07d): ring the row, wait for his own click, check, look again — on fakes with a scripted user."""

from __future__ import annotations

from nova.actions.marks import Box, Target
from nova.actions.rows import Row
from nova.brain.plan import Step
from nova.brain.risk import Scene
from nova.brain.task import GUIDE_MAX_S, NUDGE, Choice, Loop, Steer, View


def _row(number: int, name: str, role: str = "menu item", state: str = "") -> Row:
    return Row(number, Target(Box(0, number * 20, 100, number * 20 + 18), name, role, "uia", None), state=state)


MENU_BAR = (_row(1, "File", "menu"), _row(2, "Edit", "menu"), _row(3, "View", "menu"))
VIEW_MENU = (_row(1, "Zoom"), _row(2, "Status bar"), _row(3, "Word wrap"))
EDIT_MENU = (_row(1, "Undo"), _row(2, "Cut"), _row(3, "Delete"))
WRAP = "turn on word wrap"


def _sig(value: int) -> tuple[int, ...]:
    return (value,) * 10


def _middle(box: Box) -> tuple[int, int]:
    return (box.left + box.right) // 2, (box.top + box.bottom) // 2


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


class GuideWorld:
    """The user's hand is scripted: at each time, "ring" clicks the ringed row (the screen moves on), "hover" only
    lights it up (pixels change, rows don't), ("other", n, screen) clicks row n and the screen becomes ``screen``,
    ("do",) / ("guide",) switch modes the way "just do it" / "let me do it" would, ("poke", "next" | "where") is "next" /
    "where?"."""

    def __init__(self, screens, clock: Clock, user=(), *, exe: str = "notepad.exe", steer: Steer | None = None):
        self.screens = screens
        self.clock = clock
        self.user = list(user)
        self.exe = exe
        self.steer = steer
        self.at = 0
        self.hover = 0
        self.pointer: tuple[int, int] | None = None
        self.ring: Box | None = None
        self.shown: list[str] = []
        self.hidden = 0
        self.acted: list[str] = []

    def _tick(self) -> None:
        while self.user and self.user[0][0] <= self.clock.t:
            _, what = self.user.pop(0)
            if what == "ring":
                self.pointer = _middle(self.ring)
                self.at += 1
            elif what == "hover":
                self.pointer = _middle(self.ring)
                self.hover += 1
            elif what == "near":  # the mouse rests on the ring, nothing lights up
                self.pointer = _middle(self.ring)
            elif what == "away":  # clicked the ring and moved off before Nova looked
                self.pointer = (999, 999)
                self.at += 1
            elif what == "key":  # a key press: the screen moves on, the mouse doesn't
                self.at += 1
            elif what[0] == "other":
                self.pointer = _middle(self.screens[self.at][what[1] - 1].target.box)
                self.at = what[2]
            elif what[0] == "poke":  # "next" / "where?" said while the ring is up
                self.steer.poke = what[1]
            else:
                self.steer.mode = what[0]

    def look(self) -> View:
        self._tick()
        return View(self.screens[self.at], self.signature())

    def signature(self) -> tuple[int, ...]:
        self._tick()
        return _sig(self.at * 10 + self.hover)

    def where(self, s: Step | None = None) -> Scene:
        return Scene(app="Notepad", exe=self.exe, title="Untitled - Notepad")

    def act(self, row, choice) -> None:
        self.acted.append(row.target.name)
        self.at += 1

    def show(self, row: Row | None, caption: str) -> Box | None:
        self.ring = row.target.box if row is not None else None
        if not self.shown or self.shown[-1] != caption:
            self.shown.append(caption)
        return self.ring

    def hide(self) -> None:
        self.hidden += 1
        self.ring = None

    def cursor(self) -> tuple[int, int] | None:
        return self.pointer


def picker(*answers):
    calls: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
    queue = list(answers)

    def pick(goal, lines, did, app, title):
        calls.append((tuple(lines), tuple(did)))
        return queue.pop(0) if queue else None

    pick.calls = calls  # type: ignore[attr-defined]
    return pick


def run(world: GuideWorld, pick, *, mode: str = "guide", point: bool = False, stop_at: float = 1e9,
        steer: Steer | None = None):
    notes: list[str] = []
    asked: list[str] = []
    loop = Loop(world, pick, lambda q: asked.append(q) or True, clock=world.clock.now, sleep=world.clock.sleep,
                stopped=lambda: world.clock.t >= stop_at, note=notes.append, steer=steer)
    return loop.run(WRAP, mode=mode, point=point), notes, asked


def test_guide_rings_each_step_and_waits_for_his_click():
    clock = Clock()
    world = GuideWorld([MENU_BAR, VIEW_MENU, MENU_BAR], clock, [(1.0, "ring"), (2.0, "ring")])
    result, notes, asked = run(world, picker(Choice(row=3), Choice(row=3), Choice(done=True, say="Word wrap is on")))
    assert result.exit == "done" and result.detail == "Word wrap is on"
    assert world.acted == []  # he clicks, Nova only points
    assert world.shown == ["Click “View”", "Click “Word wrap”"]
    assert result.did == ("Click “View”", "Click “Word wrap”")
    assert result.kept == (Step("ui_click", {"name": "View", "role": "menu"}),
                           Step("ui_click", {"name": "Word wrap", "role": "menu item"}))
    assert world.hidden >= 1 and world.ring is None  # the ring goes when the task ends
    assert notes == [] and asked == []
    assert len(result.ring_ms) == 1 and all(ms <= 800 for ms in result.ring_ms)  # his click -> next ring (SPEC §16)
    assert len(result.step_ms) == 2  # his click -> next pick answered, for each click


def test_a_hover_is_not_a_click():
    clock = Clock()
    world = GuideWorld([MENU_BAR, VIEW_MENU, MENU_BAR], clock,
                       [(0.5, "hover"), (1.0, "hover"), (2.0, "ring"), (3.0, "ring")])
    pick = picker(Choice(row=3), Choice(row=3), Choice(done=True))
    result, _, _ = run(world, pick)
    assert result.did == ("Click “View”", "Click “Word wrap”")
    assert len(pick.calls) == 3  # no extra look-and-pick for the hovers


def test_a_wrong_click_says_what_it_opened_and_goes_on_from_there():
    clock = Clock()
    world = GuideWorld([MENU_BAR, EDIT_MENU], clock, [(1.0, ("other", 2, 1))])
    pick = picker(Choice(row=3), Choice(done=True, say="ok"))
    result, notes, _ = run(world, pick)
    assert notes == ["That opened “Edit” — the step was “View”"]
    assert pick.calls[1][0] == tuple(r.text for r in EDIT_MENU)  # looked again from where he is now
    assert result.did == ()


def test_a_click_counts_when_the_mouse_moved_off_before_nova_looked():
    clock = Clock()
    world = GuideWorld([MENU_BAR, VIEW_MENU], clock, [(0.5, "near"), (1.0, "away")])
    result, notes, _ = run(world, picker(Choice(row=3), Choice(done=True)))
    assert result.did == ("Click “View”",) and notes == []


def test_a_key_step_has_no_ring_and_any_real_change_counts():
    clock = Clock()
    world = GuideWorld([MENU_BAR, VIEW_MENU], clock, [(1.0, "key")])
    result, _, _ = run(world, picker(Choice(verb="press", keys="ctrl+s"), Choice(done=True)))
    assert world.shown == ["Press Ctrl + S"] and result.did == ("Press Ctrl + S",)


def test_a_nudge_after_30_s_and_out_after_3_min():
    clock = Clock()
    world = GuideWorld([MENU_BAR], clock)
    result, notes, _ = run(world, picker(Choice(row=3), Choice(row=3)))
    assert notes == [NUDGE]
    assert result.exit == "limit"
    assert GUIDE_MAX_S <= clock.t < GUIDE_MAX_S + 1
    assert world.ring is None


def test_next_counts_a_step_nova_could_not_see():
    clock = Clock()
    steer = Steer()
    world = GuideWorld([MENU_BAR, VIEW_MENU], clock, [(1.0, ("poke", "next"))], steer=steer)
    pick = picker(Choice(row=3), Choice(done=True))
    result, notes, _ = run(world, pick, steer=steer)
    assert result.did == ("Click “View”",) and notes == []
    assert pick.calls[1] == (tuple(r.text for r in MENU_BAR), ("Click “View”",))  # looked again, from his word
    assert steer.poke == ""


def test_a_next_is_not_timed_as_his_click():
    clock = Clock()
    steer = Steer()
    world = GuideWorld([MENU_BAR, VIEW_MENU], clock, [(1.0, ("poke", "next")), (2.0, "ring")], steer=steer)
    result, _, _ = run(world, picker(Choice(row=3), Choice(row=3), Choice(done=True)), steer=steer)
    assert len(result.did) == 2 and result.ring_ms == ()  # his word, not a click the pixels saw: no click -> ring


def test_where_rings_it_again_and_restarts_the_nudge_clock():
    clock = Clock()
    steer = Steer()
    world = GuideWorld([MENU_BAR, VIEW_MENU], clock, [(20.0, ("poke", "where")), (45.0, "ring")], steer=steer)
    result, notes, _ = run(world, picker(Choice(row=3), Choice(done=True)), steer=steer)
    assert result.did == ("Click “View”",)
    assert notes == []  # 45 s after the ring went up, but only 25 s after "where?"
    assert world.hidden >= 2  # taken away and put back, then gone at the end


def test_a_next_said_before_the_ring_went_up_does_not_count():
    clock = Clock()
    steer = Steer()
    steer.poke = "next"
    world = GuideWorld([MENU_BAR, VIEW_MENU], clock, [(1.0, "ring")], steer=steer)
    result, _, _ = run(world, picker(Choice(row=3), Choice(done=True)), steer=steer)
    assert result.did == ("Click “View”",) and clock.t >= 1.0


def test_stop_while_guiding_takes_the_ring_away():
    clock = Clock()
    world = GuideWorld([MENU_BAR], clock)
    result, _, _ = run(world, picker(Choice(row=3)), stop_at=2.0)
    assert result.exit == "stopped" and world.ring is None


def test_just_do_it_mid_guide_does_the_ringed_step_and_the_rest():
    clock = Clock()
    steer = Steer()
    world = GuideWorld([MENU_BAR, VIEW_MENU, MENU_BAR], clock, [(1.0, ("do",))], steer=steer)
    result, _, _ = run(world, picker(Choice(row=3), Choice(row=3), Choice(done=True)), steer=steer)
    assert result.exit == "done"
    assert world.acted == ["View", "Word wrap"]
    assert world.ring is None  # the ring goes when Nova takes over


def test_let_me_do_it_mid_task_rings_the_next_step():
    clock = Clock()
    steer = Steer()

    class Handing(GuideWorld):
        def act(self, row, choice):
            super().act(row, choice)
            steer.mode = "guide"  # he said "let me do it" while the first click ran

    world = Handing([MENU_BAR, VIEW_MENU, MENU_BAR], clock, [(5.0, "ring")], steer=steer)
    result, _, _ = run(world, picker(Choice(row=3), Choice(row=3), Choice(done=True)), mode="do", steer=steer)
    assert result.exit == "done"
    assert world.acted == ["View"] and world.shown == ["Click “Word wrap”"]


def test_where_is_rings_once_without_waiting():
    clock = Clock()
    world = GuideWorld([MENU_BAR], clock)
    pick = picker(Choice(row=3))
    result, _, _ = run(world, pick, point=True)
    assert result.exit == "done" and result.detail == "There: “View”"
    assert world.shown == ["Click “View”"] and world.ring is not None  # the ring stays up (the hands time it out)
    assert len(pick.calls) == 1 and clock.t < 1


def test_guide_never_confirms_because_he_clicks_himself():
    clock = Clock()
    world = GuideWorld([MENU_BAR, EDIT_MENU, MENU_BAR], clock, [(1.0, "ring"), (2.0, "ring")])
    result, _, asked = run(world, picker(Choice(row=2), Choice(row=3), Choice(done=True)))
    assert result.did == ("Click “Edit”", "Click “Delete”") and asked == []


def test_settings_run_guide_mode_and_do_it_there_keeps_pointing():
    clock = Clock()
    steer = Steer()
    world = GuideWorld([MENU_BAR, VIEW_MENU, MENU_BAR], clock, [(1.0, ("do",)), (3.0, "ring"), (4.0, "ring")],
                       exe="SystemSettings.exe", steer=steer)
    result, notes, _ = run(world, picker(Choice(row=3), Choice(row=3), Choice(done=True)), steer=steer)
    assert world.acted == []  # never acts in settings
    assert any("only point" in n for n in notes)
    assert result.exit == "done" and steer.mode == "guide"
