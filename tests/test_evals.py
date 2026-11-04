"""`nova eval` scoring (SPEC §18): action + key arguments, not exact strings."""

import pytest
from catalog_fixture import CATALOG

from nova.brain.plan import Plan, act, step
from nova.evals import Item, parse_want, score, summarize


def ok(plan, want):
    return score(plan, parse_want(want, CATALOG), CATALOG)


def test_open_means_open_or_switch_by_app_or_site():
    assert ok(act(step("open_app", app="Google Chrome")), "open chrome")
    assert ok(act(step("switch_app", app="Google Chrome")), "open Google Chrome")
    assert ok(act(step("open_url", url="https://canvas.vt.edu/courses")), "open canvas")
    assert ok(act(step("open_url", url="https://www.reddit.com")), "open reddit.com")
    assert not ok(act(step("open_app", app="Notepad")), "open chrome")


def test_window_ops_and_their_layout_twins():
    assert ok(act(step("window", op="snap_left", target="Google Chrome")), "window snap_left Google Chrome")
    assert ok(act(step("layout", slots=({"app": "Google Chrome", "area": "left"},))), "window snap_left Google Chrome")
    assert ok(act(step("window", op="fullscreen", target="active")), "window maximize|fullscreen active")
    assert not ok(act(step("window", op="minimize", target="active")), "window maximize|fullscreen active")
    assert not ok(act(step("window", op="snap_left", target="Notepad")), "window snap_left Google Chrome")


def test_layouts_compare_as_sets():
    plan = act(step("layout", slots=({"app": "Claude", "area": "right"}, {"app": "Autodesk Fusion", "area": "left"})))
    assert ok(plan, "layout Autodesk Fusion:left, Claude:right")
    assert not ok(plan, "layout Autodesk Fusion:right, Claude:left")


def test_search_queries_ignore_case_punctuation_and_edge_fillers():
    plan = act(step("web_search", query="Lofi hip-hop", engine="youtube"))
    assert ok(plan, "search@youtube lofi hip hop")
    assert not ok(plan, "search lofi hip hop")  # engine matters
    assert ok(act(step("web_search", query="some thermo notes", engine="google")), "search thermo notes")
    assert not ok(act(step("web_search", query="thermo", engine="google")), "search thermo notes")


def test_typing_and_keys():
    assert ok(act(step("type_text", text="Hello, World", submit=False)), "type hello world")
    assert not ok(act(step("type_text", text="hello world", submit=True)), "type hello world")
    assert ok(act(step("type_text", text="see you", submit=True)), "type+enter see you")
    assert ok(act(step("press", keys="shift+ctrl+t")), "press ctrl+shift+t")


def test_simple_ops_volume_media_scroll():
    assert ok(act(step("volume", op="set", level=40)), "volume set 40")
    assert not ok(act(step("volume", op="set", level=30)), "volume set 40")
    assert ok(act(step("volume", op="up", step=20)), "volume up")
    assert ok(act(step("media", op="play_pause")), "media pause|play_pause")
    assert ok(act(step("scroll", dir="down", amount="small")), "scroll down")
    assert ok(act(step("tab", op="reopen")), "tab reopen")
    assert ok(act(step("page_click", description="the first email")), "click")


def test_chains_and_alternatives():
    plan = act(step("open_app", app="Notepad"), step("type_text", text="hello", submit=False))
    assert ok(plan, "open Notepad ; type hello")
    assert not ok(act(step("open_app", app="Notepad")), "open Notepad ; type hello")
    assert ok(act(step("open_url", url="https://www.youtube.com")), "open chrome ; open youtube || open youtube")


def test_kinds():
    question = Plan("clarify", question="Open which one?", choices=("A", "B"))
    assert ok(question, "clarify") and not ok(question, "open chrome")
    handoff = Plan("handoff", handoff={"to": "chatgpt", "mode": "text", "prompt": "x"})
    assert ok(handoff, "handoff chatgpt") and ok(handoff, "handoff") and not ok(handoff, "handoff claude")
    assert score(None, parse_want("teach", CATALOG), CATALOG, kind="teach")
    assert not score(None, parse_want("open chrome", CATALOG), CATALOG)


@pytest.mark.parametrize("want", ["window wobble active", "open", "search@altavista cats", "layout chrome", "fly"])
def test_bad_wants_are_errors(want):
    with pytest.raises(ValueError):
        parse_want(want, CATALOG)


def test_summary_numbers():
    items = [
        Item("open chrome", "positive", True, "0", "act", 2.0, None, "open_app"),
        Item("open the thing", "positive", False, "1", "act", 900.0, 600.0, "clarify"),
        Item("we went home", "negative", True, "1", "ignore", 800.0, 500.0, ""),
        Item("dude just send it", "negative", False, "1", "act", 700.0, 400.0, ""),
    ]
    s = summarize(items)
    assert s.positives == 2 and s.correct == 1 and s.negatives == 2 and s.false_actions == 1
    assert s.accuracy == 0.5 and s.false_rate == 0.5
    assert s.tier1_p50 == 800.0 and s.by_action["open_app"] == (1, 1) and s.by_action["clarify"] == (0, 1)


def test_active_is_the_window_in_front():
    plan = act(step("window", op="fullscreen", target="active"))
    assert score(plan, parse_want("window fullscreen chrome", CATALOG), CATALOG, front="Google Chrome")
    assert not score(plan, parse_want("window fullscreen chrome", CATALOG), CATALOG, front="Notepad")


def test_the_app_just_opened_is_in_front():
    plan = act(step("open_app", app="Notepad"), step("window", op="snap_left", target="active"))
    assert score(plan, parse_want("open Notepad ; window snap_left Notepad", CATALOG), CATALOG, front="Google Chrome")
