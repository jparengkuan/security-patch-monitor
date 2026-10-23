"""End-of-life data from endoflife.date, shared by the EOL and PHP release sources."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from patch_monitor.http import HttpClient
from patch_monitor.models import Item, Kind, Product, Severity
from patch_monitor.sources.base import SourceContext
from patch_monitor.text import parse_iso_date

API_URL = "https://endoflife.date/api/{slug}.json"
PAGE_URL = "https://endoflife.date/{slug}"

PRODUCT_SLUGS = {
    Product.DRUPAL: "drupal",
    Product.PHP: "php",
    Product.NEXTJS: "nextjs",
    Product.NODE: "nodejs",
}


@dataclass(frozen=True, slots=True)
class Cycle:
    """One release line, e.g. PHP 8.4 or Node.js 22."""

    name: str
    eol: date | None
    support_until: date | None
    actively_supported: bool
    latest: str

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> Cycle:
        support = raw.get("support")
        return cls(
            name=str(raw.get("cycle", "?")),
            eol=parse_iso_date(raw.get("eol")),
            support_until=parse_iso_date(support),
            actively_supported=support is True,
            latest=str(raw.get("latest", "?")),
        )

    def is_actively_supported(self, today: date) -> bool:
        return self.actively_supported or (self.support_until is not None and self.support_until >= today)


def fetch_cycles(http: HttpClient, product: Product) -> list[Cycle]:
    data = http.get_json(API_URL.format(slug=PRODUCT_SLUGS[product]))
    if not isinstance(data, list):
        raise ValueError(f"Unexpected endoflife.date response for {product.label}")
    return [Cycle.from_api(raw) for raw in data if isinstance(raw, dict)]


class EndOfLifeSource:
    """End of security support per version line: after this date no more patches are released."""

    name = "End of life (endoflife.date)"

    def __init__(self, products: tuple[Product, ...] = tuple(PRODUCT_SLUGS)) -> None:
        self._products = products

    def collect(self, ctx: SourceContext) -> list[Item]:
        items = []
        for product in self._products:
            for cycle in fetch_cycles(ctx.http, product):
                if cycle.eol is None:
                    continue
                days_left = (cycle.eol - ctx.today).days
                if not -ctx.settings.history_days <= days_left <= ctx.settings.eol_warning_days:
                    continue
                items.append(self._item(product, cycle, cycle.eol, days_left))
        return items

    @staticmethod
    def _item(product: Product, cycle: Cycle, eol: date, days_left: int) -> Item:
        name = f"{product.label} {cycle.name}"
        if days_left < 0:
            title, severity = f"{name} is end-of-life", Severity.INFO
        else:
            title = f"{name}: end of security support"
            severity = Severity.HIGH if days_left <= 90 else Severity.MODERATE if days_left <= 180 else Severity.INFO
        return Item(
            product=product,
            kind=Kind.EOL,
            title=title,
            date=eol,
            url=PAGE_URL.format(slug=PRODUCT_SLUGS[product]),
            severity=severity,
            detail=f"After {eol.isoformat()} there are no more security patches for {name}. "
            f"Latest version in this line: {cycle.latest}.",
        )
