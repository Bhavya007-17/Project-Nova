import numpy as np
import pytest

from nova.pause import looks_finished, looks_unfinished
from nova.vad import FRAME, Endpointer, EndpointRules


@pytest.mark.parametrize("text", [
    "Open the", "Nova, open", "switch to", "Search YouTube for", "open Chrome and", "Hey Nova, can you",
    "Type hello,", "go to my", "open a new tab and then", "make a note called", "um", "Nova, type",
])
def test_unfinished(text):
    assert looks_unfinished(text)


@pytest.mark.parametrize("text", [
    "", "Open Chrome.", "Nova", "Hey Nova.", "scroll down", "turn it on", "zoom in", "volume up", "Close this tab.",
    "pause", "Search YouTube for lo-fi beats", "what is due this week?", "stop listening", "go back",
    "I need you to go through the whole document and open",
])
def test_not_unfinished(text):
    assert not looks_unfinished(text)


def test_finished_needs_a_sentence_end():
    assert looks_finished("Open the Chrome settings.")
    assert looks_finished("What's due this week?")
    assert not looks_finished("Open the Chrome settings")
    assert not looks_finished("Open the.")  # a full stop after a dangling word proves nothing


def run(ep, frames, start=0):
    events = []
    for i, p in enumerate(frames):
        event = ep.push(start + i * FRAME, np.zeros(FRAME, dtype=np.float32), p)
        if event is not None:
            events.append((i, event))
    return events


def frames_of(ms):
    return round(ms / 32)


def test_more_holds_the_endpoint_until_the_unfinished_silence():
    ep = Endpointer(EndpointRules())
    run(ep, [0.9] * 20)
    silence = 0
    while ep.active is not None and silence < 100:
        if silence == 5:
            ep.verdict = "more"
        ep.push((20 + silence) * FRAME, np.zeros(FRAME, dtype=np.float32), 0.0)
        silence += 1
    assert silence == 32  # the first frame count at or past 1000 ms


def test_wait_caps_at_the_long_silence_and_speech_clears_the_verdict():
    ep = Endpointer(EndpointRules())
    run(ep, [0.9] * 20)
    ep.verdict = "wait"
    events = run(ep, [0.0] * 40, start=20 * FRAME)
    assert events[0][0] + 1 == frames_of(700)
    ep = Endpointer(EndpointRules())
    run(ep, [0.9] * 20)
    ep.verdict = "more"
    run(ep, [0.0] * 12 + [0.9], start=20 * FRAME)
    assert ep.verdict is None and ep.active is not None


def test_poll_ends_at_once_when_the_words_are_done():
    ep = Endpointer(EndpointRules())
    run(ep, [0.9] * 20)
    ep.verdict = "wait"
    assert not run(ep, [0.0] * 12, start=20 * FRAME)  # 384 ms: past the short silence, held for the decode
    assert ep.poll() is None
    ep.verdict = None
    ended = ep.poll()
    assert ended is not None and ended.reason == "silence"


def test_done_shortens_the_long_silence_only():
    ep = Endpointer(EndpointRules())
    run(ep, [0.9] * 100)  # 3.2 s of speech: the long silence applies
    ep.verdict = "done"
    events = run(ep, [0.0] * 40, start=100 * FRAME)
    assert events[0][0] + 1 == 15  # 480 ms: the first frame count past 450 ms


def test_stt_threads_scale_with_the_cpu():
    from nova.stt.parakeet import default_threads

    assert [default_threads(n) for n in (2, 4, 8, 16, 32)] == [2, 2, 4, 4, 4]
