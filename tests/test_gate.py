"""The trigger gate (SPEC §5): may this final act, and with what text?"""

import pytest

from nova.gate import Addressing, Env, StubResolver, WakeName, command_words, decide, strip_wake

APPS = ["Google Chrome", "Spotify", "Notepad", "Calculator", "Autodesk Fusion", "Claude", "Word", "Excel",
        "Visual Studio Code", "Microsoft Teams", "Camera", "Web Server for Chrome", "Outlook (classic)"]
RESOLVER = StubResolver(APPS)
WAKE = WakeName.of("Nova", ["Noah va"])
IDLE = Addressing()


def env(**kw):
    base = {"wake": WAKE, "always_listening": True, "resolver": RESOLVER}
    return Env(**{**base, **kw})


def judge(text, addressing=IDLE, **kw):
    return decide(text, addressing, env(**kw))


# --- wake name ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("heard", "rest"),
    [
        ("Nova, open Chrome.", "open Chrome."),
        ("nova open chrome", "open chrome"),
        ("Hey Nova, open Spotify.", "open Spotify."),
        ("Okay, Nova: snap Chrome left", "snap Chrome left"),
        ("Nova.", ""),
        ("Nova", ""),
        ("Nova?", ""),
        ("Nova's a cool name", "a cool name"),
        ("Novas, open chrome", "open chrome"),
        ("Novah open chrome", "open chrome"),
        ("Noah va, open chrome", "open chrome"),  # a configured spelling (two words)
    ],
)
def test_wake_name_is_found_and_stripped(heard, rest):
    assert strip_wake(heard, WAKE) == rest


@pytest.mark.parametrize(
    "heard",
    ["I think Nova is cool", "Novak won again", "Noah, pass the salt", "Nora, open Chrome", "", "Hey", "no VA"],
)
def test_not_the_wake_name(heard):
    assert strip_wake(heard, WAKE) is None


def test_wake_name_opens_even_when_always_listening_is_off():
    decision = judge("Nova, open Chrome", always_listening=False)
    assert decision.accepted and decision.via == "wake" and decision.text == "open Chrome"


def test_nova_is_a_cool_name_wakes_only_because_it_starts_with_nova():
    decision = judge("Nova is a cool name")
    assert decision.accepted and decision.via == "wake" and decision.text == "is a cool name"
    assert not decision.wake_only  # the router gets it (and asks what he wants)
    assert not judge("I think Nova is a cool name").accepted


def test_name_alone_opens_a_listening_session():
    decision = judge("Nova.")
    assert decision.accepted and decision.wake_only and decision.text == ""


def test_name_works_in_passthrough_asleep_busy_and_over_speakers():
    for addressing in (Addressing(passthrough=True), Addressing(asleep=True), Addressing(busy=True)):
        assert judge("Nova, pause", addressing).accepted
    assert judge("Nova, open Chrome", speakers=True, mic_busy="Teams.exe").accepted


def test_wake_name_can_be_turned_off():
    decision = judge("Nova, open a restaurant", wake=WakeName.of("Nova", enabled=False))
    assert not decision.accepted  # not a wake; as a plain sentence it isn't a command either


# --- key session / follow-up ---------------------------------------------------------------------


def test_key_session_accepts_anything_and_strips_the_name():
    assert judge("I'm gonna open a restaurant someday", Addressing(session="copilot")).accepted
    decision = judge("Nova, open Chrome", Addressing(session="chord"))
    assert decision.accepted and decision.via == "key" and decision.text == "open Chrome"


def test_wake_session_accepts_the_next_utterance():
    decision = judge("open the thing", Addressing(session="wake"))
    assert decision.accepted and decision.via == "wake"


def test_empty_text_is_never_accepted():
    for addressing in (IDLE, Addressing(session="copilot")):
        decision = judge("  ", addressing)
        assert not decision.accepted and decision.via == "empty"


def test_follow_up_window_takes_commands_without_the_name():
    decision = judge("now close Spotify", Addressing(follow_up=True))
    assert decision.accepted and decision.via == "follow-up" and not decision.safe_only
    assert judge("snap it left", Addressing(follow_up=True)).accepted  # no known name needed
    assert not judge("we should go", Addressing(follow_up=True)).accepted


def test_follow_up_works_with_always_listening_off():
    assert judge("type hello", Addressing(follow_up=True), always_listening=False).accepted


# --- always-listening ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "heard",
    [
        "I'm gonna open a restaurant someday",
        "did you close the door",
        "we should switch to the other lab",
        "open the door",
        "open a restaurant",
        "can you open the window",
        "go to the store",
        "switch to decaf",
        "play the guitar",
        "move the couch",
        "start the car",
        "pause for a second",
        "go back home",
        "stop it",
        "let's open Spotify later",
        "that open Chrome thing was weird",
        "I said open Spotify",
        "chrome is slow today",
        "turn off the lights",
        "make sure you call me",
        "put your coat on",
        "search for apartments",
    ],
)
def test_chatter_is_ignored(heard):
    assert not judge(heard).accepted


@pytest.mark.parametrize(
    "heard",
    [
        "open spotify",
        "Open Spotify.",
        "launch Autodesk Fusion",
        "open the Calculator app",
        "open Visual Studio Code",
        "switch to Google Chrome",
        "go to Claude",
        "open Google Chrome and Autodesk Fusion",
        "minimize Google Chrome",
        "snap Google Chrome left",
        "move Autodesk Fusion to the second display",
        "restore the window",
        "open youtube",
        "go to github",
        "new tab",
        "next tab",
        "reopen closed tab",
        "reopen close tab",  # how Parakeet heard Bhavya's "reopen closed tab"
        "go back",
        "reload the page",
        "search youtube for flood fill algorithm",
        "scroll down a lot",
        "scroll to the top",
        "volume up",
        "pause the music",
        "stop listening",
        "can you open spotify",
        "could you please open chrome",
        "please open notepad",
        "um open spotify",
        "open word document",
        "open chrome",
    ],
)
def test_safe_commands_are_accepted_without_the_name(heard):
    decision = judge(heard)
    assert decision.accepted and decision.via == "always" and decision.safe_only, decision


@pytest.mark.parametrize(
    "heard",
    [
        "close Claude",
        "close Claude, open Google Chrome",
        "close this window",
        "close this tab",
        "type hello world and press enter",
        "write dear professor",
        "press control shift t",
        "hit escape",
        "search for lofi hip hop",
        "start dictation",
        "click the first link",
    ],
)
def test_risky_commands_need_the_name_or_the_key(heard):
    decision = judge(heard)
    assert not decision.accepted and decision.via == "needs-name", decision
    assert judge(f"Nova, {heard}").accepted


def test_always_listening_off_ignores_plain_commands():
    decision = judge("open spotify", always_listening=False)
    assert not decision.accepted and decision.via == "off"


def test_speakers_or_another_app_on_the_mic_require_the_name():
    playing = judge("open spotify", speakers=True)
    assert not playing.accepted and playing.via == "speakers"
    call = judge("open spotify", mic_busy="Teams.exe")
    assert not call.accepted and call.via == "mic-busy" and "Teams" in call.why
    assert not judge("snap it left", Addressing(follow_up=True), speakers=True).accepted


def test_passthrough_asleep_and_busy_ignore_plain_commands():
    assert judge("open spotify", Addressing(passthrough=True)).via == "passthrough"
    assert judge("open spotify", Addressing(asleep=True)).via == "asleep"
    assert judge("open spotify", Addressing(busy=True)).via == "busy"


def test_unknown_app_is_not_resolvable():
    decision = judge("open fortnite")
    assert not decision.accepted and decision.via == "unresolved"
    assert decide("open fortnite", IDLE, env(resolver=RESOLVER.with_names(APPS + ["Fortnite"]))).accepted


def test_resolver_needs_whole_names_not_common_words():
    assert not judge("open code").accepted  # "code" is a word, not "Visual Studio Code"
    assert not judge("open server").accepted
    assert judge("open teams").accepted  # stub alias for Microsoft Teams
    assert judge("open outlook classic").accepted  # "(classic)" punctuation dropped


def test_command_words():
    assert command_words("Um, can you please open Spotify?") == ("open", "spotify")
    assert command_words("and then snap it left") == ("snap", "it", "left")
    assert command_words("I'm gonna open a restaurant") is None
    assert command_words("") is None
