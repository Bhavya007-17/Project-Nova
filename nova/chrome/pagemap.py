"""Page maps (SPEC §9): which element on the page does he mean? Pure: the extension's element list + his words in,
a ranking and a decision out ("click this one", "ask which of these", "nothing like that here").

Scores (0-100) mix: his content words found in the element's name (exact, plural, prefix "thermo" ->
"Thermodynamics", near spellings), the same words in its section / row (weaker), how much of the name his words
cover, the whole phrase ("signin" ~ "Sign in"), role words ("button", "link", "box"), dates ("for today", "due
friday" against the row's "Due Nov 4"), ordinals ("the second reply" counts matches in page order), on screen or
not, disabled. ``points_at_page`` decides whether an utterance needs the page map at all (SPEC §7.1).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from rapidfuzz import fuzz

from ..brain.risk import risky_label

ONE_AT = 70.0  # click without asking from here up...
MARGIN = 8.0  # ...when nothing different scores within this of the best
ASK_AT = 45.0  # below this: not a match
PHRASE_AT = 85.0  # a whole-phrase spelling match counts from here ("signin" / "Sign in" = 92)
MAX_OPTIONS = 4

_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset({
    "the", "a", "an", "on", "in", "at", "to", "of", "for", "my", "this", "that", "these", "those", "one", "ones",
    "please", "click", "tap", "press", "hit", "push", "select", "choose", "open", "go", "it", "me", "and", "with",
    "i", "can", "you", "just", "there", "here", "page", "from", "under", "below", "above", "near", "by", "is",
    "thing", "called", "says", "named", "labeled", "labelled", "which", "where", "what", "find", "show", "get",
    "form", "section", "part", "area", "up", "out", "down", "into", "onto", "do", "due", "its", "your", "our",
    "fill", "type", "enter", "put", "write", "scroll", "jump", "take", "bring", "head", "then", "now",
})
_PARTICLES = frozenset({"in", "up", "out", "on", "down", "to", "back", "next", "all", "more", "new"})  # "sign in" ≠ "sign up"
# What the map can't see (colours, sizes, places on screen): left out unless an element is named that ("Back to top")
_UNSEEN = frozenset({
    "blue", "red", "green", "yellow", "orange", "purple", "pink", "black", "white", "gray", "grey", "big", "small",
    "large", "little", "tiny", "huge", "bottom", "top", "left", "right", "corner", "upper", "lower", "middle", "side",
    "bold", "highlighted", "shiny",
})
_GENERIC = frozenset({"assignment", "assignments", "homework", "hw", "item", "stuff", "work", "task", "todo",
                      "thing", "entry"})
_SYNONYMS = {"homework": ("hw", "assignment"), "hw": ("homework",), "assignment": ("hw", "homework"),
             "login": ("log", "sign"), "signin": ("sign",), "logout": ("log", "sign"), "settings": ("preferences",),
             "x": ("close",), "close": ("x", "dismiss")}
_ROLE_WORDS = {
    "button": frozenset({"button"}), "buttons": frozenset({"button"}),
    "link": frozenset({"link"}), "links": frozenset({"link"}),
    "tab": frozenset({"tab"}),
    "checkbox": frozenset({"checkbox", "switch"}), "toggle": frozenset({"checkbox", "switch", "button"}),
    "switch": frozenset({"switch", "checkbox"}),
    "box": frozenset({"textbox", "searchbox", "combobox", "checkbox"}), "field": frozenset({"textbox", "searchbox", "combobox"}),
    "input": frozenset({"textbox", "searchbox", "combobox"}), "bar": frozenset({"searchbox", "textbox"}),
    "dropdown": frozenset({"combobox", "button"}), "menu": frozenset({"menuitem", "button", "combobox"}),
    "option": frozenset({"option", "radio", "menuitem"}), "radio": frozenset({"radio"}),
    "heading": frozenset({"heading"}), "title": frozenset({"heading"}), "icon": frozenset(), "image": frozenset(),
}
_SECTION_ROLES = frozenset({"heading", "region", "section", "navigation", "complementary", "main", "article", "form"})
_ORDINALS = {"first": 0, "1st": 0, "second": 1, "2nd": 1, "third": 2, "3rd": 2, "fourth": 3, "4th": 3, "fifth": 4,
             "5th": 4, "sixth": 5, "seventh": 6, "eighth": 7, "ninth": 8, "tenth": 9, "last": -1}
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_ANY_DATE = re.compile(r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.? \d{1,2}\b|\b\d{1,2}/\d{1,2}\b"
                       r"|\b(?:today|tomorrow|yesterday)\b", re.IGNORECASE)


# --- what the extension sends ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Element:
    id: str  # "<frameId>:<n>" (valid until the page changes)
    role: str
    name: str
    section: str = ""
    row: str = ""
    href: str = ""
    in_view: bool = True
    disabled: bool = False
    editable: bool = False
    secret: bool = False  # a password / card field: never typed into
    search: bool = False
    submits: bool = False  # a form's submit button
    checked: bool | None = None
    tag: str = ""
    value: str = ""
    box: tuple[int, int, int, int] = (0, 0, 0, 0)

    @classmethod
    def of(cls, data: Any) -> Element | None:
        if not isinstance(data, Mapping) or not isinstance(data.get("id"), str) or not isinstance(data.get("role"), str):
            return None
        box = data.get("box")
        checked = data.get("checked")
        return cls(
            id=data["id"], role=data["role"], name=str(data.get("name") or ""), section=str(data.get("section") or ""),
            row=str(data.get("row") or ""), href=str(data.get("href") or ""), in_view=bool(data.get("inView", True)),
            disabled=bool(data.get("disabled")), editable=bool(data.get("editable")), secret=bool(data.get("secret")),
            search=bool(data.get("search")), submits=bool(data.get("submits")),
            checked=checked if isinstance(checked, bool) else None, tag=str(data.get("tag") or ""),
            value=str(data.get("value") or ""),
            box=tuple(int(v) for v in box[:4]) if isinstance(box, list) and len(box) >= 4 else (0, 0, 0, 0),
        )


@dataclass(frozen=True)
class PageView:
    url: str
    title: str
    elements: tuple[Element, ...]
    tab: int | None = None
    ms: float = 0.0  # Nova -> extension -> every frame -> back
    total: int = 0  # actionable elements on the page before the cap
    viewport: tuple[int, int] | None = None  # the window's width and height (CSS pixels)

    @classmethod
    def of(cls, data: Mapping[str, Any], ms: float = 0.0) -> PageView:
        elements = tuple(e for e in (Element.of(raw) for raw in data.get("elements") or ()) if e is not None)
        tab = data.get("tab")
        tab_id = tab.get("id") if isinstance(tab, Mapping) and isinstance(tab.get("id"), int) else None
        size = data.get("viewport")
        viewport = (int(size[0]), int(size[1])) if isinstance(size, list) and len(size) == 2 else None
        return cls(str(data.get("url") or ""), str(data.get("title") or ""), elements, tab_id, ms,
                   int(data.get("total") or len(elements)), viewport)

    def find(self, element_id: str) -> Element | None:
        return next((e for e in self.elements if e.id == element_id), None)


# --- his words --------------------------------------------------------------------------------------------------------


def words(text: str) -> list[str]:
    return _WORD.findall(text.lower().replace("'", "").replace("’", ""))


@dataclass(frozen=True)
class Query:
    words: tuple[str, ...]  # content words, in order, no repeats
    roles: frozenset[str]
    ordinal: int | None
    days: tuple[date, ...]
    phrase: str
    places: frozenset[str] = frozenset()  # top | bottom | left | right: where on screen he says it is


_PLACES = {"top": "top", "upper": "top", "bottom": "bottom", "lower": "bottom", "left": "left", "right": "right"}


def _days(said: Sequence[str], now: datetime) -> tuple[date, ...]:
    today = now.date()
    out: list[date] = []
    for i, w in enumerate(said):
        if w in ("today", "todays", "tonight", "tonights"):
            out.append(today)
        elif w in ("tomorrow", "tomorrows"):
            out.append(today + timedelta(days=1))
        elif w in _WEEKDAYS or w.rstrip("s") in _WEEKDAYS:
            target = _WEEKDAYS.index(w if w in _WEEKDAYS else w.rstrip("s"))
            ahead = (target - today.weekday()) % 7
            if i > 0 and said[i - 1] == "next" and ahead == 0:
                ahead = 7
            out.append(today + timedelta(days=ahead))
    return tuple(dict.fromkeys(out))


def parse(description: str, vocabulary: frozenset[str], now: datetime | None = None) -> Query:
    said = words(description)
    roles: set[str] = set()
    ordinal = None
    content: list[str] = []
    places = frozenset(_PLACES[w] for w in said if w in _PLACES and w not in vocabulary)
    days = _days(said, now or datetime.now())
    date_words = {"today", "todays", "tonight", "tonights", "tomorrow", "tomorrows", *_WEEKDAYS,
                  *(d + "s" for d in _WEEKDAYS)}
    for w in said:
        if w in _ORDINALS and ordinal is None:
            ordinal = _ORDINALS[w]
            continue
        if w in _ROLE_WORDS:
            roles |= _ROLE_WORDS[w]
            continue
        if w in date_words and days:
            continue
        if w in _STOP and not (w in _PARTICLES and w in vocabulary):
            continue
        if w in _UNSEEN and w not in vocabulary:
            continue
        if w not in content:
            content.append(w)
    return Query(tuple(content), frozenset(roles), ordinal, days, " ".join(content), places)


def _date_marks(day: date) -> re.Pattern[str]:
    month = _MONTHS[day.month - 1]
    return re.compile(
        rf"\b{month}[a-z]*\.? 0?{day.day}(?!\d)|\b0?{day.month}/0?{day.day}(?!\d)|\b{_WEEKDAYS[day.weekday()][:3]}[a-z]*\b",
        re.IGNORECASE,
    )


def _date_hit(text: str, days: Sequence[date], now: datetime) -> bool:
    low = text.lower()
    for day in days:
        if day == now.date() and re.search(r"\btoday\b", low):
            return True
        if day == now.date() + timedelta(days=1) and re.search(r"\btomorrow\b", low):
            return True
        if _date_marks(day).search(low):
            return True
    return False


def _match(word: str, candidates: Sequence[str]) -> float:
    """How well one of his words matches the best of some element words: 1 exact, .9 plural / prefix / synonym,
    .8 a near spelling."""
    best = 0.0
    for c in candidates:
        if c == word:
            return 1.0
        if (word.rstrip("s") == c.rstrip("s")) or (len(word) >= 4 and c.startswith(word)) or c in _SYNONYMS.get(word, ()):
            best = max(best, 0.9)
        elif len(word) >= 4 and len(c) >= 4 and not word.isdigit() and fuzz.ratio(word, c) >= 80:
            best = max(best, 0.8)
    return best


# --- ranking ------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Scored:
    element: Element
    score: float
    index: int  # page order
    why: str = ""  # "ordinal" when picked by "the second ..."


def _name_words(name: str, query: Query) -> list[str]:
    return [w for w in words(name) if w not in _STOP and w not in _ROLE_WORDS] or words(name)


def rank(elements: Sequence[Element], description: str, *, now: datetime | None = None, kind: str = "click",
         viewport: tuple[int, int] | None = None) -> list[Scored]:
    """Elements that could be what he means, best first. ``kind``: click | fill (fields only, never secret ones) |
    section (headings and sections preferred: scroll_to). ``viewport`` (w, h): "up top" / "at the bottom" / "on the
    left" prefer what is there on screen."""
    now = now or datetime.now()
    vocabulary = frozenset(w for e in elements for w in words(e.name))
    query = parse(description, vocabulary, now)
    out: list[Scored] = []
    for index, e in enumerate(elements):
        if kind == "fill" and (not e.editable or e.secret):
            continue
        score = _score(e, query, now, kind)
        if score > 0 and query.places and viewport:
            score += _placed(e, query.places, viewport)
        if score > 0:
            out.append(Scored(e, score, index))  # not capped: a role or date bonus on top of a full match still counts
    out.sort(key=lambda s: (-s.score, s.index))
    if query.ordinal is not None and out:
        pool = [s for s in out if s.score >= out[0].score - 15]
        pool.sort(key=lambda s: s.index)
        if pool and (query.ordinal == -1 or query.ordinal < len(pool)):
            chosen = pool[query.ordinal]
            out = [Scored(chosen.element, max(90.0, chosen.score), chosen.index, "ordinal"),
                   *(s for s in out if s is not chosen)]
    return out


def _score(e: Element, query: Query, now: datetime, kind: str) -> float:
    name_words = words(e.name)
    meaningful = _name_words(e.name, query)
    context = words(f"{e.section} {e.row} {_href_words(e.href)}")
    total = matched_name = matched_context = 0.0
    used: set[str] = set()
    for w in query.words:
        weight = 0.35 if w in _GENERIC else 1.0
        total += weight
        m = _match(w, name_words)
        if m:
            matched_name += weight * m
            used.update(c for c in name_words if _match(w, (c,)))
        else:
            matched_context += weight * 0.6 * _match(w, context)
    score = 0.0
    if total:
        coverage = (matched_name + matched_context) / total
        precision = len(used & set(meaningful)) / len(meaningful) if meaningful else 0.0
        score = 100.0 * coverage * (0.7 + 0.3 * precision)
        if query.phrase and name_words:  # "signin" ~ "Sign in": only a near-identical whole phrase counts
            phrase = fuzz.ratio(query.phrase.replace(" ", ""), "".join(name_words))
            if phrase >= PHRASE_AT:
                score = max(score, 0.95 * phrase)
    if query.days:
        if _date_hit(f"{e.name} {e.row} {e.section}", query.days, now):
            score = max(score, 55.0) + 30.0
        else:
            score -= 35.0
    if score <= 0 and not query.words and not query.days and (query.roles or query.ordinal is not None):
        score = 50.0  # "the first link": any link, by position
    if score <= 0:
        return 0.0
    if query.roles:
        score += 8.0 if e.role in query.roles else -8.0
    if kind == "section":
        score += 10.0 if e.role in _SECTION_ROLES else (-10.0 if e.editable else 0.0)
    if kind == "click" and e.secret:
        score -= 10.0
    if not e.in_view:
        score -= 4.0
    if e.disabled:
        score -= 50.0
    return score


def _placed(e: Element, places: frozenset[str], viewport: tuple[int, int]) -> float:
    """+10 per place he named that the element is in (thirds of the window), -10 when it's in the opposite one."""
    width, height = viewport
    if not e.in_view or width <= 0 or height <= 0:
        return 0.0
    x, y, w, h = e.box
    cx, cy = (x + w / 2) / width, (y + h / 2) / height
    where = {"top": cy < 1 / 3, "bottom": cy > 2 / 3, "left": cx < 1 / 3, "right": cx > 2 / 3}
    opposite = {"top": "bottom", "bottom": "top", "left": "right", "right": "left"}
    return sum(10.0 if where[p] else (-10.0 if where[opposite[p]] else 0.0) for p in places)


def _href_words(href: str) -> str:
    if not href:
        return ""
    path = href.split("//", 1)[-1].split("/", 1)[-1].split("?", 1)[0]
    return " ".join(w for w in re.split(r"[/_\-.#]+", path) if w and not w.isdigit())


# --- the decision ---------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Decision:
    kind: str  # one | ask | none
    best: Element | None = None
    options: tuple[Element, ...] = ()


def _same(a: Element, b: Element) -> bool:
    return " ".join(words(a.name)) == " ".join(words(b.name))


def decide(scored: Sequence[Scored]) -> Decision:
    if not scored or scored[0].score < ASK_AT:
        return Decision("none")
    best = scored[0]
    if best.why == "ordinal":
        return Decision("one", best.element, (best.element,))
    near = [s for s in scored[1:] if s.score >= best.score - MARGIN]
    if best.score >= ONE_AT:
        if not near:
            return Decision("one", best.element, (best.element,))
        if all(_same(s.element, best.element) for s in near) and not risky_label(best.element.name):
            return Decision("one", best.element, (best.element,))  # twins ("Sign in" twice): the first on screen
        options = [best.element, *(s.element for s in near)]
        return Decision("ask", best.element, tuple(options[:MAX_OPTIONS]))
    options = tuple(s.element for s in scored if s.score >= ASK_AT)[:MAX_OPTIONS]
    return Decision("ask", best.element, options)


def label(e: Element, limit: int = 30) -> str:
    """A choice in the island: the name, and where it is when that tells twins apart."""
    name = " ".join(e.name.split()) or e.role
    where = " ".join((e.row or e.section).split())
    text = f"{name} · {where}" if where and where.lower() != name.lower() else name
    return text if len(text) <= limit else text[: limit - 1] + "…"


LLM_CHARS = 1800  # all page lines together (~500 tokens): Tier 1's 4,096-token window also holds a ~3,000-token prompt


def _short(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def llm_rows(scored: Sequence[Scored], limit: int = 40, chars: int = LLM_CHARS) -> list[str]:
    """The top elements for Tier 1, one compact line each, numbered from 1 (the model answers with the number):
    "3 button: Sign in [header]", "7 link: HW 5 [To Do] Due Nov 4 at 11:59pm". Stops at ``chars`` in all."""
    rows: list[str] = []
    used = 0
    for i, s in enumerate(scored[:limit]):
        e = s.element
        line = f"{i + 1} {e.role}: {_short(e.name, 50) or '(no name)'}"
        if e.section:
            line += f" [{_short(e.section, 30)}]"
        if e.row:
            line += f" {_short(e.row, 40)}"
        if used + len(line) > chars and rows:
            break
        rows.append(line)
        used += len(line)
    return rows


def top_for_llm(elements: Sequence[Element], said: str, *, now: datetime | None = None, limit: int = 40,
                viewport: tuple[int, int] | None = None) -> list[Scored]:
    """What Tier 1 sees: the best matches for what he said, then whatever is on screen (so "the blue one" still has
    candidates), at most ``limit``."""
    ranked = rank(elements, said, now=now, viewport=viewport)
    seen = {s.element.id for s in ranked}
    rest = [Scored(e, 0.0, i) for i, e in enumerate(elements) if e.id not in seen and e.in_view and not e.disabled]
    return [*ranked, *rest][:limit]


# --- does the utterance point at the page? ----------------------------------------------------------------------------

_NOUNS = r"(?:button|buttons|link|links|checkbox|check ?box|dropdown|drop ?down|text ?box|search ?box|search bar|field|input|toggle|switch|radio button|icon|result|results|option|options|menu item|section|heading|comments?|reviews?|article|post|item|row|card|thumbnail|video)"
_POINTERS = [
    re.compile(r"\b(?:click|double click|right click|tap)\b"),
    re.compile(r"\b(?:press|hit|push|smash)\b(?: on)? (?:the|that|this)\b.*\b(?:" + _NOUNS + r"|thing|one)\b"),
    re.compile(r"\b(?:check|uncheck|tick|untick|toggle|enable|disable)\b.*\b(?:box|checkbox|toggle|switch|option)\b"),
    re.compile(r"\b(?:select|choose|pick)\b (?:the|that|this|an?)\b"),
    re.compile(r"\b(?:open|go to|jump to|take me to|show me|bring up|pull up|follow|play)\b (?:the|that|this) (?:\w+ ){0,3}" + _NOUNS + r"\b"),
    re.compile(r"\b(?:open|go to|follow|play) the (?:first|second|third|fourth|fifth|last|next|top|\d+(?:st|nd|rd|th))\b"),
    re.compile(r"\bfill (?:in|out)\b|\bfill\b.*\b(?:field|box|form)\b"),
    re.compile(r"\b(?:type|enter|put|write)\b.*\b(?:in|into|in to)\b (?:the|that|this) (?:\w+ ){0,3}(?:box|field|bar|input|form)\b"),
    re.compile(r"\b(?:scroll|jump|skip|go|head|take me|bring me) (?:down |up |back )?to the (?!top\b|bottom\b|end\b|start\b|beginning\b)\w+"),
    re.compile(r"\b(?:on|in|of) (?:this|the) (?:page|site|website|form)\b|\bthis page\b"),
]


def points_at_page(text: str) -> bool:
    """Does he point at something on the page (so the page map is worth fetching)? SPEC §7.1."""
    low = " ".join(words(text))
    if re.fullmatch(r"(?:go |scroll )?(?:back|forward|up|down)(?: \w+)?", low) or "select all" in low:
        return False
    return any(p.search(low) for p in _POINTERS)


_SECTION_ASK = re.compile(
    r"\bscroll(?: down| up| back)? to\b|\b(?:go|jump|skip|head|take me|bring me)(?: down| up| back)? to the \w+(?: \w+)? "
    r"(?:section|part|heading|area)\b|\b(?:section|heading)\b")


def wants_sections(text: str) -> bool:
    """"scroll to the comments", "go to the reviews section": a part of the page, not a control."""
    return bool(_SECTION_ASK.search(" ".join(words(text))))


def names(elements: Iterable[Element]) -> frozenset[str]:
    return frozenset(w for e in elements for w in words(e.name))
