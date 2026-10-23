"""Whether a Drupal project is enabled, according to the project's exported ``core.extension.yml``.

The exported configuration lists every enabled module and theme::

    module:
      admin_toolbar: 0
      simple_oauth: 0
    theme:
      claro: 0
    profile: standard

The main sync directory decides "enabled"; config-split directories (``config/split/<name>/``) are reported
separately, because those modules are only enabled in some environments.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import PurePosixPath

from patch_monitor.project_files import ProjectFiles, ProjectFilesError

log = logging.getLogger(__name__)

CORE_EXTENSION = "core.extension.yml"
MAX_DEPTH = 7
MAX_FILE_CHARS = 1024 * 1024
# Directories that never contain the exported site configuration (or are too large to search).
SKIP_DIRS = frozenset(
    {
        "node_modules", "vendor", "core", "modules", "themes", "profiles", "libraries",
        "styles", "css", "js", "php", "translations", "simpletest", "tests", "private",
    }
)  # fmt: skip
SPLIT_PARENTS = frozenset({"split", "splits", "config_split"})
WEB_ROOTS = ("web", "docroot", "html", "public", "")
CONTRIB_DIRS = ("modules/contrib", "themes/contrib", "profiles/contrib")
_ENTRY = re.compile(r"^\s{2,}([a-z0-9_]+)\s*:")


class EnabledState(StrEnum):
    ENABLED = "enabled"
    SPLIT_ONLY = "split_only"  # enabled only in one or more config splits
    NOT_ENABLED = "not_enabled"
    UNKNOWN = "unknown"  # no core.extension.yml found
    ALWAYS = "always"  # Drupal core itself: always enabled


@dataclass(frozen=True, slots=True)
class ExtensionConfig:
    source: str  # project-relative path of the main core.extension.yml
    extensions: frozenset[str]  # enabled modules and themes
    splits: dict[str, frozenset[str]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EnabledStatus:
    state: EnabledState
    names: tuple[str, ...] = ()  # the enabled (sub)modules
    splits: tuple[str, ...] = ()
    source: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "names": list(self.names),
            "splits": list(self.splits),
            "source": self.source,
        }


def parse_core_extension(text: str) -> frozenset[str]:
    """Enabled module and theme names from the YAML of ``core.extension.yml``."""
    names: set[str] = set()
    section: str | None = None
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line[0].isspace():
            section = line.split(":", 1)[0].strip()
            continue
        if section in ("module", "theme"):
            match = _ENTRY.match(line)
            if match:
                names.add(match.group(1))
    return frozenset(names)


def _split_name(path: str) -> str | None:
    parts = PurePosixPath(path).parts
    return parts[-2] if len(parts) >= 3 and parts[-3] in SPLIT_PARENTS else None


def _read(files: ProjectFiles, path: str) -> frozenset[str] | None:
    try:
        text = files.read_text(path)
    except (FileNotFoundError, OSError, UnicodeDecodeError, ProjectFilesError) as exc:
        log.warning("Ignoring %s (%s): %s", path, files.label, exc)
        return None
    if len(text) > MAX_FILE_CHARS:
        log.warning("Ignoring %s (%s): unexpectedly large", path, files.label)
        return None
    return parse_core_extension(text)


def load_extension_config(files: ProjectFiles) -> ExtensionConfig | None:
    """The project's main ``core.extension.yml`` plus config splits; ``None`` if there is none."""
    candidates = [
        path
        for path in files.list_files(skip_dirs=SKIP_DIRS, max_depth=MAX_DEPTH)
        if PurePosixPath(path).name == CORE_EXTENSION
    ]
    main = [path for path in candidates if _split_name(path) is None]
    if not main:
        return None
    # Prefer a directory called "sync" (the usual config_sync_directory), then the shallowest path.
    source = min(main, key=lambda p: (PurePosixPath(p).parent.name != "sync", p.count("/"), p))
    extensions = _read(files, source)
    if extensions is None:
        return None
    splits = {}
    for path in candidates:
        name = _split_name(path)
        if name and (parsed := _read(files, path)) is not None:
            splits[name] = parsed
    return ExtensionConfig(source=source, extensions=extensions, splits=splits)


def extension_names(sources: Iterable[ProjectFiles], machine_name: str) -> frozenset[str]:
    """The project machine name plus its submodules/subthemes, from ``*.info.yml`` files in the code.

    Contrib code is usually installed by Composer and not committed, so callers pass the working tree as
    a fallback source next to a git ref.
    """
    names = {machine_name}
    for files in sources:
        for web_root in WEB_ROOTS:
            for contrib in CONTRIB_DIRS:
                under = "/".join(part for part in (web_root, contrib, machine_name) if part)
                found = [p for p in files.list_files(under) if p.endswith(".info.yml")]
                if found:
                    names.update(PurePosixPath(p).name.removesuffix(".info.yml") for p in found)
                    return frozenset(names)
    return frozenset(names)


def enabled_status(config: ExtensionConfig | None, candidates: frozenset[str]) -> EnabledStatus:
    if config is None:
        return EnabledStatus(EnabledState.UNKNOWN)
    enabled = tuple(sorted(candidates & config.extensions))
    if enabled:
        return EnabledStatus(EnabledState.ENABLED, enabled, source=config.source)
    split_hits = {name: candidates & mods for name, mods in config.splits.items() if candidates & mods}
    if split_hits:
        names = tuple(sorted(set().union(*split_hits.values())))
        return EnabledStatus(EnabledState.SPLIT_ONLY, names, tuple(sorted(split_hits)), config.source)
    return EnabledStatus(EnabledState.NOT_ENABLED, source=config.source)
