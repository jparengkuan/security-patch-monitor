from datetime import date

import pytest

from patch_monitor.models import Severity
from patch_monitor.text import (
    WEDNESDAY,
    dates_in_text,
    next_weekday,
    parse_iso_date,
    parse_rfc822_date,
    severity_from_text,
    strip_tags,
    truncate,
)


def test_strip_tags_keeps_escaped_version_constraints() -> None:
    assert strip_tags("<p>Affected versions:&nbsp;&lt;10.4.9 || &gt;=11.0.0</p>") == (
        "Affected versions: <10.4.9 || >=11.0.0"
    )


def test_dates_in_text_finds_all_written_dates() -> None:
    text = "Critical PHP update on Wednesday, October 21, 2026 and November 4 2026; bogus February 30, 2026"
    assert dates_in_text(text) == [date(2026, 10, 21), date(2026, 11, 4)]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Highly critical", Severity.CRITICAL),
        ("Critical", Severity.CRITICAL),
        ("Moderately critical", Severity.MODERATE),
        ("Less critical", Severity.LOW),
        ("Not critical", Severity.LOW),
        ("high", Severity.HIGH),
        ("", Severity.INFO),
    ],
)
def test_severity_from_text(text: str, expected: Severity) -> None:
    assert severity_from_text(text) is expected


def test_next_weekday_includes_the_start_day() -> None:
    assert next_weekday(date(2026, 10, 21), WEDNESDAY) == date(2026, 10, 21)
    assert next_weekday(date(2026, 10, 23), WEDNESDAY) == date(2026, 10, 28)


def test_date_parsers_are_lenient() -> None:
    assert parse_rfc822_date("Wed, 14 Oct 2026 17:25:38 +0000") == date(2026, 10, 14)
    assert parse_rfc822_date("garbage") is None
    assert parse_iso_date("2026-10-22T16:42:31Z") == date(2026, 10, 22)
    assert parse_iso_date(False) is None


def test_truncate() -> None:
    assert truncate("abc", 5) == "abc"
    assert truncate("abcdefgh", 5) == "abcd…"


def test_severity_parse_normalises_github_values() -> None:
    assert Severity.parse("MEDIUM") is Severity.MODERATE
    assert Severity.parse("unknown") is Severity.INFO
