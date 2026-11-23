"""Finding things on screen (SPEC §8.1), the pure part: rank names, merge boxes from UI Automation / the page map /
OCR so one thing gets one tag, number them row by row, read spoken numbers, and the 3 × 3 grid.

All boxes are physical virtual-desktop pixels. Nothing here touches Windows.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace

MAX_TAGS = 99
MIN_LOGICAL_PX = 8
MAX_SCREEN_SHARE = 0.25
GRID_LEVELS = 3


@dataclass(frozen=True)
class Box:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    @property
    def area(self) -> int:
        return max(0, self.width) * max(0, self.height)

    @property
    def centre(self) -> tuple[int, int]:
        return (self.left + self.right) // 2, (self.top + self.bottom) // 2

    def contains(self, x: int, y: int) -> bool:
        return self.left <= x < self.right and self.top <= y < self.bottom

    def overlap(self, other: Box) -> float:
        """Intersection over the smaller box (1.0 = one sits inside the other)."""
        w = min(self.right, other.right) - max(self.left, other.left)
        h = min(self.bottom, other.bottom) - max(self.top, other.top)
        if w <= 0 or h <= 0:
            return 0.0
        smaller = min(self.area, other.area)
        return (w * h) / smaller if smaller else 0.0


@dataclass(frozen=True)
class Target:
    """Something he can point at by voice: a UIA element, a page element or an OCR line."""

    box: Box
    name: str = ""
    role: str = ""  # button | link | tab | menu | checkbox | field | item | text | ...
    source: str = "uia"  # uia | dom | ocr
    ref: object = None  # how to act on it: a UIA element index, a page-map id; None for OCR lines


@dataclass(frozen=True)
class Mark:
    number: int
    target: Target


# --- names ----------------------------------------------------------------------------------------------------------

_ROLE_WORDS = {
    "button": "button", "buttons": "button", "link": "link", "tab": "tab", "menu": "menu", "checkbox": "checkbox",
    "check box": "checkbox", "field": "field", "box": "field", "text box": "field", "search box": "field",
    "icon": "", "option": "item", "item": "item", "folder": "item", "file": "item",
}
_ROLE_SYNONYMS = {
    "button": ("button", "split button", "menu button"), "link": ("link", "hyperlink"), "tab": ("tab", "tab item"),
    "menu": ("menu", "menu item"), "checkbox": ("checkbox", "check box", "radio"),
    "field": ("field", "edit", "textbox", "combo", "searchbox", "input"), "item": ("item", "list item", "tree item"),
}
_APOSTROPHES = str.maketrans({"’": "", "'": "", "‘": "", "`": ""})


def normalize(text: str) -> str:
    """Lower case, no apostrophes ("Don't save" = "dont save"), punctuation to spaces, single spaces."""
    text = text.translate(_APOSTROPHES).lower().replace("&", " and ")
    return " ".join(re.sub(r"[^\w]+", " ", text).split())


def split_role(query: str) -> tuple[str, str]:
    """ "the save button" -> ("save", "button"); "OK" -> ("ok", "")."""
    text = normalize(query)
    text = re.sub(r"^(the|a|an|that|this|on|at)\s+", "", text)
    for word in sorted(_ROLE_WORDS, key=len, reverse=True):
        if text.endswith(" " + word):
            return text[: -len(word) - 1].strip(), _ROLE_WORDS[word]
    return text, ""


def role_matches(want: str, have: str) -> bool:
    if not want:
        return True
    have = have.lower()
    return any(word in have for word in _ROLE_SYNONYMS.get(want, (want,)))


def name_score(query: str, name: str) -> int:
    """4 exact, 3 starts with, 2 whole words, 1 contains, 0 no match (both sides normalized)."""
    q, n = normalize(query), normalize(name)
    if not q or not n:
        return 0
    if n == q:
        return 4
    if n.startswith(q + " ") or n.startswith(q):
        return 3 if n.startswith(q + " ") or len(q) >= 3 else 0
    if re.search(rf"(^|\s){re.escape(q)}(\s|$)", n):
        return 2
    return 1 if len(q) >= 3 and q in n else 0


def rank(query: str, targets: Sequence[Target], *, role: str = "", near: str = "") -> list[Target]:
    """The best-scoring targets for ``query`` (exact > starts-with > whole word > contains). A role narrows them when
    any match it; ``near`` keeps the one closest to the best match for that label. Empty = nothing matched."""
    name, said_role = split_role(query)
    role = role or said_role
    scored = [(name_score(name, t.name), t) for t in targets]
    best = max((score for score, _ in scored), default=0)
    if best == 0:
        return []
    found = [t for score, t in scored if score == best]
    typed = [t for t in found if role_matches(role, t.role)]
    found = typed or found
    found = _drop_nested(found)
    if near and len(found) > 1:
        anchors = rank(near, [t for t in targets if t not in found])
        if anchors:
            ax, ay = anchors[0].box.centre
            found = [min(found, key=lambda t: (t.box.centre[0] - ax) ** 2 + (t.box.centre[1] - ay) ** 2)]
    return found


BUTTON_WORDS = 4  # a bare button name ("don't save", "OK", "replace the file") is short


def button_named(said: str, targets: Sequence[Target]) -> str | None:
    """The one button named exactly what he said ("don't save" -> "Don't save"), else None: a bare dialog answer,
    told from everything else without the LLM. Only the whole name counts, and only one button."""
    want = normalize(said)
    if not want or len(want.split()) > BUTTON_WORDS:
        return None
    hits = _drop_nested([t for t in targets if role_matches("button", t.role) and normalize(t.name) == want])
    return hits[0].name if len(hits) == 1 else None


def _drop_nested(found: list[Target]) -> list[Target]:
    """A button and the text inside it with the same name are one thing: keep the outer one."""
    kept: list[Target] = []
    for t in sorted(found, key=lambda t: -t.box.area):
        if not any(k.box.overlap(t.box) > 0.9 and normalize(k.name) == normalize(t.name) for k in kept):
            kept.append(t)
    return kept


# --- one thing, one tag ---------------------------------------------------------------------------------------------

def merge(uia: Iterable[Target], dom: Iterable[Target], ocr: Iterable[Target], *, screen: Box,
          scale: float = 1.0) -> list[Target]:
    """UIA and page-map boxes win; an OCR line inside a tagged box names it (when it had no name), a line that
    matches nothing gets its own tag. Too small (< 8 logical px) or too big (> 25 % of the screen) is dropped."""
    def fits(t: Target) -> bool:
        return (min(t.box.width, t.box.height) >= MIN_LOGICAL_PX * scale
                and t.box.area <= MAX_SCREEN_SHARE * screen.area)

    dom_kept = [t for t in dom if fits(t)]
    boxes: list[Target] = list(dom_kept)
    for t in uia:
        if fits(t) and not any(d.box.overlap(t.box) > 0.7 for d in dom_kept):
            boxes.append(t)
    boxes = _dedupe(boxes)
    for line in ocr:
        if not line.name.strip():
            continue
        x, y = line.box.centre
        inside = [i for i, t in enumerate(boxes) if t.box.contains(x, y)]
        if inside:
            i = min(inside, key=lambda i: boxes[i].box.area)
            if not boxes[i].name.strip():
                boxes[i] = replace(boxes[i], name=line.name)
        elif fits(line):
            boxes.append(line)
    return boxes


def _dedupe(boxes: list[Target]) -> list[Target]:
    """Near-identical boxes (a list item and its only child) become one; the named / earlier one wins."""
    kept: list[Target] = []
    for t in boxes:
        same = next((i for i, k in enumerate(kept) if _same_box(k.box, t.box)), None)
        if same is None:
            kept.append(t)
        elif not kept[same].name.strip() and t.name.strip():
            kept[same] = t
    return kept


def _same_box(a: Box, b: Box) -> bool:
    return a.overlap(b) > 0.9 and min(a.area, b.area) >= 0.8 * max(a.area, b.area)


def reading_order(targets: Sequence[Target]) -> list[Target]:
    """Row by row, top to bottom, left to right. A target joins a row when its centre is within half the row's
    height of the row's first centre."""
    rows: list[list[Target]] = []
    for t in sorted(targets, key=lambda t: (t.box.centre[1], t.box.left)):
        if rows:
            first = rows[-1][0]
            if abs(t.box.centre[1] - first.box.centre[1]) <= max(4, min(first.box.height, t.box.height) // 2):
                rows[-1].append(t)
                continue
        rows.append([t])
    return [t for row in rows for t in sorted(row, key=lambda t: t.box.left)]


def number(targets: Sequence[Target], previous: Sequence[Mark] = (), *, page: int = 0) -> list[Mark]:
    """Tags 1..99 for one page of targets in reading order. A target whose box barely moved since the last showing
    keeps its number."""
    ordered = reading_order(targets)[page * MAX_TAGS:(page + 1) * MAX_TAGS]
    taken: dict[int, Target] = {}
    rest: list[Target] = []
    for t in ordered:
        old = next((m for m in previous if m.number not in taken and _barely_moved(m.target.box, t.box)), None)
        if old is not None:
            taken[old.number] = t
        else:
            rest.append(t)
    free = (n for n in range(1, MAX_TAGS + 1) if n not in taken)
    for t in rest:
        taken[next(free)] = t
    return sorted((Mark(n, t) for n, t in taken.items()), key=lambda m: m.number)


def pages(count: int) -> int:
    return max(1, -(-count // MAX_TAGS))


def _barely_moved(a: Box, b: Box) -> bool:
    ax, ay = a.centre
    bx, by = b.centre
    return abs(ax - bx) <= 8 and abs(ay - by) <= 8 and abs(a.width - b.width) <= max(8, a.width // 5) \
        and abs(a.height - b.height) <= max(8, a.height // 5)


# --- spoken numbers -------------------------------------------------------------------------------------------------

_UNITS = {
    "zero": 0, "oh": 0, "one": 1, "won": 1, "two": 2, "to": 2, "too": 2, "three": 3, "four": 4, "for": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "ate": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fourty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
         "eighty": 80, "ninety": 90}
_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8,
             "ninth": 9, "tenth": 10}


def parse_number(text: str) -> int | None:
    """ "12", "twelve", "twenty one", "twenty-one", "number 7", "#7" -> the number (1-99). None = not just a number."""
    words = normalize(text.replace("-", " ").replace("#", " number ")).split()
    if words[:1] == ["number"] or words[:1] == ["tag"]:
        words = words[1:]
    if not words or len(words) > 2:
        return None
    if len(words) == 1 and words[0].isdigit():
        value = int(words[0])
        return value if 1 <= value <= MAX_TAGS else None
    if len(words) == 1:
        value = _UNITS.get(words[0], _TENS.get(words[0], _ORDINALS.get(words[0])))
        return value if value else None
    tens, unit = _TENS.get(words[0]), _UNITS.get(words[1])
    if tens and unit is not None and 1 <= unit <= 9:
        return tens + unit
    return None


# --- the grid -------------------------------------------------------------------------------------------------------

def grid_cells(area: Box, size: int = 3) -> list[Box]:
    """``size`` × ``size`` cells over ``area``, numbered 1.. row by row (index 0 = cell 1)."""
    xs = [area.left + area.width * i // size for i in range(size + 1)]
    ys = [area.top + area.height * i // size for i in range(size + 1)]
    return [Box(xs[c], ys[r], xs[c + 1], ys[r + 1]) for r in range(size) for c in range(size)]


def grid_zoom(area: Box, cell: int, size: int = 3) -> Box:
    """The cell ``cell`` (1-based) of ``area``: the next level's area."""
    cells = grid_cells(area, size)
    if not 1 <= cell <= len(cells):
        raise ValueError(f"cell {cell} is not on the grid")
    return cells[cell - 1]
