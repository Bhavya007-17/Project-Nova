"""Which Chrome tab (pure): a tab already on the site he opens again ("go to canvas" switches to the Canvas tab
instead of opening another), the tab he names ("the youtube tab"), the window that shows a tab."""

from __future__ import annotations

import re
from collections.abc import Sequence
from urllib.parse import urlsplit

from rapidfuzz import fuzz

from .bridge import TabInfo

_WORD = re.compile(r"[a-z0-9]+")
_FILLER = frozenset({"the", "my", "a", "that", "this", "one", "tab", "page", "site", "website", "window", "please"})
_SITE_WORDS = frozenset({"www", "com", "org", "net", "edu", "io", "co", "google", "html", "https", "http"})


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def same_site(tab_url: str, target: str) -> bool:
    """Is a tab already on ``target`` (same host; under the target's path if it has one)?"""
    tab, want = urlsplit(tab_url), urlsplit(target)
    if tab.scheme not in ("http", "https") or not want.hostname or _host(tab_url) != _host(target):
        return False
    path = want.path.rstrip("/")
    return not path or tab.path.rstrip("/").lower().startswith(path.lower())


def _words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def best_tab(tabs: Sequence[TabInfo], said: str) -> list[TabInfo]:
    """Tabs whose title or site match what he called it, best (then most recently used) first."""
    want = [w for w in _words(said) if w not in _FILLER]
    if not want:
        return []
    phrase = " ".join(want)
    scored = []
    for tab in tabs:
        host = _host(tab.url)
        site = [w for w in _words(host) if w not in _SITE_WORDS] + _words(host.split(".")[0])
        title = _words(tab.title)
        hits = sum(1 for w in want if w in site or w in title or any(t.startswith(w) for t in title if len(w) >= 4))
        score = 100.0 * hits / len(want)
        score = max(score, fuzz.partial_ratio(phrase, tab.title.lower()) if len(phrase) >= 4 else 0.0)
        if score >= 75:
            scored.append((score, tab.last, tab))
    scored.sort(key=lambda item: (-item[0], -item[1]))
    if not scored:
        return []
    top = scored[0][0]
    return [tab for score, _, tab in scored if score >= top - 10]


def window_title_matches(window_title: str, tab_title: str) -> bool:
    """Chrome titles its window "<active tab's title> - Google Chrome"."""
    tab_title = " ".join(tab_title.split())
    return bool(tab_title) and " ".join(window_title.split()).startswith(tab_title)
