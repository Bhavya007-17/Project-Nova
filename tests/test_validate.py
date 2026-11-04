"""Checking Tier 1's plans (SPEC §7.4-7.6): schema, payloads are his words, entities exist, unsure -> ask."""

import pytest
from catalog_fixture import CATALOG

from nova.brain.plan import Plan, step
from nova.brain.validate import check_llm, span


def act(*steps, confidence=0.9):
    return {"kind": "act", "steps": list(steps), "confidence": confidence}


def check(data, said="", running=()):
    return check_llm(data, said, CATALOG, running=frozenset(running))


# --- payloads are cut from what he said ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("said", "value", "expected"),
    [
        ("Search YouTube for lofi hip hop.", "lofi hip hop", "lofi hip hop"),
        ("type Hello World please", "hello world", "Hello World"),
        ("type hi, how are you", "hi how are you", "hi, how are you"),
        ("search for lo-fi beats", "lofi beats", "lo-fi beats"),
        ("look up what's the capital of France", "what's the capital of france", "what's the capital of France"),
        ("put hello there in the box", "Hello there", "hello there"),
    ],
)
def test_span_finds_his_words(said, value, expected):
    assert span(said, value) == expected


@pytest.mark.parametrize(
    ("said", "value"),
    [
        ("search youtube for music", "lofi hip hop"),  # made up
        ("search youtube for music", "music videos"),  # added a word
        ("type hello", ""),
        ("", "hello"),
        ("write a note to mom", "write a note to my mother"),  # reworded
    ],
)
def test_span_refuses_words_he_didnt_say(said, value):
    assert span(said, value) is None


def test_typed_text_and_queries_become_his_exact_words():
    checked = check(act({"action": "web_search", "query": "LOFI hip hop", "engine": "youtube"}),
                    "search youtube for Lofi Hip Hop")
    assert checked.plan.steps == (step("web_search", query="Lofi Hip Hop", engine="youtube"),)
    checked = check(act({"action": "type_text", "text": "hello there", "submit": True}),
                    "put Hello there in the box and send it")
    assert checked.plan.steps == (step("type_text", text="Hello there", submit=True),)


def test_invented_payload_is_rejected():
    checked = check(act({"action": "type_text", "text": "Dear professor, I will be late", "submit": False}),
                    "write something nice to my professor")
    assert checked.plan is None and "said" in checked.problem


# --- entities ------------------------------------------------------------------------------------------------------


def test_apps_resolve_against_the_catalog():
    checked = check(act({"action": "open_app", "app": "chrome"}), "get chrome up")
    assert checked.plan.steps == (step("open_app", app="Google Chrome"),)
    checked = check(act({"action": "switch_app", "app": "Spotify app"}), "go to spotify")
    assert checked.plan.steps == (step("switch_app", app="Spotify"),)


def test_a_short_name_for_a_site_opens_the_site():
    checked = check(act({"action": "open_app", "app": "Canvas"}), "can you get canvas up for me")
    assert checked.plan.steps == (step("open_url", url="https://canvas.vt.edu"),)


def test_an_app_that_isnt_installed_is_said_plainly():
    checked = check(act({"action": "open_app", "app": "Blender"}), "open blender")
    assert checked.plan is None and "Blender" in checked.problem


def test_an_unclear_app_becomes_a_question_with_options():
    checked = check(act({"action": "open_app", "app": "Microsoft"}), "open the microsoft one")
    assert checked.plan.kind == "clarify" and len(checked.plan.choices) >= 2
    assert all(o.kind == "act" and o.steps[0].action == "open_app" for o in checked.plan.options)
    assert checked.plan.question.startswith("Open")


def test_window_targets():
    checked = check(act({"action": "window", "op": "maximize", "target": "this window"}), "make this window big")
    assert checked.plan.steps == (step("window", op="maximize", target="active"),)
    checked = check(act({"action": "window", "op": "snap_left", "target": "Chrome"}), "shove chrome onto the left half")
    assert checked.plan.steps == (step("window", op="snap_left", target="Google Chrome"),)


def test_layout_slots_resolve():
    data = act({"action": "layout", "slots": [{"app": "fusion", "area": "left"}, {"app": "Claude", "area": "right"}]})
    checked = check(data, "fusion left claude right")
    assert checked.plan.steps[0].get("slots") == ({"app": "Autodesk Fusion", "area": "left"},
                                                  {"app": "Claude", "area": "right"})


def test_urls_must_be_his_short_names_or_sites_he_named():
    ok = check(act({"action": "open_url", "url": "https://www.reddit.com"}), "pull up reddit")
    assert ok.plan.kind == "act" and ok.plan.steps[0].get("url") == "https://www.reddit.com"
    alias = check(act({"action": "open_url", "url": "https://mail.google.com/mail/u/0"}), "get my email up")
    assert alias.plan.kind == "act"
    squashed = check(act({"action": "open_url", "url": "youtube.com"}), "open you tube")
    assert squashed.plan.steps[0].get("url") == "https://youtube.com"


def test_a_site_he_didnt_name_is_asked_first():
    checked = check(act({"action": "open_url", "url": "https://www.bankofamerica.com"}), "open my bank")
    assert checked.plan.kind == "clarify" and checked.plan.choices == ("Open bankofamerica.com",)
    assert checked.plan.options[0].steps[0].get("url") == "https://www.bankofamerica.com"


def test_bad_steps_are_problems():
    assert check(act({"action": "press", "keys": "ctrl+banana"}), "press ctrl banana").plan is None
    assert check(act({"action": "volume", "op": "set", "level": 400}), "volume 400").plan is None
    assert check(act({"action": "teleport", "where": "home"}), "take me home").plan is None
    assert check(act(), "do it").plan is None  # act without steps


# --- kinds and confidence -------------------------------------------------------------------------------------------


def test_unsure_act_becomes_a_yes_no_question():
    checked = check(act({"action": "open_app", "app": "Notepad"}, confidence=0.45), "open the notes thing")
    assert checked.plan.kind == "clarify" and checked.plan.choices == ("Open Notepad",)
    assert checked.plan.options == (Plan("act", (step("open_app", app="Notepad"),), confidence=0.45),)


def test_confidence_is_normalized():
    assert check(act({"action": "tab", "op": "new"}, confidence=95), "new tab").plan.confidence == 0.95
    assert check(act({"action": "tab", "op": "new"}, confidence=300), "new tab").plan.confidence == 1.0
    assert check(act({"action": "tab", "op": "new"}, confidence="high"), "new tab").plan.kind == "clarify"  # 0.5


def test_model_questions_keep_up_to_four_choices():
    data = {"kind": "clarify", "steps": [], "confidence": 0.4, "question": "Open which one?",
            "choices": ["Google Chrome", "Claude", "", "Claude", "Notepad", "Calculator", "Spotify"]}
    checked = check(data, "open the thing")
    assert checked.plan.kind == "clarify" and checked.plan.options == ()
    assert checked.plan.choices == ("Google Chrome", "Claude", "Notepad", "Calculator")
    open_question = check({"kind": "clarify", "steps": [], "confidence": 0.3, "question": "What should I open?"}, "open")
    assert open_question.plan.choices == ()
    assert check({"kind": "clarify", "steps": [], "confidence": 0.3, "question": " "}, "uh").plan is None


def test_handoff_prompt_is_his_words():
    data = {"kind": "handoff", "steps": [], "confidence": 0.9,
            "handoff": {"to": "chatgpt", "mode": "text", "prompt": "explain eigenvalues simply"}}
    checked = check(data, "ask chatgpt to explain eigenvalues simply")
    assert checked.plan.kind == "handoff"
    assert dict(checked.plan.handoff) == {"to": "chatgpt", "mode": "text", "prompt": "explain eigenvalues simply"}
    reworded = {"kind": "handoff", "steps": [], "confidence": 0.9,
                "handoff": {"to": "claude", "mode": "text", "prompt": "Write an essay about the French revolution"}}
    checked = check(reworded, "write me an essay on the french revolution")
    assert checked.plan.handoff["prompt"] == "write me an essay on the french revolution"  # whole utterance instead


def test_dictate_teach_ignore():
    dictate = check({"kind": "dictate", "steps": [], "confidence": 0.9}, "take a note")
    assert dictate.plan.steps == (step("dictation", op="start"),)
    teach = check({"kind": "teach", "steps": [], "confidence": 0.7}, "turn on dark mode in fusion")
    assert teach.plan is None and teach.kind == "teach"
    ignore = check({"kind": "ignore", "steps": [], "confidence": 0.9}, "my roommate opened the window")
    assert ignore.plan is None and ignore.kind == "ignore"


def test_garbage_is_a_problem():
    assert check({"kind": "dance"}, "x").problem
    assert check("not a plan", "x").problem
    assert check({"kind": "act", "steps": "open chrome", "confidence": 0.9}, "x").problem


def test_enter_only_when_he_asked_for_it():
    checked = check(act({"action": "type_text", "text": "thanks so much", "submit": True}), "put thanks so much in the box")
    assert checked.plan.steps == (step("type_text", text="thanks so much", submit=False),)
    sent = check(act({"action": "type_text", "text": "on my way", "submit": True}), "type on my way and send it")
    assert sent.plan.steps[0].get("submit") is True
