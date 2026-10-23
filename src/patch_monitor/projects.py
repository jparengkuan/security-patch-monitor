"""Projects: named local checkouts, each identified by its ``composer.lock``."""

from __future__ import annotations

import json
import logging
import re
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from patch_monitor.storage import atomic_write_text

if TYPE_CHECKING:
    from patch_monitor.project_reader import ProjectReader

log = logging.getLogger(__name__)

LOCK_FILE = "composer.lock"
MAX_LOCK_BYTES = 32 * 1024 * 1024
MAX_NAME_LENGTH = 80
DRUPAL_VENDOR = "drupal/"
DRUPAL_CORE = "drupal/core"


class ProjectError(ValueError):
    """A project cannot be added or read; the message is safe to show to the user."""


@dataclass(frozen=True, slots=True)
class Project:
    id: str
    name: str
    lock_path: Path
    branch: str | None = None  # read files from origin/<branch> instead of the working tree

    @property
    def directory(self) -> Path:
        return self.lock_path.parent

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "lock_path": str(self.lock_path), "branch": self.branch}


@dataclass(frozen=True, slots=True)
class LockedPackages:
    """Installed package versions from a ``composer.lock`` (runtime and dev dependencies)."""

    versions: dict[str, str]

    @classmethod
    def from_lock(cls, data: dict[str, Any]) -> LockedPackages:
        versions: dict[str, str] = {}
        for section in ("packages", "packages-dev"):
            for package in data.get(section) or []:
                if isinstance(package, dict) and isinstance(package.get("name"), str):
                    versions[package["name"]] = str(package.get("version", ""))
        return cls(versions)

    @property
    def drupal_core(self) -> str | None:
        return self.versions.get(DRUPAL_CORE)

    @property
    def drupal_contrib(self) -> dict[str, str]:
        """Drupal projects other than core (modules, themes, profiles)."""
        return {
            name: version
            for name, version in self.versions.items()
            if name.startswith(DRUPAL_VENDOR) and not name.startswith(DRUPAL_CORE)
        }

    def summary(self) -> dict[str, Any]:
        return {"drupal_core": self.drupal_core, "drupal_projects": len(self.drupal_contrib)}


def resolve_lock_path(raw: str) -> Path:
    """Accept a ``composer.lock`` file or a directory containing one; return the absolute file path."""
    if not raw or not raw.strip():
        raise ProjectError("Choose a composer.lock file.")
    path = Path(raw.strip()).expanduser()
    if path.is_dir():
        path = path / LOCK_FILE
    if not path.exists():
        raise ProjectError(f"No composer.lock found at {path}. Run `composer install` in the project first.")
    if path.name != LOCK_FILE or not path.is_file():
        raise ProjectError("The selected file must be named composer.lock.")
    return path.resolve()


def parse_lock(text: str) -> LockedPackages:
    """Parse and validate the contents of a composer.lock file."""
    if len(text) > MAX_LOCK_BYTES:
        raise ProjectError("composer.lock is unexpectedly large.")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProjectError(f"composer.lock is not valid JSON (line {exc.lineno}).") from exc
    if not isinstance(data, dict) or not isinstance(data.get("packages"), list):
        raise ProjectError("This is not a valid composer.lock (no package list).")
    return LockedPackages.from_lock(data)


def read_lock(path: Path) -> LockedPackages:
    try:
        return parse_lock(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ProjectError(f"composer.lock not found at {path}.") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise ProjectError(f"composer.lock cannot be read: {exc}.") from exc


class ProjectStore(Protocol):
    def load(self) -> list[Project]: ...

    def save(self, projects: list[Project]) -> None: ...


class JsonProjectStore:
    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> list[Project]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Cannot read %s (%s); starting with no projects", self._path, exc)
            return []
        projects = []
        for raw in data if isinstance(data, list) else []:
            try:
                branch = raw.get("branch")
                projects.append(
                    Project(
                        str(raw["id"]),
                        str(raw["name"]),
                        Path(raw["lock_path"]),
                        str(branch) if isinstance(branch, str) and branch else None,
                    )
                )
            except (KeyError, TypeError):
                log.warning("Skipping invalid project entry: %r", raw)
        return projects

    def save(self, projects: list[Project]) -> None:
        atomic_write_text(self._path, json.dumps([p.to_dict() for p in projects], indent=2))


class ProjectService:
    """Validates and persists projects. Thread-safe, as it is used from request threads."""

    def __init__(self, store: ProjectStore, reader: ProjectReader) -> None:
        self._store = store
        self._reader = reader
        self._lock = threading.Lock()
        self._projects = store.load()

    def projects(self) -> list[Project]:
        with self._lock:
            return list(self._projects)

    def list_projects(self) -> list[dict[str, Any]]:
        """Projects with a fresh summary of their composer.lock (or the reason it cannot be read)."""
        return [self._describe(project) for project in self.projects()]

    def add_project(self, name: str, path: str, branch: str | None = None) -> dict[str, Any]:
        project = self._validated(uuid.uuid4().hex[:12], name, path, branch)
        with self._lock:
            self._store.save([*self._projects, project])
            self._projects.append(project)
        log.info("Project added: %s (%s, %s)", project.name, project.lock_path, project.branch or "local files")
        return self._describe(project)

    def update_project(self, project_id: str, name: str, path: str, branch: str | None = None) -> dict[str, Any]:
        """Change a project's name, composer.lock or source; on any error the project stays unchanged."""
        with self._lock:
            if not any(p.id == project_id for p in self._projects):
                raise ProjectError("Project not found.")
        project = self._validated(project_id, name, path, branch)
        with self._lock:
            projects = [project if p.id == project_id else p for p in self._projects]
            self._store.save(projects)
            self._projects = projects
        log.info("Project updated: %s (%s, %s)", project.name, project.lock_path, project.branch or "local files")
        return self._describe(project)

    def remove_project(self, project_id: str) -> None:
        with self._lock:
            remaining = [p for p in self._projects if p.id != project_id]
            if len(remaining) == len(self._projects):
                raise ProjectError("Project not found.")
            self._store.save(remaining)
            self._projects = remaining
        log.info("Project removed: %s", project_id)

    def _validated(self, project_id: str, name: str, path: str, branch: str | None) -> Project:
        """Build a project from user input and check that its source can actually be read."""
        clean_name = " ".join((name or "").split())
        if not clean_name:
            raise ProjectError("Enter a project name.")
        if len(clean_name) > MAX_NAME_LENGTH:
            raise ProjectError(f"The project name can be at most {MAX_NAME_LENGTH} characters.")
        project = Project(project_id, clean_name, resolve_lock_path(path), validate_branch(branch))

        with self._lock:
            for existing in self._projects:
                if existing.id == project_id:
                    continue
                if existing.name.casefold() == clean_name.casefold():
                    raise ProjectError(f"A project named “{existing.name}” already exists.")
                if existing.lock_path == project.lock_path and existing.branch == project.branch:
                    raise ProjectError(f"This composer.lock is already added as “{existing.name}”.")

        # Fetch the branch now so the newest commit is checked, and reject unreadable sources up front.
        self._reader.sync([project], force=True)
        self._reader.read(project)
        return project

    def _describe(self, project: Project) -> dict[str, Any]:
        result = project.to_dict()
        result["directory"] = str(project.directory)
        try:
            state = self._reader.read(project)
        except ProjectError as exc:
            result.update(summary=None, source=None, config=None, warning=None, error=str(exc))
            return result
        result.update(
            summary=state.locked.summary(),
            source=state.source,
            config=state.config_summary(),
            warning=self._reader.warning(project),
            error=None,
        )
        return result


_BRANCH = re.compile(r"^(?!-)(?!.*\.\.)[A-Za-z0-9._/-]{1,100}(?<![./])$")


def validate_branch(branch: str | None) -> str | None:
    """``None``/empty means "local files"; otherwise a safe git branch name."""
    if branch is None or not branch.strip():
        return None
    clean = branch.strip().removeprefix("origin/")
    if not _BRANCH.match(clean):
        raise ProjectError(f"“{branch}” is not a valid branch name.")
    return clean
