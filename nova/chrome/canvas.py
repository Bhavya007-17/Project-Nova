"""Canvas adapter (SPEC §10), the pure part: Canvas's own planner JSON (read from inside his logged-in tab, no API
token) -> items; date ranges ("today", "this week", "next week", "overdue"); which item he means ("my assignment for
today", "the thermo homework", "HW 5", "my next quiz"); short island text. A DOM fallback reads the To Do / planner
rows of the page when the JSON can't be had. ``nova.actions.chrome`` does the fetching.
"""

from __future__ import annotations

import html.parser
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from urllib.parse import urlencode, urlsplit

RANGES = ("today", "tomorrow", "week", "next_week", "weekend", "upcoming", "overdue")
_KINDS = {"assignment": "assignment", "sub_assignment": "assignment", "quiz": "quiz", "discussion_topic": "discussion",
          "wiki_page": "page", "assessment_request": "peer review"}  # planner types that are coursework
_GUARD = "while(1);"
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset({
    "my", "the", "a", "an", "for", "due", "on", "in", "at", "of", "to", "is", "that", "this", "click", "open", "show",
    "me", "pull", "up", "bring", "go", "get", "one", "ones", "thing", "things", "i", "have", "do", "what", "whats",
    "which", "please", "canvas", "class", "assignment", "assignments", "homework", "hw", "work", "task", "tasks",
    "todo", "todos", "item", "items", "stuff", "it", "are", "there", "any", "from", "with", "and", "on", "page",
    "up", "can", "you", "pull", "take", "tap", "clicks", "start", "starting", "find", "does", "say", "says", "about",
    "instructions", "description", "asking", "ask", "asks", "read", "tell", "require", "requires", "want", "wants",
    "anything", "something", "everything", "nothing", "got", "need", "left", "list", "whats", "check", "planner",
    "deadline", "deadlines", "classes", "courses", "course", "due", "else", "still", "yet", "else",
})
_NUMBERED = frozenset({"hw", "homework", "lab", "quiz", "exam", "project", "problem", "set", "pset", "chapter", "unit",
                       "module", "week", "assignment", "test", "essay", "paper", "reading"})
_HARD_KINDS = {"quiz": "quiz", "quizzes": "quiz", "discussion": "discussion", "discussions": "discussion",
               "forum": "discussion"}


def words(text: str) -> list[str]:
    return _WORD.findall(text.lower().replace("'", "").replace("’", ""))


@dataclass(frozen=True)
class Item:
    title: str
    course: str
    due: datetime  # aware
    url: str
    kind: str  # assignment | quiz | discussion | page | peer review
    done: bool = False  # submitted / excused / marked complete
    missing: bool = False
    item_id: str = ""
    course_id: str = ""


# --- reading Canvas's JSON ---------------------------------------------------------------------------------------------


def strip_guard(text: str) -> str:
    """Canvas starts JSON answers with ``while(1);`` (against JSON hijacking)."""
    return text[len(_GUARD):] if text.startswith(_GUARD) else text


def _when(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo is not None else None


def _absolute(url: str, origin: str) -> str:
    if url.startswith(("http://", "https://")):
        return url
    return origin.rstrip("/") + "/" + url.lstrip("/")


def parse_planner(rows: Any, origin: str) -> tuple[Item, ...]:
    """/api/v1/planner/items rows -> coursework items (announcements, events and notes left out), by due date."""
    items = []
    for row in rows if isinstance(rows, list) else ():
        if not isinstance(row, Mapping) or row.get("plannable_type") not in _KINDS:
            continue
        thing = row.get("plannable") if isinstance(row.get("plannable"), Mapping) else {}
        due = _when(thing.get("due_at")) or _when(thing.get("todo_date")) or _when(row.get("plannable_date"))
        title = " ".join(str(thing.get("title") or thing.get("name") or "").split())
        if due is None or not title:
            continue
        subs = row.get("submissions") if isinstance(row.get("submissions"), Mapping) else {}
        override = row.get("planner_override") if isinstance(row.get("planner_override"), Mapping) else {}
        done = bool(subs.get("submitted") or subs.get("excused") or override.get("marked_complete"))
        items.append(Item(
            title=title, course=" ".join(str(row.get("context_name") or "").split()), due=due,
            url=_absolute(str(row.get("html_url") or ""), origin), kind=_KINDS[row["plannable_type"]], done=done,
            missing=bool(subs.get("missing")), item_id=str(row.get("plannable_id") or thing.get("id") or ""),
            course_id=str(row.get("course_id") or ""),
        ))
    return tuple(sorted(items, key=lambda i: i.due))


def planner_path(start: datetime, end: datetime) -> str:
    return "/api/v1/planner/items?" + urlencode({"start_date": start.isoformat(), "end_date": end.isoformat(),
                                                  "per_page": 100})


def next_page(link: str, origin: str) -> str | None:
    """The rel="next" page of a Link header, as a path on the same Canvas (anything else is ignored)."""
    for part in link.split(","):
        url, _, rest = part.partition(";")
        if 'rel="next"' in rest:
            url = url.strip().strip("<>")
            if url.startswith(origin.rstrip("/") + "/"):
                parts = urlsplit(url)
                return parts.path + (f"?{parts.query}" if parts.query else "")
    return None


def item_api(item: Item) -> str | None:
    """Where Canvas keeps an item's own JSON (its description)."""
    if not item.course_id or not item.item_id:
        return None
    folder = {"assignment": "assignments", "quiz": "quizzes", "discussion": "discussion_topics"}.get(item.kind)
    return f"/api/v1/courses/{item.course_id}/{folder}/{item.item_id}" if folder else None


class _Text(html.parser.HTMLParser):
    _BLOCKS = frozenset({"p", "div", "li", "br", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "ul", "ol", "table",
                         "section", "article", "blockquote", "pre", "hr"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in self._BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag in self._BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_text(markup: str, limit: int = 8000) -> str:
    parser = _Text()
    parser.feed(markup or "")
    parser.close()
    lines = (" ".join(line.replace("\xa0", " ").split()) for line in "".join(parser.parts).split("\n"))
    return "\n".join(line for line in lines if line)[:limit]


def description(data: Any) -> str:
    """An assignment's / quiz's description or a discussion's message, as plain text."""
    if not isinstance(data, Mapping):
        return ""
    return html_text(str(data.get("description") or data.get("message") or ""))


# --- dates ----------------------------------------------------------------------------------------------------------------


def _midnight(day: date, tz: Any) -> datetime:
    return datetime.combine(day, time(0, 0), tzinfo=tz)


def window(name: str, now: datetime) -> tuple[datetime, datetime]:
    """[start, end) for a range name, in his time zone."""
    tz = now.tzinfo
    today = now.date()
    monday = today + timedelta(days=7 - today.weekday())  # the next Monday
    match name:
        case "today":
            return _midnight(today, tz), _midnight(today + timedelta(days=1), tz)
        case "tomorrow":
            return _midnight(today + timedelta(days=1), tz), _midnight(today + timedelta(days=2), tz)
        case "week":
            end = monday if (monday - today).days > 2 else monday + timedelta(days=7)  # on a weekend: the coming week
            return _midnight(today, tz), _midnight(end, tz)
        case "next_week":
            return _midnight(monday, tz), _midnight(monday + timedelta(days=7), tz)
        case "weekend":
            saturday = monday - timedelta(days=2)
            return _midnight(max(saturday, today), tz), _midnight(monday, tz)
        case "upcoming":
            return now, now + timedelta(days=7)
        case "overdue":
            return now - timedelta(days=21), now
    if name in _WEEKDAYS:  # "due friday": the next one (today if it's Friday)
        day = today + timedelta(days=(_WEEKDAYS.index(name) - today.weekday()) % 7)
        return _midnight(day, tz), _midnight(day + timedelta(days=1), tz)
    raise ValueError(f"unknown range {name!r}")


def fetch_window(name: str | None, now: datetime) -> tuple[datetime, datetime]:
    """What to ask Canvas for: the range, plus enough around it to name the next due item and recent overdue ones."""
    start = _midnight(now.date(), now.tzinfo) - timedelta(days=21)
    end = _midnight(now.date(), now.tzinfo) + timedelta(days=22)
    if name in RANGES or name in _WEEKDAYS:
        lo, hi = window(name, now)
        start, end = min(start, lo), max(end, hi + timedelta(days=14))
    return start, end


def _local(item: Item, now: datetime) -> datetime:
    return item.due.astimezone(now.tzinfo)


def _order(items: Iterable[Item]) -> list[Item]:
    """Outstanding first, each part by due date."""
    items = sorted(items, key=lambda i: i.due)
    return [i for i in items if not i.done] + [i for i in items if i.done]


def due(items: Sequence[Item], name: str, now: datetime) -> list[Item]:
    start, end = window(name, now)
    if name == "overdue":
        return [i for i in sorted(items, key=lambda i: i.due) if start <= i.due < end and not i.done]
    return _order(i for i in items if start <= i.due < end)


def next_due(items: Sequence[Item], after: datetime) -> Item | None:
    return next((i for i in sorted(items, key=lambda i: i.due) if i.due >= after and not i.done), None)


# --- which item he means ---------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Ref:
    range: str | None = None
    day: date | None = None
    next: bool = False
    kinds: frozenset[str] = frozenset()
    words: tuple[str, ...] = ()


def range_of(text: str) -> str | None:
    """The range a question names ("what's due this week" -> week), if any."""
    return parse_ref(text, datetime.now().astimezone()).range


def parse_ref(text: str, now: datetime) -> Ref:
    said = words(text)
    rng: str | None = None
    day: date | None = None
    nxt = False
    kinds: set[str] = set()
    content: list[str] = []
    i = 0
    while i < len(said):
        w = said[i]
        after = said[i + 1] if i + 1 < len(said) else ""
        before = said[i - 1] if i else ""
        if w in ("today", "todays", "tonight", "tonights"):
            rng = "today"
        elif w in ("tomorrow", "tomorrows"):
            rng = "tomorrow"
        elif w == "next" and after in ("week", "weeks"):
            rng = "next_week"
            i += 1
        elif w in ("week", "weeks") and before not in _NUMBERED:
            rng = rng or "week"
        elif w in ("weekend", "weekends"):
            rng = "weekend"
        elif w in ("overdue", "late", "missing", "past"):
            rng = "overdue"
        elif w in ("next", "upcoming", "soonest", "coming", "earliest"):
            nxt = True
            if w == "coming" and after == "up":
                i += 1
        elif w in _WEEKDAYS or (w.endswith("s") and w[:-1] in _WEEKDAYS):
            target = _WEEKDAYS.index(w if w in _WEEKDAYS else w[:-1])
            ahead = (target - now.weekday()) % 7
            if before == "next" and ahead == 0:
                ahead = 7
            day = now.date() + timedelta(days=ahead)
            nxt = False if before == "next" else nxt
        elif w in _NUMBERED and after.isdigit():
            content.append(w)
        elif w in _HARD_KINDS:
            kinds.add(_HARD_KINDS[w])
        elif w in _STOP:
            pass
        elif w not in content:
            content.append(w)
        i += 1
    if day is not None and rng in ("week", None):
        rng = None
    return Ref(rng, day, nxt and rng is None and day is None, frozenset(kinds), tuple(content))


_COURSE_CODE = re.compile(r"\b[A-Z]{3,5}\b")


def _matches(item: Item, said: Sequence[str]) -> bool:
    if not said:
        return True
    title = words(item.title)
    names = title + words(item.course)
    codes = {code.lower() for code in _COURSE_CODE.findall(item.course)}  # "PHYS 2306": "physics" says it too
    hits = 0
    for w in said:
        if w.isdigit():
            if w not in title:
                return False
            hits += 1
        elif any(t == w or (len(w) >= 3 and t.startswith(w)) or (w.rstrip("s") == t.rstrip("s")) for t in names) \
                or any(w.startswith(code) for code in codes):
            hits += 1
    return hits / len(said) >= 0.66


def only(items: Sequence[Item], said: Sequence[str]) -> list[Item]:
    """The items his other words name ("anything for physics due this week" -> the physics ones)."""
    return [i for i in items if _matches(i, said)]


def select(items: Sequence[Item], ref: Ref, now: datetime) -> list[Item]:
    """The items he could mean, best first (several = ask which; none = nothing like that)."""
    if ref.range:
        pool = due(items, ref.range, now)
    elif ref.day is not None:
        pool = [i for i in items if _local(i, now).date() == ref.day]
    elif ref.next:
        pool = [i for i in items if i.due >= now and not i.done]
    else:
        pool = [i for i in items if now - timedelta(days=21) <= i.due <= now + timedelta(days=14)]
    if ref.kinds:
        pool = [i for i in pool if i.kind in ref.kinds or any(k in i.title.lower() for k in ref.kinds)]
    pool = [i for i in pool if _matches(i, ref.words)]
    outstanding = [i for i in pool if not i.done]
    pool = sorted(outstanding or pool, key=lambda i: i.due)
    if ref.next:
        return pool[:1]
    if not ref.range and ref.day is None and ref.words and len(pool) > 1:
        first, second = pool[0], pool[1]
        if first.due >= now and second.due - first.due >= timedelta(days=2):
            return pool[:1]  # "the thermo homework": the one due next, well before the one after it
    return pool


# --- island text -----------------------------------------------------------------------------------------------------------


def _clock(when: datetime) -> str:
    hour = when.hour % 12 or 12
    minutes = f":{when.minute:02d}" if when.minute else ""
    return f"{hour}{minutes} {'AM' if when.hour < 12 else 'PM'}"


def day_word(item: Item, now: datetime) -> str:
    local = _local(item, now)
    days = (local.date() - now.date()).days
    if days == 0:
        return "today"
    if days == 1:
        return "tomorrow"
    if 1 < days <= 5:  # a weekday name only while it can't mean last week's or next week's
        return local.strftime("%a")
    return f"{local.strftime('%b')} {local.day}"


def when(item: Item, now: datetime) -> str:
    """ "today 11:59 PM", "tomorrow 5 PM", "Fri 5 PM", "Nov 10"."""
    local = _local(item, now)
    word = day_word(item, now)
    days = (local.date() - now.date()).days
    return f"{word} {_clock(local)}" if 0 <= days <= 5 else word


_TERM = re.compile(r"\((?:[^)]*)\)|\b(?:fall|spring|summer|winter)\s*\d{4}\b|\b\d{4}\s*(?:fa|sp|su|wi|f|s)\b|\b\d{6}\b",
                   re.IGNORECASE)
_CODE = re.compile(r"\b([A-Z]{2,5})[\s\-_]*(\d{4}[A-Z]?)\b")


def course_short(course: str) -> str:
    """ "ME 3124 Thermodynamics" -> "Thermodynamics"; a name that is only codes -> its course code."""
    text = _TERM.sub(" ", course)
    code = _CODE.search(text)
    rest = _CODE.sub(" ", text) if code else text
    rest = re.sub(r"^[\s\-_:|·,]+|[\s\-_:|·,]+$", "", " ".join(rest.split()))
    if sum(c.isalpha() for c in rest) >= 3:
        return rest
    if code:
        return f"{code.group(1)} {code.group(2)}"
    return " ".join(course.split())


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def label(item: Item, now: datetime) -> str:
    """A choice in the island (≤ 32 characters): "HW 5 · Thermodynamics · today"."""
    day = day_word(item, now)
    room = 32 - len(day) - 3
    title = _cut(("✓ " if item.done else "") + item.title, max(8, room - 9))
    course = course_short(item.course)
    if len(course) > 12 and " " in course:
        course = course.split()[0]  # "Foundations of Physics" -> "Foundations"
    left = f"{title} · {course}" if course and len(title) + 3 + 4 <= room else title
    return f"{_cut(left, room)} · {day}"


def range_words(name: str) -> str:
    if name in _WEEKDAYS:
        return f"on {name.capitalize()}"
    return {"today": "today", "tomorrow": "tomorrow", "week": "this week", "next_week": "next week",
            "weekend": "this weekend", "upcoming": "in the next 7 days", "overdue": "overdue"}.get(name, name)


def nothing_due(name: str, upcoming: Item | None, now: datetime) -> str:
    said = "Nothing overdue" if name == "overdue" else f"Nothing due {range_words(name)}"
    if upcoming is None:
        return f"{said}, and nothing coming up"
    course = course_short(upcoming.course)
    return f"{said} — next: {upcoming.title}{f' ({course})' if course else ''}, {when(upcoming, now)}"


def describe(item: Item, now: datetime) -> str:
    """One line: "HW 5 (Thermodynamics), today 11:59 PM"."""
    course = course_short(item.course)
    return f"{item.title}{f' ({course})' if course else ''}, {when(item, now)}"


# --- DOM fallback ------------------------------------------------------------------------------------------------------------

_DOM_DATE = re.compile(
    r"(?:(?P<rel>today|tomorrow)|(?P<mon>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(?P<day>\d{1,2})"
    r"(?:,?\s+(?P<year>\d{4}))?)(?:\s*(?:at|by|,)?\s*(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>am|pm))?",
    re.IGNORECASE,
)


def _dom_due(text: str, now: datetime) -> datetime | None:
    match = _DOM_DATE.search(text)
    if match is None:
        return None
    if match["rel"]:
        day = now.date() + timedelta(days=1 if match["rel"].lower() == "tomorrow" else 0)
    else:
        month = _MONTHS.index(match["mon"].lower()[:3]) + 1
        year = int(match["year"]) if match["year"] else now.year
        if not match["year"] and month < now.month - 6:
            year += 1
        try:
            day = date(year, month, int(match["day"]))
        except ValueError:
            return None
    hour, minute = 23, 59
    if match["h"]:
        hour = int(match["h"]) % 12 + (12 if match["ap"].lower() == "pm" else 0)
        minute = int(match["m"] or 0)
    return datetime.combine(day, time(hour, minute), tzinfo=now.tzinfo)


def dom_items(rows: Any, now: datetime) -> tuple[Item, ...]:
    """The page's own To Do / planner rows ({title, url, row}) -> items with the due date their row shows."""
    items = []
    for row in rows if isinstance(rows, list) else ():
        if not isinstance(row, Mapping):
            continue
        text = str(row.get("row") or "")
        due_at = _dom_due(re.sub(r"(?i)\bdue\b:?", " ", text), now)
        title = " ".join(str(row.get("title") or "").split())
        url = str(row.get("url") or "")
        if due_at is None or not title or not url:
            continue
        kind = "quiz" if "/quizzes/" in url else "discussion" if "/discussion_topics/" in url else "assignment"
        found = re.search(r"/courses/(\d+)/\w+/(\d+)", url)
        items.append(Item(title, "", due_at, url, kind, item_id=found.group(2) if found else "",
                          course_id=found.group(1) if found else ""))
    return tuple(sorted(items, key=lambda i: i.due))
