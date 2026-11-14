"""`nova eval --sessions` (Stage 07b): talk sessions clause by clause through the real machine, gate and brain."""

from dataclasses import replace

from catalog_fixture import CATALOG, ROWS

from nova import tts
from nova.actions.catalog import DEFAULT_ALIASES, Catalog, apps_from_rows, parse_aliases, words_of
from nova.brain.plan import act, step
from nova.brain.router import Routed, Router
from nova.session_eval import (
    Line,
    Screen,
    Session,
    advance,
    align,
    handle_runner,
    load_sessions,
    run_session,
    summarize,
    timings,
)

_NOTEPAD = act(step("open_app", app="Notepad"))
_TABLE = {
    "open notepad": _NOTEPAD,
    "create a new note": act(step("press", keys="ctrl+n")),
    "type hello": act(step("type_text", text="hello", submit=False)),
    "open calculator": act(step("open_app", app="Calculator")),
    "snap it left": act(step("window", op="snap_left", target="active")),
    "thats all": act(step("listening", op="end_session")),
}


class TableRouter(Router):
    """Tier 0 replaced by a table of clause texts (no model). Records what each clause was shown."""

    def __init__(self, table=None, default=None):
        super().__init__()
        self._table = _TABLE if table is None else table
        self._default = default
        self.seen = []  # (clause text, app in front, before)

    def _route(self, text, ctx, catalog, *, strict, via, running, tier1_only):
        front = ctx.foreground.app if ctx.foreground is not None else None
        self.seen.append((text, front, self.before))
        plan = self._table.get(" ".join(words_of(text)), self._default)
        return Routed(plan, "0" if plan is not None else "none", "" if plan is not None else "Didn't get it")


def session(*lines, opener="tap", screen=Screen("Google Chrome", ("Google Chrome",))):
    return Session("test", "", screen, 900, opener, tuple(lines))


def line(say, *want, fail=False):
    return Line.of(say, want, CATALOG, fail=fail)


def run(sess, router=None):
    router = router or TableRouter()
    return run_session(sess, handle_runner(router, CATALOG), CATALOG), router


def test_sessions_yaml_loads_and_case_1_is_the_recording():
    # sessions.yaml accepts either Windows notes app for "the notes app": the laptop has both, so the catalog does too.
    sticky = {"Name": "Sticky Notes", "AppID": "Microsoft.MicrosoftStickyNotes_8wekyb3d8bbwe!App", "Exe": "", "Args": ""}
    sessions = load_sessions(Catalog(apps_from_rows([*ROWS, sticky]), parse_aliases(DEFAULT_ALIASES)))
    first = sessions[0]
    assert first.name == "recording" and first.opener == "tap" and first.screen.front == "Claude"
    assert [ln.say for ln in first.lines] == [
        "Alright, can you open up the notes app for me", "and once you're there can you create a new note",
        "and inside this new note let's make the title say hello", "Great, okay.",
        "Let's move on and can you open up the browser", "google search Norbert Wiener", "now open x.com", "Nice.",
        "Cool, thank you. That's all."]
    assert [ln.want for ln in first.lines][-1] == ("listening end_session",)
    assert sum(not ln.want for ln in first.lines) == 2
    assert len(sessions) >= 7
    said = [ln.say.lower() for s in sessions for ln in s.lines]
    for must in ("wait", "never mind", "hold on", "great job"):
        assert must in said
    assert any(s.opener == "name" for s in sessions)
    assert any(ln.fail for s in sessions for ln in s.lines)
    assert any("and then goodbye" in text for text in said)


def test_a_perfect_fake_router_scores_100():
    sess = session(line("open notepad and then create a new note", "open Notepad", "press ctrl+n"),
                   line("great, okay"), line("type hello", "type hello"), line("That's all.", "listening end_session"))
    results, router = run(sess)
    summary = summarize(results)
    assert (summary.right, summary.total, summary.quiet_actions) == (5, 5, 0)
    assert [r.right for r in results] == [2, 1, 1, 1]
    # The second clause saw the first one's result: one router holds the session's context.
    assert router.seen[1][0] == "create a new note" and router.seen[1][2][0].startswith("open notepad:")


def test_small_talk_lines_must_cause_no_command():
    acts_on_anything = TableRouter(table={}, default=_NOTEPAD)
    sess = session(line("great, okay"), line("my brother said we should close the garage"), line("hmm"))
    results, _ = run(sess, acts_on_anything)
    assert results[0].right == 1 and not results[0].got  # acknowledgements never reach the brain
    assert results[1].right == 0 and results[1].quiet_actions == 1
    assert summarize(results).quiet_actions == 1
    assert "small talk" in " ".join(results[0].notes)


def test_wrong_or_extra_commands_count_as_misses():
    sess = session(line("open notepad and then open calculator", "open Notepad"), line("open notepad", "open Calculator"))
    results, _ = run(sess)
    assert (results[0].right, results[0].units) == (1, 2)  # the extra command is a miss
    assert (results[1].right, results[1].units) == (0, 1)
    assert summarize(results).misses == 2


def test_a_failed_step_skips_what_waits_behind_it():
    sess = session(line("open notepad and then create a new note", "open Notepad", fail=True),
                   line("open calculator", "open Calculator"))
    results, _ = run(sess)
    assert [g.text for g in results[0].got] == ["open notepad"]
    assert any(note.startswith("skipped") and "create a new note" in note for note in results[0].notes)
    assert results[0].right == results[0].units == 1 and results[1].right == 1


def test_the_screen_follows_wanted_opens():
    screen = Screen("Google Chrome", ("Google Chrome", "Claude"))
    wants = line("x", "open Notepad", "open x.com", "close Claude").wants
    moved = screen
    for want in wants:
        moved = advance(moved, want)
    assert moved.front == "Google Chrome" and moved.tab == "x.com" and moved.open_apps == ("Google Chrome", "Notepad")
    # The router missed the open, but the next line still sees Notepad in front (one miss doesn't cascade).
    router = TableRouter(table={"snap it left": _TABLE["snap it left"]})
    results, router = run(session(line("open notepad", "open Notepad"), line("snap it left", "window snap_left Notepad")),
                          router)
    assert router.seen[1][:2] == ("snap it left", "Notepad")
    assert results[0].right == 0 and results[1].right == 1


def test_the_name_opens_a_session_and_its_first_command_is_remembered():
    sess = session(line("Nova, open notepad", "open Notepad"), line("type hello", "type hello"), opener="name")
    results, router = run(sess)
    assert [r.right for r in results] == [1, 1]
    assert router.seen[1][2] and router.seen[1][2][0].startswith("open notepad:")


def test_queue_wait_is_reported_apart_from_start_latency():
    records = [{"tier": "0", "start_ms": 400.0, "queue_ms": None, "text": "open notepad"},
               {"tier": "0", "start_ms": 900.0, "queue_ms": 500.0, "text": "create a new note"},
               {"tier": "1", "start_ms": 1300.0, "queue_ms": None, "text": "type hello", "prompt_tokens": 3120},
               {"tier": "0", "start_ms": None, "queue_ms": None, "text": "not run"}]
    t = timings(records)
    assert t.start_p50 == {"0": 400.0, "1": 1300.0}  # nothing queued ahead only
    assert t.queue_p50 == 500.0 and t.queue_p95 == 500.0
    assert [row.after_turn_ms for row in t.rows] == [400.0, 400.0, 1300.0, None]
    assert t.prompt_tokens == (3120,)


def test_finals_are_aligned_to_lines():
    lines = ["open notepad", "great okay", "google search norbert wiener"]
    seg = align(lines, ["Open Notepad. Great, okay.", "Google search Norbert", "Wiener."])
    assert seg.merged == ((0, (0, 1)),)  # final 0 holds lines 0 and 1
    assert seg.cut == ((2, (1, 2)),)  # line 2 came out as finals 1 and 2
    assert align(lines, ["open notepad", "great okay", "google search norbert wiener"]) == replace(seg, merged=(), cut=())


def test_ssml_puts_breaks_between_lines():
    text = tts.ssml(["Open <notepad> & go", "Nice."], pause_ms=900)
    assert text.startswith("<speak") and text.rstrip().endswith("</speak>")
    assert "Open &lt;notepad&gt; &amp; go" in text
    assert text.count('<break time="900ms"/>') == 2
    assert text.index("Open") < text.index('<break time="900ms"/>') < text.index("Nice.")
