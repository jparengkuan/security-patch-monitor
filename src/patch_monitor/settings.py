"""User settings and their persistence."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Protocol

from patch_monitor.storage import atomic_write_text

log = logging.getLogger(__name__)

MIN_INTERVAL_MINUTES = 1
MAX_INTERVAL_MINUTES = 24 * 60


@dataclass(frozen=True, slots=True)
class Settings:
    interval_minutes: int = 30  # how often to check while the app is open
    notifications: bool = True  # system notification for new items
    history_days: int = 60  # how far back advisories are shown
    eol_warning_days: int = 365  # how far ahead end-of-support dates are shown

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "interval_minutes", _clamp(self.interval_minutes, MIN_INTERVAL_MINUTES, MAX_INTERVAL_MINUTES)
        )
        object.__setattr__(self, "notifications", bool(self.notifications))
        object.__setattr__(self, "history_days", _clamp(self.history_days, 1, 3650))
        object.__setattr__(self, "eol_warning_days", _clamp(self.eol_warning_days, 0, 3650))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Settings:
        """Build settings from untrusted input, ignoring unknown keys and invalid values."""
        defaults = cls()
        values: dict[str, Any] = {}
        for f in fields(cls):
            if f.name not in data:
                continue
            raw = data[f.name]
            expected = type(getattr(defaults, f.name))
            if expected is bool and isinstance(raw, bool):
                values[f.name] = raw
            elif expected is int and isinstance(raw, int | float | str) and not isinstance(raw, bool):
                try:
                    values[f.name] = int(raw)
                except ValueError:
                    log.warning("Ignoring invalid value for %s: %r", f.name, raw)
        return cls(**values)

    def merged(self, changes: dict[str, Any]) -> Settings:
        """Apply (untrusted) changes; invalid values keep their current setting."""
        return Settings.from_dict({**self.to_dict(), **changes})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


class SettingsStore(Protocol):
    def load(self) -> Settings: ...

    def save(self, settings: Settings) -> None: ...


class JsonSettingsStore:
    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> Settings:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return Settings()
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Cannot read %s (%s); using defaults", self._path, exc)
            return Settings()
        return Settings.from_dict(data) if isinstance(data, dict) else Settings()

    def save(self, settings: Settings) -> None:
        atomic_write_text(self._path, json.dumps(settings.to_dict(), indent=2))
