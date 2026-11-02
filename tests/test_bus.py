import sys
import threading
from dataclasses import dataclass

import pytest

from nova.bus import Bus


@dataclass(frozen=True)
class Ping:
    n: int


@dataclass(frozen=True)
class LoudPing(Ping):
    volume: int = 11


@dataclass(frozen=True)
class Pong:
    pass


def test_handler_receives_published_event():
    bus = Bus()
    got = []
    bus.subscribe(Ping, got.append)
    bus.publish(Ping(1))
    assert got == [Ping(1)]


def test_only_matching_type_is_delivered():
    bus = Bus()
    pings, pongs = [], []
    bus.subscribe(Ping, pings.append)
    bus.subscribe(Pong, pongs.append)
    bus.publish(Pong())
    assert pings == [] and pongs == [Pong()]


def test_base_class_subscribers_get_subclass_events_after_exact_ones():
    bus = Bus()
    order = []
    bus.subscribe(Ping, lambda e: order.append("base"))
    bus.subscribe(LoudPing, lambda e: order.append("exact"))
    bus.publish(LoudPing(2))
    assert order == ["exact", "base"]


def test_object_subscriber_sees_everything():
    bus = Bus()
    got = []
    bus.subscribe(object, got.append)
    bus.publish(Ping(1))
    bus.publish(Pong())
    assert got == [Ping(1), Pong()]


def test_unsubscribe_stops_delivery_and_is_idempotent():
    bus = Bus()
    got = []
    unsubscribe = bus.subscribe(Ping, got.append)
    unsubscribe()
    unsubscribe()
    bus.publish(Ping(1))
    assert got == []


def test_same_handler_twice_is_two_subscriptions():
    bus = Bus()
    got = []
    first = bus.subscribe(Ping, got.append)
    bus.subscribe(Ping, got.append)
    first()
    bus.publish(Ping(1))
    assert got == [Ping(1)]


def test_failing_handler_does_not_stop_others(caplog):
    bus = Bus()
    got = []

    def boom(event):
        raise RuntimeError("boom")

    bus.subscribe(Ping, boom)
    bus.subscribe(Ping, got.append)
    bus.publish(Ping(1))
    assert got == [Ping(1)]
    assert "boom" in caplog.text


def test_publish_rejects_non_dataclass_events():
    bus = Bus()
    with pytest.raises(TypeError):
        bus.publish({"not": "an event"})
    with pytest.raises(TypeError):
        bus.publish(Ping)  # the class, not an instance


def test_subscribe_during_publish_applies_to_next_publish():
    bus = Bus()
    late = []

    def add_late(event):
        bus.subscribe(Ping, late.append)

    bus.subscribe(Ping, add_late)
    bus.publish(Ping(1))
    assert late == []
    bus.publish(Ping(2))
    assert late == [Ping(2)]


def test_concurrent_publish_while_earlier_handlers_churn():
    # The churner keeps removing the handler *ahead of* the counter. A bus that mutated the
    # live handler list in place would shift the counter mid-iteration and skip it.
    bus = Bus()
    count = 0
    lock = threading.Lock()

    def noop(event):
        pass

    def counter(event):
        nonlocal count
        with lock:
            count += 1

    ahead = bus.subscribe(Ping, noop)
    bus.subscribe(Ping, counter)
    errors = []

    def publisher():
        try:
            for i in range(2000):
                bus.publish(Ping(i))
        except Exception as error:  # pragma: no cover - failure path
            errors.append(error)

    def churner():
        nonlocal ahead
        try:
            for _ in range(4000):
                ahead()
                ahead = bus.subscribe(Ping, noop)
        except Exception as error:  # pragma: no cover - failure path
            errors.append(error)

    old_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)  # force frequent thread switches
    try:
        threads = [threading.Thread(target=publisher) for _ in range(4)] + [threading.Thread(target=churner)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        sys.setswitchinterval(old_interval)
    assert errors == []
    assert count == 8000
