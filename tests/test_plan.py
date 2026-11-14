"""Plan schema (SPEC §7.5): validation of plans from Tier 0 now and the LLM later."""

import pytest

from nova.brain.plan import Plan, PlanError, Step, act, from_dict, keys_ok, step


def test_step_args_are_read_only_and_compare_by_value():
    a = step("open_app", app="Google Chrome")
    assert a == Step("open_app", {"app": "Google Chrome"})
    assert a.get("app") == "Google Chrome" and a.get("missing", 3) == 3
    with pytest.raises(TypeError):
        a.args["app"] = "x"  # type: ignore[index]


def test_act_plan_round_trips_through_dict():
    plan = act(step("window", op="snap_left", target="Claude"), step("type_text", text="hi", submit=True), say="ok")
    data = plan.to_dict()
    assert data["kind"] == "act" and data["steps"][0] == {"action": "window", "op": "snap_left", "target": "Claude"}
    assert from_dict(data) == plan


@pytest.mark.parametrize(
    "data",
    [
        {"kind": "act", "steps": [{"action": "explode"}]},  # unknown action
        {"kind": "act", "steps": [{"action": "open_app"}]},  # missing app
        {"kind": "act", "steps": [{"action": "window", "op": "spin"}]},  # unknown op
        {"kind": "act", "steps": []},  # act with nothing to do
        {"kind": "act", "steps": [{"action": "type_text", "text": ""}]},
        {"kind": "act", "steps": [{"action": "volume", "op": "set", "level": 140}]},
        {"kind": "act", "steps": [{"action": "open_url", "url": "file:///C:/Windows"}]},  # only http(s)
        {"kind": "act", "steps": [{"action": "layout", "slots": [{"app": "Claude", "area": "nowhere"}]}]},
        {"kind": "clarify", "question": "", "choices": []},
        {"kind": "dance"},
        {"kind": "act", "steps": [{"action": "press", "keys": "ctrl+"}]},
    ],
)
def test_bad_plans_are_refused(data):
    with pytest.raises(PlanError):
        from_dict(data)


@pytest.mark.parametrize("keys", ["ctrl+v+v", "a+b", "shift+a+b"])
def test_a_chord_with_two_keys_besides_modifiers_is_refused(keys):
    assert not keys_ok(keys)
    with pytest.raises(PlanError):
        from_dict({"kind": "act", "steps": [{"action": "press", "keys": keys}]})


@pytest.mark.parametrize("keys", ["ctrl+c", "alt+tab", "ctrl+shift+t", "enter", "v+ctrl", "win", "ctrl+alt+e"])
def test_ordinary_chords_are_valid(keys):
    assert keys_ok(keys)


def test_clarify_plan_keeps_one_option_per_choice():
    options = (act(step("open_app", app="Google Chrome")), act(step("open_app", app="Calculator")))
    plan = Plan("clarify", question="Which one?", choices=("Google Chrome", "Calculator"), options=options)
    assert plan.option(1) == options[1]
    assert plan.option(7) is None
