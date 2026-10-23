"""Local web UI: an HTTP API plus static assets, rendered in a desktop window or browser."""

from patch_monitor.web.assets import render_static_report
from patch_monitor.web.server import LocalServer

__all__ = ["LocalServer", "render_static_report"]
