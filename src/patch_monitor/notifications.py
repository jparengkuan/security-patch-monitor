"""System notifications: what to say (``compose``) and how to show it (one ``Notifier`` per platform)."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from patch_monitor import APP_NAME
from patch_monitor.drupal_config import EnabledState
from patch_monitor.impact import Impact
from patch_monitor.models import Item, Kind, Severity

log = logging.getLogger(__name__)

MAX_LISTED = 3
HELPER_NAME = "Security Patch Monitor Notifier.app"
APP_BUNDLE_ENV = "SPM_APP_BUNDLE"


@dataclass(frozen=True, slots=True)
class Notification:
    title: str
    body: str
    subtitle: str = ""
    url: str | None = None  # opened when the notification (or "Open advisory") is clicked
    thread: str = "security"  # groups related notifications in Notification Centre
    id: str = ""

    @property
    def plain_text(self) -> str:
        """Body including the subtitle, for platforms without a subtitle line."""
        return f"{self.subtitle}\n{self.body}" if self.subtitle else self.body


class Notifier(Protocol):
    def notify(self, notification: Notification) -> None: ...


# -- composing --------------------------------------------------------------------------

_DRUPAL_TITLE = re.compile(r"^(?P<name>.+?) - (?P<risk>[^-]+?) - (?P<vuln>.+) - (?P<id>P?SA-[A-Z]+-\d{4}-\d+)$")
_TRAILING_ID = re.compile(r"^(?P<text>.+?)\s*\((?P<id>(?:CVE|GHSA)-[^()]+)\)$")


def compose(new_items: Sequence[Item], impacts: Mapping[str, Sequence[Impact]] | None = None) -> Notification:
    """One notification about the given new items, most urgent first.

    Items that affect one of the user's projects (with the module enabled) take precedence.
    """
    if not new_items:
        raise ValueError("Nothing to summarize")
    active = {key: [i for i in found if i.active] for key, found in (impacts or {}).items()}
    affecting = [item for item in new_items if active.get(item.key)]
    if affecting:
        return _affecting(affecting, active, len(new_items))
    ordered = sorted(new_items, key=lambda i: (i.kind is not Kind.UPCOMING, i.severity.rank))
    return _single(ordered[0]) if len(ordered) == 1 else _several(ordered)


def sample_notification() -> Notification:
    """Sent on request (settings or ``--test-notification``) to check that notifications get through."""
    return Notification(
        title=f"{APP_NAME} notifications are working",
        body="New security advisories will appear like this.",
        thread="test",
        id="test",
    )


def describe(item: Item) -> tuple[str, str]:
    """``(subtitle, summary)`` for an item, e.g. ``("Simple OAuth · SA-CONTRIB-2026-242", "Access bypass")``."""
    drupal = _DRUPAL_TITLE.match(item.title)
    if drupal:
        return f"{drupal['name']} · {drupal['id']}", drupal["vuln"]
    trailing = _TRAILING_ID.match(item.title)
    if trailing:
        text = trailing["text"].removeprefix(f"{item.product.label}: ")
        return trailing["id"], text
    when = f"{item.date:%a %d %b %Y}" if item.date else ""
    return when, item.title


def _severity_word(severity: Severity) -> str:
    return "" if severity is Severity.INFO else f"{severity.value} "


def _impact_line(impact: Impact) -> str:
    fix = f" → update to {impact.fixed_in}" if impact.fixed_in else ""
    state = {
        EnabledState.ENABLED: "enabled",
        EnabledState.SPLIT_ONLY: f"enabled in split {', '.join(impact.enabled.splits)}",
        EnabledState.UNKNOWN: "enabled state unknown",
    }.get(impact.enabled.state)
    return f"{impact.project}: {impact.installed}{fix}" + (f" ({state})" if state else "")


def _counts(items: Sequence[Item]) -> str:
    parts = []
    announced = sum(1 for i in items if i.kind is Kind.UPCOMING)
    if announced:
        parts.append(f"{announced} announced")
    for severity in (Severity.CRITICAL, Severity.HIGH, Severity.MODERATE):
        count = sum(1 for i in items if i.severity is severity and i.kind is not Kind.UPCOMING)
        if count:
            parts.append(f"{count} {severity.value}")
    return " · ".join(parts)


def _notification_id(items: Sequence[Item]) -> str:
    return hashlib.sha1("|".join(sorted(i.key for i in items)).encode(), usedforsecurity=False).hexdigest()[:16]


def _affecting(items: Sequence[Item], impacts: Mapping[str, Sequence[Impact]], total: int) -> Notification:
    ordered = sorted(items, key=lambda i: i.severity.rank)
    projects = sorted({impact.project for item in ordered for impact in impacts[item.key]})
    target = projects[0] if len(projects) == 1 else f"{len(projects)} projects"
    others = total - len(ordered)
    more = f"\n+{others} other new item{'s' if others > 1 else ''}" if others else ""

    if len(ordered) == 1:
        item = ordered[0]
        subtitle, summary = describe(item)
        lines = [_impact_line(impact) for impact in impacts[item.key]]
        shown = lines[:MAX_LISTED] + ([f"+{len(lines) - MAX_LISTED} more"] if len(lines) > MAX_LISTED else [])
        return Notification(
            title=f"⚠ {_severity_word(item.severity).capitalize()}{item.product.label} advisory affects {target}",
            subtitle=subtitle,
            body="\n".join([summary, *shown]) + more,
            url=item.url or None,
            thread="affected",
            id=_notification_id(ordered),
        )

    lines = []
    for item in ordered[:MAX_LISTED]:
        name, _ = describe(item)
        where = ", ".join(sorted({impact.project for impact in impacts[item.key]}))
        lines.append(f"{name or item.title} → {where}")
    if len(ordered) > MAX_LISTED:
        lines.append(f"+{len(ordered) - MAX_LISTED} more affecting advisories")
    return Notification(
        title=f"⚠ {len(ordered)} new advisories affect {target}",
        subtitle=_counts(ordered),
        body="\n".join(lines) + more,
        url=ordered[0].url or None,
        thread="affected",
        id=_notification_id(ordered),
    )


def _single(item: Item) -> Notification:
    subtitle, summary = describe(item)
    product = item.product.label
    if item.kind is Kind.UPCOMING:
        when = f" for {item.date:%a %d %b}" if item.date else ""
        title, subtitle, summary = f"{product} security release announced{when}", "", item.title
    elif item.kind is Kind.EOL:
        title, subtitle, summary = item.title, f"{item.date:%a %d %b %Y}" if item.date else "", item.detail
    else:
        title = f"New {_severity_word(item.severity)}{product} advisory"
    return Notification(
        title=title,
        subtitle=subtitle,
        body=summary,
        url=item.url or None,
        thread=item.product.value,
        id=_notification_id([item]),
    )


def _several(ordered: Sequence[Item]) -> Notification:
    lines = []
    for item in ordered[:MAX_LISTED]:
        name, summary = describe(item)
        label = name.split(" · ")[0] if _DRUPAL_TITLE.match(item.title) else summary
        lines.append(f"{item.product.label}: {label}")
    if len(ordered) > MAX_LISTED:
        lines.append(f"+{len(ordered) - MAX_LISTED} more")
    products = {item.product for item in ordered}
    return Notification(
        title=f"{len(ordered)} new security items",
        subtitle=_counts(ordered),
        body="\n".join(lines),
        url=ordered[0].url or None,
        thread=next(iter(products)).value if len(products) == 1 else "security",
        id=_notification_id(ordered),
    )


# -- delivering -------------------------------------------------------------------------

CommandRunner = Callable[[list[str], str | None], bool]


def run_command(args: list[str], stdin: str | None = None) -> bool:
    try:
        # Generous timeout: the very first native notification waits for the user to allow notifications.
        result = subprocess.run(args, input=stdin, timeout=120, check=False, capture_output=True, text=True)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("Notification command %s failed: %s", Path(args[0]).name, exc)
        return False
    if result.returncode != 0:
        log.warning(
            "Notification command %s exited with %d: %s", Path(args[0]).name, result.returncode, result.stderr.strip()
        )
        return False
    log.info("Notification command %s succeeded", Path(args[0]).name)
    return True


class MacOSNotifier:
    """AppleScript notifications (shown with Script Editor's name and icon). Used as fallback."""

    def __init__(self, runner: CommandRunner = run_command) -> None:
        self._run = runner

    def notify(self, notification: Notification) -> None:
        script = f"display notification {_applescript(notification.body)} with title {_applescript(notification.title)}"
        if notification.subtitle:
            script += f" subtitle {_applescript(notification.subtitle)}"
        self._run(["osascript", "-e", f'{script} sound name "default"'], None)


def _applescript(value: str) -> str:
    """AppleScript string literal. Only backslash and double quote need escaping; other characters (⚠, →,
    newlines) must stay literal, because AppleScript has no \\uXXXX escapes."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class MacOSNativeNotifier:
    """Native notifications with the app's own name and icon, an "Open advisory" button and grouping.

    Delivered by a small Swift helper app inside the app bundle (``packaging/macos/notifier``), because macOS
    attributes notifications to the posting app bundle. Falls back to AppleScript when the helper fails.
    """

    def __init__(self, helper: Path, fallback: Notifier | None = None, runner: CommandRunner = run_command) -> None:
        self._helper = helper
        self._fallback = fallback
        self._run = runner

    def notify(self, notification: Notification) -> None:
        payload = json.dumps(asdict(notification))
        if not self._run([str(self._helper), "post"], payload) and self._fallback:
            self._fallback.notify(notification)

    def authorize(self) -> None:
        """Ask for notification permission now (on first launch), rather than when the first advisory arrives."""
        self._run([str(self._helper), "authorize"], None)


class LinuxNotifier:
    def __init__(self, icon: Path | None = None, runner: CommandRunner = run_command) -> None:
        self._icon = icon
        self._run = runner

    def notify(self, notification: Notification) -> None:
        args = ["notify-send", "--app-name", APP_NAME]
        if self._icon:
            args += ["--icon", str(self._icon)]
        self._run([*args, notification.title, notification.plain_text], None)


class WindowsNotifier:
    def __init__(self, runner: CommandRunner = run_command) -> None:
        self._run = runner

    def notify(self, notification: Notification) -> None:
        script = (
            "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
            "ContentType=WindowsRuntime] > $null;"
            "$t=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(1);"
            "$x=$t.GetElementsByTagName('text');"
            f"$x.Item(0).AppendChild($t.CreateTextNode({_ps_string(notification.title)}))>$null;"
            f"$x.Item(1).AppendChild($t.CreateTextNode({_ps_string(notification.plain_text)}))>$null;"
            f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier({_ps_string(APP_NAME)})"
            ".Show([Windows.UI.Notifications.ToastNotification]::new($t))"
        )
        self._run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], None)


def _ps_string(value: str) -> str:
    """Single-quoted PowerShell literal (only ' needs escaping, by doubling)."""
    return "'" + value.replace("'", "''") + "'"


class NullNotifier:
    def notify(self, notification: Notification) -> None:
        log.info("Notification: %s – %s", notification.title, notification.plain_text)


def find_macos_helper() -> Path | None:
    """The notifier helper inside the installed app bundle (see scripts/build_macos_app.sh)."""
    bundles = [Path(os.environ[APP_BUNDLE_ENV])] if os.environ.get(APP_BUNDLE_ENV) else []
    bundles += [Path.home() / "Applications" / f"{APP_NAME}.app", Path("/Applications") / f"{APP_NAME}.app"]
    for bundle in bundles:
        helper = bundle / "Contents" / "Helpers" / HELPER_NAME / "Contents" / "MacOS" / "notifier"
        if helper.is_file() and os.access(helper, os.X_OK):
            return helper
    return None


def platform_notifier(icon: Path | None = None) -> Notifier:
    system = platform.system()
    if system == "Darwin":
        fallback = MacOSNotifier() if shutil.which("osascript") else None
        helper = find_macos_helper()
        if helper:
            return MacOSNativeNotifier(helper, fallback)
        if fallback:
            return fallback
    if system == "Linux" and shutil.which("notify-send"):
        return LinuxNotifier(icon)
    if system == "Windows" and shutil.which("powershell"):
        return WindowsNotifier()
    return NullNotifier()
