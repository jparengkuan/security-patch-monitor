"""Reads the security-relevant state of a project from its chosen source (working tree or git branch)."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from patch_monitor.drupal_config import (
    EnabledStatus,
    ExtensionConfig,
    enabled_status,
    extension_names,
    load_extension_config,
)
from patch_monitor.lockfiles import Ecosystem, LockedPackages, LockError, parse_lock_file
from patch_monitor.project_files import (
    GitRefFiles,
    GitRepository,
    GitRunner,
    GitSync,
    ProjectFiles,
    ProjectFilesError,
    WorkingTreeFiles,
    run_git,
)
from patch_monitor.projects import Project, ProjectError

log = logging.getLogger(__name__)

LOCAL_CACHE_SECONDS = 60.0


@dataclass(slots=True)
class ProjectState:
    """What one project looks like in its chosen source at one moment."""

    project: Project
    source: str
    locked: LockedPackages
    extensions: ExtensionConfig | None
    files: ProjectFiles
    local: ProjectFiles
    _names: dict[str, frozenset[str]] = field(default_factory=dict, repr=False)

    def enabled(self, machine_name: str) -> EnabledStatus:
        if machine_name not in self._names:
            sources = [self.files] if self.files is self.local else [self.files, self.local]
            self._names[machine_name] = extension_names(sources, machine_name)
        return enabled_status(self.extensions, self._names[machine_name])

    def config_summary(self) -> dict[str, Any] | None:
        if self.extensions is None:
            return None
        return {
            "source": self.extensions.source,
            "enabled": len(self.extensions.extensions),
            "splits": sorted(self.extensions.splits),
        }


class ProjectReader:
    """Builds :class:`ProjectState` objects, cached per source revision.

    Git sources are cached per commit (immutable); local files for a short time, so a new
    ``composer install`` or config export is picked up within a minute.
    """

    def __init__(
        self,
        git_sync: GitSync | None = None,
        *,
        runner: GitRunner = run_git,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._git_sync = git_sync or GitSync()
        self._run = runner
        self._clock = clock
        self._repos: dict[Path, GitRepository] = {}
        self._cache: dict[tuple[str, str | None], tuple[str, float, ProjectState]] = {}
        self._lock = threading.Lock()

    def sync(self, projects: Iterable[Project], *, force: bool = False) -> None:
        """Fetch the branches of git-based projects (rate-limited unless ``force``)."""
        for project in projects:
            if project.branch:
                try:
                    self._git_sync.fetch(self._repo(project), project.branch, force=force)
                except ProjectError as exc:
                    log.warning("Cannot sync %s: %s", project.name, exc)

    def warning(self, project: Project) -> str | None:
        """Why the shown commit may be outdated (the last fetch failed); never cached."""
        if not project.branch:
            return None
        try:
            error = self._git_sync.error(self._repo(project), project.branch)
        except ProjectError:
            return None
        return f"Could not fetch {project.branch} ({error}); using the last fetched commit." if error else None

    def read(self, project: Project) -> ProjectState:
        files = self._files(project)
        key = (project.id, project.branch)
        now = self._clock()
        with self._lock:
            cached = self._cache.get(key)
        if cached and cached[0] == files.fingerprint and (project.branch or now - cached[1] < LOCAL_CACHE_SECONDS):
            return cached[2]
        state = self._build(project, files)
        with self._lock:
            self._cache[key] = (files.fingerprint, now, state)
        return state

    # -- internals -------------------------------------------------------------------

    def _build(self, project: Project, files: ProjectFiles) -> ProjectState:
        lock = project.lock_name
        try:
            text = files.read_text(lock)
        except FileNotFoundError as exc:
            raise ProjectError(f"{lock} not found in {files.label}.") from exc
        except (OSError, UnicodeDecodeError, ProjectFilesError) as exc:
            raise ProjectError(f"{lock} cannot be read from {files.label}: {exc}") from exc
        try:
            locked = parse_lock_file(lock, text)
        except LockError as exc:
            raise ProjectError(str(exc)) from exc
        drupal = locked.ecosystem is Ecosystem.COMPOSER
        return ProjectState(
            project=project,
            source=files.label,
            locked=locked,
            # Enabled state only exists for Drupal; npm projects have no core.extension.yml to search for.
            extensions=load_extension_config(files) if drupal else None,
            files=files,
            local=files if not project.branch else WorkingTreeFiles(project.directory, project.lock_name),
        )

    def _files(self, project: Project) -> ProjectFiles:
        if not project.branch:
            if not project.lock_path.is_file():
                raise ProjectError(f"{project.lock_name} not found at {project.lock_path}.")
            return WorkingTreeFiles(project.directory, project.lock_name)
        repo = self._repo(project)
        prefix = project.directory.resolve().relative_to(repo.toplevel).as_posix()
        try:
            return GitRefFiles(repo, "" if prefix == "." else prefix, project.branch)
        except ProjectFilesError as exc:
            fetch_error = self._git_sync.error(repo, project.branch)
            reason = f" Fetching it failed: {fetch_error}." if fetch_error else ""
            raise ProjectError(f"{exc}{reason} Choose another branch or “Local files”.") from exc

    def _repo(self, project: Project) -> GitRepository:
        directory = project.directory
        with self._lock:
            repo = self._repos.get(directory)
        if repo is None:
            try:
                repo = GitRepository.discover(directory, self._run)
            except ProjectFilesError as exc:
                raise ProjectError(str(exc)) from exc
            with self._lock:
                self._repos[directory] = repo
        return repo
