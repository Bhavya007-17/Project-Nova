import io

from nova.events import FinalTranscript, PartialTranscript, SpeechEnded
from nova.listen import Screen


def final(text, raw=None, final_ms=310.0, stt_ms=170.0, reason="silence"):
    return FinalTranscript(1, text, raw if raw is not None else text, reason, stt_ms, final_ms, 1500.0)


def test_final_shows_text_and_timings():
    out = io.StringIO()
    screen = Screen(vt=False, out=out)
    screen.final(final("open chrome"))
    assert out.getvalue() == "open chrome   stt 170 ms · 310 ms after you stopped\n"


def test_final_shows_what_was_heard_when_vocab_changed_it():
    out = io.StringIO()
    Screen(vt=False, out=out).final(final("open CroLabs", raw="open crow labs"))
    assert "heard: open crow labs" in out.getvalue()


def test_key_release_and_cap_are_named():
    out = io.StringIO()
    screen = Screen(vt=False, out=out)
    screen.final(final("a", reason="key"))
    screen.final(final("b", reason="cap"))
    assert "after you let go" in out.getvalue() and "cut at 30 s" in out.getvalue()


def test_empty_final_prints_nothing():
    out = io.StringIO()
    screen = Screen(vt=False, out=out)
    screen.final(final(""))
    assert out.getvalue() == "" and screen.finals == []


def test_partials_need_a_real_console():
    out = io.StringIO()
    Screen(vt=False, out=out).partial(PartialTranscript(1, "open chr"))
    assert out.getvalue() == ""


def test_partial_rewrites_one_dim_line_and_final_replaces_it():
    out = io.StringIO()
    screen = Screen(vt=True, out=out)
    screen.partial(PartialTranscript(1, "open chr"))
    screen.partial(PartialTranscript(1, "open chrome and", windowed=True))
    screen.final(final("open chrome and go to canvas"))
    text = out.getvalue()
    assert text.count("\r\x1b[2K") == 3  # two partials + clearing before the final
    assert "\x1b[2m… open chrome and" in text
    assert text.rstrip().endswith("\x1b[0m")


def test_cancel_clears_the_live_line():
    out = io.StringIO()
    screen = Screen(vt=True, out=out)
    screen.partial(PartialTranscript(1, "open"))
    screen.ended(SpeechEnded(1, "cancel", None, 0, 0, 0, 0.0))
    assert out.getvalue().endswith("\r\x1b[2K")


def test_summary_percentiles():
    screen = Screen(vt=False, out=io.StringIO())
    assert screen.summary() is None
    for ms in (300, 320, 340, 360, 900):
        screen.final(final("x", final_ms=ms))
    summary = screen.summary()
    assert summary.startswith("5 finals") and "p50 340 ms" in summary and "stt p50 170 ms" in summary
