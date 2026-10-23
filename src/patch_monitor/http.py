"""HTTP access for the data sources.

Sources depend on the small :class:`HttpClient` protocol, so they can be tested with an
in-memory fake and the transport can be swapped without touching them.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Protocol

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 20.0
DEFAULT_MAX_BYTES = 10 * 1024 * 1024
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class HttpError(RuntimeError):
    """Raised when a resource cannot be fetched."""


class HttpClient(Protocol):
    def get_bytes(self, url: str) -> bytes: ...

    def get_json(self, url: str) -> Any: ...


class UrllibHttpClient:
    """Standard-library HTTP client with timeouts, size limits and retries with backoff."""

    def __init__(
        self,
        user_agent: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        retries: int = 2,
        backoff: float = 1.0,
        max_bytes: int = DEFAULT_MAX_BYTES,
        github_token: str | None = None,
    ) -> None:
        self._user_agent = user_agent
        self._timeout = timeout
        self._retries = retries
        self._backoff = backoff
        self._max_bytes = max_bytes
        self._github_token = github_token

    def get_json(self, url: str) -> Any:
        try:
            return json.loads(self.get_bytes(url))
        except json.JSONDecodeError as exc:
            raise HttpError(f"Invalid JSON from {url}: {exc}") from exc

    def get_bytes(self, url: str) -> bytes:
        if not url.startswith("https://"):
            raise HttpError(f"Refusing non-HTTPS URL: {url}")
        request = urllib.request.Request(url, headers=self._headers(url))
        for attempt in range(self._retries + 1):
            try:
                return self._read(request)
            except urllib.error.HTTPError as exc:
                if exc.code not in _RETRYABLE_STATUS or attempt == self._retries:
                    raise HttpError(f"HTTP {exc.code} for {url}") from exc
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                if attempt == self._retries:
                    raise HttpError(f"Cannot reach {url}: {exc}") from exc
            delay = self._backoff * 2**attempt
            log.debug("Retrying %s in %.1fs (attempt %d)", url, delay, attempt + 1)
            time.sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover

    def _read(self, request: urllib.request.Request) -> bytes:
        with urllib.request.urlopen(request, timeout=self._timeout) as response:
            body: bytes = response.read(self._max_bytes + 1)
        if len(body) > self._max_bytes:
            raise HttpError(f"Response from {request.full_url} exceeds {self._max_bytes} bytes")
        return body

    def _headers(self, url: str) -> dict[str, str]:
        headers = {"User-Agent": self._user_agent}
        if url.startswith("https://api.github.com/"):
            headers["Accept"] = "application/vnd.github+json"
            if self._github_token:
                headers["Authorization"] = f"Bearer {self._github_token}"
        return headers


class MemoizingHttpClient:
    """Decorator that fetches every URL at most once (used for the duration of one check)."""

    def __init__(self, inner: HttpClient) -> None:
        self._inner = inner
        self._cache: dict[str, bytes] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def get_bytes(self, url: str) -> bytes:
        with self._guard:
            lock = self._locks.setdefault(url, threading.Lock())
        with lock:
            if url not in self._cache:
                self._cache[url] = self._inner.get_bytes(url)
            return self._cache[url]

    def get_json(self, url: str) -> Any:
        try:
            return json.loads(self.get_bytes(url))
        except json.JSONDecodeError as exc:
            raise HttpError(f"Invalid JSON from {url}: {exc}") from exc
