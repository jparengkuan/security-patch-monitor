"""File-system locations and safe writes."""

from __future__ import annotations

import os
import platform
import tempfile
from dataclasses import dataclass
from pathlib import Path

DATA_DIR_ENV = "SPM_DATA_DIR"


@dataclass(frozen=True, slots=True)
class AppPaths:
    data_dir: Path

    @property
    def settings_file(self) -> Path:
        return self.data_dir / "settings.json"

    @property
    def seen_file(self) -> Path:
        return self.data_dir / "seen.json"

    @property
    def log_file(self) -> Path:
        return self.data_dir / "app.log"

    @classmethod
    def default(cls, override: Path | None = None) -> AppPaths:
        data_dir = override or _env_dir() or _platform_data_dir()
        data_dir.mkdir(parents=True, exist_ok=True)
        return cls(data_dir)


def _env_dir() -> Path | None:
    value = os.environ.get(DATA_DIR_ENV)
    return Path(value).expanduser() if value else None


def _platform_data_dir() -> Path:
    home = Path.home()
    system = platform.system()
    if system == "Darwin":
        return home / "Library" / "Application Support" / "Security Patch Monitor"
    if system == "Windows":
        return Path(os.environ.get("APPDATA", home / "AppData" / "Roaming")) / "Security Patch Monitor"
    return Path(os.environ.get("XDG_CONFIG_HOME", home / ".config")) / "security-patch-monitor"


def atomic_write_text(path: Path, content: str) -> None:
    """Write via a temporary file + rename, so a crash never leaves a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        Path(tmp).replace(path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
