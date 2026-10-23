"""Safe parsing of RSS and Atom feeds (protected against XML entity attacks)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from xml.etree.ElementTree import Element

from defusedxml import ElementTree as SafeET

from patch_monitor.text import parse_iso_date, parse_rfc822_date

ATOM_NS = "http://www.w3.org/2005/Atom"


@dataclass(frozen=True, slots=True)
class FeedEntry:
    title: str
    link: str
    published: date | None
    content: str  # raw HTML/text body


def parse_xml(data: bytes) -> Element:
    root: Element = SafeET.fromstring(data)
    return root


def parse_rss(data: bytes) -> list[FeedEntry]:
    return [
        FeedEntry(
            title=(item.findtext("title") or "").strip(),
            link=(item.findtext("link") or "").strip(),
            published=parse_rfc822_date(item.findtext("pubDate")),
            content=item.findtext("description") or "",
        )
        for item in parse_xml(data).iter("item")
    ]


def parse_atom(data: bytes) -> list[FeedEntry]:
    ns = {"a": ATOM_NS}
    entries = []
    for entry in parse_xml(data).findall("a:entry", ns):
        content = entry.find("a:content", ns)
        links = entry.findall("a:link", ns)
        link = next((el.get("href", "") for el in links if el.get("rel") in (None, "alternate")), "")
        entries.append(
            FeedEntry(
                title=(entry.findtext("a:title", "", ns) or "").strip(),
                link=link,
                published=parse_iso_date(entry.findtext("a:published", "", ns)),
                content=_inner_markup(content) if content is not None else "",
            )
        )
    return entries


def _inner_markup(element: Element) -> str:
    """Return the text of an element including the text of all nested elements."""
    return " ".join(part for part in element.itertext() if part.strip())
