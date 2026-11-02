"""Bus events shared across Nova. Frozen dataclasses only."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Settings


@dataclass(frozen=True)
class ConfigChanged:
    """settings.yaml loaded with new values."""

    settings: Settings


@dataclass(frozen=True)
class ConfigInvalid:
    """settings.yaml could not be used; Nova keeps the last good settings (the defaults if none loaded yet)."""

    path: str
    error: str
    using_defaults: bool = False


@dataclass(frozen=True)
class PowerChanged:
    """Tray On/Off. Off will close the mic once there is one (Stage 01)."""

    on: bool
