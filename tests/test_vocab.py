import os

import pytest

from nova.vocab import DEFAULT_VOCAB_YAML, Vocab, VocabError, VocabFile, parse_vocab

SEED = ["CroLabs", "HIVE", "ROS2", "MuJoCo", "Canvas", "Fusion", "Claude", "ChatGPT", "Notion", "Todoist", "Virginia Tech"]


@pytest.fixture(scope="module")
def vocab():
    return Vocab(SEED)


@pytest.mark.parametrize(
    ("heard", "fixed"),
    [
        ("crow labs is hiring", "CroLabs is hiring"),
        ("Crow Labs", "CroLabs"),
        ("open chat GPT", "open ChatGPT"),
        ("ask Chat Gpt", "ask ChatGPT"),
        ("chatgpt", "ChatGPT"),
        ("run it in Mujoco", "run it in MuJoCo"),
        ("run it in mu joco", "run it in MuJoCo"),
        ("a Mu-Jo-Co model", "a MuJoCo model"),
        ("start ROS 2 nodes", "start ROS2 nodes"),
        ("start ros two nodes", "start ROS2 nodes"),
        ("start Ross 2 nodes", "start ROS2 nodes"),
        ("the hive lab", "the HIVE lab"),
        ("add it to todoest", "add it to Todoist"),
        ("open Canvass", "open Canvas"),
        ("virginia tech", "Virginia Tech"),
        ("Open crow labs, then chat gpt.", "Open CroLabs, then ChatGPT."),
        ("what did chat GPT's answer say", "what did ChatGPT's answer say"),
    ],
)
def test_fixes_strong_matches(vocab, heard, fixed):
    assert vocab.correct(heard).text == fixed


@pytest.mark.parametrize(
    "text",
    [
        "go to cloud",  # sounds like Claude but spelled too differently: Tier 0 decides in context
        "the nation is big",  # sounds like Notion
        "slow motion",
        "night vision",
        "the cat clawed me",
        "start ROS 3",  # digits must match
        "Ross said hi",
        "beehives",
        "make a to do list",
        "crow, labs",  # never joins across punctuation
        "open canvas",  # plain words keep their case; Parakeet already capitalises names in context
        "fusion power",
        "",
        "   ",
    ],
)
def test_leaves_everything_else_alone(vocab, text):
    result = vocab.correct(text)
    assert result.text == text
    assert result.changes == ()


def test_reports_what_changed(vocab):
    result = vocab.correct("crow labs uses ROS 2")
    assert result.text == "CroLabs uses ROS2"
    assert result.changes == (("crow labs", "CroLabs"), ("ROS 2", "ROS2"))


def test_already_right_is_not_a_change(vocab):
    assert vocab.correct("CroLabs and ChatGPT").changes == ()


def test_catalog_names_can_join_in():
    vocab = Vocab(["Notepad", "Google Chrome"])
    assert vocab.correct("open note pad").text == "open Notepad"
    assert vocab.correct("open google chrome").text == "open Google Chrome"


def test_short_terms_need_exact_matches():
    vocab = Vocab(["ME 4524", "Kia"])
    assert vocab.correct("me 4524 homework").text == "ME 4524 homework"
    assert vocab.correct("key a").text == "key a"


def test_duplicate_and_blank_terms_are_ignored():
    vocab = Vocab(["HIVE", "hive", " ", "HIVE"])
    assert vocab.terms == ("HIVE",)


# --- vocab.yaml -------------------------------------------------------------------


def test_default_file_has_the_seed_terms():
    assert parse_vocab(DEFAULT_VOCAB_YAML) == SEED


@pytest.mark.parametrize("text", ["", "# nothing yet\n", "terms:\n", "terms: []\n"])
def test_empty_vocab_is_fine(text):
    assert parse_vocab(text) == []


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("- CroLabs\n", "terms:"),
        ("terms: CroLabs\n", "list"),
        ("terms:\n  - 42\n", "text"),
        ("terms:\n  - ''\n", "empty"),
        ("words:\n  - CroLabs\n", "unknown"),
        ("terms: [unclosed\n", "YAML"),
    ],
)
def test_bad_vocab_files(text, message):
    with pytest.raises(VocabError, match=message):
        parse_vocab(text)


def test_vocab_file_created_with_defaults_and_reloaded(tmp_path):
    path = tmp_path / "vocab.yaml"
    store = VocabFile(path)
    assert store.current().terms == tuple(SEED)
    assert path.read_text(encoding="utf-8") == DEFAULT_VOCAB_YAML
    path.write_text("terms:\n  - Kurdila\n", encoding="utf-8")
    os.utime(path, ns=(1, 1_000_000_000))  # make sure the change is visible even within one mtime tick
    assert store.current().terms == ("Kurdila",)


def test_broken_vocab_keeps_last_good(tmp_path):
    path = tmp_path / "vocab.yaml"
    path.write_text("terms:\n  - Kurdila\n", encoding="utf-8")
    store = VocabFile(path, extra=["Notepad"])
    assert store.current().terms == ("Kurdila", "Notepad")
    path.write_text("terms: [oops\n", encoding="utf-8")
    os.utime(path, ns=(1, 2_000_000_000))
    assert store.current().terms == ("Kurdila", "Notepad")
    assert store.error and "YAML" in store.error
    path.write_text("terms:\n  - HIVE\n", encoding="utf-8")
    os.utime(path, ns=(1, 3_000_000_000))
    assert store.current().terms == ("HIVE", "Notepad")
    assert store.error is None
