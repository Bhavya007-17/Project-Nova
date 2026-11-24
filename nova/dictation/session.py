"""The dictation session (SPEC §11): segments of speech -> rules -> text at the cursor -> light cleanup.

The state machine owns *when* Nova dictates (double-tap, "type this…", a plan, and the ends: a tap, "stop
dictation", Esc, 10 s of silence) and turns every final heard meanwhile into DictatedText. This module types
them, one at a time and in order, on its own thread ("nova-dictation"):

1. rules (``nova.dictation.rules``): spoken commands, fillers, self-corrections (also one a pause split off:
   "No wait, Wednesday." rewrites the segment before); "scratch that" takes back the last segment typed;
   "make that more formal" has the local model rewrite the last segment and swaps it in place (07e);
2. join + insert (``nova.dictation.target``): space / capital to fit the text before the cursor, a clipboard
   paste, a check that it landed — right away, so a segment lands well inside a second of the pause;
3. cleanup (``nova.dictation.cleanup``): the small model looks for misheard words meanwhile (it starts while he is
   still pausing, on the PauseCandidate). Its call takes ~1 s on this laptop, so it never holds a segment up: when
   it finds something ("meat" -> "meet"), Nova fixes it in place — back to the word, retype — only if the text
   before the cursor still ends with exactly what Nova typed (where it can't be read, e.g. Google Docs, the
   segment stays as typed).

Text that can't be typed (a password field, a terminal, nothing in front, it didn't show up) is never typed
again: it's saved in the history and the island says so. Erasing ("scratch that", a fix) only ever touches text
Nova can see it typed itself.

A plan that starts dictation ("open a Google Doc and type this: …") opens the session *held*: what he says while
the doc opens queues up, his first words go first, and it all goes in once the doc is ready (``release``).
History: the last 50 dictations (``nova.dictation.history``); "paste my last dictation" types the last one.
Every segment writes a trace line (``kind: dictation``, and ``dictation_fix`` for a fix) with its hop timings;
``nova latency`` reads them.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from ..actions.native import ActionError
from ..brain import rewrite as rw
from ..brain.llm import LOAD_TIMEOUT_S
from ..brain.risk import assess_dictation
from ..brain.tier0 import rewrite_that
from ..bus import Bus
from ..events import DictatedText, DictationChanged, DictationTyped, JobChip, PauseCandidate, SpeechEnded
from ..log import TraceLog
from .cleanup import Cleaner, Job
from .history import Entry, History
from .rules import Joined, capitalize, correction_lead, join, landed, parse
from .target import Spot, Target

_log = logging.getLogger(__name__)

MIN_LLM_WORDS = 3  # "Okay." / "Thanks!" aren't worth a model call
FIX_GIVE_UP_S = 5.0  # cleanup later than this is dropped
CONTEXT_CHARS = 300
_FIX_CHARS = 280  # a fix rewrites at most this much (and only what it can see before the cursor)
_REWRITE_CHARS = 1500  # "make that more formal" swaps at most this much (backspaces, then a paste)
_KEEP = 16
SAVED_NOTE = "Saved — say “paste my last dictation”"
NOTICE_S = 8.0


@dataclass(frozen=True)
class _Segment:
    utterance: int | None
    text: str
    at: float  # perf_counter when its final arrived
    initial: bool = False  # words said with the command that started dictation (its timing includes the plan)


@dataclass(frozen=True)
class _Open:
    why: str
    text: str | None = None  # words already said ("type this: …"): typed first
    utterance: int | None = None
    hold: bool = False  # wait for ``release`` (the target is still opening)
    new_doc: bool = False  # the target is a brand-new document: nothing before the cursor
    at: float = 0.0


@dataclass(frozen=True)
class _Release:
    wait: bool = False  # an app was just opened: first wait for a text field to have focus


@dataclass(frozen=True)
class _Close:
    why: str


@dataclass(frozen=True)
class _Done:
    event: threading.Event


@dataclass(frozen=True)
class _Typed:
    """One paste this session made."""

    text: str  # exactly what was pasted
    hwnd: int
    source: str  # the segment's own words (before fitting them to the cursor)
    before: str | None  # the text before the cursor it was fitted to
    erased: int = 0  # characters taken back before it (a period that cut a sentence in half)


@dataclass(frozen=True)
class _Fix:
    job: Job
    typed: _Typed
    seg: _Segment


def _words(text: str) -> int:
    return len(text.split())


def _iso(perf: float | None) -> str | None:
    if perf is None:
        return None
    stamp = time.time() - (time.perf_counter() - perf)
    return datetime.fromtimestamp(stamp).astimezone().isoformat(timespec="milliseconds")


def _ms(later: float | None, earlier: float | None) -> float | None:
    return round((later - earlier) * 1000, 1) if later is not None and earlier is not None else None


def _flat(text: str) -> str:
    return text.replace("\r\n", "\n")


class Dictation:
    def __init__(self, bus: Bus | None, cleaner: Cleaner, target: Target, history: History, *,
                 trace: TraceLog | None = None, clock: Callable[[], float] = time.perf_counter,
                 rewriter: Any = None) -> None:
        self._bus = bus
        self._cleaner = cleaner
        self._rewriter = rewriter  # ``nova.brain.rewrite.Rewriter``: "make that more formal" (07e); None = off
        self._ending = threading.Event()  # dictation is ending: a rewrite the dictation thread waits on gives up
        self._target = target
        self._history = history
        self._trace = trace
        self._clock = clock
        self._queue: queue.SimpleQueue[object] = queue.SimpleQueue()
        self._lock = threading.Lock()
        self._jobs: dict[tuple[int, str], Job] = {}  # cleanup started in a pause, by (utterance, text)
        self._tail = ""  # the end of what this session typed (cleanup's context while speculating)
        self._ends: dict[int, tuple[float, float]] = {}  # utterance -> (speech ended, segment ended) perf times
        self._watching = 0  # cleanup answers still to come
        # dictation thread only
        self._active = False
        self._held = False
        self._new_doc = False
        self._pending: list[_Segment] = []
        self._marker = ""  # "No wait." on its own: it corrects the last segment with what he says next
        self._break = ""  # "new line" / "new paragraph" on its own: goes in with the next words
        self._typed: list[_Typed] = []
        self._entry: Entry | None = None
        self._unsubscribe: list[Callable[[], None]] = []
        if bus is not None:
            self._unsubscribe = [
                bus.subscribe(DictationChanged, self._on_changed),
                bus.subscribe(DictatedText, lambda e: self._queue.put(_Segment(e.utterance, e.text, self._clock()))),
                bus.subscribe(PauseCandidate, self._on_candidate),
                bus.subscribe(SpeechEnded, self._on_speech_end),
            ]
        self._thread = threading.Thread(target=self._run, name="nova-dictation", daemon=True)

    # --- any thread -------------------------------------------------------------------------------------------------

    def start(self) -> None:
        self._thread.start()

    def close(self, timeout: float = 3.0) -> None:
        for unsubscribe in self._unsubscribe:
            unsubscribe()
        self._queue.put(None)
        if self._thread.is_alive():
            self._thread.join(timeout)

    def begin(self, why: str, text: str | None = None, *, utterance: int | None = None, new_doc: bool = False) -> None:
        """A plan starts dictation: hold what he says until ``release`` (the doc / field is ready)."""
        self._ending.clear()
        self._queue.put(_Open(why, text, utterance, hold=True, new_doc=new_doc, at=self._clock()))

    def release(self, wait_for_field: bool = False) -> None:
        """The target is ready (``wait_for_field``: an app was just opened — wait until its text field has focus)."""
        self._queue.put(_Release(wait_for_field))

    def drain(self, timeout: float = 10.0, fixes: bool = False) -> bool:
        """Wait until everything queued so far is done (`nova say`, tests); ``fixes``: and cleanup in flight."""
        deadline = time.monotonic() + timeout
        while True:
            done = threading.Event()
            self._queue.put(_Done(done))
            if not done.wait(max(0.0, deadline - time.monotonic())):
                return False
            with self._lock:
                busy = fixes and self._watching
            if not busy:
                return True
            time.sleep(0.05)

    def _on_changed(self, event: DictationChanged) -> None:
        if event.on:
            self._ending.clear()
        else:
            self._ending.set()  # Esc / a tap: a rewrite in flight stops now, not when the model answers
        self._queue.put(_Open(event.why, at=event.at) if event.on else _Close(event.why))

    def _on_speech_end(self, event: SpeechEnded) -> None:
        if event.reason != "cancel":
            with self._lock:
                self._ends[event.utterance] = (event.speech_end_at, self._clock())
                while len(self._ends) > _KEEP:
                    self._ends.pop(next(iter(self._ends)))

    def _on_candidate(self, event: PauseCandidate) -> None:
        """A pause that may end the segment: start its cleanup now (bus thread; quick)."""
        if not self._cleaner.ready:
            return
        parsed = parse(event.text)
        if parsed.scratch or _words(parsed.text) < MIN_LLM_WORDS:
            return
        key = (event.utterance, parsed.text)
        with self._lock:
            if key in self._jobs:
                return
            for other in [k for k in self._jobs if k[0] == event.utterance]:
                self._jobs.pop(other).cancel()  # he went on talking: that guess is stale
            job = self._cleaner.start(parsed.text, self._tail)
            if job is not None:
                self._jobs[key] = job

    def _claim(self, utterance: int | None, text: str) -> Job | None:
        with self._lock:
            job = self._jobs.pop((utterance, text), None) if utterance is not None else None
            for key in [k for k in self._jobs if utterance is None or k[0] <= utterance]:
                self._jobs.pop(key).cancel()
            return job

    def _watch(self, job: Job, typed: _Typed, seg: _Segment) -> None:
        """Hand the cleanup's answer back to the dictation thread when it comes."""
        with self._lock:
            self._watching += 1

        def wait() -> None:
            job.wait(FIX_GIVE_UP_S)
            self._queue.put(_Fix(job, typed, seg))

        threading.Thread(target=wait, name="nova-cleanup-wait", daemon=True).start()

    # --- the dictation thread -------------------------------------------------------------------------------------

    def _run(self) -> None:
        from ..actions import native

        native.com_init()
        while True:
            item = self._queue.get()
            if item is None:
                return
            try:
                self._handle(item)
            except Exception:  # noqa: BLE001 - one bad segment must not end dictation
                _log.exception("dictation error on %r", item)

    def _handle(self, item: object) -> None:
        if isinstance(item, _Open):
            self._open(item)
        elif isinstance(item, _Release):
            if item.wait and self._pending and not self._target.ready():
                _log.info("no text field had focus after %.0f s; typing anyway", 3.0)
            self._held = False
            pending, self._pending = self._pending, []
            for segment in pending:
                self._segment(segment)
        elif isinstance(item, _Segment):
            if not self._active:
                self._open(_Open("speech", at=item.at))
            if self._held:
                self._pending.append(item)
            else:
                self._segment(item)
        elif isinstance(item, _Fix):
            try:
                self._fix(item)
            finally:
                with self._lock:
                    self._watching -= 1
        elif isinstance(item, _Close):
            self._close(item.why)
        elif isinstance(item, _Done):
            item.event.set()

    def _open(self, item: _Open) -> None:
        if self._active and not item.hold:
            return  # the state machine confirming a session the brain already opened
        if not self._active:
            self._typed, self._entry, self._marker, self._break = [], None, "", ""
            with self._lock:
                self._tail = ""
            self._cleaner.warm()
        self._active, self._held = True, item.hold
        self._new_doc = item.new_doc
        if item.text and item.text.strip():
            self._pending.insert(0, _Segment(item.utterance, item.text, item.at, initial=True))
            if not self._held:
                self._handle(_Release())

    def _close(self, why: str) -> None:
        if not self._active:
            return
        self._active = self._held = False
        if self._pending:
            words = " ".join(p for p in (parse(s.text).text.strip() for s in self._pending) if p)
            self._pending = []
            if words:
                self._lost(None, words, "Nothing was ready to type into" if why == "failed" else "Dictation ended")
        with self._lock:
            for job in self._jobs.values():
                job.cancel()  # guesses for segments that will never come
            self._jobs.clear()
        _log.info("dictation ended (%s): %d segments typed", why, len(self._typed))

    # --- one segment ----------------------------------------------------------------------------------------------

    def _segment(self, seg: _Segment) -> None:
        began = self._clock()
        said = seg.text
        if self._marker:
            said, self._marker = f"{self._marker} {said}", ""
        record: dict[str, Any] = {"kind": "dictation", "utterance": seg.utterance, "raw_text": said,
                                  "initial": seg.initial}
        if self._typed and self._rewriter is not None and not self._break:
            instruction = rewrite_that(said)
            if instruction:  # "make that more formal": the segment just typed, rewritten in place
                self._rewrite_last(instruction, seg, record)
                return
        lead = correction_lead(said)
        if lead == "only" and self._typed:
            self._marker = said  # "No wait." — what he says next is the correction
            self._write({**record, "correction": "waiting"}, seg)
            return
        parsed = parse(said)
        if lead == "lead" and self._typed:  # "No wait, Wednesday." after "Let's meet on Tuesday."
            previous = self._typed[-1].source
            combined = parse(f"{previous} {said}")
            if combined.text and not combined.text.startswith(previous.rstrip(" .!?…")) and self._scratch(1, quiet=True):
                parsed = combined
                record["correction"] = "previous segment"
        record.update(rules_ms=round((self._clock() - began) * 1000, 2), scratch=parsed.scratch)
        scratch = parsed.scratch
        if scratch and self._break:  # "new paragraph" ... "scratch that": the break hadn't gone in yet
            self._break, scratch = "", scratch - 1
        if scratch:
            record["scratched"] = self._scratch(scratch)
        text = parsed.text
        if text and not text.strip("\n"):
            # Only line breaks: editors ignore a paste of nothing but line breaks (RichEdit / Notepad), so they go
            # in with the words that follow.
            self._break += text
            self._write({**record, "break": "waiting"}, seg)
            return
        if text and self._break:
            text, self._break = self._break + text.lstrip(" "), ""
        if not text:
            self._write(record, seg)
            return
        job = self._claim(seg.utterance, parsed.text)
        speculative = job is not None
        spot = self._target.look()
        record.update(look_ms=round(spot.ms, 1), app=spot.app, where=spot.kind)
        if spot.win is None:
            self._lost(seg.utterance, text, "No window is in front to type into", record, seg, job)
            return
        risk = assess_dictation(text, spot.scene())
        if risk is not None:
            self._lost(seg.utterance, text, risk.reason, record, seg, job)
            return
        before = self._before(spot)
        ours = bool(self._typed) and self._typed[-1].hwnd == spot.hwnd and before is not None \
            and landed(before, self._typed[-1].text)
        joined = join(before, text, fix_period=ours)
        try:
            if joined.erase:
                spot = self._target.erase(spot, joined.erase)
            result = self._target.insert(spot, joined.insert)
        except ActionError as error:
            self._lost(seg.utterance, text, str(error), record, seg, job)
            return
        record.update(paste_ms=round(result.ms, 1), landed=result.ok, erase=joined.erase)
        if result.ok is False:
            self._lost(seg.utterance, text, "It didn't show up where you're typing", record, seg, job,
                       landed_at=result.at)
            return
        typed = _Typed(joined.insert, spot.hwnd, text, before, joined.erase)
        self._typed.append(typed)
        with self._lock:
            self._tail = (self._tail[: len(self._tail) - joined.erase] + joined.insert)[-CONTEXT_CHARS:]
        self._remember(joined, spot)
        self._publish(DictationTyped(seg.utterance, text.strip()))
        if job is None and self._cleaner.ready and _words(text) >= MIN_LLM_WORDS:
            job = self._cleaner.start(text.lstrip("\n"), (before or "")[-CONTEXT_CHARS:])
        if job is not None:
            self._watch(job, typed, seg)
        record.update(text=joined.insert, llm="pending" if job is not None else
                      ("off" if not self._cleaner.ready else "short"), speculative=speculative)
        self._write(record, seg, landed_at=result.at)

    def _before(self, spot: Spot) -> str | None:
        """The text before the cursor: what the field says, else what this session typed there, else unknown."""
        if spot.before is not None:
            return spot.before
        if self._typed and self._typed[-1].hwnd == spot.hwnd:
            with self._lock:
                return self._tail
        if self._new_doc and spot.kind == "docs" and not self._typed:
            return ""
        return None

    # --- cleanup's answer: fix in place ------------------------------------------------------------------------------

    def _fix(self, item: _Fix) -> None:
        job, typed, seg = item.job, item.typed, item.seg
        record: dict[str, Any] = {"kind": "dictation_fix", "utterance": seg.utterance, "model": self._cleaner.model,
                                  "llm_ms": round(job.ms, 1) if job.done else None, "initial": seg.initial}
        result = job.result if job.done and not job.cancelled else None
        if result is None:
            late = not job.done  # before cancel(), which marks the job done
            job.cancel()
            outcome = "late" if late else ("cold" if getattr(job, "cold", False) else "failed")
            record.update(llm=outcome, llm_error=job.error or None)
            self._write(record, seg)
            return
        if result.rejected or not result.fixes:
            record.update(llm="rejected" if result.rejected else "clean", llm_error=result.rejected or None)
            self._write(record, seg)
            return
        record.update(llm="fixed", changes=[list(f) for f in result.fixes])
        applied, outcome = self._replace(typed, result.text, _FIX_CHARS)
        if outcome is None:
            self._write({**record, "applied": applied}, seg)
            return
        said = ", ".join(f"{heard} → {meant}" for heard, meant in result.fixes)
        self._publish(DictationTyped(seg.utterance, result.text.strip(), True, f"Fixed {said}"))
        record.update(applied="yes", fix_landed=outcome.ok)
        self._write(record, seg, landed_at=outcome.at)

    def _replace(self, typed: _Typed, words: str, limit: int) -> tuple[str, Any]:
        """Swap a segment this session typed for ``words`` in place: back to where they differ, retype the rest (and
        what came after it) — only if the text before the cursor still ends with exactly what Nova typed.
        Returns ("yes", the paste's outcome) or (why not, None)."""
        index = next((i for i, t in enumerate(self._typed) if t is typed), None)
        if index is None:
            return "scratched since", None
        breaks = typed.source[: len(typed.source) - len(typed.source.lstrip("\n"))]
        if breaks and not words.startswith("\n"):  # (cleanup started before a paragraph break joined it)
            words = breaks + words
        fixed = join(typed.before, words, fix_period=typed.erased > 0).insert
        later = "".join(t.text for t in self._typed[index + 1 :])
        old, new = _flat(typed.text + later), _flat(fixed + later)
        keep = 0
        while keep < min(len(old), len(new)) and old[keep] == new[keep]:
            keep += 1
        spot = self._target.look()
        if spot.hwnd != typed.hwnd or spot.before is None or len(old) > limit \
                or not landed(spot.before, old, tail=len(old)):
            why = "can't read the field" if spot.before is None else "the text changed"
            return f"no: {why}", None
        try:
            spot = self._target.erase(spot, len(old) - keep)
            outcome = self._target.insert(spot, new[keep:])
        except ActionError as error:
            return f"no: {error}", None
        self._typed[index] = replace(typed, text=fixed, source=words)
        with self._lock:
            self._tail = "".join(t.text for t in self._typed)[-CONTEXT_CHARS:]
        if self._entry is not None and _flat(self._entry.text).endswith(old):
            self._entry = replace(self._entry, text=_flat(self._entry.text)[: -len(old)] + new)
            self._history.update_last(self._entry)
        return "yes", outcome

    # --- "make that more formal": the segment just typed, rewritten (07e) -------------------------------------------

    def _rewrite_last(self, instruction: str, seg: _Segment, record: dict[str, Any]) -> None:
        """Have the local model rewrite the segment just typed and swap it in place. This thread waits for the model
        (what he says meanwhile queues up); dictation ending (Esc / a tap) stops the wait. The trace keeps the kind,
        word counts and timings — never the rewritten text."""
        rewriter, typed = self._rewriter, self._typed[-1]
        source = typed.source.lstrip("\n")
        what = rw.kind(instruction)
        n = rw.word_count(source)
        record.update(rewrite=what, rewrite_words_in=n)
        try:
            choice = rewriter.choice()
            too_long = n > rewriter.limit(instruction, choice)
        except Exception as error:  # noqa: BLE001 - no model to choose: say so, keep dictating
            self._rewrite_note(seg, record, f"no model: {error}", "I can't rewrite right now.")
            return
        if too_long:
            self._rewrite_note(seg, record, "too long", "That's too long to rewrite here.")
            return
        began = self._clock()
        job = rewriter.start(instruction, source, choice)
        deadline = time.monotonic() + job.timeout + LOAD_TIMEOUT_S + 5
        while not job.done and not self._ending.is_set() and time.monotonic() < deadline:
            job.wait(0.05)
        out = job.wait(0) if job.done else None
        if not job.done:
            job.cancel()
        record.update(rewrite_ms=round((self._clock() - began) * 1000, 1), device=job.device,
                      cold=job.cold or None)
        if out is None:
            if self._ending.is_set():
                self._write({**record, "rewrite_result": "stopped"}, seg)
            else:
                self._rewrite_note(seg, record, f"failed: {job.error or 'no answer'}",
                                   "The rewrite didn't come back — try again.")
            return
        verdict = rw.guard(source, out, what)
        if verdict == "fail":
            self._rewrite_note(seg, record, "empty", "The rewrite came back empty — try again.")
            return
        if verdict == "same":
            self._rewrite_note(seg, record, "same", "Already reads that way — nothing changed", ok=True)
            return
        if verdict == "preview":  # an odd length and no card while he dictates: his words stay, the rewrite is saved
            self._history.add(Entry(datetime.now().astimezone().isoformat(timespec="seconds"), out.strip(),
                                    self._entry.app if self._entry is not None else "", landed=False))
            self._entry = None
            self._rewrite_note(seg, record, "odd length", f"The rewrite looked off, so I kept yours · {SAVED_NOTE}")
            return
        applied, outcome = self._replace(typed, rw.keep_edges(source, out.strip()), _REWRITE_CHARS)
        record.update(rewrite_result=applied, rewrite_words_out=rw.word_count(out))
        if outcome is None:
            why = "the text changed" if "changed" in applied or "scratched" in applied else "I can't read that field"
            self._publish(DictationTyped(seg.utterance, "", False, f"Couldn't rewrite that — {why}"))
            self._write(record, seg)
            return
        self._publish(DictationTyped(seg.utterance, "", True, f"Rewrote {n} word{'' if n == 1 else 's'}"))
        self._write({**record, "fix_landed": outcome.ok}, seg, landed_at=outcome.at)

    def _rewrite_note(self, seg: _Segment, record: dict[str, Any], result: str, note: str, ok: bool = False) -> None:
        self._publish(DictationTyped(seg.utterance, "", ok, note))
        self._write({**record, "rewrite_result": result}, seg)

    # --- taking text back -------------------------------------------------------------------------------------------

    def _scratch(self, count: int, quiet: bool = False) -> int:
        """Take back the last ``count`` segments typed, if the text before the cursor still ends with them."""
        done = 0
        for _ in range(count):
            if not self._typed:
                break
            last = self._typed[-1]
            spot = self._target.look()
            if spot.win is None or spot.hwnd != last.hwnd:
                self._publish(DictationTyped(None, "", False, "Couldn't scratch that — you've moved on"))
                break
            if spot.before is not None and not landed(spot.before, last.text):
                self._publish(DictationTyped(None, "", False, "Couldn't scratch that — the text changed"))
                break
            try:
                spot = self._target.erase(spot, len(_flat(last.text)))
                if last.erased:
                    self._target.insert(spot, ".")  # the period it had taken back
            except ActionError as error:
                self._publish(DictationTyped(None, "", False, f"Couldn't scratch that — {error}"))
                break
            self._typed.pop()
            with self._lock:
                self._tail = "".join(t.text for t in self._typed)[-CONTEXT_CHARS:]
            if self._entry is not None and self._entry.text.endswith(last.text):
                self._entry = replace(self._entry, text=self._entry.text[: -len(last.text)])
                self._history.update_last(self._entry)
            done += 1
        if done and not quiet:
            self._publish(DictationTyped(None, "", True, "Scratched"))
        return done

    # --- history, notices, traces ------------------------------------------------------------------------------------

    def _remember(self, joined: Joined, spot: Spot) -> None:
        entry = self._entry
        if entry is None or entry.app != spot.app or not entry.landed:
            self._entry = Entry(datetime.now().astimezone().isoformat(timespec="seconds"),
                                capitalize(joined.insert.lstrip(" ")), spot.app)
            self._history.add(self._entry)
            return
        text = entry.text[: len(entry.text) - joined.erase] if joined.erase else entry.text
        self._entry = replace(entry, text=text + joined.insert)
        self._history.update_last(self._entry)

    def _lost(self, utterance: int | None, text: str, why: str, record: dict[str, Any] | None = None,
              seg: _Segment | None = None, job: Job | None = None, landed_at: float | None = None) -> None:
        """Couldn't type it: save it (never type twice) and say so."""
        if job is not None:
            job.cancel()
        self._history.add(Entry(datetime.now().astimezone().isoformat(timespec="seconds"), text.strip(),
                                record.get("app", "") if record else "", landed=False))
        self._entry = None
        note = f"{why} · {SAVED_NOTE}"
        _log.info("dictation not typed: %s", why)
        self._publish(DictationTyped(utterance, text.strip(), False, note))
        if not self._active and self._bus is not None:
            bus = self._bus
            bus.publish(JobChip("dictation", note, "failed"))
            timer = threading.Timer(NOTICE_S, lambda: bus.publish(JobChip("dictation", note, "gone")))
            timer.daemon = True
            timer.start()
        if record is not None and seg is not None:
            record.update(landed=False, lost=why, text=text)
            self._write(record, seg, landed_at=landed_at)

    def _publish(self, event: object) -> None:
        if self._bus is not None:
            self._bus.publish(event)

    def _write(self, record: dict[str, Any], seg: _Segment, landed_at: float | None = None) -> None:
        with self._lock:
            ends = self._ends.get(seg.utterance) if seg.utterance is not None else None
        speech_end, endpoint = ends if ends is not None else (None, None)
        done = self._clock()
        record.update(
            speech_end=_iso(speech_end), words=_words(str(record.get("text") or "")),
            final_ms=_ms(seg.at, speech_end), endpoint_ms=_ms(endpoint, speech_end),
            total_ms=_ms(landed_at, speech_end), after_pause_ms=_ms(landed_at, endpoint),
            work_ms=round((done - seg.at) * 1000, 1),
        )
        if self._trace is not None:
            self._trace.write(**record)
        if landed_at is not None:
            _log.info("%s: %s ms after speech (%s ms after the pause)", record["kind"], record.get("total_ms"),
                      record.get("after_pause_ms"))

    # --- "paste my last dictation" (brain thread) ----------------------------------------------------------------------

    def paste_last(self) -> str:
        entry = self._history.last()
        if entry is None or not entry.text.strip():
            raise ActionError("There's no dictation to paste yet.")
        spot = self._target.look()
        if spot.win is None:
            raise ActionError("No window is in front to paste into.")
        risk = assess_dictation(entry.text, spot.scene())
        if risk is not None:
            raise ActionError(risk.reason)
        joined = join(spot.before, entry.text.strip(" "), fix_period=False)
        result = self._target.insert(spot, joined.insert)
        if result.ok is False:
            raise ActionError("I pasted it, but it didn't show up.")
        if not entry.landed:
            self._history.update_last(replace(entry, landed=True))
        words = _words(entry.text)
        return f"Pasted your last dictation ({words} word{'s' if words != 1 else ''})"
