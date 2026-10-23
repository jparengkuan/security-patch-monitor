"""Domain model: the items shown in the calendar and the report that groups them."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any


class Product(StrEnum):
    DRUPAL = "drupal"
    PHP = "php"
    NEXTJS = "nextjs"
    NODE = "node"

    @property
    def label(self) -> str:
        return _PRODUCT_LABELS[self]


_PRODUCT_LABELS = {
    Product.DRUPAL: "Drupal",
    Product.PHP: "PHP",
    Product.NEXTJS: "Next.js",
    Product.NODE: "Node.js",
}


class Kind(StrEnum):
    """What an item tells you about a security patch."""

    UPCOMING = "upcoming"  # officially pre-announced release
    EXPECTED = "expected"  # release window or estimate, not confirmed
    EOL = "eol"  # end of security support for a version line
    ADVISORY = "advisory"  # already published fix / advisory

    @property
    def notifiable(self) -> bool:
        """Expected items move every week and would only produce noise."""
        return self is not Kind.EXPECTED


class Severity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MODERATE = "moderate"
    LOW = "low"
    INFO = "info"

    @property
    def rank(self) -> int:
        return list(Severity).index(self)

    @classmethod
    def parse(cls, value: str | None) -> Severity:
        normalized = (value or "").strip().lower().replace("medium", "moderate")
        try:
            return cls(normalized)
        except ValueError:
            return cls.INFO


@dataclass(frozen=True, slots=True)
class Item:
    product: Product
    kind: Kind
    title: str
    date: date | None
    url: str = ""
    severity: Severity = Severity.INFO
    detail: str = ""
    package: str | None = None  # Composer package an advisory applies to, e.g. "drupal/webform"
    affected: str | None = None  # affected version range, e.g. "<4.2.1 || >=5.0.0 <5.0.3"

    @property
    def key(self) -> str:
        """Stable identity used to detect new items between checks."""
        return f"{self.product}|{self.kind}|{self.title}"

    def to_dict(self, *, new: bool = False) -> dict[str, Any]:
        return {
            "key": self.key,
            "product": self.product.value,
            "kind": self.kind.value,
            "title": self.title,
            "date": self.date.isoformat() if self.date else None,
            "url": self.url,
            "severity": self.severity.value,
            "detail": self.detail,
            "package": self.package,
            "affected": self.affected,
            "new": new,
        }


@dataclass(frozen=True, slots=True)
class Report:
    generated_at: datetime
    items: tuple[Item, ...]
    errors: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self, new_keys: frozenset[str] = frozenset()) -> dict[str, Any]:
        return {
            "generated": self.generated_at.isoformat(timespec="seconds"),
            "items": [item.to_dict(new=item.key in new_keys) for item in self.items],
            "errors": list(self.errors),
        }
