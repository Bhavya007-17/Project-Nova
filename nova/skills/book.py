"""Learned skills (SPEC §7.3, §12): a plan the LLM got right once replays with no LLM the next time.

A skill is one JSON file in ``data/skills/``: the phrasings that trigger it and the plan's steps. Human-readable,
editable, and local like everything else.

- **Learn**: after a Tier-1 plan ran and worked, with nothing asked on the way, and only if it would mean the
  same thing tomorrow (``learnable``): no "this / that / the third one", no steps that act on whatever happens to
  be in front, no transient page element ids.
- **Match** (``Book.match``, well under 1 ms): the words that carry meaning, in order ("could you open my thermo
  course on Canvas please" and "open the thermo course in canvas" are the same four words). A long word may be
  off by a letter or two (speech-to-text), never a short word or a number. No embedding model: it would be a
  ~100 MB download that idles in RAM to save an LLM call that another phrasing triggers once — after which that
  phrasing is a trigger too.
- **Teach by voice**: "when I say study mode, open Canvas and Notion" (``teaching``) saves the plan for the
  second half under the first.
- **Stale**: a skill that fails is skipped until the LLM's next working plan for the same words replaces it.

The risk check still runs on every replayed step (the executor does it): a learned skill never skips a confirm.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz

from ..brain.plan import Plan, PlanError, from_dict

_log = logging.getLogger(__name__)

MAX_TRIGGERS = 12
MAX_SKILLS = 500
_NEAR = 88  # a long word this close is the same word misheard ("thermo" / "thermos")
_NEAR_MIN_LEN = 6

_WORD = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")
_FILLER = frozenset("""
a an the my me i you your please can could would will hey hi ok okay nova just now for to of on in at into onto
with and then also want need like wanna gonna go ahead kindly some um uh so
""".split())
# Words that point at whatever is in front right now: the same words mean something else tomorrow.
_POINTING = frozenset("""
this that it these those here them same again selected selection current first second third fourth fifth last
next previous other one
""".split())

# Steps that mean the same thing wherever he is...
_STANDS_ALONE = frozenset({"open_app", "switch_app", "close_app", "layout", "open_url", "web_search", "volume",
                           "media", "canvas_due", "canvas_open_item", "docs_new"})
# ...and steps that act on what's in front: fine only after a step of the plan put something there.
_NEEDS_ANCHOR = frozenset({"window", "tab", "nav", "scroll", "press", "type_text", "page_click", "page_fill",
                           "scroll_to"})
_ANCHORS = frozenset({"open_app", "switch_app", "open_url", "docs_new", "canvas_open_item"})
_TRANSIENT_ARGS = ("element", "label", "submits", "secret", "search")  # page element ids die with the page


def words(text: str) -> tuple[str, ...]:
    """The words that carry the meaning, in order."""
    return tuple(w for w in _WORD.findall(text.lower()) if w not in _FILLER)


def same(a: Sequence[str], b: Sequence[str]) -> bool:
    if len(a) != len(b) or not a:
        return False
    for x, y in zip(a, b, strict=True):
        if x == y:
            continue
        if min(len(x), len(y)) < _NEAR_MIN_LEN or any(c.isdigit() for c in x + y) or fuzz.ratio(x, y) < _NEAR:
            return False
    return True


def stable_steps(plan: Plan) -> list[dict[str, Any]] | None:
    """The plan's steps as they can be replayed any day, or None if they depend on the moment."""
    if plan.kind != "act" or not plan.steps:
        return None
    out: list[dict[str, Any]] = []
    anchored = False
    for s in plan.steps:
        if s.action in _NEEDS_ANCHOR:
            targeted = s.action == "window" and s.get("target") not in (None, "", "active")
            new_tab = s.action == "tab" and s.get("op") in ("new", "goto")
            if not (anchored or targeted or new_tab):
                return None
        elif s.action not in _STANDS_ALONE:
            return None
        anchored = anchored or s.action in _ANCHORS
        out.append({k: v for k, v in s.to_dict().items() if k not in _TRANSIENT_ARGS})
    return out


def learnable(text: str, plan: Plan) -> list[dict[str, Any]] | None:
    """Steps worth keeping for these words, or None (SPEC §12 determinism rules)."""
    spoken = words(text)
    if len(spoken) < 2 or _POINTING.intersection(_WORD.findall(text.lower())):
        return None
    return stable_steps(plan)


_TEACH = re.compile(
    r"^\W*(?:(?:hey|ok|okay)\s+)?(?:nova\W+)?(?:when(?:ever)?|if)\s+i\s+say\s+(?P<phrase>.+?)\s*[,:;–—-]?\s+"
    r"(?:(?:then|it)\s+)?(?:(?:you\s+(?:should|can|need\s+to)|i\s+(?:mean|want\s+you\s+to)|please|do|just)\s+)?"
    r"(?P<command>(?:open|close|go|switch|start|launch|play|pause|search|show|put|set|turn|mute|snap|bring|"
    r"maximi[sz]e|minimi[sz]e|pull|type|press|look)\b.+)$", re.IGNORECASE)


def teaching(text: str) -> tuple[str, str] | None:
    """ "When I say study mode, open Canvas and Notion" -> ("study mode", "open Canvas and Notion")."""
    found = _TEACH.match(text.strip())
    if found is None:
        return None
    phrase = found["phrase"].strip(" \"'“”‘’.,")
    command = found["command"].strip(" .")
    return (phrase, command) if words(phrase) and command else None


_FORGET = re.compile(r"^\W*forget\s+(?:about\s+)?(?P<what>.+?)\W*$", re.IGNORECASE)
_FORGET_LAST = re.compile(r"^(?:that|it|this)(?:\s+(?:one|skill|shortcut))?$|^what\s+you\s+(?:just\s+)?learn(?:ed|t)$",
                          re.IGNORECASE)
_LIST = re.compile(r"^\W*(?:what\s+(?:have|did)\s+you\s+learn(?:ed|t)?|what\s+(?:skills|shortcuts)\s+do\s+you\s+(?:have|know)|"
                   r"(?:list|show)\s+(?:me\s+)?(?:your|my|the)\s+(?:skills|shortcuts))\W*$", re.IGNORECASE)


@dataclass
class Skill:
    id: str
    name: str
    triggers: list[str]
    steps: list[dict[str, Any]]
    taught: bool = False  # he said "when I say …" (kept even if a replay fails once)
    stale: bool = False
    uses: int = 0
    last_ok: str = ""
    keys: list[tuple[str, ...]] = field(default_factory=list, repr=False, compare=False)

    def plan(self) -> Plan:
        return from_dict({"kind": "act", "steps": self.steps, "confidence": 1.0})

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        del data["keys"]
        return data


def _slug(text: str) -> str:
    return "-".join(words(text))[:48].strip("-") or "skill"


def _today() -> str:
    return time.strftime("%Y-%m-%d")


class Book:
    """The skills on disk. Used from one thread (the brain's); the CLI opens its own."""

    def __init__(self, folder: Path) -> None:
        self._folder = folder
        self._skills: dict[str, Skill] = {}
        self._last: str | None = None  # the skill used or learned last ("forget that")
        self._load()

    def _load(self) -> None:
        if not self._folder.is_dir():
            return
        for path in sorted(self._folder.glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                skill = Skill(id=path.stem, name=str(raw["name"]), triggers=[str(t) for t in raw["triggers"]],
                              steps=list(raw["steps"]), taught=bool(raw.get("taught")), stale=bool(raw.get("stale")),
                              uses=int(raw.get("uses", 0)), last_ok=str(raw.get("last_ok", "")))
                skill.plan()  # a hand-edited file must still be a valid plan
            except (OSError, ValueError, KeyError, TypeError, PlanError) as error:
                _log.warning("skipping skill %s: %s", path.name, error)
                continue
            skill.keys = [words(t) for t in skill.triggers]
            self._skills[skill.id] = skill

    def _save(self, skill: Skill) -> None:
        try:
            self._folder.mkdir(parents=True, exist_ok=True)
            path = self._folder / f"{skill.id}.json"
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(skill.to_json(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            tmp.replace(path)
        except OSError as error:  # a full disk must not fail the command that just worked
            _log.warning("couldn't save skill %s: %s", skill.id, error)

    # --- reading ------------------------------------------------------------------------------------------------

    def all(self) -> list[Skill]:
        return sorted(self._skills.values(), key=lambda s: (-s.uses, s.name))

    def get(self, skill_id: str) -> Skill | None:
        return self._skills.get(skill_id)

    def find(self, text: str, *, stale: bool = False) -> Skill | None:
        spoken = words(text)
        if not spoken:
            return None
        for skill in self._skills.values():
            if (stale or not skill.stale) and any(same(spoken, key) for key in skill.keys):
                return skill
        return None

    def match(self, text: str, ctx: Any = None) -> Plan | None:
        """The router's Tier 0.5."""
        skill = self.find(text)
        if skill is None:
            return None
        try:
            plan = skill.plan()
        except PlanError:
            return None
        self._last = skill.id
        return plan

    # --- writing ------------------------------------------------------------------------------------------------

    def learn(self, text: str, plan: Plan, *, taught: bool = False) -> Skill | None:
        """Keep the plan for these words. Returns the skill, or None when it isn't worth keeping."""
        steps = stable_steps(plan) if taught else learnable(text, plan)
        if steps is None or not words(text):
            return None
        text = " ".join(text.split())
        skill = self.find(text, stale=True)
        if skill is None:  # the same steps under other words: one skill, one more trigger
            skill = next((s for s in self._skills.values() if s.steps == steps), None)
        if skill is None:
            if len(self._skills) >= MAX_SKILLS:
                return None
            skill_id = base = _slug(text)
            n = 2
            while skill_id in self._skills:
                skill_id, n = f"{base}-{n}", n + 1
            skill = Skill(skill_id, text.strip(" .,!?"), [], steps, taught=taught)
            self._skills[skill_id] = skill
        skill.steps = steps
        skill.stale = False
        skill.taught = skill.taught or taught
        skill.last_ok = _today()
        if not any(same(words(text), key) for key in skill.keys) and len(skill.triggers) < MAX_TRIGGERS:
            skill.triggers.append(text)
            skill.keys.append(words(text))
        self._last = skill.id
        self._save(skill)
        return skill

    def worked(self, text: str) -> None:
        skill = self.find(text)
        if skill is not None:
            skill.uses += 1
            skill.last_ok = _today()
            self._save(skill)

    def failed(self, text: str) -> None:
        """A replay broke: the LLM takes these words next time, and its working plan replaces this one."""
        skill = self.find(text)
        if skill is not None:
            skill.stale = True
            self._save(skill)

    def delete(self, skill_id: str) -> bool:
        skill = self._skills.pop(skill_id, None)
        if skill is None:
            return False
        if self._last == skill_id:
            self._last = None
        try:
            (self._folder / f"{skill_id}.json").unlink(missing_ok=True)
        except OSError as error:
            _log.warning("couldn't delete skill %s: %s", skill_id, error)
        return True

    # --- by voice -----------------------------------------------------------------------------------------------

    def manage(self, text: str) -> str | None:
        """ "Forget that", "forget study mode", "what have you learned": the island's answer, or None (not for
        the skills)."""
        if _LIST.match(text):
            names = [s.name for s in self.all() if not s.stale]
            if not names:
                return "Nothing learned yet — ask me something new, or say “when I say …, open …”"
            more = f" +{len(names) - 6} more" if len(names) > 6 else ""
            return "Learned: " + " · ".join(names[:6]) + more
        found = _FORGET.match(text)
        if found is None:
            return None
        what = found["what"].strip()
        if _FORGET_LAST.match(what):
            skill = self._skills.get(self._last or "")
            if skill is None:
                return "Nothing learned just now to forget"
        else:
            skill = self.find(what, stale=True)
            if skill is None:
                return None  # "forget it" about something else: the router's business
        self.delete(skill.id)
        return f"Forgot “{skill.name}”"
