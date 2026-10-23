from __future__ import annotations

from datetime import date

import pytest

from conftest import TODAY, FakeHttp, rss, rss_item
from patch_monitor.models import Kind, Product, Severity
from patch_monitor.sources import SourceContext
from patch_monitor.sources.drupal import (
    CONTRIB_FEED,
    CORE_FEED,
    PSA_FEED,
    DrupalAdvisorySource,
    DrupalPsaSource,
    DrupalReleaseWindowSource,
    advisory_field,
)
from patch_monitor.sources.endoflife import API_URL, EndOfLifeSource
from patch_monitor.sources.nextjs import API_URL as NEXT_URL
from patch_monitor.sources.nextjs import NextJsAdvisorySource
from patch_monitor.sources.node import FEED as NODE_FEED
from patch_monitor.sources.node import NodeSecuritySource
from patch_monitor.sources.php import NEWS_FEED, RELEASES_FEED, PhpNewsSource, PhpReleaseEstimateSource

CONTRIB_DESCRIPTION = (
    '<div>Project:&nbsp;<a href="/project/monster_menus">Monster Menus</a></div>'
    "<div>Project machine name:&nbsp;<code>monster_menus</code></div>"
    "<div>Date:&nbsp;2026-October-21</div>"
    "<div>Security risk:&nbsp;Critical 16 ∕ 25</div>"
    "<div>Affected versions:&nbsp;&lt;9.5.6 || &gt;=10.0.0 &lt;10.0.2</div>"
    "<div>CVE IDs:&nbsp;CVE-2026-1</div>"
)


def test_advisory_field_distinguishes_similar_labels() -> None:
    text = "Project: Monster Menus Project machine name: monster_menus Date: 2026-October-21"
    assert advisory_field(text, "Project") == "Monster Menus"
    assert advisory_field(text, "Project machine name") == "monster_menus"


def test_drupal_psa_future_date_is_upcoming(ctx: SourceContext, http: FakeHttp) -> None:
    http.responses[PSA_FEED] = rss(
        rss_item(
            "Upcoming critical Drupal core release on November 4, 2026 - PSA-2026-11-02",
            "Mon, 02 Nov 2026 10:00:00 +0000",
        ),
        rss_item(
            "Upcoming highly critical release on May 20, 2026 - PSA-2026-05-18", "Mon, 18 May 2026 10:00:00 +0000"
        ),
    )
    items = DrupalPsaSource().collect(ctx)
    assert len(items) == 1  # the May PSA is outside the 60-day history window
    assert items[0].kind is Kind.UPCOMING
    assert items[0].date == date(2026, 11, 4)
    assert items[0].severity is Severity.CRITICAL


def test_drupal_contrib_advisory_parses_fields(ctx: SourceContext, http: FakeHttp) -> None:
    http.responses[CONTRIB_FEED] = rss(
        rss_item(
            "Monster Menus - Critical - PHP object injection - SA-CONTRIB-2026-253",
            "Wed, 21 Oct 2026 17:00:00 +0000",
            description=CONTRIB_DESCRIPTION,
        )
    )
    [item] = DrupalAdvisorySource.contrib().collect(ctx)
    assert item.product is Product.DRUPAL
    assert item.severity is Severity.CRITICAL
    assert "monster_menus" in item.detail
    assert "<9.5.6 || >=10.0.0 <10.0.2" in item.detail


def test_drupal_core_advisory_has_package_and_range(ctx: SourceContext, http: FakeHttp) -> None:
    http.responses[CORE_FEED] = rss(
        rss_item(
            "Drupal core - Critical - Remote code execution - SA-CORE-2026-014",
            "Wed, 14 Oct 2026 16:10:00 +0000",
            description="<div>Affected versions:&nbsp;&gt;= 11.0.0 &lt; 11.3.19 || &gt;= 11.4.0 &lt; 11.4.10</div>"
            "<div>CVE IDs:&nbsp;CVE-2026-2</div>",
        )
    )
    [item] = DrupalAdvisorySource.core().collect(ctx)
    assert (item.package, item.affected) == ("drupal/core", ">= 11.0.0 < 11.3.19 || >= 11.4.0 < 11.4.10")


def test_drupal_release_window_is_next_wednesday(ctx: SourceContext) -> None:
    [item] = DrupalReleaseWindowSource().collect(ctx)
    assert item.kind is Kind.EXPECTED
    assert item.date == date(2026, 10, 28)


def test_node_announcement_uses_date_from_title(ctx: SourceContext, http: FakeHttp) -> None:
    http.responses[NODE_FEED] = rss(
        rss_item("Tuesday, November 10, 2026 Security Releases", "Tue, 03 Nov 2026 10:00:00 GMT"),
        rss_item("Wednesday, October 21, 2026 Security Releases", "Wed, 21 Oct 2026 22:00:00 GMT"),
    )
    upcoming, published = NodeSecuritySource().collect(ctx)
    assert (upcoming.kind, upcoming.date) == (Kind.UPCOMING, date(2026, 11, 10))
    assert (published.kind, published.date) == (Kind.ADVISORY, date(2026, 10, 21))


def test_php_news_detects_preannouncement(ctx: SourceContext, http: FakeHttp) -> None:
    http.responses[NEWS_FEED] = """<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry><title>Upcoming critical security release</title>
        <published>2026-10-22T00:00:00+00:00</published>
        <link rel="alternate" href="https://www.php.net/a"/>
        <content type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml">
          <p>A <strong>critical</strong> security release is planned for Thursday, October 29, 2026.</p>
        </div></content></entry>
      <entry><title>PHP 8.6.0 Alpha 1 available for testing</title>
        <published>2026-10-20T00:00:00+00:00</published>
        <content type="html">Nothing to see</content></entry>
    </feed>"""
    [item] = PhpNewsSource().collect(ctx)
    assert item.kind is Kind.UPCOMING
    assert item.date == date(2026, 10, 29)
    assert item.severity is Severity.CRITICAL
    assert item.url == "https://www.php.net/a"
    assert "planned for Thursday" in item.detail


def test_php_release_estimate_skips_eol_branches(ctx: SourceContext, http: FakeHttp) -> None:
    http.responses[API_URL.format(slug="php")] = [
        {"cycle": "8.4", "eol": "2028-12-31", "support": "2026-12-31", "latest": "8.4.27"},
        {"cycle": "8.0", "eol": "2023-11-26", "support": "2022-11-26", "latest": "8.0.30"},
    ]
    http.responses[RELEASES_FEED] = """<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom" xmlns:php="http://php.net/ns/releases">
      <entry><php:version>8.4.27</php:version><published>2026-10-21T00:00:00+00:00</published></entry>
      <entry><php:version>8.4.26</php:version><published>2026-09-23T00:00:00+00:00</published></entry>
      <entry><php:version>8.0.30</php:version><published>2023-08-03T00:00:00+00:00</published></entry>
    </feed>"""
    [item] = PhpReleaseEstimateSource().collect(ctx)
    assert item.title.startswith("PHP 8.4")
    assert item.date == date(2026, 11, 18)
    assert "Active support" in item.detail


def test_nextjs_advisories(ctx: SourceContext, http: FakeHttp) -> None:
    http.responses[NEXT_URL] = [
        {
            "summary": "RCE in image optimizer",
            "cve_id": "CVE-2026-1",
            "ghsa_id": "GHSA-x",
            "severity": "critical",
            "published_at": "2026-10-22T16:42:31Z",
            "withdrawn_at": None,
            "html_url": "https://github.com/advisories/GHSA-x",
            "vulnerabilities": [
                {
                    "package": {"name": "next"},
                    "vulnerable_version_range": ">= 16.0.0, <= 16.4.0",
                    "first_patched_version": "16.4.1",
                },
                {"package": {"name": "other"}, "vulnerable_version_range": "<1"},
            ],
        },
        {"summary": "withdrawn", "published_at": "2026-10-20T00:00:00Z", "withdrawn_at": "2026-10-21"},
        {"summary": "old", "published_at": "2025-01-01T00:00:00Z"},
    ]
    [item] = NextJsAdvisorySource().collect(ctx)
    assert item.severity is Severity.CRITICAL
    assert item.title == "RCE in image optimizer (CVE-2026-1)"
    assert item.detail == "Affected: >= 16.0.0, <= 16.4.0 · patched in: 16.4.1"
    assert (item.package, item.affected, item.patched) == ("next", ">= 16.0.0, <= 16.4.0", ("16.4.1",))


@pytest.mark.parametrize(
    ("eol", "expected_severity", "expected_title"),
    [
        ("2026-12-16", Severity.HIGH, "Drupal 10.6: end of security support"),
        ("2027-03-01", Severity.MODERATE, "Drupal 10.6: end of security support"),
        ("2027-07-07", Severity.INFO, "Drupal 10.6: end of security support"),
        ("2026-10-01", Severity.INFO, "Drupal 10.6 is end-of-life"),
    ],
)
def test_end_of_life_severity_by_distance(
    ctx: SourceContext, http: FakeHttp, eol: str, expected_severity: Severity, expected_title: str
) -> None:
    http.responses[API_URL.format(slug="drupal")] = [{"cycle": "10.6", "eol": eol, "latest": "10.6.20"}]
    [item] = EndOfLifeSource(products=(Product.DRUPAL,)).collect(ctx)
    assert item.severity is expected_severity
    assert item.title == expected_title


def test_end_of_life_ignores_far_future_and_unknown(ctx: SourceContext, http: FakeHttp) -> None:
    http.responses[API_URL.format(slug="nodejs")] = [
        {"cycle": "26", "eol": "2029-04-30"},
        {"cycle": "27", "eol": False},
    ]
    assert EndOfLifeSource(products=(Product.NODE,)).collect(ctx) == []


def test_today_constant_is_a_friday() -> None:
    assert TODAY.weekday() == 4
