"""What Nova can do, for "what can I say?" (prompt 07c §8): generated from the action list, so it never goes stale.

Every plan action belongs to one category with an example phrase. The card is four rows (one per category, its
verbs); picking a row shows that category's examples. Pure.
"""

from __future__ import annotations

from dataclasses import dataclass

CATEGORIES = ("apps", "web", "screen", "system")
TITLES = {"apps": "Apps & windows", "web": "Web & Chrome", "screen": "On screen", "system": "System & Nova"}
QUESTION = "What I can do — pick one for examples"
HOLD_S = 8.0  # examples are read, not glanced at
MAX_EXAMPLES = 5


@dataclass(frozen=True)
class Ability:
    category: str
    verb: str  # what the card's row lists ("open", "snap"); "" = folded into another action's verb
    example: str  # a phrase that works, as he'd say it


ACTIONS: dict[str, Ability] = {
    "open_app": Ability("apps", "open", "open Spotify"),
    "switch_app": Ability("apps", "switch", "switch to Chrome"),
    "close_app": Ability("apps", "close", "close Notepad"),
    "window": Ability("apps", "snap", "snap this left"),
    "layout": Ability("apps", "arrange", "Chrome left, Claude right"),
    "open_path": Ability("apps", "folders", "open my downloads"),
    "open_url": Ability("web", "go to", "go to youtube.com"),
    "web_search": Ability("web", "search", "search for flights to Denver"),
    "tab": Ability("web", "tabs", "new tab"),
    "nav": Ability("web", "back", "go back"),
    "page_click": Ability("web", "", "click sign in"),
    "page_fill": Ability("web", "fill", "type hello in the search box"),
    "page_read": Ability("web", "read", "read this page"),
    "scroll_to": Ability("web", "", "scroll to the comments"),
    "canvas_due": Ability("web", "Canvas", "what's due this week"),
    "canvas_open_item": Ability("web", "", "open my physics homework"),
    "canvas_item_text": Ability("web", "", "read the lab assignment"),
    "canvas_open": Ability("web", "", ""),  # what canvas_open_item resolves to; never said
    "docs_new": Ability("web", "", "new Google Doc"),
    "scroll": Ability("screen", "scroll", "scroll down"),
    "autoscroll": Ability("screen", "", "keep scrolling"),
    "doc": Ability("screen", "pages", "go to page 5"),
    "ui_click": Ability("screen", "click", "click Save"),
    "numbers": Ability("screen", "numbers", "show numbers"),
    "grid": Ability("screen", "", "show grid"),
    "pick": Ability("screen", "", "click 4"),
    "type_text": Ability("screen", "type", "type hello world"),
    "press": Ability("screen", "keys", "press enter"),
    "dictation": Ability("screen", "dictate", "dictate"),
    "rewrite": Ability("screen", "rewrite", "make this more formal"),
    "task": Ability("screen", "show me how", "show me how to turn on word wrap"),
    "volume": Ability("system", "volume", "volume 30"),
    "media": Ability("system", "music", "pause the music"),
    "screenshot": Ability("system", "screenshot", "take a screenshot"),
    "camera": Ability("system", "photo", "take a photo"),
    "system": Ability("system", "lock", "lock the computer"),
    "listening": Ability("system", "", "stop listening"),
    "nova": Ability("system", "undo", "undo that"),
}
_MORE = {  # a second phrase for the actions that do the most
    "apps": ("open the budget spreadsheet",),
    "screen": ("right-click the file", "zoom in"),
    "system": ("do that again", "what can I say"),
}


def row(category: str) -> str:
    """ "Apps & windows: open, switch, close…" — one row of the card."""
    verbs = [a.verb for a in ACTIONS.values() if a.category == category and a.verb]
    return f"{TITLES[category]}: {', '.join(verbs[:4])}…"


def card() -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    """(question, choices, topics): the help card, one row per category."""
    return QUESTION, tuple(row(c) for c in CATEGORIES), CATEGORIES


def examples(category: str) -> tuple[str, ...]:
    said = [a.example for a in ACTIONS.values() if a.category == category and a.example and a.verb]
    said.extend(_MORE.get(category, ()))
    return tuple(dict.fromkeys(said))


def topic(category: str) -> str:
    """What picking a row shows: a handful of phrases to say."""
    if category not in TITLES:
        return "Say “what can I say?” for the list."
    return " · ".join(f"“{p}”" for p in examples(category)[:MAX_EXAMPLES])
