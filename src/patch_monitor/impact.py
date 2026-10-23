"""Which configured projects are affected by a Drupal core or contrib advisory.

For every project the installed version comes from its ``composer.lock`` and the enabled state from its
exported ``core.extension.yml``, both read from the project's source (working tree or git branch).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from patch_monitor.drupal_config import EnabledState, EnabledStatus
from patch_monitor.models import Item
from patch_monitor.project_reader import ProjectReader, ProjectState
from patch_monitor.projects import DRUPAL_CORE, DRUPAL_VENDOR, Project, ProjectError
from patch_monitor.version import match_constraint

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
    """Matches Drupal core and contrib advisories against the locked versions of every project."""

    def __init__(self, projects: Callable[[], list[Project]], reader: ProjectReader) -> None:
        self._projects = projects
        self._reader = reader

    def has_projects(self) -> bool:
        return bool(self._projects())

    def refresh(self) -> None:
        self._reader.sync(self._projects())

    def assess(self, items: Iterable[Item]) -> dict[str, list[Impact]]:
        relevant = [item for item in items if item.package and item.package.startswith(DRUPAL_VENDOR)]
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
    if package == DRUPAL_CORE:
        enabled = EnabledStatus(EnabledState.ALWAYS)
    else:
        enabled = state.enabled(package.removeprefix(DRUPAL_VENDOR))
    return Impact(
        project_id=state.project.id,
        project=state.project.name,
        package=package,
        installed=installed,
        fixed_in=match.fixed_in,
        enabled=enabled,
        source=state.source,
    )
