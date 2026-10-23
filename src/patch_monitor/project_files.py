"""Read a project's files either from the working tree or from a commit on a git branch.

Reading from ``origin/<branch>`` (after a ``git fetch``) answers "what is on develop right now", even when
the local checkout is behind or has uncommitted changes. Git is only ever asked to *read*: fetching updates
remote-tracking refs and never touches the working tree, the checked-out branch or local changes.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Protocol

log = logging.getLogger(__name__)

GIT_TIMEOUT = 60.0
REMOTE = "origin"
# Never prompt for credentials: a background app must fail fast instead of hanging.
GIT_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "",
    "SSH_ASKPASS": "",
    "GIT_SSH_COMMAND": "ssh -o BatchMode=yes -o ConnectTimeout=15",
    "LC_ALL": "C",
}

GitRunner = Callable[[list[str], Path], str]


class ProjectFilesError(RuntimeError):
    """Files cannot be read from the chosen source; the message is safe to show to the user."""


class ProjectFiles(Protocol):
    """Read-only view of a project directory."""

    @property
    def label(self) -> str:
        """Human-readable origin, e.g. ``Local files`` or ``origin/develop @ 3f2a1c9 (2026-10-22)``."""
        ...

    @property
    def fingerprint(self) -> str:
        """Changes whenever the content may have changed (commit SHA, or file mtimes)."""
        ...

    def read_text(self, relative: str) -> str:
        """Raises ``FileNotFoundError`` when the file does not exist in this source."""
        ...

    def list_files(
        self, under: str = "", *, skip_dirs: frozenset[str] = frozenset(), max_depth: int | None = None
    ) -> list[str]:
        """File paths below ``under`` (project-relative, POSIX separators).

        Directories named in ``skip_dirs`` (and hidden directories) are not descended into; ``max_depth``
        limits how many directory levels below ``under`` are searched.
        """
        ...


# -- working tree ---------------------------------------------------------------------


class WorkingTreeFiles:
    def __init__(self, root: Path, lock_name: str = "composer.lock") -> None:
        self._root = root
        self._lock_name = lock_name

    @property
    def label(self) -> str:
        return "Local files"

    @property
    def fingerprint(self) -> str:
        lock = self._root / self._lock_name
        try:
            return f"local:{lock.stat().st_mtime_ns}"
        except OSError:
            return "local:missing"

    def read_text(self, relative: str) -> str:
        return (self._root / relative).read_text(encoding="utf-8")

    def list_files(
        self, under: str = "", *, skip_dirs: frozenset[str] = frozenset(), max_depth: int | None = None
    ) -> list[str]:
        base = self._root / under if under else self._root
        if not base.is_dir():
            return []
        found: list[str] = []
        base_depth = len(base.parts)
        for current, dirs, files in os.walk(base):
            path = Path(current)
            if max_depth is not None and len(path.parts) - base_depth >= max_depth:
                dirs.clear()
            dirs[:] = [d for d in dirs if d not in skip_dirs and not d.startswith(".")]
            rel = path.relative_to(self._root).as_posix()
            found.extend(f"{rel}/{name}" if rel != "." else name for name in files)
        return found


# -- git ------------------------------------------------------------------------------


def run_git(args: list[str], cwd: Path) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
            env={**os.environ, **GIT_ENV},
            check=False,
        )
    except FileNotFoundError as exc:
        raise ProjectFilesError("git is not installed.") from exc
    except subprocess.TimeoutExpired as exc:
        raise ProjectFilesError(f"git {args[0]} timed out after {GIT_TIMEOUT:.0f}s.") from exc
    if result.returncode != 0:
        raise ProjectFilesError(_git_error(result.stderr or result.stdout) or f"git {args[0]} failed")
    return result.stdout


def _git_error(output: str) -> str:
    """The most useful line of git's error output, e.g. "Could not read from remote repository."."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    for line in lines:
        for prefix in ("fatal: ", "error: "):
            if line.startswith(prefix):
                return line.removeprefix(prefix)
    return lines[0] if lines else ""


@dataclass(frozen=True, slots=True)
class Commit:
    sha: str
    committed: datetime
    subject: str

    @property
    def short(self) -> str:
        return self.sha[:7]


class GitRepository:
    def __init__(self, toplevel: Path, runner: GitRunner = run_git) -> None:
        self.toplevel = toplevel
        self._run = runner

    @classmethod
    def discover(cls, directory: Path, runner: GitRunner = run_git) -> GitRepository:
        try:
            toplevel = runner(["rev-parse", "--show-toplevel"], directory).strip()
        except ProjectFilesError as exc:
            raise ProjectFilesError(f"{directory} is not inside a git repository.") from exc
        return cls(Path(toplevel).resolve(), runner)

    def fetch(self, branch: str) -> None:
        self._run(["fetch", "--quiet", "--no-tags", REMOTE, branch], self.toplevel)

    def commit(self, ref: str) -> Commit:
        try:
            out = self._run(["log", "-1", "--format=%H%x00%cI%x00%s", ref, "--"], self.toplevel)
        except ProjectFilesError as exc:
            raise ProjectFilesError(f"Branch {ref} not found.") from exc
        sha, committed, subject = out.rstrip("\n").split("\x00", 2)
        return Commit(sha, datetime.fromisoformat(committed), subject)

    def show(self, sha: str, path: str) -> str:
        try:
            return self._run(["show", f"{sha}:{path}"], self.toplevel)
        except ProjectFilesError as exc:
            raise FileNotFoundError(path) from exc

    def ls_tree(self, sha: str, prefix: str) -> list[str]:
        args = ["ls-tree", "-r", "--name-only", "-z", sha]
        if prefix:
            args += ["--", prefix]
        return [p for p in self._run(args, self.toplevel).split("\x00") if p]


class GitRefFiles:
    """The project directory as it is in one commit (resolved from ``origin/<branch>`` once)."""

    def __init__(self, repo: GitRepository, prefix: str, branch: str) -> None:
        self._repo = repo
        self._prefix = prefix.strip("/")
        self.ref = f"{REMOTE}/{branch}"
        self.commit = repo.commit(self.ref)
        self._tree: list[str] | None = None

    @property
    def label(self) -> str:
        return f"{self.ref} @ {self.commit.short} ({self.commit.committed:%Y-%m-%d %H:%M})"

    @property
    def fingerprint(self) -> str:
        return f"git:{self.commit.sha}"

    def read_text(self, relative: str) -> str:
        return self._repo.show(self.commit.sha, self._path(relative))

    def list_files(
        self, under: str = "", *, skip_dirs: frozenset[str] = frozenset(), max_depth: int | None = None
    ) -> list[str]:
        if self._tree is None:
            self._tree = self._repo.ls_tree(self.commit.sha, self._prefix)
        start = len(self._prefix) + 1 if self._prefix else 0
        base = under.strip("/")
        result = []
        for path in (p[start:] for p in self._tree):
            if base and not path.startswith(base + "/"):
                continue
            dirs = PurePosixPath(path[len(base) + 1 :] if base else path).parts[:-1]
            if max_depth is not None and len(dirs) > max_depth:
                continue
            if any(d in skip_dirs or d.startswith(".") for d in dirs):
                continue
            result.append(path)
        return result

    def _path(self, relative: str) -> str:
        return str(PurePosixPath(self._prefix, relative)) if self._prefix else relative


class GitSync:
    """Fetches project branches in the background, at most once per ``min_interval`` per branch."""

    def __init__(self, min_interval: float = 300.0, clock: Callable[[], float] = time.monotonic) -> None:
        self._min_interval = min_interval
        self._clock = clock
        self._last: dict[tuple[Path, str], float] = {}
        self._errors: dict[tuple[Path, str], str] = {}
        self._lock = threading.Lock()

    def fetch(self, repo: GitRepository, branch: str, *, force: bool = False) -> None:
        key = (repo.toplevel, branch)
        now = self._clock()
        with self._lock:
            if not force and now - self._last.get(key, -1e18) < self._min_interval:
                return
            self._last[key] = now
        try:
            repo.fetch(branch)
        except ProjectFilesError as exc:
            log.warning("git fetch %s %s in %s failed: %s", REMOTE, branch, repo.toplevel, exc)
            with self._lock:
                self._errors[key] = str(exc)
            return
        with self._lock:
            self._errors.pop(key, None)

    def error(self, repo: GitRepository, branch: str) -> str | None:
        with self._lock:
            return self._errors.get((repo.toplevel, branch))
