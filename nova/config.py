"""Settings: config/settings.yaml -> frozen dataclasses.

Unknown keys and wrong types are errors (with a "did you mean"), so typos never
silently do nothing. ``ConfigStore.watch()`` polls the file once a second; a
broken save keeps the last good settings and publishes ``ConfigInvalid``, a good
one publishes ``ConfigChanged`` when any value changed.
"""

from __future__ import annotations

import dataclasses
import difflib
import ipaddress
import logging
import threading
import typing
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .bus import Bus
from .events import ConfigChanged, ConfigInvalid
from .keys import parse_chord

_log = logging.getLogger(__name__)


class ConfigError(ValueError):
    """settings.yaml content that Nova cannot use. The message is shown to Bhavya as-is."""


@dataclass(frozen=True)
class Triggers:
    copilot_key: bool = True
    wake_name: bool = True
    always_listening: bool = True
    chord: bool = True


@dataclass(frozen=True)
class Ollama:
    url: str = "http://127.0.0.1:11434"


@dataclass(frozen=True)
class Logs:
    transcripts: bool = True


@dataclass(frozen=True)
class Settings:
    wake_name: str = "Nova"
    chord: str = "ctrl+alt+n"
    triggers: Triggers = field(default_factory=Triggers)
    ollama: Ollama = field(default_factory=Ollama)
    logs: Logs = field(default_factory=Logs)


DEFAULT_YAML = """\
# Nova settings. Save the file and Nova picks the change up within a second.
# If something here is wrong, Nova keeps the last good settings and tells you why.

# Say this name to talk to Nova ("Nova, open Chrome").
wake_name: Nova

# Fallback chord: same hold / tap behaviour as the Copilot key.
chord: ctrl+alt+n

# Ways to start talking. Set any of them to false to turn it off.
triggers:
  copilot_key: true        # hold = push-to-talk, tap = listen until you stop
  wake_name: true          # "Nova, ..."
  always_listening: true   # plain commands like "open Chrome" with no name
  chord: true              # the chord above

# Local LLM runtime. Must be on this laptop.
ollama:
  url: http://127.0.0.1:11434

logs:
  transcripts: true        # false = logs keep timings but not what you said
"""


# --- parsing -----------------------------------------------------------------


class _Loader(yaml.SafeLoader):
    """Safe YAML that rejects a key written twice instead of keeping the last one."""

    def construct_mapping(self, node, deep=False):
        seen = set()
        for key_node, _value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in seen
            except TypeError:  # unhashable key: let PyYAML report it
                continue
            if duplicate:
                raise ConfigError(f"{key} appears twice (line {key_node.start_mark.line + 1})")
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def _yaml_message(error: yaml.YAMLError) -> str:
    if not isinstance(error, yaml.MarkedYAMLError):
        return f"not valid YAML: {error}"
    parts = []
    if error.context and error.context_mark is not None:
        parts.append(f"{error.context} on line {error.context_mark.line + 1}")
    if error.problem and error.problem_mark is not None:
        parts.append(f"{error.problem} on line {error.problem_mark.line + 1}")
    return "not valid YAML: " + ("; ".join(parts) or str(error))


def load_text(text: str) -> Settings:
    """Parse and validate settings.yaml text. Raises ConfigError."""
    try:
        data = yaml.load(text, Loader=_Loader)
    except yaml.YAMLError as error:
        raise ConfigError(_yaml_message(error)) from error
    except ConfigError:
        raise
    except (ValueError, TypeError) as error:  # e.g. an impossible date like 2026-02-30
        raise ConfigError(f"not valid YAML: {error}") from error
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError("settings.yaml must be a mapping of `name: value` lines")
    settings = build(Settings, data)
    _check(settings)
    return settings


T = typing.TypeVar("T")


def build(cls: type[T], data: dict[str, Any], prefix: str = "") -> T:
    """Build frozen dataclass ``cls`` from a mapping; missing keys keep their defaults."""
    fields = dataclasses.fields(cls)
    names = [f.name for f in fields]
    for key in data:
        if key not in names:
            close = difflib.get_close_matches(str(key), names, n=1)
            hint = f"; did you mean {prefix}{close[0]}?" if close else f" (known: {', '.join(names)})"
            raise ConfigError(f"unknown setting {prefix}{key}{hint}")
    hints = typing.get_type_hints(cls)
    values = {f.name: _value(hints[f.name], data[f.name], prefix + f.name) for f in fields if f.name in data}
    return cls(**values)


def _value(kind: Any, value: Any, path: str) -> Any:
    if dataclasses.is_dataclass(kind):
        if value is None:  # every line under the header commented out: use the defaults
            value = {}
        if not isinstance(value, dict):
            raise ConfigError(f"{path} must be a section (indented `name: value` lines), not {value!r}")
        return build(kind, value, path + ".")
    if kind is bool:
        if not isinstance(value, bool):
            raise ConfigError(f"{path} must be true or false, not {value!r}")
        return value
    if kind is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{path} must be a whole number, not {value!r}")
        return value
    if kind is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{path} must be a number, not {value!r}")
        return float(value)
    if kind is str:
        if not isinstance(value, str):
            raise ConfigError(f"{path} must be text, not {value!r}")
        return value
    raise TypeError(f"setting {path} has unsupported type {kind!r}")


def _check(settings: Settings) -> None:
    """Rules beyond types."""
    if not settings.wake_name.strip():
        raise ConfigError("wake_name must not be empty")
    try:
        parse_chord(settings.chord)
    except ValueError as error:
        raise ConfigError(f"chord: {error}") from error
    _check_local_url(settings.ollama.url, "ollama.url")


def _check_local_url(url: str, path: str) -> None:
    try:
        parts = urllib.parse.urlsplit(url)
        parts.port  # noqa: B018 - raises ValueError on a bad port
    except ValueError as error:
        raise ConfigError(f"{path} must look like http://127.0.0.1:11434 ({error})") from error
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ConfigError(f"{path} must look like http://127.0.0.1:11434, not {url!r}")
    host = parts.hostname
    if host == "localhost":
        # Windows resolves localhost to ::1 first; Ollama listens on 127.0.0.1, so each connection waits ~2 s.
        raise ConfigError(f"{path}: write 127.0.0.1 instead of localhost (localhost costs ~2 s per connection)")
    try:
        local = ipaddress.ip_address(host).is_loopback
    except ValueError:
        local = False
    if not local:
        raise ConfigError(f"{path} must point at this laptop (127.0.0.1), not {host!r}")


# --- the store -----------------------------------------------------------------


class ConfigStore:
    """Holds the current Settings (an immutable snapshot) and reloads them from disk."""

    def __init__(self, path: Path, bus: Bus | None = None) -> None:
        self.path = Path(path)
        self._bus = bus
        self._settings = Settings()
        self._signature: tuple[int, int] | None = None
        self._lock = threading.Lock()
        self._loaded_once = False  # until a good load, the "last good" settings are the defaults
        self.error: str | None = None

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def loaded_once(self) -> bool:
        """True once settings.yaml has loaded cleanly; before that the settings in use are the defaults."""
        return self._loaded_once

    def load(self) -> Settings:
        """Read settings.yaml, first writing the commented defaults if it doesn't exist."""
        try:
            with self.path.open("x", encoding="utf-8") as file:
                file.write(DEFAULT_YAML)
            _log.info("created %s with defaults", self.path)
        except FileExistsError:
            pass
        except FileNotFoundError:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            return self.load()
        return self._read()

    def reload_if_changed(self) -> bool:
        """Re-read the file if its mtime or size changed. Returns True if it was re-read."""
        try:
            signature = self._stat()
        except OSError:
            # Deleted, or mid-save by an editor that renames: keep what we have.
            return False
        if signature == self._signature:
            return False
        self._read()
        return True

    def watch(self, interval: float = 1.0) -> Callable[[], None]:
        """Poll for saves on a daemon thread. Returns a stop function."""
        stop = threading.Event()

        def loop() -> None:
            while not stop.wait(interval):
                try:
                    self.reload_if_changed()
                except Exception:
                    _log.exception("settings watcher failed")

        thread = threading.Thread(target=loop, name="nova-settings-watch", daemon=True)
        thread.start()

        def stop_watching() -> None:
            stop.set()
            if thread is not threading.current_thread():
                thread.join(timeout=2)

        return stop_watching

    def _stat(self) -> tuple[int, int]:
        stat = self.path.stat()
        return stat.st_mtime_ns, stat.st_size

    def _read(self) -> Settings:
        event: ConfigChanged | ConfigInvalid | None = None
        with self._lock:
            try:
                signature = self._stat()
                raw = self.path.read_bytes()
            except OSError as error:
                # Often an editor holding the file mid-save; the next poll retries.
                message = f"{self.path.name}: cannot read it ({error.strerror or error})"
                if message != self.error:
                    _log.warning("%s; keeping last good settings", message)
                self.error = message
                return self._settings
            self._signature = signature
            try:
                try:
                    text = raw.decode("utf-8-sig")
                except UnicodeDecodeError as error:
                    raise ConfigError(f"save it as UTF-8 ({error.reason} at byte {error.start})") from error
                settings = load_text(text)
            except ConfigError as error:
                self.error = f"{self.path.name}: {error}"
                using_defaults = not self._loaded_once
                _log.warning("%s; using %s settings", self.error, "default" if using_defaults else "last good")
                event = ConfigInvalid(str(self.path), self.error, using_defaults)
            else:
                # Also announce a fix that restores the same values, so the UI can clear its warning.
                if settings != self._settings or self.error is not None:
                    event = ConfigChanged(settings)
                self._settings = settings
                self._loaded_once = True
                self.error = None
        if event is not None and self._bus is not None:
            self._bus.publish(event)
        return self._settings
