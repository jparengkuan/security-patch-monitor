"""Remembers which items the user has already been notified about."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from datetime import date, timedelta
from pathlib import Path
from typing import Protocol

from patch_monitor.storage import atomic_write_text

log = logging.getLogger(__name__)

RETENTION = timedelta(days=365)


class SeenStore(Protocol):
    def is_initialized(self) -> bool: ...

    def unseen(self, keys: Iterable[str]) -> set[str]: ...

    def remember(self, keys: Iterable[str], today: date) -> None: ...


class JsonSeenStore:
    """Stores ``key -> last seen date`` and forgets keys that have not appeared for a year.

    Keeping keys after they drop out of a report prevents a repeat notification when a
    source fails once and its items reappear on the next check.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._entries: dict[str, str] | None = None

    def is_initialized(self) -> bool:
        return self._load() is not None

    def unseen(self, keys: Iterable[str]) -> set[str]:
        known = self._load() or {}
        return {key for key in keys if key not in known}

    def remember(self, keys: Iterable[str], today: date) -> None:
        entries = dict(self._load() or {})
        stamp = today.isoformat()
        entries.update(dict.fromkeys(keys, stamp))
        cutoff = (today - RETENTION).isoformat()
        entries = {key: seen for key, seen in entries.items() if seen >= cutoff}
        atomic_write_text(self._path, json.dumps(entries, indent=0, sort_keys=True))
        self._entries = entries

    def _load(self) -> dict[str, str] | None:
        if self._entries is None:
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return None
            except (OSError, json.JSONDecodeError) as exc:
                log.warning("Cannot read %s (%s); starting fresh", self._path, exc)
                return None
            self._entries = {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
        return self._entries
