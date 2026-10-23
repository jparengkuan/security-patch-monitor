"""Pure helpers for extracting dates, severities and plain text from feed content."""

from __future__ import annotations

import html
import re
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime

from patch_monitor.models import Severity

_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
_DATE_IN_TEXT = re.compile(r"\b(" + "|".join(_MONTHS) + r")\s+(\d{1,2}),?\s+(\d{4})")
_TAG = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"\s+")

WEDNESDAY = 2


def strip_tags(markup: str | None) -> str:
    """Convert an HTML fragment to collapsed plain text.

    Tags are removed *before* unescaping, so an escaped ``&lt;10.4.9`` survives as ``<10.4.9``.
    """
    text = _TAG.sub(" ", markup or "")
    return _WHITESPACE.sub(" ", html.unescape(text).replace("\xa0", " ")).strip()


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def dates_in_text(text: str | None) -> list[date]:
    """Find dates written like ``October 21, 2026`` in free text."""
    found: list[date] = []
    for month, day, year in _DATE_IN_TEXT.findall(text or ""):
        try:
            found.append(date(int(year), _MONTHS.index(month) + 1, int(day)))
        except ValueError:
            continue
    return found


def parse_rfc822_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).date()
    except (TypeError, ValueError):
        return None


def parse_iso_date(value: object) -> date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def severity_from_text(text: str | None) -> Severity:
    """Map Drupal-style risk labels ("Highly critical", "Moderately critical", ...) to a severity."""
    t = (text or "").lower()
    if "highly critical" in t:
        return Severity.CRITICAL
    if "moderately critical" in t:
        return Severity.MODERATE
    if "less critical" in t or "not critical" in t:
        return Severity.LOW
    if "critical" in t:
        return Severity.CRITICAL
    if "moderate" in t:
        return Severity.MODERATE
    if "high" in t:
        return Severity.HIGH
    if "low" in t:
        return Severity.LOW
    return Severity.INFO


def next_weekday(start: date, weekday: int) -> date:
    """The first date on or after ``start`` that falls on ``weekday`` (Monday = 0)."""
    return start + timedelta(days=(weekday - start.weekday()) % 7)
