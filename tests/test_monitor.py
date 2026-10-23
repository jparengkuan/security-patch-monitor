from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from patch_monitor.models import Item, Kind, Product, Report, Severity
from patch_monitor.monitor import Monitor
from patch_monitor.notifications import Notification, compose
from patch_monitor.report import ReportBuilder
from patch_monitor.seen import JsonSeenStore
from patch_monitor.settings import JsonSettingsStore, Settings
from patch_monitor.sources import SourceContext


def item(title: str, kind: Kind = Kind.ADVISORY, severity: Severity = Severity.HIGH) -> Item:
    return Item(Product.DRUPAL, kind, title, date(2026, 10, 21), severity=severity)


class StubProvider:
    def __init__(self) -> None:
        self.items: list[Item] = []

    def build(self, settings: Settings) -> Report:
        return Report(datetime(2026, 10, 23, tzinfo=UTC), tuple(self.items))


class RecordingNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def notify(self, notification: Notification) -> None:
        self.sent.append((notification.title, notification.subtitle, notification.body))


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def parts(tmp_path: Path) -> tuple[Monitor, StubProvider, RecordingNotifier, FakeClock]:
    provider, notifier, clock = StubProvider(), RecordingNotifier(), FakeClock()
    monitor = Monitor(
        provider,
        JsonSettingsStore(tmp_path / "settings.json"),
        JsonSeenStore(tmp_path / "seen.json"),
        notifier,
        clock=clock,
    )
    return monitor, provider, notifier, clock


def test_first_run_records_items_without_notifying(parts) -> None:
    monitor, provider, notifier, _ = parts
    provider.items = [item("A"), item("B")]
    monitor.check()
    assert notifier.sent == []
    assert not any(i["new"] for i in monitor.snapshot()["report"]["items"])


def test_new_items_are_notified_and_highlighted(parts) -> None:
    monitor, provider, notifier, _ = parts
    provider.items = [item("A")]
    monitor.check()
    provider.items = [item("A"), item("B", Kind.UPCOMING, Severity.CRITICAL), item("C")]
    monitor.check()
    assert notifier.sent == [("2 new security items", "1 announced · 1 high", "Drupal: B\nDrupal: C")]
    new = {i["title"] for i in monitor.snapshot()["report"]["items"] if i["new"]}
    assert new == {"B", "C"}


def test_expected_items_never_notify(parts) -> None:
    monitor, provider, notifier, _ = parts
    monitor.check()
    provider.items = [item("window", Kind.EXPECTED)]
    monitor.check()
    assert notifier.sent == []


def test_highlights_survive_until_marked_seen(parts) -> None:
    monitor, provider, _, _ = parts
    monitor.check()
    provider.items = [item("B")]
    monitor.check()
    monitor.check()  # nothing new now, but B stays highlighted
    assert [i["new"] for i in monitor.snapshot()["report"]["items"]] == [True]
    monitor.mark_all_seen()
    assert [i["new"] for i in monitor.snapshot()["report"]["items"]] == [False]


def test_items_reappearing_after_source_failure_do_not_renotify(parts) -> None:
    monitor, provider, notifier, _ = parts
    provider.items = [item("A")]
    monitor.check()
    provider.items = []  # source failed this time
    monitor.check()
    provider.items = [item("A")]
    monitor.check()
    assert notifier.sent == []


def test_test_notification_is_sent_even_when_notifications_are_off(parts) -> None:
    monitor, _, notifier, _ = parts
    monitor.update_settings({"notifications": False})
    monitor.send_test_notification()
    assert notifier.sent == [
        ("Security Patch Monitor notifications are working", "", "New security advisories will appear like this.")
    ]


def test_notifications_can_be_disabled(parts) -> None:
    monitor, provider, notifier, _ = parts
    monitor.check()
    monitor.update_settings({"notifications": False})
    provider.items = [item("B")]
    monitor.check()
    assert notifier.sent == []


def test_update_settings_validates_persists_and_reschedules(parts, tmp_path: Path) -> None:
    monitor, _, _, clock = parts
    monitor.check()
    settings = monitor.update_settings({"interval_minutes": "15", "notifications": "yes", "bogus": 1})
    assert settings == Settings(interval_minutes=15, notifications=True)
    assert JsonSettingsStore(tmp_path / "settings.json").load() == settings
    assert monitor.status().next_check == datetime.fromtimestamp(clock.now + 15 * 60).isoformat(timespec="seconds")
    assert monitor.update_settings({"interval_minutes": 0}).interval_minutes == 1


def test_report_builder_isolates_failing_sources() -> None:
    class Good:
        name = "good"

        def collect(self, ctx: SourceContext) -> list[Item]:
            return [item("A"), item("A")]

    class Broken:
        name = "broken"

        def collect(self, ctx: SourceContext) -> list[Item]:
            raise ValueError("boom")

    from conftest import FakeHttp

    report = ReportBuilder([Good(), Broken()], FakeHttp()).build(Settings())
    assert [i.title for i in report.items] == ["A"]  # deduplicated
    assert report.errors == ("broken: boom",)


def test_compose_single_drupal_advisory() -> None:
    advisory = item("ABC - Critical - Cross-site scripting - SA-CONTRIB-2026-224", severity=Severity.CRITICAL)
    notification = compose([advisory])
    assert (notification.title, notification.subtitle, notification.body) == (
        "New critical Drupal advisory",
        "ABC · SA-CONTRIB-2026-224",
        "Cross-site scripting",
    )
    assert notification.thread == "drupal"
    with pytest.raises(ValueError, match="Nothing"):
        compose([])


def test_compose_nextjs_and_announcement() -> None:
    nextjs = Item(
        Product.NEXTJS, Kind.ADVISORY, "Next.js: Denial of Service in App Router (CVE-2026-94482)",
        date(2026, 10, 22), url="https://github.com/advisories/x", severity=Severity.HIGH,
    )  # fmt: skip
    n = compose([nextjs])
    assert (n.title, n.subtitle, n.body, n.url) == (
        "New high Next.js advisory", "CVE-2026-94482", "Denial of Service in App Router", "https://github.com/advisories/x",
    )  # fmt: skip
    release = Item(Product.NODE, Kind.UPCOMING, "Tuesday, November 10, 2026 Security Releases", date(2026, 11, 10))
    assert compose([release]).title == "Node.js security release announced for Tue 10 Nov"
