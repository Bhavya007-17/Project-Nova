"""nova/stt/sherpa.py: hotword lines, the vocab made from tokens.txt, which names become hotwords (no model)."""

import pytest

from nova.actions.catalog import parse_aliases
from nova.stt import sherpa


def test_hotword_lines_keep_spelling_drop_punctuation_and_repeats():
    lines = sherpa.hotword_lines(["ChatGPT", "Visual Studio Code", "chatgpt", "Intel® Graphics", "", "  ", "ROS2"])
    assert lines == ["ChatGPT :2", "Visual Studio Code :2", "Intel Graphics :2", "ROS2 :2"]


def test_hotword_lines_score():
    assert sherpa.hotword_lines(["Nova"], score=1.5) == ["Nova :1.5"]


def test_bpe_vocab_from_tokens_txt():
    tokens = "<unk> 0\n<|endoftext|> 3\n▁the 12\nrome 400\n▁Chr 2000\n<blk> 8192\n"
    assert sherpa.bpe_vocab(tokens) == "▁the\t-12\nrome\t-400\n▁Chr\t-2000\n"


def test_names_for_hotwords_are_his_names_in_his_spelling():
    aliases = parse_aliases("chrome: Google Chrome\nvs code: Visual Studio Code\ndocs: https://docs.google.com\n"
                            "chatgpt: ChatGPT\n")
    apps = ["Google Chrome", "Visual Studio Code", "ChatGPT", "Notepad", "Python 3.12 (64-bit)", "Microsoft Word"]
    names = sherpa.names_for_hotwords(aliases, apps, ["CroLabs", "MuJoCo", "ChatGPT"], "Nova")
    assert names[0] == "Nova"
    for name in ("CroLabs", "MuJoCo", "Chrome", "Google Chrome", "VS Code", "Visual Studio Code", "Docs", "ChatGPT"):
        assert name in names, name
    assert "Notepad" not in names and "Microsoft Word" not in names  # every installed app: WER rose 14 -> 19 %
    assert "chatgpt" not in names  # the alias is written the way the vocab spells it
    lines = sherpa.hotword_lines(names)
    assert len(lines) == len({line.casefold() for line in lines})


def test_missing_model_files_say_how_to_get_them(tmp_path):
    with pytest.raises(FileNotFoundError, match="get_models.py --sherpa"):
        sherpa.SherpaTranscriber(tmp_path)
