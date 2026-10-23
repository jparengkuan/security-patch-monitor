"""Node.js vulnerability blog: security releases are pre-announced about a week ahead."""

from __future__ import annotations

from patch_monitor.feeds import parse_rss
from patch_monitor.models import Item, Kind, Product, Severity
from patch_monitor.sources.base import SourceContext
from patch_monitor.text import dates_in_text

FEED = "https://nodejs.org/en/feed/vulnerability.xml"


class NodeSecuritySource:
    name = "Node.js security feed"

    def collect(self, ctx: SourceContext) -> list[Item]:
        items = []
        for entry in parse_rss(ctx.http.get_bytes(FEED)):
            # Titles carry the release date: "Wednesday, October 21, 2026 Security Releases"
            dates = dates_in_text(entry.title)
            target = dates[0] if dates else entry.published
            if target is None:
                continue
            upcoming = target >= ctx.today
            if not upcoming and target < ctx.history_start:
                continue
            is_release = "security release" in entry.title.lower()
            items.append(
                Item(
                    product=Product.NODE,
                    kind=Kind.UPCOMING if upcoming else Kind.ADVISORY,
                    title=entry.title,
                    date=target,
                    url=entry.link,
                    severity=Severity.HIGH if is_release else Severity.INFO,
                    detail="Announced security release – see link for affected release lines and highest severity."
                    if upcoming
                    else "Published security release / report.",
                )
            )
        return items
