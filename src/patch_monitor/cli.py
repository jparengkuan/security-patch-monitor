"""Command-line entry point and composition root: the only place where concrete classes are wired."""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import signal
import threading
import webbrowser
from collections.abc import Sequence
from pathlib import Path

from patch_monitor import APP_NAME, __version__
from patch_monitor.desktop import DesktopUnavailableError, run_window
from patch_monitor.http import UrllibHttpClient
from patch_monitor.logging_setup import configure_logging
from patch_monitor.monitor import Monitor
from patch_monitor.notifications import platform_notifier
from patch_monitor.report import ReportBuilder
from patch_monitor.seen import JsonSeenStore
from patch_monitor.settings import JsonSettingsStore
from patch_monitor.sources import default_sources
from patch_monitor.storage import AppPaths
from patch_monitor.web import LocalServer, render_static_report

log = logging.getLogger(__name__)

BROWSER_PORT = 8765


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="security-patch-monitor",
        description="Calendar of announced, expected and released security patches for "
        "Drupal, PHP, Next.js and Node.js.",
    )
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--browser", action="store_true", help="open the UI in a browser instead of a window")
    output.add_argument("--json", action="store_true", help="print one report as JSON and exit")
    output.add_argument("--html", metavar="FILE", type=Path, help="write a self-contained HTML report and exit")
    parser.add_argument("--port", type=int, default=0, help="UI port (default: random, or 8765 with --browser)")
    parser.add_argument("--data-dir", type=Path, help="where settings, state and logs are stored")
    parser.add_argument("-v", "--verbose", action="store_true", help="verbose logging")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def build_report_builder() -> ReportBuilder:
    http = UrllibHttpClient(
        user_agent=f"security-patch-monitor/{__version__}",
        github_token=os.environ.get("GITHUB_TOKEN"),
    )
    return ReportBuilder(default_sources(), http)


def build_monitor(paths: AppPaths) -> Monitor:
    return Monitor(
        provider=build_report_builder(),
        settings_store=JsonSettingsStore(paths.settings_file),
        seen_store=JsonSeenStore(paths.seen_file),
        notifier=platform_notifier(),
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    paths = AppPaths.default(args.data_dir)
    one_shot = args.json or args.html
    configure_logging(None if one_shot else paths.log_file, verbose=args.verbose)

    if one_shot:
        # One-shot reports never touch the notification state of the running app.
        report = build_report_builder().build(JsonSettingsStore(paths.settings_file).load())
        if args.json:
            print(json.dumps(report.to_dict(), indent=2))
        else:
            snapshot = {"report": report.to_dict(), "status": None}
            args.html.write_text(render_static_report(snapshot), encoding="utf-8")
            print(f"Report written to {args.html}")
        return 1 if report.errors and not report.items else 0

    port = args.port or (BROWSER_PORT if args.browser else 0)
    return run_app(build_monitor(paths), port=port, browser=args.browser)


def run_app(monitor: Monitor, *, port: int, browser: bool) -> int:
    server = LocalServer(monitor, port=port)
    server.start()
    monitor.start()
    log.info("%s %s started", APP_NAME, __version__)
    try:
        if not browser:
            try:
                run_window(server.url)
                return 0
            except DesktopUnavailableError as exc:
                log.warning("%s – falling back to the browser", exc)
        print(f"{APP_NAME} running at {server.url}  (Ctrl+C to stop)")
        webbrowser.open(server.url)
        _wait_for_interrupt()
        return 0
    finally:
        monitor.stop()
        server.stop()
        log.info("%s stopped", APP_NAME)


def _wait_for_interrupt() -> None:
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    with contextlib.suppress(KeyboardInterrupt):
        stop.wait()
