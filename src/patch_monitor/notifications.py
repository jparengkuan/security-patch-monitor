"""System notifications, one implementation per platform behind a common protocol."""

from __future__ import annotations

import json
import logging
import platform
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Protocol

from patch_monitor import APP_NAME
from patch_monitor.impact import Impact
from patch_monitor.models import Item, Kind

log = logging.getLogger(__name__)

CommandRunner = Callable[[list[str]], None]
MAX_LISTED = 3


class Notifier(Protocol):
    def notify(self, title: str, message: str) -> None: ...


def run_command(args: list[str]) -> None:
    try:
        subprocess.run(args, timeout=15, check=False, capture_output=True)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("Notification command failed: %s", exc)


class MacOSNotifier:
    def __init__(self, runner: CommandRunner = run_command) -> None:
        self._run = runner

    def notify(self, title: str, message: str) -> None:
        # json.dumps produces a correctly escaped AppleScript string literal.
        script = f'display notification {json.dumps(message)} with title {json.dumps(title)} sound name "default"'
        self._run(["osascript", "-e", script])


class LinuxNotifier:
    def __init__(self, icon: Path | None = None, runner: CommandRunner = run_command) -> None:
        self._icon = icon
        self._run = runner

    def notify(self, title: str, message: str) -> None:
        args = ["notify-send", "--app-name", APP_NAME]
        if self._icon:
            args += ["--icon", str(self._icon)]
        self._run([*args, title, message])


class WindowsNotifier:
    def __init__(self, runner: CommandRunner = run_command) -> None:
        self._run = runner

    def notify(self, title: str, message: str) -> None:
        script = (
            "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
            "ContentType=WindowsRuntime] > $null;"
            "$t=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(1);"
            "$x=$t.GetElementsByTagName('text');"
            f"$x.Item(0).AppendChild($t.CreateTextNode({_ps_string(title)}))>$null;"
            f"$x.Item(1).AppendChild($t.CreateTextNode({_ps_string(message)}))>$null;"
            f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier({_ps_string(APP_NAME)})"
            ".Show([Windows.UI.Notifications.ToastNotification]::new($t))"
        )
        self._run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script])


def _ps_string(value: str) -> str:
    """Single-quoted PowerShell literal (only ' needs escaping, by doubling)."""
    return "'" + value.replace("'", "''") + "'"


class NullNotifier:
    def notify(self, title: str, message: str) -> None:
        log.info("Notification: %s – %s", title, message)


def platform_notifier(icon: Path | None = None) -> Notifier:
    system = platform.system()
    if system == "Darwin" and shutil.which("osascript"):
        return MacOSNotifier()
    if system == "Linux" and shutil.which("notify-send"):
        return LinuxNotifier(icon)
    if system == "Windows" and shutil.which("powershell"):
        return WindowsNotifier()
    return NullNotifier()


def summarize(new_items: Sequence[Item], impacts: Mapping[str, Sequence[Impact]] | None = None) -> tuple[str, str]:
    """Title and body for one notification about the given new items (most urgent first).

    Items that affect one of the user's projects take precedence over everything else.
    """
    if not new_items:
        raise ValueError("Nothing to summarize")
    # Only modules that are (or may be) enabled make an advisory urgent for a project.
    active = {key: [i for i in found if i.active] for key, found in (impacts or {}).items()}
    affecting = [item for item in new_items if active.get(item.key)]
    if affecting:
        return _summarize_affecting(affecting, active, len(new_items))
    ordered = sorted(new_items, key=lambda i: (i.kind is not Kind.UPCOMING, i.severity.rank))
    if len(ordered) == 1:
        item = ordered[0]
        return f"New: {item.product.label} ({item.severity})", item.title
    announced = sum(1 for i in ordered if i.kind is Kind.UPCOMING)
    title = f"{len(ordered)} new security items" + (f" · {announced} announced" if announced else "")
    body = "; ".join(f"{i.product.label}: {i.title}" for i in ordered[:MAX_LISTED])
    return title, body


def _summarize_affecting(items: Sequence[Item], impacts: Mapping[str, Sequence[Impact]], total: int) -> tuple[str, str]:
    ordered = sorted(items, key=lambda i: i.severity.rank)
    projects = sorted({impact.project for item in ordered for impact in impacts[item.key]})
    target = projects[0] if len(projects) == 1 else f"{len(projects)} projects"
    if len(ordered) == 1:
        title = f"⚠ New advisory affects {target}"
    else:
        title = f"⚠ {len(ordered)} new advisories affect {target}"
    lines = []
    for item in ordered[:MAX_LISTED]:
        where = ", ".join(sorted({impact.project for impact in impacts[item.key]}))
        lines.append(f"{item.title} → {where}")
    others = total - len(ordered)
    if others:
        lines.append(f"+{others} other new item{'s' if others > 1 else ''}")
    return title, "; ".join(lines)
