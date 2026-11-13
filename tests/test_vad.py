import math

import numpy as np
import pytest

from nova.events import SpeechEnded, SpeechStarted
from nova.vad import FRAME, UNADDRESSED_MAX_MS, EndpointRules, Endpointer, EnergyGate, Silero, unaddressed

SR = 16_000
MS_PER_FRAME = FRAME * 1000 / SR  # 32 ms


def frame(index: int) -> np.ndarray:
    """A frame whose samples encode its index, so pcm contents can be checked."""
    return np.full(FRAME, index, dtype=np.float32)


class Feed:
    """Pushes synthetic VAD probabilities through an Endpointer, one frame each."""

    def __init__(self, rules: EndpointRules | None = None) -> None:
        self.ep = Endpointer(rules or EndpointRules(), clock=lambda pos: pos / SR)
        self.index = 0
        self.events: list[object] = []

    def push(self, *probs: float) -> list[object]:
        out = []
        for p in probs:
            event = self.ep.push(self.index * FRAME, frame(self.index), p)
            self.index += 1
            if event is not None:
                out.append(event)
        self.events.extend(out)
        return out

    def speech(self, ms: float, p: float = 0.9) -> list[object]:
        return self.push(*[p] * math.ceil(ms / MS_PER_FRAME))

    def silence(self, ms: float, p: float = 0.05) -> list[object]:
        return self.push(*[p] * math.ceil(ms / MS_PER_FRAME))


def frames_in(pcm: np.ndarray) -> list[int]:
    return [int(v) for v in pcm[::FRAME]]


# --- speech start ----------------------------------------------------------------


def test_one_loud_frame_is_not_speech():
    f = Feed()
    assert f.push(0.1, 0.9, 0.1, 0.9, 0.2) == []
    assert f.ep.active is None


def test_two_frames_above_half_start_speech():
    f = Feed()
    f.silence(500)
    first_speech = f.index
    events = f.push(0.6, 0.7)
    assert len(events) == 1 and isinstance(events[0], SpeechStarted)
    assert events[0].utterance == 1
    assert events[0].at == pytest.approx(first_speech * FRAME / SR)


def test_exactly_half_is_not_above_half():
    f = Feed()
    assert f.push(0.5, 0.5, 0.5) == []


def test_pre_roll_is_at_least_300_ms():
    f = Feed()
    f.silence(1000)
    first_speech = f.index
    f.speech(500)
    f.silence(400)
    ended = f.events[-1]
    assert isinstance(ended, SpeechEnded)
    covered = frames_in(ended.pcm)
    pre_roll = first_speech - covered[0]
    assert pre_roll * MS_PER_FRAME >= 300
    assert covered == list(range(covered[0], covered[0] + len(covered)))  # contiguous
    assert ended.start == covered[0] * FRAME


def test_pre_roll_is_shorter_at_stream_start():
    f = Feed()
    f.push(0.1)
    f.speech(300)
    f.silence(400)
    assert frames_in(f.events[-1].pcm)[0] == 0


# --- endpoints ---------------------------------------------------------------------


def test_short_utterance_ends_after_300_ms_of_silence():
    f = Feed()
    f.silence(400)
    f.speech(1000)
    speech_end = f.index * FRAME
    assert f.silence(288) == []  # 9 frames = 288 ms
    events = f.push(0.05)  # 320 ms of silence
    assert len(events) == 1
    ended = events[0]
    assert isinstance(ended, SpeechEnded) and ended.reason == "silence"
    assert ended.speech_end == speech_end
    assert ended.speech_end_at == pytest.approx(speech_end / SR)
    # Audio runs to 160 ms past the last speech frame, not to the endpoint.
    assert ended.end == speech_end + 160 * SR // 1000
    assert len(ended.pcm) == ended.end - ended.start


def test_long_utterance_needs_700_ms_of_silence():
    f = Feed()
    f.speech(2600)
    assert f.silence(650) == []
    events = f.silence(100)
    assert [type(e) for e in events] == [SpeechEnded]
    assert events[0].reason == "silence"


def test_speech_just_under_long_threshold_uses_short_endpoint():
    f = Feed()
    f.speech(2400)
    events = f.silence(320)
    assert [type(e) for e in events] == [SpeechEnded]


def test_frames_between_thresholds_keep_speech_going():
    f = Feed()
    f.speech(500)
    assert f.push(*[0.4] * 30) == []  # 0.35 <= p: still talking
    assert f.ep.active is not None


def test_pause_shorter_than_endpoint_keeps_one_utterance():
    f = Feed()
    f.speech(600)
    f.silence(200)
    f.speech(600)
    f.silence(400)
    starts = [e for e in f.events if isinstance(e, SpeechStarted)]
    ends = [e for e in f.events if isinstance(e, SpeechEnded)]
    assert len(starts) == 1 and len(ends) == 1


def test_cap_ends_long_commands():
    f = Feed(EndpointRules(max_ms=3000))
    f.speech(2900)
    events = f.speech(200)
    assert [type(e) for e in events] == [SpeechEnded]
    assert events[0].reason == "cap"
    assert len(events[0].pcm) * 1000 / SR >= 3000


def test_after_a_cap_the_rest_of_the_monologue_is_not_a_new_utterance():
    f = Feed(EndpointRules(max_ms=3000))
    f.speech(3100)
    assert f.speech(2000) == []  # still the same monologue: no fragment becomes a command
    f.silence(320)
    events = f.speech(100)
    assert [type(e) for e in events] == [SpeechStarted]


def test_default_cap_is_30_s_and_none_means_no_cap():
    assert EndpointRules().max_ms == 30_000
    f = Feed(EndpointRules(max_ms=None))
    f.speech(40_000)
    assert not [e for e in f.events if isinstance(e, SpeechEnded)]


def test_end_now_ends_at_once_with_all_audio():
    f = Feed()
    f.speech(800)
    f.silence(100)
    end = f.index * FRAME
    ended = f.ep.end_now()
    assert isinstance(ended, SpeechEnded) and ended.reason == "key"
    assert ended.end == end  # key release keeps everything up to the release
    assert ended.speech_end_at == pytest.approx(end / SR)
    assert f.ep.active is None


def test_end_now_when_quiet_does_nothing():
    f = Feed()
    f.silence(300)
    assert f.ep.end_now() is None


def test_the_tail_of_his_words_after_a_key_release_is_not_a_new_utterance():
    # Logged: 57 of 179 key releases started another utterance within 100 ms (a ~200 ms clip, heard as "Yeah.").
    f = Feed()
    f.speech(800)
    f.ep.end_now()
    assert f.speech(200) == []  # he let go mid-word
    assert f.silence(220) == []  # not 250 ms of quiet yet
    assert f.speech(100) == []
    f.silence(260)
    events = f.speech(100)
    assert [type(e) for e in events] == [SpeechStarted]


def test_a_new_key_press_is_heard_at_once_after_a_release():
    f = Feed()
    f.speech(800)
    f.ep.end_now()
    f.ep.listen_now()  # the speech worker calls this when the key goes down again
    events = f.speech(100)
    assert [type(e) for e in events] == [SpeechStarted]


def test_quiet_after_a_key_release_is_a_rule():
    assert EndpointRules().after_key_ms == 250
    f = Feed(EndpointRules(after_key_ms=0))
    f.speech(800)
    f.ep.end_now()
    assert [type(e) for e in f.speech(100)] == [SpeechStarted]


def test_cancel_reports_and_forgets():
    f = Feed()
    f.speech(500)
    ended = f.ep.cancel()
    assert ended is not None and ended.reason == "cancel"
    assert f.ep.cancel() is None
    f.push(0.9)  # a streak from before the cancel must not carry over
    assert f.ep.active is None


def test_every_start_has_one_end_and_ids_grow():
    f = Feed()
    for _ in range(3):
        f.silence(400)
        f.speech(700)
        f.silence(400)
    kinds = [type(e).__name__ for e in f.events]
    assert kinds == ["SpeechStarted", "SpeechEnded"] * 3
    assert [e.utterance for e in f.events] == [1, 1, 2, 2, 3, 3]


def test_next_utterance_gets_pre_roll_from_the_tail_of_the_last():
    f = Feed()
    f.speech(600)
    f.silence(320)  # ends the first utterance
    first_end = f.events[-1].end
    f.speech(300)
    f.silence(400)
    second = f.events[-1]
    assert second.start < f.events[2].at * SR  # pre-roll before the new speech
    assert second.start >= first_end  # but never re-uses audio already sent


def test_early_final_range_matches_the_final():
    """Once 160 ms of silence has passed, the audio that will become the final is known."""
    f = Feed()
    f.speech(900)
    f.silence(160)
    utt = f.ep.active
    assert utt is not None and utt.silence >= f.ep.rules.tail_samples
    early = f.ep.final_range()
    f.silence(200)
    ended = f.events[-1]
    assert isinstance(ended, SpeechEnded)
    assert early == (ended.start, ended.end)


def test_final_range_only_while_paused():
    f = Feed()
    assert f.ep.final_range() is None
    f.speech(500)
    assert f.ep.final_range() is None


def test_audio_slice_of_active_utterance():
    f = Feed()
    f.silence(400)
    f.speech(500)
    utt = f.ep.active
    pcm = f.ep.audio(utt.start, utt.end)
    assert len(pcm) == utt.end - utt.start
    assert frames_in(pcm)[0] * FRAME == utt.start


def test_rules_validate():
    with pytest.raises(ValueError):
        EndpointRules(start_frames=0)
    with pytest.raises(ValueError):
        EndpointRules(stop_threshold=0.6, start_threshold=0.5)
    with pytest.raises(ValueError):
        EndpointRules(short_silence_ms=800, long_silence_ms=700)


# --- energy gate ---------------------------------------------------------------------


def energy(dbfs: float) -> float:
    return 10 ** (dbfs / 10)


def test_gate_skips_steady_background_noise():
    gate = EnergyGate()
    decisions = [gate.check(energy(-60), busy=False) for _ in range(100)]
    assert not any(decisions[-60:])


def test_gate_opens_on_a_jump_and_holds():
    gate = EnergyGate(hold_ms=320)
    for _ in range(50):
        gate.check(energy(-60), busy=False)
    assert gate.check(energy(-30), busy=False)
    held = [gate.check(energy(-60), busy=False) for _ in range(12)]
    assert all(held[:10]) and not any(held[10:])


def test_gate_never_skips_while_busy():
    gate = EnergyGate()
    for _ in range(50):
        gate.check(energy(-60), busy=False)
    assert all(gate.check(energy(-60), busy=True) for _ in range(20))


def test_gate_ignores_digital_silence():
    gate = EnergyGate()
    assert not any(gate.check(0.0, busy=False) for _ in range(30))
    assert not gate.check(1e-12, busy=False)


def test_gate_background_follows_a_louder_room():
    gate = EnergyGate()
    for _ in range(50):
        gate.check(energy(-75), busy=False)
    louder = [gate.check(energy(-58), busy=False) for _ in range(250)]  # 8 s of a fan at -58 dB
    assert louder[0] is True
    assert not any(louder[-50:])


def test_gate_first_frame_runs_vad():
    assert EnergyGate().check(energy(-40), busy=False)


def test_gate_skips_noise_suppressed_mic_chatter():
    """The Intel array's noise suppression swings 'quiet' between ~-95 and ~-65 dB frame to frame."""
    gate = EnergyGate()
    rng = np.random.default_rng(0)
    quiet = rng.uniform(-96, -64, size=600)
    decisions = [gate.check(energy(db), busy=False) for db in quiet]
    assert sum(decisions[100:]) / 500 < 0.15


def test_gate_always_checks_frames_louder_than_the_cap():
    gate = EnergyGate()
    for _ in range(200):
        gate.check(energy(-52), busy=False)  # fan at -52 dB: background + 15 dB would be -37 dB
    assert gate.check(energy(-44), busy=False)  # the -45 dB cap still lets soft speech through


# --- push-to-talk hold (Stage 02) ---------------------------------------------------


def test_held_key_suppresses_the_silence_endpoint():
    f = Feed()
    f.ep.held = True
    f.speech(600)
    assert f.silence(1500) == []  # pauses while holding the key never end the utterance
    assert f.ep.active is not None
    assert f.ep.final_range() is None  # no early decode while the key is down
    f.ep.held = False  # a tap: back to normal endpointing, and this pause is already long enough
    events = f.silence(32)
    assert [type(e) for e in events] == [SpeechEnded] and events[0].reason == "silence"


def test_held_key_still_has_the_30_s_cap():
    f = Feed()
    f.ep.held = True
    f.speech(1000)
    events = f.silence(30_000)
    assert [e.reason for e in events if isinstance(e, SpeechEnded)] == ["cap"]


def test_release_after_hold_ends_with_everything():
    f = Feed()
    f.ep.held = True
    f.speech(500)
    f.silence(900)
    end = f.index * FRAME
    f.ep.held = False
    ended = f.ep.end_now()
    assert ended is not None and ended.reason == "key" and ended.end == end


# --- dictation (Stage 07) ---------------------------------------------------------------------------


def test_dictation_rules_wait_600_ms_and_have_no_cap():
    from nova.vad import DICTATION_RULES

    feed = Feed()
    feed.ep.set_rules(DICTATION_RULES)
    feed.speech(1000)
    assert not feed.silence(500)  # a pause inside a sentence
    feed.speech(40_000)  # longer than the command cap
    assert not [e for e in feed.events if isinstance(e, SpeechEnded)]
    ended = [e for e in feed.silence(700) if isinstance(e, SpeechEnded)]
    assert ended and ended[0].reason == "silence"


def test_switching_back_to_commands_ends_on_the_short_pause():
    from nova.vad import DICTATION_RULES

    feed = Feed()
    feed.ep.set_rules(DICTATION_RULES)
    feed.speech(1000)
    feed.ep.set_rules(EndpointRules())
    assert [e for e in feed.silence(400) if isinstance(e, SpeechEnded)]


def test_a_skipped_frame_keeps_sileros_state():
    """Soft speech sits near the gate's threshold: frames are skipped in the middle of words. Starting Silero from
    zero after each skip missed 28-30 of 134 recordings at -30 dB (9-10 with no gate at all)."""
    vad = object.__new__(Silero)  # no model file in this test
    state = np.ones((2, 1, 128), dtype=np.float32)
    vad._state, vad._zero_state = state, np.zeros((2, 1, 128), dtype=np.float32)
    vad._context = np.zeros((1, Silero.CONTEXT), dtype=np.float32)
    skipped = np.arange(FRAME, dtype=np.float32)
    vad.skip(skipped)
    assert vad._state is state
    assert np.array_equal(vad._context[0], skipped[-Silero.CONTEXT :])


def test_unaddressed_speech_is_capped_at_10_s():
    assert UNADDRESSED_MAX_MS == 10_000
    assert unaddressed(EndpointRules()).max_ms == 10_000
    assert unaddressed(EndpointRules(max_ms=3000)).max_ms == 3000  # a shorter cap stays
    assert unaddressed(EndpointRules(max_ms=None)).max_ms == 10_000
    f = Feed(unaddressed(EndpointRules()))
    f.speech(9_900)
    events = f.speech(200)
    assert [e.reason for e in events] == ["cap"]


def test_a_key_or_the_name_mid_utterance_lifts_the_cap():
    f = Feed(EndpointRules(max_ms=3000))
    f.speech(2000)
    f.ep.set_rules(EndpointRules(max_ms=6000))  # he pressed the key 2 s in: it is a command now
    assert f.speech(2000) == []
    assert [e.reason for e in f.speech(2100)] == ["cap"]
