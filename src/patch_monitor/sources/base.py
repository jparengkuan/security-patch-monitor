"""The contract every data source implements."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Protocol

from patch_monitor.http import HttpClient
from patch_monitor.models import Item
from patch_monitor.settings import Settings


@dataclass(frozen=True, slots=True)
class SourceContext:
    """Everything a source needs for one check; injected so sources stay pure and testable."""

    http: HttpClient
    today: date
    settings: Settings

    @property
    def history_start(self) -> date:
        """Oldest date for which already-published items are still shown."""
        return self.today - timedelta(days=self.settings.history_days)


class Source(Protocol):
    """A provider of calendar items. Add a new feed by implementing this protocol."""

    @property
    def name(self) -> str: ...

    def collect(self, ctx: SourceContext) -> list[Item]: ...
