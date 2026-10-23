"""System notifications, one implementation per platform behind a common protocol."""

from __future__ import annotations

import json
import logging
import platform
import shutil
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol

from patch_monitor import APP_NAME
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


def summarize(new_items: Sequence[Item]) -> tuple[str, str]:
    """Title and body for one notification about the given new items (most urgent first)."""
    if not new_items:
        raise ValueError("Nothing to summarize")
    ordered = sorted(new_items, key=lambda i: (i.kind is not Kind.UPCOMING, i.severity.rank))
    if len(ordered) == 1:
        item = ordered[0]
        return f"New: {item.product.label} ({item.severity})", item.title
    announced = sum(1 for i in ordered if i.kind is Kind.UPCOMING)
    title = f"{len(ordered)} new security items" + (f" · {announced} announced" if announced else "")
    body = "; ".join(f"{i.product.label}: {i.title}" for i in ordered[:MAX_LISTED])
    return title, body
