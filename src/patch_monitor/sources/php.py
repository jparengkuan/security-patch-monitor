"""PHP: security announcements from php.net and estimates of the next regular releases."""

from __future__ import annotations

from datetime import date, timedelta

from patch_monitor.feeds import ATOM_NS, parse_atom, parse_xml
from patch_monitor.models import Item, Kind, Product, Severity
from patch_monitor.sources.base import SourceContext
from patch_monitor.sources.endoflife import fetch_cycles
from patch_monitor.text import dates_in_text, parse_iso_date, severity_from_text, strip_tags, truncate

NEWS_FEED = "https://www.php.net/feed.atom"
RELEASES_FEED = "https://www.php.net/releases/feed.php"
RELEASES_URL = "https://www.php.net/releases/"
RELEASES_NS = "http://php.net/ns/releases"

RELEASE_CADENCE = timedelta(days=28)
_SECURITY_MARKERS = ("security", "vulnerab", "cve-")


class PhpNewsSource:
    """php.net news: pre-announced security releases and published security advisories."""

    name = "PHP news"

    def collect(self, ctx: SourceContext) -> list[Item]:
        items = []
        for entry in parse_atom(ctx.http.get_bytes(NEWS_FEED)):
            content = strip_tags(entry.content)
            blob = f"{entry.title} {content}"
            lowered = blob.lower()
            if not any(marker in lowered for marker in _SECURITY_MARKERS):
                continue
            critical = "critical" in lowered
            future = [d for d in dates_in_text(blob) if d >= ctx.today]
            if future:
                kind, when = Kind.UPCOMING, min(future)
                severity = severity_from_text(lowered) if critical else Severity.HIGH
            elif entry.published and entry.published >= ctx.history_start:
                kind, when = Kind.ADVISORY, entry.published
                severity = Severity.CRITICAL if critical else Severity.HIGH
            else:
                continue
            items.append(
                Item(
                    product=Product.PHP,
                    kind=kind,
                    title=entry.title,
                    date=when,
                    url=entry.link,
                    severity=severity,
                    detail=truncate(content, 280),
                )
            )
        return items


class PhpReleaseEstimateSource:
    """Estimates the next release per supported branch from the ~4-week release cadence."""

    name = "PHP releases"

    def collect(self, ctx: SourceContext) -> list[Item]:
        cycles = {cycle.name: cycle for cycle in fetch_cycles(ctx.http, Product.PHP)}
        items = []
        for branch, (version, released) in sorted(self._latest_per_branch(ctx).items(), reverse=True):
            cycle = cycles.get(branch)
            if cycle is None or (cycle.eol is not None and cycle.eol < ctx.today):
                continue
            estimate = self._next_after(released, ctx.today)
            cadence = (
                "Active support: releases roughly every 4 weeks (often including security fixes)."
                if cycle.is_actively_supported(ctx.today)
                else "Security support only: releases only when needed."
            )
            items.append(
                Item(
                    product=Product.PHP,
                    kind=Kind.EXPECTED,
                    title=f"PHP {branch}: next release (estimate)",
                    date=estimate,
                    url=RELEASES_URL,
                    detail=f"Latest release {version} on {released.isoformat()}. {cadence}",
                )
            )
        return items

    @staticmethod
    def _next_after(released: date, today: date) -> date:
        estimate = released + RELEASE_CADENCE
        while estimate < today:
            estimate += RELEASE_CADENCE
        return estimate

    @staticmethod
    def _latest_per_branch(ctx: SourceContext) -> dict[str, tuple[str, date]]:
        ns = {"a": ATOM_NS, "php": RELEASES_NS}
        latest: dict[str, tuple[str, date]] = {}
        for entry in parse_xml(ctx.http.get_bytes(RELEASES_FEED)).findall("a:entry", ns):
            version = entry.findtext("php:version", "", ns)
            released = parse_iso_date(entry.findtext("a:published", "", ns))
            if not version or released is None:
                continue
            branch = ".".join(version.split(".")[:2])
            if branch not in latest or released > latest[branch][1]:
                latest[branch] = (version, released)
        return latest
