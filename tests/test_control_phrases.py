"""Stage 07c phrases: numbers on the screen, clicks by name, steady scrolling, documents, screenshots, files, help.

Tier 0 turns them into steps; the gate lets a bare number / "stop" through while numbers / scrolling are up; the
router answers those without parsing.
"""

import pytest
from catalog_fixture import CATALOG

from nova.brain.plan import step
from nova.brain.router import Router
from nova.brain.tier0 import Tier0Resolver, describe, parse
from nova.gate import Addressing, Env, WakeName, decide

RESOLVER = Tier0Resolver(CATALOG)
WAKE = WakeName.of("Nova")


def one(text):
    plan = parse(text, CATALOG)
    assert plan is not None and plan.kind == "act", text
    assert len(plan.steps) == 1, plan.steps
    return plan.steps[0]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # numbers on the screen
        ("click 12", step("pick", n=12)),
        ("click number twelve", step("pick", n=12)),
        ("click twenty one", step("pick", n=21)),
        ("click on 5", step("pick", n=5)),
        ("select 2", step("pick", n=2)),
        ("tap tag four", step("pick", n=4)),
        ("double click 7", step("pick", n=7, verb="double")),
        ("right click 3", step("pick", n=3, verb="right")),
        ("right-click 3", step("pick", n=3, verb="right")),
        ("hover over 4", step("pick", n=4, verb="hover")),
        ("drag 3 to 9", step("pick", n=3, to=9)),
        ("drag number three onto twelve", step("pick", n=3, to=12)),
        ("drag five over to nine", step("pick", n=5, to=9)),
        ("drag twenty one to thirty two", step("pick", n=21, to=32)),
        ("show numbers", step("numbers", op="show")),
        ("numbers", step("numbers", op="show")),
        ("numbers here", step("numbers", op="show", scope="here")),
        ("show numbers everywhere", step("numbers", op="show", scope="everywhere")),
        ("show me the numbers on the whole screen", step("numbers", op="show", scope="everywhere")),
        ("hide the numbers", step("numbers", op="hide")),
        ("hide grid", step("numbers", op="hide")),
        ("more numbers", step("numbers", op="more")),
        ("show grid", step("grid")),
        ("grid everywhere", step("grid", scope="everywhere")),
        # by name
        ("click sign in", step("ui_click", name="sign in")),
        ("click the blue button", step("ui_click", name="the blue button")),
        ("press the submit button", step("ui_click", name="the submit button")),
        ("double click the readme", step("ui_click", name="the readme", count=2)),
        ("right click the recycle bin", step("ui_click", name="recycle bin", button="right")),
        ("hover over the settings button", step("ui_click", name="settings button", button="hover")),
        ("click the first link", step("page_click", description="the first link")),
        # steady scrolling
        ("keep scrolling", step("autoscroll", op="start", dir="down")),
        ("keep scrolling up slowly", step("autoscroll", op="start", dir="up", speed="slow")),
        ("scroll down slowly", step("autoscroll", op="start", dir="down", speed="slow")),
        ("scroll continuously", step("autoscroll", op="start", dir="down")),
        ("faster", step("autoscroll", op="faster")),
        ("a bit slower", step("autoscroll", op="slower")),
        ("stop scrolling", step("autoscroll", op="stop")),
        # documents
        ("go to page 12", step("doc", op="goto_page", page=12)),
        ("page five", step("doc", op="goto_page", page=5)),
        ("next page", step("doc", op="next_page")),
        ("previous page", step("doc", op="prev_page")),
        ("zoom in", step("doc", op="zoom_in")),
        ("zoom out", step("doc", op="zoom_out")),
        ("reset zoom", step("doc", op="zoom_reset")),
        ("fit to width", step("doc", op="fit_width")),
        # screenshots, camera, the computer
        ("take a screenshot", step("screenshot")),
        ("screenshot the window", step("screenshot", target="window")),
        ("take a screenshot of the screen", step("screenshot", target="screen")),
        ("take a photo", step("camera", op="photo")),
        ("take a selfie", step("camera", op="photo")),
        ("lock the computer", step("system", op="lock")),
        ("put the laptop to sleep", step("system", op="sleep")),
        # folders and files
        ("open downloads", step("open_path", name="downloads")),
        ("open my documents folder", step("open_path", name="documents")),
        ("go up a folder", step("nav", op="up")),
        ("open the budget spreadsheet", step("open_path", name="budget spreadsheet")),
        ("open the report pdf in downloads", step("open_path", name="report pdf", folder="downloads")),
        ("open the lab report in documents", step("open_path", name="lab report", folder="documents")),
        ("pull up resume from my desktop", step("open_path", name="resume", folder="desktop")),
        # Nova itself
        ("what can i say", step("nova", op="help")),
        ("what can i say about the web", step("nova", op="help", topic="web")),
        ("help", step("nova", op="help")),
        ("do that again", step("nova", op="again")),
        ("undo that", step("nova", op="undo")),
    ],
)
def test_phrases(text, expected):
    assert one(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("page down", step("scroll", dir="down", amount="page")),
        ("go back", step("nav", op="back")),
        ("go to sleep", step("listening", op="sleep")),
        ("open a new tab", step("tab", op="new")),
        ("open word", step("open_app", app="Word")),
        ("redo", step("press", keys="ctrl+y")),
    ],
)
def test_older_phrases_unchanged(text, expected):
    assert one(text) == expected


def test_a_name_that_is_not_a_number_stays_a_name():
    assert one("click on first") == step("ui_click", name="first")
    assert one("click the second result") != step("pick", n=2)


def test_island_lines():
    def said(text):
        return describe(parse(text, CATALOG))

    assert said("right click 3") == "Right-click 3"
    assert said("drag 3 to 9") == "Drag 3 to 9"
    assert said("double click the readme") == "Double-click “the readme”"
    assert said("put the computer to sleep") == "Put the computer to sleep"
    assert said("open downloads") == "Open downloads"


def env(**kw):
    return Env(**{"wake": WAKE, "always_listening": False, "resolver": RESOLVER, **kw})


def test_a_bare_number_picks_only_while_numbers_show():
    assert decide("seven", Addressing(), env(numbers=True)).via == "numbers"
    assert decide("seven", Addressing(), env(numbers=True)).text == "seven"
    assert not decide("seven", Addressing(), env()).accepted
    assert decide("um, number twelve", Addressing(), env(numbers=True)).text == "number twelve"
    assert decide("click 12", Addressing(), env(numbers=True)).via == "numbers"
    assert decide("right click 4", Addressing(), env(numbers=True)).text == "right click 4"
    assert decide("hide the numbers", Addressing(), env(numbers=True)).accepted
    assert not decide("open notepad", Addressing(), env(numbers=True)).accepted  # still needs the name


def test_a_bare_stop_stops_steady_scrolling():
    assert decide("stop", Addressing(), env(scrolling=True)).via == "scrolling"
    assert decide("faster", Addressing(), env(scrolling=True)).via == "scrolling"
    assert not decide("stop", Addressing(), env()).accepted
    assert not decide("seven", Addressing(), env(scrolling=True)).accepted


def test_steering_needs_quiet_speakers_and_an_awake_nova():
    assert not decide("seven", Addressing(), env(numbers=True, speakers=True)).accepted
    assert not decide("seven", Addressing(asleep=True), env(numbers=True)).accepted
    assert not decide("seven", Addressing(), env(numbers=True, mic_busy="Teams")).accepted


def test_router_answers_a_bare_number_and_stop_without_parsing():
    router = Router(numbers=lambda: True, scrolling=lambda: True)
    routed = router.route("twelve", None, CATALOG)
    assert routed.tier == "0" and routed.plan.steps == (step("pick", n=12),)
    assert router.route("stop", None, CATALOG).plan.steps == (step("autoscroll", op="stop"),)
    quiet = Router()
    assert quiet.route("stop", None, CATALOG).plan is None or quiet.route("stop", None, CATALOG).plan.steps != (
        step("autoscroll", op="stop"),)
