from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from patch_monitor.notifications import (
    LinuxNotifier,
    MacOSNativeNotifier,
    MacOSNotifier,
    Notification,
    WindowsNotifier,
)
from patch_monitor.seen import JsonSeenStore
from patch_monitor.settings import JsonSettingsStore, Settings
from patch_monitor.storage import atomic_write_text


def test_settings_defaults_when_missing_or_corrupt(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    assert JsonSettingsStore(path).load() == Settings()
    path.write_text("{not json")
    assert JsonSettingsStore(path).load() == Settings()
    path.write_text("[1, 2]")
    assert JsonSettingsStore(path).load() == Settings()


def test_settings_from_dict_clamps_and_ignores_invalid() -> None:
    settings = Settings.from_dict({"interval_minutes": 99999, "history_days": "abc", "notifications": 1})
    assert settings.interval_minutes == 24 * 60
    assert settings.history_days == Settings().history_days
    assert settings.notifications is True  # non-bool ignored, default kept


def test_settings_round_trip(tmp_path: Path) -> None:
    store = JsonSettingsStore(tmp_path / "nested" / "settings.json")
    store.save(Settings(interval_minutes=5, notifications=False))
    assert store.load() == Settings(interval_minutes=5, notifications=False)


def test_seen_store_prunes_old_entries(tmp_path: Path) -> None:
    path = tmp_path / "seen.json"
    store = JsonSeenStore(path)
    assert not store.is_initialized()
    store.remember(["old"], date(2025, 1, 1))
    store.remember(["new"], date(2026, 10, 23))
    assert json.loads(path.read_text()) == {"new": "2026-10-23"}
    assert JsonSeenStore(path).unseen(["new", "other"]) == {"other"}


def test_atomic_write_leaves_no_temp_files(tmp_path: Path) -> None:
    target = tmp_path / "file.txt"
    atomic_write_text(target, "one")
    atomic_write_text(target, "two")
    assert target.read_text() == "two"
    assert [p.name for p in tmp_path.iterdir()] == ["file.txt"]


class Recorder:
    def __init__(self, result: bool = True) -> None:
        self.calls: list[tuple[list[str], str | None]] = []
        self.result = result

    def __call__(self, args: list[str], stdin: str | None) -> bool:
        self.calls.append((args, stdin))
        return self.result


def test_notifiers_escape_user_content() -> None:
    run = Recorder()
    MacOSNotifier(run).notify(Notification(title='Say "hi"', subtitle="sub", body="it's \\ fine"))
    args, _ = run.calls[-1]
    assert args[:2] == ["osascript", "-e"]
    assert '"Say \\"hi\\""' in args[2] and 'subtitle "sub"' in args[2]

    WindowsNotifier(run).notify(Notification(title="O'Brien", body="x"))
    assert "'O''Brien'" in run.calls[-1][0][-1]

    LinuxNotifier(runner=run).notify(Notification(title="t", subtitle="s", body="m"))
    assert run.calls[-1][0][-2:] == ["t", "s\nm"]


def test_native_notifier_sends_json_and_falls_back(tmp_path: Path) -> None:
    helper = tmp_path / "notifier"
    notification = Notification(title="T", subtitle="S", body="B", url="https://x", thread="drupal", id="1")
    fallback = Recorder()

    MacOSNativeNotifier(helper, MacOSNotifier(fallback), Recorder(result=True)).notify(notification)
    assert fallback.calls == []

    native = Recorder(result=False)
    MacOSNativeNotifier(helper, MacOSNotifier(fallback), native).notify(notification)
    args, stdin = native.calls[0]
    assert args == [str(helper), "post"]
    assert json.loads(stdin or "") == {
        "title": "T", "body": "B", "subtitle": "S", "url": "https://x", "thread": "drupal", "id": "1",
    }  # fmt: skip
    assert fallback.calls[0][0][0] == "osascript"  # helper failed → AppleScript fallback


def test_native_notifier_asks_for_permission(tmp_path: Path) -> None:
    helper = tmp_path / "notifier"
    run = Recorder()
    MacOSNativeNotifier(helper, runner=run).authorize()
    assert run.calls == [([str(helper), "authorize"], None)]
