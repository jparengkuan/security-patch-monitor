from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from patch_monitor.notifications import LinuxNotifier, MacOSNotifier, WindowsNotifier
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


def test_notifiers_escape_user_content() -> None:
    calls: list[list[str]] = []
    MacOSNotifier(calls.append).notify('Say "hi"', "it's \\ fine")
    assert calls[-1][:2] == ["osascript", "-e"]
    assert '"Say \\"hi\\""' in calls[-1][2]

    WindowsNotifier(calls.append).notify("O'Brien", "x")
    assert "'O''Brien'" in calls[-1][-1]

    LinuxNotifier(runner=calls.append).notify("t", "m")
    assert calls[-1][-2:] == ["t", "m"]
