"""Installed package versions from lock files: one parser per format behind a common interface.

Supported formats and the advisories they are checked against:

=====================  ==========  ===================================================
Lock file              Ecosystem   Advisories
=====================  ==========  ===================================================
``composer.lock``      composer    Drupal core and contrib
``package-lock.json``  npm         Next.js (GitHub Advisory Database)
``yarn.lock``          npm         Next.js
``pnpm-lock.yaml``     npm         Next.js
=====================  ==========  ===================================================
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

MAX_LOCK_CHARS = 64 * 1024 * 1024
DRUPAL_VENDOR = "drupal/"
DRUPAL_CORE = "drupal/core"
NEXT = "next"


class LockError(ValueError):
    """The lock file cannot be parsed; the message is safe to show to the user."""


class Ecosystem(StrEnum):
    COMPOSER = "composer"
    NPM = "npm"


@dataclass(frozen=True, slots=True)
class LockedPackages:
    """Installed package name → version, as recorded in a lock file."""

    ecosystem: Ecosystem
    versions: dict[str, str]

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
        if self.ecosystem is Ecosystem.COMPOSER:
            return {
                "ecosystem": self.ecosystem.value,
                "drupal_core": self.drupal_core,
                "drupal_projects": len(self.drupal_contrib),
            }
        return {"ecosystem": self.ecosystem.value, "next": self.versions.get(NEXT), "packages": len(self.versions)}


class LockFormat(Protocol):
    filename: str
    ecosystem: Ecosystem

    def parse(self, text: str) -> LockedPackages: ...


def _load_json(text: str, filename: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LockError(f"{filename} is not valid JSON (line {exc.lineno}).") from exc
    if not isinstance(data, dict):
        raise LockError(f"This is not a valid {filename}.")
    return data


class ComposerLock:
    filename = "composer.lock"
    ecosystem = Ecosystem.COMPOSER

    def parse(self, text: str) -> LockedPackages:
        data = _load_json(text, self.filename)
        if not isinstance(data.get("packages"), list):
            raise LockError("This is not a valid composer.lock (no package list).")
        versions: dict[str, str] = {}
        for section in ("packages", "packages-dev"):
            for package in data.get(section) or []:
                if isinstance(package, dict) and isinstance(package.get("name"), str):
                    versions[package["name"]] = str(package.get("version", ""))
        return LockedPackages(self.ecosystem, versions)


class NpmLock:
    """``package-lock.json`` v1 (``dependencies``) and v2/v3 (``packages``)."""

    filename = "package-lock.json"
    ecosystem = Ecosystem.NPM

    def parse(self, text: str) -> LockedPackages:
        data = _load_json(text, self.filename)
        versions: dict[str, str] = {}
        packages = data.get("packages")
        if isinstance(packages, dict):
            for key, info in packages.items():
                # Only top-level installs: "node_modules/next", "node_modules/@scope/name".
                if not key.startswith("node_modules/") or "/node_modules/" in key or not isinstance(info, dict):
                    continue
                if isinstance(info.get("version"), str):
                    versions[key.removeprefix("node_modules/")] = info["version"]
        elif isinstance(data.get("dependencies"), dict):
            for name, info in data["dependencies"].items():
                if isinstance(info, dict) and isinstance(info.get("version"), str):
                    versions[name] = info["version"]
        else:
            raise LockError("This is not a valid package-lock.json (no packages).")
        return LockedPackages(self.ecosystem, versions)


_YARN_VERSION = re.compile(r'^\s+version:?\s+"?([^"\s]+)"?\s*$')


def _spec_name(spec: str) -> str:
    """``next@^14.2.0`` → ``next``; ``@next/env@npm:14.2.3`` → ``@next/env``."""
    spec = spec.strip().strip('"')
    at = spec.find("@", 1)  # skip the leading @ of scoped packages
    return spec[:at] if at > 0 else spec


class YarnLock:
    """Yarn classic (v1) and Yarn Berry (v2+) lock files."""

    filename = "yarn.lock"
    ecosystem = Ecosystem.NPM

    def parse(self, text: str) -> LockedPackages:
        versions: dict[str, str] = {}
        names: list[str] = []
        for line in text.splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if not line[0].isspace() and line.rstrip().endswith(":"):
                names = [_spec_name(spec) for spec in line.rstrip()[:-1].split(",")]
                continue
            match = _YARN_VERSION.match(line)
            if match and names:
                for name in names:
                    versions.setdefault(name, match.group(1))
                names = []
        if not versions:
            raise LockError("This is not a valid yarn.lock (no packages found).")
        return LockedPackages(self.ecosystem, versions)


_PNPM_PACKAGE = re.compile(r"""^ {2}['"]?/?((?:@[^/\s'"]+/)?[^@/\s'"(]+)[@/](\d[\w.+-]*)""")


class PnpmLock:
    """``pnpm-lock.yaml`` v5 (``/next/14.2.3``), v6 (``/next@14.2.3``) and v9 (``next@14.2.3``)."""

    filename = "pnpm-lock.yaml"
    ecosystem = Ecosystem.NPM

    def parse(self, text: str) -> LockedPackages:
        versions: dict[str, str] = {}
        section = None
        for line in text.splitlines():
            if line and not line[0].isspace():
                section = line.split(":", 1)[0].strip()
                continue
            if section in ("packages", "snapshots"):
                match = _PNPM_PACKAGE.match(line)
                if match:
                    versions.setdefault(match.group(1), match.group(2))
        if "lockfileVersion" not in text[:200] or not versions:
            raise LockError("This is not a valid pnpm-lock.yaml (no packages found).")
        return LockedPackages(self.ecosystem, versions)


FORMATS: dict[str, LockFormat] = {fmt.filename: fmt for fmt in (ComposerLock(), NpmLock(), YarnLock(), PnpmLock())}
SUPPORTED_NAMES = tuple(FORMATS)


def lock_format(filename: str) -> LockFormat:
    try:
        return FORMATS[filename]
    except KeyError:
        raise LockError(f"Unsupported lock file “{filename}”. Choose one of: {', '.join(SUPPORTED_NAMES)}.") from None


def parse_lock_file(filename: str, text: str) -> LockedPackages:
    if len(text) > MAX_LOCK_CHARS:
        raise LockError(f"{filename} is unexpectedly large.")
    return lock_format(filename).parse(text)
