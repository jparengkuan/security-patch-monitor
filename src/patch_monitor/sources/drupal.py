"""Drupal security team feeds: PSAs (pre-announcements), core and contrib advisories."""

from __future__ import annotations

import re
from dataclasses import dataclass

from patch_monitor.feeds import parse_rss
from patch_monitor.models import Item, Kind, Product, Severity
from patch_monitor.sources.base import SourceContext
from patch_monitor.text import WEDNESDAY, dates_in_text, next_weekday, severity_from_text, strip_tags

PSA_FEED = "https://www.drupal.org/security/psa/rss.xml"
CORE_FEED = "https://www.drupal.org/security/rss.xml"
CONTRIB_FEED = "https://www.drupal.org/security/contrib/rss.xml"
RELEASE_TIMING_URL = "https://www.drupal.org/drupal-security-team/security-release-numbers-and-release-timing"

_FIELD_LABELS = (
    "Project",
    "Project machine name",
    "Date",
    "Security risk",
    "Vulnerability",
    "Affected versions",
    "CVE IDs",
    "Description",
    "Solution",
    "Install versions",
)


def advisory_field(text: str, label: str) -> str:
    """Read a ``Label: value`` field from the plain text of a Drupal advisory."""
    others = "|".join(re.escape(lbl) for lbl in _FIELD_LABELS if lbl != label)
    match = re.search(rf"\b{re.escape(label)}:\s*(.+?)\s*(?:\b(?:{others}):|$)", text)
    return match.group(1).strip() if match else ""


class DrupalPsaSource:
    """Public service announcements, used to pre-announce (highly) critical releases."""

    name = "Drupal PSAs"

    def collect(self, ctx: SourceContext) -> list[Item]:
        items = []
        for entry in parse_rss(ctx.http.get_bytes(PSA_FEED)):
            dates = dates_in_text(entry.title) or dates_in_text(strip_tags(entry.content))
            target = max(dates) if dates else entry.published
            if target is None or target < ctx.history_start:
                continue
            upcoming = target >= ctx.today
            critical = "critical" in entry.title.lower()
            items.append(
                Item(
                    product=Product.DRUPAL,
                    kind=Kind.UPCOMING if upcoming else Kind.ADVISORY,
                    title=entry.title,
                    date=target,
                    url=entry.link,
                    severity=severity_from_text(entry.title) if critical else Severity.INFO,
                    detail="Public Service Announcement (pre-announcement)"
                    + ("" if upcoming else " – date has passed"),
                )
            )
        return items


@dataclass(frozen=True, slots=True)
class DrupalAdvisorySource:
    """Published security advisories for core or contributed projects."""

    name: str
    feed_url: str
    scope: str  # "Core" or "Contrib"

    @classmethod
    def core(cls) -> DrupalAdvisorySource:
        return cls("Drupal core advisories", CORE_FEED, "Core")

    @classmethod
    def contrib(cls) -> DrupalAdvisorySource:
        return cls("Drupal contrib advisories", CONTRIB_FEED, "Contrib")

    def collect(self, ctx: SourceContext) -> list[Item]:
        items = []
        for entry in parse_rss(ctx.http.get_bytes(self.feed_url)):
            if entry.published is None or entry.published < ctx.history_start:
                continue
            text = strip_tags(entry.content)
            affected = advisory_field(text, "Affected versions") or "see advisory"
            # Titles look like "Project - Moderately critical - Access bypass - SA-CONTRIB-2026-001"
            parts = [part.strip() for part in entry.title.split(" - ")]
            risk = parts[1] if len(parts) > 1 else entry.title
            prefix = self.scope
            if self.scope != "Core":
                machine_name = advisory_field(text, "Project machine name").split(" ")[0] or "?"
                prefix = f"{self.scope} · {machine_name}"
            items.append(
                Item(
                    product=Product.DRUPAL,
                    kind=Kind.ADVISORY,
                    title=entry.title,
                    date=entry.published,
                    url=entry.link,
                    severity=severity_from_text(risk),
                    detail=f"{prefix} · affected versions: {affected}",
                )
            )
        return items


class DrupalReleaseWindowSource:
    """Drupal publishes security releases on Wednesdays between 16:00 and 22:00 UTC."""

    name = "Drupal release window"

    def collect(self, ctx: SourceContext) -> list[Item]:
        return [
            Item(
                product=Product.DRUPAL,
                kind=Kind.EXPECTED,
                title="Drupal security release window",
                date=next_weekday(ctx.today, WEDNESDAY),
                url=RELEASE_TIMING_URL,
                detail="Core and contrib security releases are published on Wednesdays "
                "(16:00–22:00 UTC). A (critical) release is only certain when a PSA has been published.",
            )
        ]
