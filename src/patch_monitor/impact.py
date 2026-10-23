"""Which configured projects are affected by an advisory.

The installed version comes from the project's lock file. For Drupal contrib modules the enabled state comes
from the exported ``core.extension.yml``; Drupal core and npm packages are always in use. Everything is read
from the project's source (working tree or git branch).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from patch_monitor.drupal_config import EnabledState, EnabledStatus
from patch_monitor.lockfiles import DRUPAL_CORE, DRUPAL_VENDOR, Ecosystem
from patch_monitor.models import Item
from patch_monitor.project_reader import ProjectReader, ProjectState
from patch_monitor.projects import Project, ProjectError
from patch_monitor.version import Version, match_constraint

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Impact:
    project_id: str
    project: str
    package: str
    installed: str
    fixed_in: str | None  # first safe version for the installed release line, when known
    enabled: EnabledStatus
    source: str  # where the versions were read, e.g. "origin/develop @ 3f2a1c9 (2026-10-22 14:03)"

    @property
    def active(self) -> bool:
        """The vulnerable code is (or may be) running: enabled, enabled in a split, or unknown."""
        return self.enabled.state is not EnabledState.NOT_ENABLED

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "project": self.project,
            "package": self.package,
            "installed": self.installed,
            "fixed_in": self.fixed_in,
            "enabled": self.enabled.to_dict(),
            "source": self.source,
        }


class ImpactAssessor(Protocol):
    def has_projects(self) -> bool: ...

    def refresh(self) -> None:
        """Bring project sources up to date (e.g. git fetch); called before every check."""
        ...

    def assess(self, items: Iterable[Item]) -> dict[str, list[Impact]]: ...


class ImpactAnalyzer:
    """Matches advisories that name a package (Drupal core/contrib, Next.js) against every project's lock file."""

    def __init__(self, projects: Callable[[], list[Project]], reader: ProjectReader) -> None:
        self._projects = projects
        self._reader = reader

    def has_projects(self) -> bool:
        return bool(self._projects())

    def refresh(self) -> None:
        self._reader.sync(self._projects())

    def assess(self, items: Iterable[Item]) -> dict[str, list[Impact]]:
        relevant = [item for item in items if item.package]
        if not relevant:
            return {}
        states = self._states()
        impacts: dict[str, list[Impact]] = {}
        for item in relevant:
            for state in states:
                impact = _assess(item, state)
                if impact:
                    impacts.setdefault(item.key, []).append(impact)
        return impacts

    def _states(self) -> list[ProjectState]:
        states = []
        for project in self._projects():
            try:
                states.append(self._reader.read(project))
            except ProjectError as exc:
                log.warning("Skipping project %s: %s", project.name, exc)
        return states


def _assess(item: Item, state: ProjectState) -> Impact | None:
    package = item.package or ""
    installed = state.locked.versions.get(package)
    if installed is None:
        return None
    match = match_constraint(installed, item.affected)
    if match is None or not match.affected:
        return None
    # Drupal contrib modules can be switched off; core and npm dependencies are always in use.
    contrib = state.locked.ecosystem is Ecosystem.COMPOSER and package != DRUPAL_CORE
    enabled = state.enabled(package.removeprefix(DRUPAL_VENDOR)) if contrib else EnabledStatus(EnabledState.ALWAYS)
    return Impact(
        project_id=state.project.id,
        project=state.project.name,
        package=package,
        installed=installed,
        fixed_in=match.fixed_in or _first_patched_after(installed, item.patched),
        enabled=enabled,
        source=state.source,
    )


def _first_patched_after(installed: str, patched: tuple[str, ...]) -> str | None:
    """The lowest listed fixed version above the installed one, preferring the same major version."""
    current = Version.parse(installed)
    if current is None:
        return None
    newer = sorted(v for v in (Version.parse(p) for p in patched) if v is not None and v > current)
    same_major = [v for v in newer if v.numbers[0] == current.numbers[0]]
    candidates = same_major or newer
    return str(candidates[0]) if candidates else None
