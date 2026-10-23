"""Next.js advisories from the GitHub Advisory Database (Next.js does not pre-announce)."""

from __future__ import annotations

from typing import Any

from patch_monitor.models import Item, Kind, Product, Severity
from patch_monitor.sources.base import SourceContext
from patch_monitor.text import parse_iso_date

API_URL = "https://api.github.com/advisories?ecosystem=npm&affects=next&per_page=50&sort=published&direction=desc"


class NextJsAdvisorySource:
    name = "Next.js advisories (GitHub)"

    def collect(self, ctx: SourceContext) -> list[Item]:
        data = ctx.http.get_json(API_URL)
        if not isinstance(data, list):
            raise ValueError("Unexpected GitHub advisories response")
        items = []
        for advisory in data:
            published = parse_iso_date(advisory.get("published_at"))
            if published is None or published < ctx.history_start or advisory.get("withdrawn_at"):
                continue
            ranges, patched = self._next_versions(advisory)
            identifier = advisory.get("cve_id") or advisory.get("ghsa_id")
            items.append(
                Item(
                    product=Product.NEXTJS,
                    kind=Kind.ADVISORY,
                    title=f"{advisory.get('summary', 'Next.js advisory')} ({identifier})",
                    date=published,
                    url=advisory.get("html_url", ""),
                    severity=Severity.parse(advisory.get("severity")),
                    detail=f"Affected: {' | '.join(ranges) or '?'} · "
                    f"patched in: {', '.join(patched) or 'no patch yet!'}",
                )
            )
        return items

    @staticmethod
    def _next_versions(advisory: dict[str, Any]) -> tuple[list[str], list[str]]:
        ranges, patched = [], []
        for vuln in advisory.get("vulnerabilities") or []:
            if (vuln.get("package") or {}).get("name") != "next":
                continue
            if vuln.get("vulnerable_version_range"):
                ranges.append(vuln["vulnerable_version_range"])
            if vuln.get("first_patched_version"):
                patched.append(vuln["first_patched_version"])
        return ranges, patched
