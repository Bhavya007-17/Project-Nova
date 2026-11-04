"""Tier 1's prompt (SPEC §7.4): a static prefix and a small per-request message.

The prefix (rules, the action list, his short names, ~12 worked examples) is byte-identical
from one request to the next, so Ollama's prompt cache keeps it: after the first request
(or the preload) only the new message is read. The message is compact JSON: what he said,
the app in front, the open apps, Chrome's tab, and installed apps whose names match his
words. The reply is forced into ``SCHEMA`` (Ollama ``format``), a Plan (SPEC §7.5) without
"say": Nova writes the island text itself, and every token the model doesn't write is time saved.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from ..actions.catalog import Catalog, Target, words_of
from .plan import AREAS, ENGINES, MEDIA_OPS, NAV_OPS, SCROLL_AMOUNTS, SCROLL_DIRS, WINDOW_OPS

MAX_APPS = 12  # installed apps named in one request (the whole Start menu is ~1,200 tokens)

_LLM_TAB_OPS = ("new", "close", "next", "prev", "reopen")
_VOLUME_OPS = ("up", "down", "mute", "unmute", "set")


def _obj(properties: dict[str, Any], *optional: str) -> dict[str, Any]:
    return {"type": "object", "properties": properties,
            "required": [name for name in properties if name not in optional]}


def _enum(values: Iterable[str]) -> dict[str, Any]:
    return {"enum": sorted(values)}


_TEXT = {"type": "string"}


def _step(action: str, optional: Sequence[str] = (), **args: dict[str, Any]) -> dict[str, Any]:
    return _obj({"action": {"const": action}, **args}, *optional)


STEP_SCHEMAS = (
    _step("open_app", app=_TEXT),
    _step("switch_app", app=_TEXT),
    _step("close_app", app=_TEXT),
    _step("window", ("monitor",), op=_enum(WINDOW_OPS), target=_TEXT,
          monitor=_enum(("next", "prev", "left", "right", "primary", "other"))),
    _step("layout", slots={"type": "array", "minItems": 1, "maxItems": 4,
                           "items": _obj({"app": _TEXT, "area": _enum(AREAS)})}),
    _step("open_url", url=_TEXT),
    _step("web_search", query=_TEXT, engine=_enum(ENGINES)),
    _step("tab", op=_enum(_LLM_TAB_OPS)),
    _step("nav", op=_enum(NAV_OPS)),
    _step("scroll", dir=_enum(SCROLL_DIRS), amount=_enum(SCROLL_AMOUNTS)),
    _step("type_text", text=_TEXT, submit={"type": "boolean"}),
    _step("press", keys=_TEXT),
    _step("volume", ("level",), op=_enum(_VOLUME_OPS), level={"type": "integer"}),
    _step("media", op=_enum(MEDIA_OPS)),
    _step("page_click", description=_TEXT),
    _step("dictation", op=_enum(("start", "stop"))),
    _step("listening", op=_enum(("sleep", "wake"))),
)

# Required properties come out first, in this order (llama.cpp's schema grammar), then the optional ones.
SCHEMA: dict[str, Any] = _obj(
    {
        "kind": _enum(("act", "clarify", "handoff", "dictate", "teach", "ignore")),
        "steps": {"type": "array", "maxItems": 4, "items": {"anyOf": list(STEP_SCHEMAS)}},
        "confidence": {"type": "number"},
        "question": _TEXT,
        "choices": {"type": "array", "maxItems": 4, "items": _TEXT},
        "handoff": _obj({"to": _enum(("claude", "chatgpt")), "mode": _enum(("text", "voice")), "prompt": _TEXT}),
    },
    "question", "choices", "handoff",
)

_RULES = """\
You are Nova, the voice control on Bhavya's Windows laptop. Turn what he said into one JSON plan.

kind:
- act: do it now with steps (run in order).
- clarify: you can't tell which app, site or window he means. Short question + 2-4 short choices.
- handoff: open-ended work for an AI (write, solve, research, explain, summarize, plan something), or he names Claude or ChatGPT ("ask Claude", "have ChatGPT", "on Claude, ...", "put ... on Claude"). to: claude unless he says ChatGPT. prompt: his words for the job.
- dictate: he wants to dictate text he hasn't said yet ("take a note", "let me dictate").
- teach: a task on this computer that none of the actions can do.
- ignore: not a command for Nova. A command asks Nova to do something now. Statements and chat are not commands, even with words like open, close, next, search, page or song in them.

actions:
open_app{app}: open an app, or bring it to the front
switch_app{app}: go to an app that's open ("go to", "focus on", "back to")
close_app{app}: quit an app
window{op,target}: one window. op: maximize | minimize (hide, get it out of the way) | restore | close | fullscreen | snap_left | snap_right | snap_top | snap_bottom | center | move_monitor (to the other screen). target: the app he names, or "active" for this / it / the window
layout{slots:[{app,area}]}: several windows at once. area: left | right (halves) | top | bottom | top_left | top_right | bottom_left | bottom_right | left_third | middle_third | right_third | full
open_url{url}: a website, full https address
web_search{query,engine}: query: what to look for. engine: google unless he names youtube, bing, duckduckgo, wikipedia, amazon, github, maps or images (pictures). Play, watch or find something on youtube: engine youtube
tab{op}: browser tabs: new | close | next | prev (the tab before) | reopen (bring back a closed tab)
nav{op}: back (the previous page) | forward | reload
scroll{dir,amount}: dir up | down | left | right | top | bottom; amount small | page | large
type_text{text,submit}: type at the cursor. submit: true only if he says enter, send or submit
press{keys}: copy ctrl+c, paste ctrl+v, cut ctrl+x, undo ctrl+z, redo ctrl+y, save ctrl+s, select all ctrl+a, zoom in ctrl+=, zoom out ctrl+-, show the desktop win+d, enter, esc
volume{op,level}: up (louder) | down (quieter) | mute | unmute | set (only when he says a number: level 0-100)
media{op}: play_pause | play | pause | next (skip) | prev | stop. Works on whatever is playing: never ask which app
page_click{description}: click something on the web page in front
dictation{op}: start | stop
listening{op}: sleep | wake

rules:
- text, query and prompt are copied word for word from "said". Never add, fix, translate or reword them.
- "front", "open", "tab" and "apps" only describe the screen. Act only on what he said: never open, type or search something just because it's on screen.
- Use the app names in "apps", "open" and "front". A site he names: open_url. His short names are below.
- "this", "it", "the window", "this window": target "active". Don't ask which window.
- "the browser": the browser in "open" (Google Chrome).
- confidence: how sure you are this is what he wants. 0.9+ sure, 0.6-0.9 likely, under 0.6 unsure: then clarify.
- Never make up an app, site or text he didn't mention. Not sure which app or site: clarify.
- "asked" and "answer": you asked him something and this is his answer; now plan it.
"""

# (request, plan) pairs: worked examples that show the patterns. Different from docs/eval/intents.yaml on purpose.
EXAMPLES: tuple[tuple[dict[str, Any], dict[str, Any]], ...] = (
    ({"said": "yo can you throw spotify on for me", "front": "Google Chrome", "open": ["Google Chrome", "Claude"],
      "apps": ["Spotify"]},
     {"kind": "act", "steps": [{"action": "open_app", "app": "Spotify"}], "confidence": 0.95}),
    ({"said": "blow this up so it fills the screen", "front": "Notepad", "open": ["Notepad", "Google Chrome"]},
     {"kind": "act", "steps": [{"action": "window", "op": "maximize", "target": "active"}], "confidence": 0.9}),
    ({"said": "hide this for now", "front": "Claude", "open": ["Claude", "Google Chrome"]},
     {"kind": "act", "steps": [{"action": "window", "op": "minimize", "target": "active"}], "confidence": 0.9}),
    ({"said": "throw this over to the right", "front": "Notepad", "open": ["Notepad", "Claude", "Google Chrome"]},
     {"kind": "act", "steps": [{"action": "window", "op": "snap_right", "target": "active"}], "confidence": 0.9}),
    ({"said": "stick claude over on the right side", "front": "Google Chrome", "open": ["Google Chrome", "Claude"],
      "apps": ["Claude"]},
     {"kind": "act", "steps": [{"action": "window", "op": "snap_right", "target": "Claude"}], "confidence": 0.95}),
    ({"said": "I wanna watch some piano covers on youtube", "front": "Claude", "open": ["Claude"],
      "apps": ["YouTube"]},
     {"kind": "act", "steps": [{"action": "web_search", "query": "piano covers", "engine": "youtube"}],
      "confidence": 0.9}),
    ({"said": "split the screen between fusion and claude", "front": "Autodesk Fusion",
      "open": ["Autodesk Fusion", "Claude"], "apps": ["Autodesk Fusion", "Claude"]},
     {"kind": "act", "steps": [{"action": "layout", "slots": [{"app": "Autodesk Fusion", "area": "left"},
                                                             {"app": "Claude", "area": "right"}]}],
      "confidence": 0.9}),
    ({"said": "get rid of this tab", "front": "Google Chrome", "open": ["Google Chrome"]},
     {"kind": "act", "steps": [{"action": "tab", "op": "close"}], "confidence": 0.9}),
    ({"said": "jump to the tab before this one", "front": "Google Chrome", "open": ["Google Chrome"]},
     {"kind": "act", "steps": [{"action": "tab", "op": "prev"}], "confidence": 0.9}),
    ({"said": "put see you soon in there", "front": "WhatsApp", "open": ["WhatsApp", "Google Chrome"]},
     {"kind": "act", "steps": [{"action": "type_text", "text": "see you soon", "submit": False}], "confidence": 0.85}),
    ({"said": "type on my way and hit send", "front": "WhatsApp", "open": ["WhatsApp"]},
     {"kind": "act", "steps": [{"action": "type_text", "text": "on my way", "submit": True}], "confidence": 0.9}),
    ({"said": "it's way too loud", "front": "Google Chrome", "open": ["Google Chrome"]},
     {"kind": "act", "steps": [{"action": "volume", "op": "down"}], "confidence": 0.85}),
    ({"said": "switch to the other one", "front": "Google Chrome", "open": ["Google Chrome", "Claude", "Notepad"]},
     {"kind": "clarify", "steps": [], "confidence": 0.4, "question": "Switch to which one?",
      "choices": ["Claude", "Notepad"]}),
    ({"said": "pull that up for me", "front": "Claude", "open": ["Claude", "Google Chrome", "Spotify"],
      "tab": "Inbox (3) - Gmail"},
     {"kind": "clarify", "steps": [], "confidence": 0.3, "question": "Pull up which one?",
      "choices": ["Google Chrome", "Spotify", "Claude"]}),
    ({"said": "open that thing", "front": "Google Chrome", "open": ["Google Chrome", "Notepad"],
      "asked": "Open which one?", "answer": "Notepad"},
     {"kind": "act", "steps": [{"action": "open_app", "app": "Notepad"}], "confidence": 0.9}),
    ({"said": "write a cover letter for the robotics internship", "front": "Google Chrome",
      "open": ["Google Chrome", "Claude"]},
     {"kind": "handoff", "steps": [], "confidence": 0.9,
      "handoff": {"to": "claude", "mode": "text", "prompt": "write a cover letter for the robotics internship"}}),
    ({"said": "put the lab writeup on claude", "front": "Google Chrome", "open": ["Google Chrome", "Claude"],
      "apps": ["Claude"]},
     {"kind": "handoff", "steps": [], "confidence": 0.9,
      "handoff": {"to": "claude", "mode": "text", "prompt": "the lab writeup"}}),
    ({"said": "ask chatgpt what the integral of x squared is", "front": "Claude", "open": ["Claude", "ChatGPT"],
      "apps": ["ChatGPT"]},
     {"kind": "handoff", "steps": [], "confidence": 0.95,
      "handoff": {"to": "chatgpt", "mode": "text", "prompt": "what the integral of x squared is"}}),
    ({"said": "turn on dark mode in fusion", "front": "Autodesk Fusion", "open": ["Autodesk Fusion"],
      "apps": ["Autodesk Fusion"]},
     {"kind": "teach", "steps": [], "confidence": 0.7}),
    ({"said": "yeah I told him we'd open the lab at nine", "front": "Google Chrome", "open": ["Google Chrome"]},
     {"kind": "ignore", "steps": [], "confidence": 0.9}),
    ({"said": "the next video in that playlist is so good", "front": "Google Chrome", "open": ["Google Chrome"],
      "tab": "lofi beats - YouTube"},
     {"kind": "ignore", "steps": [], "confidence": 0.9}),
    ({"said": "we should really close up the shop early on friday", "front": "Claude", "open": ["Claude"]},
     {"kind": "ignore", "steps": [], "confidence": 0.9}),
)


def dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def system_prompt(aliases: Iterable[tuple[str, Target]] = ()) -> str:
    short = "; ".join(f"{phrase} = {target.url or target.name}" for phrase, target in aliases)
    return _RULES + (f"\nhis short names: {short}\n" if short else "")


def prefix(aliases: Iterable[tuple[str, Target]] = ()) -> tuple[dict[str, str], ...]:
    """The static messages: system rules + the worked examples as earlier turns. Same input -> same text."""
    messages = [{"role": "system", "content": system_prompt(aliases)}]
    for request, plan in EXAMPLES:
        messages.append({"role": "user", "content": dumps(request)})
        messages.append({"role": "assistant", "content": dumps(plan)})
    return tuple(messages)


# --- the per-request message ----------------------------------------------------------------------------------

_SKIP = frozenset({
    "the", "a", "an", "my", "me", "to", "on", "in", "for", "of", "and", "up", "it", "this", "that", "can", "you",
    "please", "open", "get", "put", "go", "make", "pull", "bring", "show", "switch", "close", "start", "launch",
    "with", "at", "is", "be", "i", "want", "need", "over", "side", "half", "left", "right", "screen", "window",
    "new", "tab", "some", "thing", "one", "just", "now", "then", "would", "could", "will", "app",
})


def relevant_apps(text: str, catalog: Catalog, running: Iterable[str] = (), limit: int = MAX_APPS) -> list[str]:
    """Installed apps and short-named sites whose names match his words (1-3 word runs), best first."""
    words = [w for w in words_of(text)]
    scored: dict[str, float] = {}
    familiar = frozenset(running)
    for size in (3, 2, 1):
        for i in range(len(words) - size + 1):
            run = words[i : i + size]
            if all(w in _SKIP for w in run) or (size == 1 and (run[0] in _SKIP or len(run[0]) < 3)):
                continue
            for match in catalog.resolve(" ".join(run), familiar=familiar).matches[:3]:
                if match.score >= 60:
                    name = match.target.name
                    scored[name] = max(scored.get(name, 0.0), match.score)
    if not scored:
        return []
    top = max(scored.values())
    floor = max(60.0, top - 15.0)  # "sound recorder" is Sound Recorder: no Steps / Performance Recorder next to it
    ranked = sorted((name for name in scored if scored[name] >= floor), key=lambda name: -scored[name])
    return ranked[:limit]


def request(text: str, *, front: str | None, open_apps: Sequence[str], tab: str | None,
            apps: Sequence[str], asked: str | None = None, answer: str | None = None) -> dict[str, Any]:
    data: dict[str, Any] = {"said": text.strip()}
    if front:
        data["front"] = front
    if open_apps:
        data["open"] = list(open_apps)
    if tab:
        data["tab"] = tab
    if apps:
        data["apps"] = list(apps)
    if asked:
        data["asked"] = asked
        data["answer"] = answer or ""
    return data


def messages(prefix_messages: Sequence[Mapping[str, str]], request_data: Mapping[str, Any]) -> list[dict[str, str]]:
    return [*map(dict, prefix_messages), {"role": "user", "content": dumps(request_data)}]
