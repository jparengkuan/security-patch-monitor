"""Runs all sources concurrently and combines their output into one report."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Protocol

from patch_monitor.http import HttpClient, MemoizingHttpClient
from patch_monitor.models import Item, Report
from patch_monitor.settings import Settings
from patch_monitor.sources import Source, SourceContext

log = logging.getLogger(__name__)

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


class ReportProvider(Protocol):
    def build(self, settings: Settings) -> Report: ...


class ReportBuilder:
    """Collects items from every source; a failing source is reported, never fatal."""

    def __init__(
        self,
        sources: Sequence[Source],
        http: HttpClient,
        *,
        clock: Clock = utc_now,
        max_workers: int = 8,
    ) -> None:
        self._sources = list(sources)
        self._http = http
        self._clock = clock
        self._max_workers = max_workers

    def build(self, settings: Settings) -> Report:
        now = self._clock()
        # One cache per check: sources that share an endpoint fetch it only once.
        ctx = SourceContext(http=MemoizingHttpClient(self._http), today=now.date(), settings=settings)
        items: list[Item] = []
        errors: list[str] = []
        with ThreadPoolExecutor(max_workers=self._max_workers, thread_name_prefix="source") as pool:
            futures = [(source, pool.submit(source.collect, ctx)) for source in self._sources]
            for source, future in futures:
                try:
                    items.extend(future.result())
                except Exception as exc:
                    log.warning("Source %r failed: %s", source.name, exc, exc_info=log.isEnabledFor(logging.DEBUG))
                    errors.append(f"{source.name}: {exc}")
        return Report(generated_at=now, items=tuple(_dedupe(items)), errors=tuple(errors))


def _dedupe(items: list[Item]) -> list[Item]:
    unique: dict[str, Item] = {}
    for item in items:
        unique.setdefault(item.key, item)
    return list(unique.values())
