"""Tier 1 (SPEC §7.4): the local LLM planner. What he said + a compact context -> the model's plan JSON.

How it talks to Ollama, per model family (measured on Ollama 0.30.10, see ``nova.brain.llm``):
- Qwen: Nova renders the chat template itself (ChatML; an empty think block for thinking
  models, so they answer at once) and calls /api/generate with ``raw``. Ollama's /api/chat with
  ``think: false`` ignores ``format`` for Qwen 3.5; raw mode applies the schema from the first
  token, and the static prefix is byte-identical, so the prompt cache always hits.
- Others (Gemma): /api/chat with ``think: false`` + ``format``.

Preload (key down / wake name, SPEC §7.4): load the model the governor picks and read the static
prefix once ("warm"), so a hybrid model like Qwen 3.5 keeps a resume point right where each
request's own part begins; afterwards a request only reads its own ~50 tokens.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from ..actions.catalog import Catalog
from ..config import Llm
from ..governor import Choice, Governor
from . import prompt
from .llm import LLMCancelled, LLMError, Ollama, Reply

_log = logging.getLogger(__name__)

NUM_CTX = 4096
MAX_TOKENS = 200  # the longest plan (4 steps / a 4-slot layout) is ~120 tokens; a rambling answer stops here
CPU_PAGE_ROWS = 12  # page elements shown to the CPU model (the GPU one gets up to 40)
PRELOAD_REASONS = frozenset({"key", "wake", "speech", "follow-up"})  # not "always": chatter never needs the LLM
WARM_AGAIN_S = 60.0
_WARM_TIMEOUT_S = 180.0  # a cold load + reading the prefix on the CPU


@dataclass(frozen=True)
class Seen:
    """What Tier 1 is told about the screen (SPEC §7.1)."""

    front: str | None = None
    open_apps: tuple[str, ...] = ()
    tab: str | None = None

    @classmethod
    def of(cls, ctx: Any) -> Seen:
        fg = getattr(ctx, "foreground", None)
        front = (fg.app or fg.win.exe_name.removesuffix(".exe")) if fg is not None else None
        names: list[str] = []
        for w in getattr(ctx, "windows", ()):
            if w.app and w.app not in names:
                names.append(w.app)
        tab = getattr(ctx, "tab", None)
        return cls(front or None, tuple(names), _tab_text(tab))


def _tab_text(tab: Any) -> str | None:
    """Chrome's tab as the model sees it: "Dashboard · canvas.vt.edu" (the site says more than a title)."""
    if tab is None:
        return None
    title = " ".join(str(getattr(tab, "title", "") or "").split())
    host = (urlsplit(str(getattr(tab, "url", "") or "")).hostname or "").removeprefix("www.")
    if title and host and host not in title.lower():
        return f"{title} · {host}"
    return title or host or None


@dataclass(frozen=True)
class Dialect:
    raw: bool  # render the template here and call /api/generate
    think_block: bool  # a thinking model: start its answer after an empty think block


def dialect_for(info: Mapping[str, Any]) -> Dialect:
    details = info.get("details") if isinstance(info.get("details"), Mapping) else {}
    family = str(details.get("family") or "")
    capabilities = info.get("capabilities") or ()
    if family.startswith("qwen"):
        return Dialect(raw=True, think_block="thinking" in capabilities)
    return Dialect(raw=False, think_block=False)


def chatml(messages: Sequence[Mapping[str, str]]) -> str:
    """The static part: every message, then the start of his turn (each request appends its own part)."""
    return "".join(f"<|im_start|>{m['role']}\n{m['content']}<|im_end|>\n" for m in messages) + "<|im_start|>user\n"


def chatml_turn(content: str, think_block: bool) -> str:
    return content + "<|im_end|>\n<|im_start|>assistant\n" + ("<think>\n\n</think>\n\n" if think_block else "")


@dataclass(frozen=True)
class Thought:
    """One Tier 1 call: the model's plan JSON, or why there's none."""

    data: Any = None
    error: str = ""
    choice: Choice | None = None
    reply: Reply | None = None
    ms: float = 0.0  # wall time, choosing the model included
    cancelled: bool = False

    def trace(self) -> dict[str, Any]:
        out: dict[str, Any] = {"llm_ms": round(self.ms, 1)}
        if self.choice is not None:
            out.update(model=self.choice.model, device=self.choice.device)
        if self.reply is not None:
            r = self.reply
            out.update(load_ms=r.load_ms, prompt_tokens=r.prompt_tokens, prompt_ms=r.prompt_ms, tokens=r.tokens,
                       eval_ms=r.eval_ms)
        if self.error:
            out["llm_error"] = self.error
        return out


class Tier1:
    def __init__(self, client: Ollama, governor: Governor, settings: Callable[[], Llm],
                 catalog: Callable[[], Catalog], *, clock: Callable[[], float] = time.monotonic) -> None:
        self._client = client
        self._background = Ollama(client.url)  # preloads: Esc aborts a command's call, never a warm-up
        self._governor = governor
        self._settings = settings
        self._catalog = catalog
        self._clock = clock
        self._dialects: dict[str, Dialect] = {}
        self._prefix_key: tuple | None = None
        self._prefix: tuple[tuple[dict[str, str], ...], str] = ((), "")
        self._lock = threading.Lock()
        self._warmed: dict[Choice, float] = {}
        self._preloading = False
        self._pinned: Choice | None = None

    @property
    def governor(self) -> Governor:
        return self._governor

    @property
    def llm(self) -> Llm:
        return self._settings()

    def pin(self, choice: Choice | None) -> None:
        """Always use ``choice`` (`nova eval --model`, the bench); None = the governor picks again."""
        self._pinned = choice

    # --- prompt pieces ------------------------------------------------------------------------------------------

    def _dialect(self, model: str) -> Dialect:
        dialect = self._dialects.get(model)
        if dialect is None:
            dialect = dialect_for(self._client.show(model))
            self._dialects[model] = dialect
        return dialect

    def _static(self, catalog: Catalog) -> tuple[tuple[dict[str, str], ...], str]:
        key = catalog.aliases
        with self._lock:
            if key != self._prefix_key:
                messages = prompt.prefix(key)
                self._prefix = (messages, chatml(messages))
                self._prefix_key = key
            return self._prefix

    @staticmethod
    def _options(choice: Choice) -> dict[str, Any]:
        return {"num_ctx": NUM_CTX, **choice.options}

    # --- planning ---------------------------------------------------------------------------------------------

    def think(self, text: str, seen: Seen, catalog: Catalog | None = None, *, asked: str | None = None,
              answer: str | None = None, choice: Choice | None = None,
              page: Sequence[str] | None = None) -> Thought:
        """``page``: the ranked page elements, one line each (``nova.chrome.pagemap.llm_rows``), when he points at
        the page."""
        began = time.perf_counter()
        catalog = catalog or self._catalog()
        llm = self._settings()
        try:
            choice = choice or self._pinned or self._governor.choose()
            dialect = self._dialect(choice.model)
            messages, static = self._static(catalog)
            listed = [name for name in prompt.relevant_apps(text, catalog, seen.open_apps) if name not in seen.open_apps]
            if page and choice.device == "cpu":
                page = page[:CPU_PAGE_ROWS]  # every row is ~20 tokens: the CPU reads them ~10x slower
            request = prompt.request(text, front=seen.front, open_apps=seen.open_apps, tab=seen.tab, apps=listed,
                                     asked=asked, answer=answer, page=page)
            if dialect.raw:
                reply = self._client.generate(
                    choice.model, static + chatml_turn(prompt.dumps(request), dialect.think_block), schema=prompt.SCHEMA,
                    options=self._options(choice), keep_alive=llm.keep_alive, timeout=llm.timeout_s,
                    num_predict=MAX_TOKENS)
            else:
                reply = self._client.chat(choice.model, prompt.messages(messages, request), schema=prompt.SCHEMA,
                                          options=self._options(choice), keep_alive=llm.keep_alive,
                                          timeout=llm.timeout_s, num_predict=MAX_TOKENS)
            self._governor.used()
        except LLMCancelled:
            return Thought(error="Cancelled", choice=choice, ms=(time.perf_counter() - began) * 1000, cancelled=True)
        except LLMError as error:
            return Thought(error=str(error), choice=choice, ms=(time.perf_counter() - began) * 1000)
        ms = (time.perf_counter() - began) * 1000
        try:
            data = json.loads(reply.text)
        except ValueError:
            _log.warning("tier 1 answer wasn't JSON: %r", reply.text[:200])
            return Thought(error="The local model's answer wasn't a plan.", choice=choice, reply=reply, ms=ms)
        return Thought(data, choice=choice, reply=reply, ms=ms)

    # --- preload --------------------------------------------------------------------------------------------------

    def warm(self, choice: Choice | None = None) -> float:
        """Load the model and read the static prefix once. Blocking; returns ms."""
        began = time.perf_counter()
        choice = choice or self._pinned or self._governor.choose(fresh=True)
        llm = self._settings()
        dialect = self._dialect(choice.model)
        _, static = self._static(self._catalog())
        if dialect.raw:
            self._background.generate(choice.model, static, options=self._options(choice), keep_alive=llm.keep_alive,
                                      timeout=_WARM_TIMEOUT_S, num_predict=1)
        else:
            self._background.load(choice.model, options=self._options(choice), keep_alive=llm.keep_alive,
                                  timeout=_WARM_TIMEOUT_S)
        self._warmed[choice] = self._clock()
        return (time.perf_counter() - began) * 1000

    def preload(self, reason: str) -> None:
        """A command may be coming (PreloadHint): warm the model in the background. Never blocks."""
        if reason not in PRELOAD_REASONS:
            return
        with self._lock:
            if self._preloading:
                return
            self._preloading = True
        threading.Thread(target=self._preload, args=(reason,), name="nova-llm-preload", daemon=True).start()

    def _preload(self, reason: str) -> None:
        try:
            choice = self._governor.choose(fresh=True)
            last = self._warmed.get(choice)
            if last is not None and self._clock() - last < WARM_AGAIN_S:
                return
            ms = self.warm(choice)
            _log.info("LLM warm (%s): %s on the %s in %.0f ms", reason, choice.model, choice.device.upper(), ms)
        except LLMError as error:
            _log.warning("LLM preload failed: %s", error)
        except Exception:  # noqa: BLE001 - a preload must never take anything down
            _log.exception("LLM preload failed")
        finally:
            with self._lock:
                self._preloading = False

    def abort(self) -> None:
        self._client.abort()

    def close(self) -> None:
        self._governor.close()
