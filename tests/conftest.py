from __future__ import annotations

import json
from datetime import date
from typing import Any

import pytest

from patch_monitor.http import HttpError
from patch_monitor.settings import Settings
from patch_monitor.sources import SourceContext

TODAY = date(2026, 10, 23)  # a Friday


class FakeHttp:
    """In-memory HttpClient: maps URLs to bytes, str or JSON-serialisable objects."""

    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[str] = []

    def get_bytes(self, url: str) -> bytes:
        self.calls.append(url)
        if url not in self.responses:
            raise HttpError(f"no fake response for {url}")
        value = self.responses[url]
        if isinstance(value, Exception):
            raise value
        if isinstance(value, bytes):
            return value
        if isinstance(value, str):
            return value.encode()
        return json.dumps(value).encode()

    def get_json(self, url: str) -> Any:
        return json.loads(self.get_bytes(url))


def rss(*items: str) -> str:
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>{"".join(items)}</channel></rss>'


def rss_item(title: str, pub_date: str, link: str = "https://example.org/x", description: str = "") -> str:
    from xml.sax.saxutils import escape

    return (
        f"<item><title>{escape(title)}</title><link>{link}</link>"
        f"<description>{escape(description)}</description><pubDate>{pub_date}</pubDate></item>"
    )


@pytest.fixture
def http() -> FakeHttp:
    return FakeHttp()


@pytest.fixture
def ctx(http: FakeHttp) -> SourceContext:
    return SourceContext(http=http, today=TODAY, settings=Settings())
