"""The dictation session with fakes: a fake text field, a fake cleaner, the real bus and history."""

import threading
from dataclasses import dataclass

import pytest

from nova.actions.native import ActionError
from nova.bus import Bus
from nova.dictation import session as session_module
from nova.dictation.cleanup import Cleaned
from nova.dictation.history import Entry, History
from nova.dictation.session import Dictation
from nova.dictation.target import Landed, Spot
from nova.events import DictatedText, DictationChanged, DictationTyped, JobChip, PauseCandidate


@dataclass(frozen=True)
class FakeWin:
    hwnd: int = 1
    exe_name: str = "notepad.exe"
    cls: str = "Notepad"
    title: str = "Untitled - Notepad"


class FakeTarget:
    """A text field: the cursor is always at the end of ``doc``."""

    def __init__(self, doc="", kind="native", readable=True, password=False, exe="notepad.exe", lands=True):
        self.doc, self.kind, self.readable, self.password, self.lands = doc, kind, readable, password, lands
        self.win = FakeWin(exe_name=exe)
        self.pastes = []

    def look(self):
        if self.win is None:
            return Spot(None)
        before = self.doc[-300:] if self.readable else None
        return Spot(self.win, "Notepad", self.kind, before, self.password, ms=1.0)

    def insert(self, spot, text):
        self.pastes.append(text)
        if self.lands:
            self.doc += text
        return Landed(True if self.lands else False, 0.0, 3.0)

    def erase(self, spot, count):
        if count > len(self.doc):
            raise ActionError("nothing to erase")
        self.doc = self.doc[:-count]

    waited = 0

    def ready(self, timeout=3.0):
        self.waited += 1
        return True


class FakeJob:
    def __init__(self, text, result, gate=None):
        self.text, self.result, self.gate = text, result, gate
        self.cancelled = False
        self.error = ""
        self.ms = 900.0

    @property
    def done(self):
        return self.gate is None or self.gate.is_set()

    def wait(self, timeout):
        if self.gate is not None and not self.gate.wait(max(0.0, timeout or 0)):
            return None
        return None if self.cancelled else self.result

    def cancel(self):
        self.cancelled = True


class FakeCleaner:
    model = "fake gpu"

    def __init__(self, ready=True, fixes=None, gate=None):
        self.ready = ready
        self.fixes = fixes or {}
        self.gate = gate
        self.started = []
        self.warmed = 0

    def warm(self, block=False):
        self.warmed += 1

    def start(self, text, before):
        if not self.ready:
            return None
        self.started.append((text, before))
        return FakeJob(text, self.fixes.get(text, Cleaned(text)), self.gate)


@pytest.fixture
def rig(tmp_path):
    made = []

    def make(target=None, cleaner=None):
        bus = Bus()
        events = []
        bus.subscribe(object, events.append)
        target = target or FakeTarget()
        cleaner = cleaner or FakeCleaner(ready=False)
        history = History(tmp_path / "history.jsonl")
        dictation = Dictation(bus, cleaner, target, history)
        dictation.start()
        made.append(dictation)

        class Rig:
            pass

        r = Rig()
        r.bus, r.events, r.target, r.cleaner, r.history, r.dictation = bus, events, target, cleaner, history, dictation

        def say(*texts, start=1):
            for n, text in enumerate(texts, start):
                bus.publish(DictatedText(n, text, 0.0))
            assert dictation.drain(5)

        r.say = say
        r.on = lambda why="double-tap": bus.publish(DictationChanged(True, why, 0.0))
        r.off = lambda why="tap": (bus.publish(DictationChanged(False, why, 0.0)), dictation.drain(5))
        r.typed = lambda: [e for e in events if isinstance(e, DictationTyped)]
        return r

    yield make
    for dictation in made:
        dictation.close()


def test_double_tap_dictation_lands_clean_sentence_by_sentence(rig):
    r = rig()
    r.on()
    r.say("Um, hello there.", "How are you?")
    assert r.target.doc == "Hello there. How are you?"
    assert r.history.last().text == "Hello there. How are you?"
    assert [e.text for e in r.typed()] == ["Hello there.", "How are you?"]


def test_new_paragraph_and_scratch_that(rig):
    r = rig()
    r.on()
    r.say("First part.", "New paragraph.", "Second part.", "Wrong sentence.", "Scratch that.", "Right one.")
    assert r.target.doc == "First part.\n\nSecond part. Right one."
    assert r.history.last().text == r.target.doc


def test_scratch_that_leaves_text_alone_when_he_moved_the_cursor(rig):
    r = rig()
    r.on()
    r.say("Keep this.")
    r.target.doc += " typed by hand"
    r.say("Scratch that.", start=2)
    assert r.target.doc == "Keep this. typed by hand"
    assert "changed" in r.typed()[-1].note


def test_a_period_that_cut_a_sentence_in_half_comes_back_out(rig):
    r = rig()
    r.on()
    r.say("I want to go to.", "The store.")
    assert r.target.doc == "I want to go to the store."


def test_mid_sentence_continuation_is_lowercased_and_spaced(rig):
    r = rig(FakeTarget(doc="Notes: we tested it with"))
    r.on()
    r.say("About three robots.")
    assert r.target.doc == "Notes: we tested it with about three robots."


def test_a_plan_holds_his_words_until_the_doc_is_ready(rig):
    r = rig(FakeTarget(kind="docs", readable=False))
    r.dictation.begin("Google Doc", "dear team, the demo is friday", utterance=1, new_doc=True)
    r.on("Google Doc")
    r.say("We meet at noon.", start=2)
    assert r.target.doc == ""
    r.dictation.release()
    assert r.dictation.drain(5)
    assert r.target.doc == "Dear team, the demo is friday we meet at noon."


def test_a_plan_that_failed_saves_what_he_said(rig):
    r = rig(FakeTarget(kind="docs", readable=False))
    r.dictation.begin("Google Doc", "dear team", utterance=1, new_doc=True)
    r.on("Google Doc")
    r.say("We meet at noon.", start=2)
    r.off("failed")
    assert r.target.doc == ""
    last = r.history.last()
    assert last.text == "Dear team We meet at noon." and last.landed is False
    assert any(isinstance(e, JobChip) for e in r.events)


def test_never_into_a_password_field(rig):
    r = rig(FakeTarget(password=True))
    r.on()
    r.say("my secret words")
    assert r.target.pastes == []
    assert r.history.last() == Entry(r.history.last().at, "My secret words", "Notepad", landed=False)
    assert "password" in r.typed()[-1].note


def test_text_that_didnt_show_up_is_saved_not_typed_twice(rig):
    r = rig(FakeTarget(lands=False))
    r.on()
    r.say("Hello there.")
    assert r.target.pastes == ["Hello there."]
    assert r.history.last().landed is False and not r.typed()[-1].ok


def test_text_lands_at_once_and_a_misheard_word_is_fixed_in_place(rig):
    cleaner = FakeCleaner(fixes={"We should meat at noon.": Cleaned("We should meet at noon.", (("meat", "meet"),))})
    cleaner.gate = threading.Event()
    r = rig(cleaner=cleaner)
    r.on()
    r.say("We should meat at noon.", "Then we go home.")
    assert r.target.doc == "We should meat at noon. Then we go home."  # typed before the model answered
    cleaner.gate.set()
    assert r.dictation.drain(5, fixes=True)
    assert r.target.doc == "We should meet at noon. Then we go home."
    assert r.history.last().text == r.target.doc
    assert r.typed()[-1].note == "Fixed meat → meet"


def test_a_fix_never_touches_text_he_changed(rig):
    cleaner = FakeCleaner(fixes={"We should meat at noon.": Cleaned("We should meet at noon.", (("meat", "meet"),))})
    cleaner.gate = threading.Event()
    r = rig(cleaner=cleaner)
    r.on()
    r.say("We should meat at noon.")
    r.target.doc += " Typed by hand."
    cleaner.gate.set()
    assert r.dictation.drain(5, fixes=True)
    assert r.target.doc == "We should meat at noon. Typed by hand."


def test_no_fix_where_the_text_cant_be_read(rig):
    cleaner = FakeCleaner(fixes={"We should meat at noon.": Cleaned("We should meet at noon.", (("meat", "meet"),))})
    r = rig(FakeTarget(kind="docs", readable=False), cleaner=cleaner)
    r.on()
    r.say("We should meat at noon.")
    assert r.dictation.drain(5, fixes=True)
    assert r.target.doc == "We should meat at noon."


def test_a_late_answer_is_dropped(rig, monkeypatch):
    monkeypatch.setattr(session_module, "FIX_GIVE_UP_S", 0.05)
    cleaner = FakeCleaner(fixes={"We should meat at noon.": Cleaned("We should meet at noon.", (("meat", "meet"),))})
    cleaner.gate = threading.Event()
    r = rig(cleaner=cleaner)
    r.on()
    r.say("We should meat at noon.")
    assert r.dictation.drain(5, fixes=True)
    cleaner.gate.set()
    assert r.target.doc == "We should meat at noon."


def test_short_segments_skip_the_model(rig):
    cleaner = FakeCleaner()
    r = rig(cleaner=cleaner)
    r.on()
    r.say("Okay.")
    assert cleaner.started == []


def test_cleanup_starts_in_the_pause_and_the_final_reuses_it(rig):
    cleaner = FakeCleaner(fixes={"We should meat at noon.": Cleaned("We should meet at noon.", (("meat", "meet"),))})
    r = rig(cleaner=cleaner)
    r.on()
    r.bus.publish(PauseCandidate(7, "We should meat at noon.", "We should meat at noon."))
    assert len(cleaner.started) == 1
    r.bus.publish(DictatedText(7, "We should meat at noon.", 0.0))
    assert r.dictation.drain(5, fixes=True)
    assert len(cleaner.started) == 1 and r.target.doc == "We should meet at noon."


def test_a_stale_guess_is_dropped_when_he_keeps_talking(rig):
    cleaner = FakeCleaner()
    r = rig(cleaner=cleaner)
    r.on()
    r.bus.publish(PauseCandidate(7, "We should go to the", "x"))
    r.bus.publish(DictatedText(7, "We should go to the park now.", 0.0))
    assert r.dictation.drain(5)
    assert [t for t, _ in cleaner.started] == ["We should go to the", "We should go to the park now."]


def test_paste_my_last_dictation(rig):
    r = rig()
    r.history.add(Entry("t", "Saved words.", "Chrome", landed=False))
    r.target.doc = "Earlier text."
    assert r.dictation.paste_last() == "Pasted your last dictation (2 words)"
    assert r.target.doc == "Earlier text. Saved words." and r.history.last().landed


def test_paste_with_nothing_saved_says_so(rig):
    with pytest.raises(ActionError, match="no dictation"):
        rig().dictation.paste_last()


def test_after_opening_an_app_the_words_wait_for_its_text_field(rig):
    r = rig()
    r.dictation.begin("voice", "it's a wonderful day", utterance=1)
    r.dictation.release(wait_for_field=True)
    assert r.dictation.drain(5)
    assert r.target.waited == 1 and r.target.doc == "It's a wonderful day"


def test_a_correction_split_by_pauses_fixes_the_segment_before(rig):
    r = rig()
    r.on()
    r.say("We finished it.", "Let's meet on Tuesday.", "No wait.", "Wednesday at noon to plan it.")
    assert r.target.doc == "We finished it. Let's meet on Wednesday at noon to plan it."
    r.say("Send it to John.", "No wait, to Mary.", start=5)
    assert r.target.doc.endswith("plan it. Send it to Mary.")
    assert r.history.last().text == r.target.doc


def test_a_paragraph_break_goes_in_with_the_next_words(rig):
    cleaner = FakeCleaner(fixes={"Next we meat.": Cleaned("Next we meet.", (("meat", "meet"),))})
    r = rig(cleaner=cleaner)
    r.on()
    r.say("End of part one.", "New paragraph.")
    assert r.target.pastes == ["End of part one."]  # editors drop a paste of only line breaks
    r.say("Next we meat.", start=3)
    assert r.target.pastes[1] == "\n\nNext we meat."
    assert r.dictation.drain(5, fixes=True)
    assert r.target.doc == "End of part one.\n\nNext we meet."


def test_new_paragraph_then_scratch_that_drops_the_break_only(rig):
    r = rig()
    r.on()
    r.say("Keep this.", "New paragraph.", "Scratch that.", "Same paragraph.")
    assert r.target.doc == "Keep this. Same paragraph."
