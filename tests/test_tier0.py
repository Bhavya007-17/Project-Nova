"""Tier 0 (SPEC §7.2): verb synonyms + entity resolution -> Plan, with "and / then" chains."""

import pytest
from catalog_fixture import CATALOG

from nova.brain.plan import step
from nova.brain.tier0 import parse


def steps(text, strict=False):
    plan = parse(text, CATALOG, strict=strict)
    assert plan is not None, f"unresolved: {text!r}"
    assert plan.kind == "act", plan
    return list(plan.steps)


def one(text, strict=False):
    found = steps(text, strict)
    assert len(found) == 1, found
    return found[0]


CHROME = "Google Chrome"
FUSION = "Autodesk Fusion"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # open / switch: verb synonyms and loose forms
        ("open chrome", step("open_app", app=CHROME)),
        ("Open Chrome.", step("open_app", app=CHROME)),
        ("yo pull up chrome", step("open_app", app=CHROME)),
        ("can you open fusion real quick", step("open_app", app=FUSION)),
        ("could you please open notepad for me", step("open_app", app="Notepad")),
        ("um, launch spotify", step("open_app", app="Spotify")),
        ("fire up claude", step("open_app", app="Claude")),
        ("bring up chat gpt", step("open_app", app="ChatGPT")),
        ("get me the calculator app", step("open_app", app="Calculator")),
        ("open word document", step("open_app", app="Word")),
        ("open web server for chrome", step("open_app", app="Web Server for Chrome")),
        ("switch to chrome", step("switch_app", app=CHROME)),
        ("go to claude", step("switch_app", app="Claude")),
        ("jump over to fusion", step("switch_app", app=FUSION)),
        ("open teams", step("open_app", app="Microsoft Teams")),
        # sites
        ("pull up canvas", step("open_url", url="https://canvas.vt.edu")),
        ("go to github", step("open_url", url="https://github.com")),
        ("open youtube", step("open_url", url="https://www.youtube.com")),
        ("open reddit.com", step("open_url", url="https://reddit.com")),
        ("go to reddit dot com", step("open_url", url="https://reddit.com")),
        # close
        ("close notepad", step("close_app", app="Notepad")),
        ("quit spotify", step("close_app", app="Spotify")),
        ("close this window", step("window", op="close", target="active")),
        ("close it", step("window", op="close", target="active")),
        # window ops
        ("maximize this", step("window", op="maximize", target="active")),
        ("maximize", step("window", op="maximize", target="active")),
        ("maximise the window", step("window", op="maximize", target="active")),
        ("minimize chrome", step("window", op="minimize", target=CHROME)),
        ("restore the window", step("window", op="restore", target="active")),
        ("make it full screen", step("window", op="fullscreen", target="active")),
        ("full screen", step("window", op="fullscreen", target="active")),
        ("center this window", step("window", op="center", target="active")),
        ("snap it left", step("window", op="snap_left", target="active")),
        ("throw claude on the right side", step("window", op="snap_right", target="Claude")),
        ("put chrome on the left half", step("window", op="snap_left", target=CHROME)),
        ("claude on the right", step("window", op="snap_right", target="Claude")),
        ("move chrome to the top", step("window", op="snap_top", target=CHROME)),
        ("move chrome to the other monitor", step("window", op="move_monitor", target=CHROME, monitor="other")),
        ("move autodesk fusion to the second display", step("window", op="move_monitor", target=FUSION, monitor="next")),
        ("send this to the left screen", step("window", op="move_monitor", target="active", monitor="left")),
        # tabs / nav / scroll
        ("new tab", step("tab", op="new")),
        ("open a new tab", step("tab", op="new")),
        ("close the tab", step("tab", op="close")),
        ("close this tab", step("tab", op="close")),
        ("next tab", step("tab", op="next")),
        ("go to the previous tab", step("tab", op="prev")),
        ("reopen closed tab", step("tab", op="reopen")),
        ("reopen close tab", step("tab", op="reopen")),
        ("go back", step("nav", op="back")),
        ("back", step("nav", op="back")),
        ("go forward", step("nav", op="forward")),
        ("reload the page", step("nav", op="reload")),
        ("refresh", step("nav", op="reload")),
        ("scroll down", step("scroll", dir="down", amount="page")),
        ("scroll down a lot", step("scroll", dir="down", amount="large")),
        ("scroll up a little", step("scroll", dir="up", amount="small")),
        ("scroll to the top", step("scroll", dir="top")),
        ("page down", step("scroll", dir="down", amount="page")),
        # search
        ("search youtube for flood fill algorithm", step("web_search", query="flood fill algorithm", engine="youtube")),
        ("search for lofi hip hop on youtube", step("web_search", query="lofi hip hop", engine="youtube")),
        ("search for lofi hip hop", step("web_search", query="lofi hip hop", engine="google")),
        ("google how tall is mount everest", step("web_search", query="how tall is mount everest", engine="google")),
        ("look up thermodynamics notes", step("web_search", query="thermodynamics notes", engine="google")),
        # typing and keys
        ("type hello world", step("type_text", text="hello world", submit=False)),
        ("type hello world and press enter", step("type_text", text="hello world", submit=True)),
        ("Type Hello, World! and hit enter.", step("type_text", text="Hello, World!", submit=True)),
        ("type rock and roll", step("type_text", text="rock and roll", submit=False)),
        ("write dear professor", step("type_text", text="dear professor", submit=False)),
        ("press enter", step("press", keys="enter")),
        ("hit escape", step("press", keys="esc")),
        ("press control shift t", step("press", keys="ctrl+shift+t")),
        ("press ctrl plus c", step("press", keys="ctrl+c")),
        ("press f5", step("press", keys="f5")),
        ("press page down", step("press", keys="pagedown")),
        ("undo that", step("press", keys="ctrl+z")),
        ("select all", step("press", keys="ctrl+a")),
        ("show the desktop", step("press", keys="win+d")),
        # volume / media
        ("volume down", step("volume", op="down")),
        ("volume up a lot", step("volume", op="up", step=20)),
        ("turn it up", step("volume", op="up")),
        ("turn the volume down a bit", step("volume", op="down", step=4)),
        ("louder", step("volume", op="up")),
        ("mute", step("volume", op="mute")),
        ("unmute the sound", step("volume", op="unmute")),
        ("set volume to 30", step("volume", op="set", level=30)),
        ("volume 50 percent", step("volume", op="set", level=50)),
        ("pause the music", step("media", op="pause")),
        ("play", step("media", op="play")),
        ("next song", step("media", op="next")),
        ("skip this track", step("media", op="next")),
        ("previous track", step("media", op="prev")),
        # Nova itself
        ("start dictation", step("dictation", op="start")),
        ("stop dictation", step("dictation", op="stop")),
        ("stop listening", step("listening", op="sleep")),
        ("go to sleep", step("listening", op="sleep")),
    ],
)
def test_single_commands(text, expected):
    assert one(text) == expected


def test_chrome_is_google_chrome_not_web_server_for_chrome():
    assert one("open chrome") == step("open_app", app=CHROME)
    assert one("open chrome", strict=True) == step("open_app", app=CHROME)


@pytest.mark.parametrize(
    "text",
    [
        "fusion on the left, claude on the right",
        "Fusion on the left and Claude on the right.",
        "put fusion left and claude right",
        "fusion left half, claude right half",
        "put fusion on the left side and claude on the right side",
    ],
)
def test_two_window_layouts(text):
    assert one(text) == step("layout", slots=({"app": FUSION, "area": "left"}, {"app": "Claude", "area": "right"}))


def test_three_window_layouts():
    assert one("chrome on the left, claude top right and fusion bottom right") == step(
        "layout",
        slots=({"app": CHROME, "area": "left"}, {"app": "Claude", "area": "top_right"},
               {"app": FUSION, "area": "bottom_right"}),
    )
    assert one("chrome, claude and fusion side by side") == step(
        "layout",
        slots=({"app": CHROME, "area": "left_third"}, {"app": "Claude", "area": "middle_third"},
               {"app": FUSION, "area": "right_third"}),
    )
    assert one("chrome and claude side by side") == step(
        "layout", slots=({"app": CHROME, "area": "left"}, {"app": "Claude", "area": "right"})
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("open chrome and then maximize it", [step("open_app", app=CHROME), step("window", op="maximize", target="active")]),
        ("open google chrome and autodesk fusion", [step("open_app", app=CHROME), step("open_app", app=FUSION)]),
        ("open chrome, then go to canvas", [step("open_app", app=CHROME), step("open_url", url="https://canvas.vt.edu")]),
        ("open notepad then type hello and press enter",
         [step("open_app", app="Notepad"), step("type_text", text="hello", submit=True)]),
        ("new tab and go to github", [step("tab", op="new"), step("open_url", url="https://github.com")]),
        ("close claude, open google chrome", [step("close_app", app="Claude"), step("open_app", app=CHROME)]),
        ("type hi then press tab", [step("type_text", text="hi", submit=False), step("press", keys="tab")]),
    ],
)
def test_chains(text, expected):
    assert steps(text) == expected


def test_type_payload_keeps_the_original_words():
    plan = parse("type See you at 5 PM, okay? and press enter", CATALOG)
    assert plan.steps == (step("type_text", text="See you at 5 PM, okay?", submit=True),)


def test_url_payloads_are_substrings_of_what_he_said():
    plan = parse("search youtube for Flood Fill Algorithm", CATALOG)
    assert plan.steps[0].get("query") == "Flood Fill Algorithm"


@pytest.mark.parametrize(("heard", "meant"), [("open foam", CHROME), ("Open home.", CHROME), ("close Claire", "Claude")])
def test_mishearings_ask_instead_of_guessing(heard, meant):
    plan = parse(heard, CATALOG)
    assert plan is not None and plan.kind == "clarify"
    assert meant in plan.choices
    chosen = plan.option(plan.choices.index(meant))
    assert chosen.kind == "act" and chosen.steps[0].get("app") == meant
    assert parse(heard, CATALOG, strict=True) is None  # always-listening never guesses


def test_partial_names_ask():
    plan = parse("open code", CATALOG)
    assert plan.kind == "clarify" and plan.choices[0] == "Visual Studio Code"
    assert "Visual Studio Code" in plan.question


@pytest.mark.parametrize(
    "text",
    [
        "",
        "the weather is nice today",
        "I'm gonna open a restaurant someday",
        "open the door",
        "go back home",
        "play the guitar",
        "pause for a second",
        "stop it",
        "turn off the lights",
        "put your coat on",
        "make sure you call me",
        "move the couch",
        "start the car",
        "switch to decaf",
        "close",
        "type",
    ],
)
def test_unresolved_or_chatter(text):
    plan = parse(text, CATALOG, strict=True)
    assert plan is None, plan


def test_strict_mode_takes_whole_names_and_aliases_only():
    assert parse("open store", CATALOG, strict=True) is None
    assert parse("go to the store", CATALOG, strict=True) is None
    assert parse("open calculater", CATALOG, strict=True) is None
    assert one("open calculater") == step("open_app", app="Calculator")  # with the key / name: a near spelling is fine
    assert one("open the calculator app", strict=True) == step("open_app", app="Calculator")


def test_commands_without_and_between_them():
    assert steps("close claude open google chrome") == [step("close_app", app="Claude"), step("open_app", app=CHROME)]
    assert steps("open chrome maximize it") == [step("open_app", app=CHROME), step("window", op="maximize", target="active")]


def test_clicks_become_page_clicks_for_stage_06():
    assert one("click on my assignment for today") == step("page_click", description="my assignment for today")
    assert one("click the first link") == step("page_click", description="the first link")


# --- from Bhavya's first live demo (2026-11-03) ---------------------------------------------------------------------

RUNNING = frozenset({"Notepad", "Google Chrome", "Claude", "ChatGPT", "Terminal"})


def test_dangling_words_after_a_command_are_ignored():
    assert one("Open Chrome, but") == step("open_app", app=CHROME)
    assert one("open chrome and") == step("open_app", app=CHROME)
    assert one("um, volume down, uh") == step("volume", op="down")


@pytest.mark.parametrize("text", ["put notepad in front", "bring notepad to the front", "put notepad up front",
                                  "bring notepad forward", "move notepad to the front"])
def test_in_front_means_switch(text):
    assert one(text) == step("switch_app", app="Notepad")


def test_in_front_then_type():
    assert steps("Put notepad in front then type hello world and press enter.") == [
        step("switch_app", app="Notepad"), step("type_text", text="hello world", submit=True)]


@pytest.mark.parametrize("heard", ["Clothes not bad.", "Close not bad.", "Clothes know it bad", "close noteback"])
def test_misheard_close_notepad_asks_about_the_running_notepad(heard):
    plan = parse(heard, CATALOG, running=RUNNING)
    assert plan is not None and plan.kind == "clarify", plan
    assert plan.choices[0] == "Notepad" and plan.question == "Close Notepad?"
    assert plan.option(0).steps == (step("close_app", app="Notepad"),)
    assert parse(heard, CATALOG, strict=True, running=RUNNING) is None


def test_a_misheard_verb_is_asked_about():
    plan = parse("Skype hello world", CATALOG, running=RUNNING)
    assert plan.kind == "clarify" and plan.question == "Type “hello world”?"
    assert plan.option(0).steps == (step("type_text", text="hello world", submit=False),)
    assert parse("Flowers notepad", CATALOG, running=RUNNING) is None  # sounds like nothing he says
    assert parse("Clothes not bad.", CATALOG, running=frozenset({"Claude"})) is None  # Notepad isn't open: no guess


def test_same_sound_opens_without_asking():
    assert one("Open cloud.") == step("open_app", app="Claude")  # "cloud" = "Claude" by sound, nothing else close
    plan = parse("close cloud", CATALOG, running=RUNNING)
    assert plan.kind == "clarify"  # closing still asks
