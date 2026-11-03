import pytest

from nova.cpu import IdleMeter, cycles_per_core_second, process_cycles


class Fake:
    def __init__(self):
        self.t = 0.0
        self.c = 0

    def advance(self, seconds, cpu_fraction, hz=1000.0):
        self.t += seconds
        self.c += int(seconds * cpu_fraction * hz)


def meter(fake, settle=1.0):
    return IdleMeter(1000.0, settle_s=settle, cycles=lambda: fake.c, clock=lambda: fake.t)


def test_counts_quiet_time_only():
    fake = Fake()
    m = meter(fake)
    m.sample()
    for _ in range(20):  # 2 s quiet at 1 %
        fake.advance(0.1, 0.01)
        m.sample()
    m.busy(1)
    for _ in range(10):  # talking at 200 %: never counted
        fake.advance(0.1, 2.0)
        m.sample()
    m.free(1)
    for _ in range(5):  # settling after the utterance: not counted
        fake.advance(0.1, 1.0)
        m.sample()
    for _ in range(30):
        fake.advance(0.1, 0.01)
        m.sample()
    assert m.percent == pytest.approx(1.0, rel=0.05)
    assert 2.0 < m.idle_seconds < 3.5


def test_overlapping_utterances_stay_busy_until_all_are_done():
    fake = Fake()
    m = meter(fake, settle=0.0)
    m.sample()
    m.busy(1)
    m.busy(2)
    m.free(1)  # first final arrives while the second utterance is still going
    fake.advance(1.0, 2.0)
    m.sample()
    assert m.idle_seconds == 0
    m.free(2)
    fake.advance(1.0, 0.02)
    m.sample()
    fake.advance(1.0, 0.02)
    m.sample()
    assert m.percent == pytest.approx(2.0, rel=0.05)


def test_no_quiet_time_means_no_number():
    fake = Fake()
    m = meter(fake)
    m.busy("mic")
    m.sample()
    fake.advance(1.0, 1.0)
    m.sample()
    assert m.percent is None


def test_real_counters_move():
    assert process_cycles() > 0
    assert cycles_per_core_second(0.05) > 1e8
