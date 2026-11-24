"""Click by meaning's Tier 1 side (``nova.brain.meaning``) and the ``ui`` rows in the request — no model."""

import pytest

from nova.brain import meaning, prompt
from nova.brain.plan import PlanError, from_dict
from nova.brain.tier1 import Thought


def act(step, confidence=0.85):
    return {"kind": "act", "steps": [step], "confidence": confidence}


def test_parse_takes_one_ui_click_with_a_number():
    assert meaning.parse(act({"action": "ui_click", "name": "x", "element": 3})) == (3, 0.85)
    assert meaning.parse(act({"action": "ui_click", "name": "x", "element": 2}, confidence=True)) == (2, 0.0)


def test_parse_refuses_everything_else():
    for data in (None, [], {"kind": "clarify", "steps": [], "confidence": 0.4},
                 act({"action": "ui_click", "name": "x"}),  # no number: it didn't find it in the rows
                 act({"action": "ui_click", "name": "x", "element": "3"}),
                 act({"action": "ui_click", "name": "x", "element": True}),
                 act({"action": "press", "keys": "ctrl+n"}),
                 {"kind": "act", "steps": [{"action": "ui_click", "name": "x", "element": 1}] * 2, "confidence": 0.9}):
        assert meaning.parse(data) is None


class FakeTier1:
    def __init__(self, thought):
        self.thought, self.calls = thought, []

    def think(self, text, seen, *, ui=None):
        self.calls.append((text, seen, ui))
        return self.thought


def test_asker_sends_the_rows_and_reads_the_pick():
    tier1 = FakeTier1(Thought(act({"action": "ui_click", "name": "the wrap thing", "element": 1}, 0.9)))
    ask = meaning.asker(tier1, front=lambda: "Notepad")
    assert ask("the wrap thing", ['1 menu item "Word wrap" off (View menu)']) == (1, 0.9)
    text, seen, ui = tier1.calls[0]
    assert text == "click the wrap thing" and seen.front == "Notepad"
    assert ui == ['1 menu item "Word wrap" off (View menu)']


def test_asker_gives_none_when_the_model_fails():
    assert meaning.asker(FakeTier1(Thought(error="timed out")))("x", ["1 button \"A\""]) is None


def test_request_carries_the_rows_and_the_schema_allows_element():
    data = prompt.request("click the wrap thing", front="Notepad", open_apps=["Notepad"], tab=None, apps=[],
                          ui=['1 menu item "Word wrap" off (View menu)'])
    assert data["ui"] == ['1 menu item "Word wrap" off (View menu)']
    assert "ui" not in prompt.request("hi", front=None, open_apps=[], tab=None, apps=[])
    steps = prompt.SCHEMA["properties"]["steps"]["items"]
    ui_click = next(s for s in steps["anyOf"] if s["properties"]["action"].get("const") == "ui_click")
    assert ui_click["properties"]["element"] == {"type": "integer"}


def test_plan_check_takes_an_element():
    plan = from_dict(act({"action": "ui_click", "name": "zoom", "element": 4}))
    assert plan.steps[0].get("element") == 4
    with pytest.raises(PlanError):
        from_dict(act({"action": "ui_click", "name": "zoom", "element": 0}))
