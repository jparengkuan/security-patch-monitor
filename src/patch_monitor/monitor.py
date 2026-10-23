"""Periodic checking, new-item detection and notifications while the app is running."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from patch_monitor.impact import Impact, ImpactAssessor
from patch_monitor.models import Report
from patch_monitor.notifications import Notifier, compose, sample_notification
from patch_monitor.report import ReportProvider
from patch_monitor.seen import SeenStore
from patch_monitor.settings import Settings, SettingsStore

log = logging.getLogger(__name__)

RETRY_AFTER_FAILURE = 60.0  # seconds


@dataclass(frozen=True, slots=True)
class Status:
    generated: str | None
    last_check: str | None
    next_check: str | None
    checking: bool
    settings: Settings

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated": self.generated,
            "last_check": self.last_check,
            "next_check": self.next_check,
            "checking": self.checking,
            "settings": self.settings.to_dict(),
        }


class Monitor:
    """Owns the current report and decides when to check and whom to tell about new items.

    Collaborators are injected (report provider, stores, notifier, clock), so the
    scheduling logic can be tested without network, disk or real time.
    """

    def __init__(
        self,
        provider: ReportProvider,
        settings_store: SettingsStore,
        seen_store: SeenStore,
        notifier: Notifier,
        *,
        impacts: ImpactAssessor | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._provider = provider
        self._impacts = impacts
        self._settings_store = settings_store
        self._seen = seen_store
        self._notifier = notifier
        self._clock = clock

        self._settings = settings_store.load()
        self._report: Report | None = None
        self._new_keys: frozenset[str] = frozenset()
        self._checking = False
        self._last_check: float | None = None
        self._next_check = clock()

        self._state_lock = threading.RLock()
        self._check_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- queries ---------------------------------------------------------------

    @property
    def settings(self) -> Settings:
        return self._settings

    def status(self) -> Status:
        with self._state_lock:
            return Status(
                generated=self._report.generated_at.isoformat(timespec="seconds") if self._report else None,
                last_check=_iso(self._last_check),
                next_check=_iso(self._next_check),
                checking=self._checking,
                settings=self._settings,
            )

    def status_dict(self) -> dict[str, Any]:
        return self.status().to_dict()

    def snapshot(self) -> dict[str, Any]:
        with self._state_lock:
            report, new_keys, status = self._report, self._new_keys, self.status()
        if report is None:
            return {"report": None, "status": status.to_dict()}
        # Reading lock files happens outside the state lock.
        return {"report": report_with_impacts(report, self._impacts, new_keys), "status": status.to_dict()}

    # -- commands --------------------------------------------------------------

    def check(self) -> Report:
        """Fetch a fresh report, detect new items and notify. Safe to call from any thread."""
        with self._check_lock:
            with self._state_lock:
                self._checking = True
                settings = self._settings
            try:
                self._refresh_projects()
                report = self._provider.build(settings)
                newly_seen = self._register(report)
                if newly_seen and settings.notifications:
                    new_items = [item for item in report.items if item.key in newly_seen]
                    self._notifier.notify(compose(new_items, self._assess(report)))
                return report
            finally:
                with self._state_lock:
                    self._checking = False
                    self._last_check = self._clock()
                    self._next_check = self._last_check + self._settings.interval_minutes * 60

    def request_check(self) -> None:
        with self._state_lock:
            self._next_check = self._clock()
        self._wake.set()

    def update_settings(self, changes: dict[str, Any]) -> Settings:
        with self._state_lock:
            self._settings = self._settings.merged(changes)
            self._settings_store.save(self._settings)
            base = self._last_check if self._last_check is not None else self._clock()
            self._next_check = max(self._clock(), base + self._settings.interval_minutes * 60)
        self._wake.set()
        return self._settings

    def mark_all_seen(self) -> None:
        with self._state_lock:
            self._new_keys = frozenset()

    def send_test_notification(self) -> None:
        """Sent even when notifications are switched off, so the user can check the system side."""
        self._notifier.notify(sample_notification())

    # -- lifecycle -------------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="monitor", daemon=True)
        self._thread.start()

    def stop(self, timeout: float | None = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._state_lock:
                wait = self._next_check - self._clock()
            if wait > 0:
                self._wake.wait(timeout=wait)
                self._wake.clear()
                continue
            try:
                self.check()
            except Exception:
                log.exception("Check failed; retrying in %.0fs", RETRY_AFTER_FAILURE)
                with self._state_lock:
                    self._next_check = self._clock() + RETRY_AFTER_FAILURE

    # -- internals -------------------------------------------------------------

    def _register(self, report: Report) -> set[str]:
        """Store the report and return the keys that should trigger a notification."""
        first_run = not self._seen.is_initialized()
        notifiable = [item.key for item in report.items if item.kind.notifiable]
        newly_seen = set() if first_run else self._seen.unseen(notifiable)
        self._seen.remember((item.key for item in report.items), report.generated_at.date())
        with self._state_lock:
            current = {item.key for item in report.items}
            # New items stay highlighted until the user marks them as seen.
            self._new_keys = frozenset((self._new_keys & current) | newly_seen)
            self._report = report
        if first_run:
            log.info("First run: %d items recorded without notifications", len(report.items))
        return newly_seen

    def _assess(self, report: Report) -> dict[str, list[Impact]]:
        return assess_safely(self._impacts, report)

    def _refresh_projects(self) -> None:
        if self._impacts is None:
            return
        try:
            self._impacts.refresh()
        except Exception:
            log.exception("Refreshing project sources failed")


def assess_safely(assessor: ImpactAssessor | None, report: Report) -> dict[str, list[Impact]]:
    """Impact per item key; analysis problems are logged and never break the report."""
    if assessor is None:
        return {}
    try:
        return assessor.assess(report.items)
    except Exception:
        log.exception("Impact analysis failed")
        return {}


def report_with_impacts(
    report: Report, assessor: ImpactAssessor | None, new_keys: frozenset[str] = frozenset()
) -> dict[str, Any]:
    """The report as sent to the UI, with the affected projects attached to each item."""
    impacts = assess_safely(assessor, report)
    data = report.to_dict(new_keys)
    for item in data["items"]:
        item["impacts"] = [impact.to_dict() for impact in impacts.get(item["key"], [])]
        # "Not affected" is only meaningful when a project of the matching type was checked:
        # a Drupal advisory says nothing about a Next.js-only setup, and vice versa.
        item["checked"] = bool(assessor and item["package"] and assessor.checks(item["package"]))
    data["projects_configured"] = assessor.has_projects() if assessor else False
    return data


def _iso(timestamp: float | None) -> str | None:
    return datetime.fromtimestamp(timestamp).isoformat(timespec="seconds") if timestamp else None
