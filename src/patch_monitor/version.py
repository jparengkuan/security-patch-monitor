"""Version parsing and constraint matching for Drupal and npm style ranges.

Supported constraint syntax (as used in Drupal and GitHub advisories)::

    <4.2.1
    <10.4.9 || >=10.5.0 <10.5.4
    >= 15.0.0, < 15.5.29
    8.x-1.5            (Drupal 7/8 style; the "8.x-" prefix is ignored)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import total_ordering

_VERSION = re.compile(r"^v?(?:\d+\.x-)?(\d+(?:\.\d+)*)(?:[-+]?([0-9A-Za-z.]+))?$")
_TOKEN = re.compile(r"(<=|>=|<|>|==|=|!=)?\s*(v?(?:\d+\.x-)?\d+(?:\.\d+)*(?:-[0-9A-Za-z.]+)?)")
_ALTERNATIVES = re.compile(r"\|\||\bor\b")


@total_ordering
@dataclass(frozen=True, slots=True)
class Version:
    numbers: tuple[int, ...]
    prerelease: str = ""

    @classmethod
    def parse(cls, raw: str | None) -> Version | None:
        """``None`` for versions that cannot be compared, such as ``dev-main``."""
        match = _VERSION.match((raw or "").strip())
        if not match:
            return None
        numbers = tuple(int(part) for part in match.group(1).split("."))
        numbers = numbers + (0,) * (3 - len(numbers))
        return cls(numbers, (match.group(2) or "").lower())

    def _key(self) -> tuple[tuple[int, ...], int, str]:
        # A pre-release sorts before the release itself: 4.2.1-beta1 < 4.2.1.
        return self.numbers, 0 if self.prerelease else 1, self.prerelease

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return self._key() < other._key()

    def __str__(self) -> str:
        text = ".".join(map(str, self.numbers))
        return f"{text}-{self.prerelease}" if self.prerelease else text


@dataclass(frozen=True, slots=True)
class Match:
    affected: bool
    fixed_in: str | None = None  # upper bound of the matching range, i.e. the first safe version


_OPERATORS = {
    "<": lambda c: c < 0,
    "<=": lambda c: c <= 0,
    ">": lambda c: c > 0,
    ">=": lambda c: c >= 0,
    "=": lambda c: c == 0,
    "==": lambda c: c == 0,
    "!=": lambda c: c != 0,
}


def _compare(a: Version, b: Version) -> int:
    return (a > b) - (a < b)


def match_constraint(version: str, constraint: str | None) -> Match | None:
    """Whether ``version`` lies in ``constraint``; ``None`` when either cannot be interpreted."""
    installed = Version.parse(version)
    if installed is None or not constraint:
        return None
    understood = False
    for alternative in _ALTERNATIVES.split(constraint):
        tokens = _TOKEN.findall(alternative)
        parsed = ((op or "=", Version.parse(raw)) for op, raw in tokens)
        bounds = [(op, bound) for op, bound in parsed if bound is not None]
        if not bounds:
            continue
        understood = True
        if all(_OPERATORS[op](_compare(installed, bound)) for op, bound in bounds):
            upper = next((str(bound) for op, bound in bounds if op == "<"), None)
            return Match(affected=True, fixed_in=upper)
    return Match(affected=False) if understood else None
