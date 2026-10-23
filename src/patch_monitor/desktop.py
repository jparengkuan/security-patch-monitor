"""Native desktop window (pywebview) around the local UI."""

from __future__ import annotations

import logging
import platform
from importlib import resources
from types import ModuleType

from patch_monitor import APP_NAME

log = logging.getLogger(__name__)

DEFAULT_SIZE = (1280, 820)
MIN_SIZE = (760, 560)
SCREEN_FRACTION = (0.9, 0.88)
MAX_WIDTH = 1600


class DesktopUnavailableError(RuntimeError):
    """pywebview is not installed or cannot start on this system."""


def load_webview() -> ModuleType:
    try:
        import webview
    except ImportError as exc:
        raise DesktopUnavailableError(
            "pywebview is not installed. Install with: pip install 'security-patch-monitor[desktop]'"
        ) from exc
    return webview


def run_window(url: str) -> None:
    """Open the app window and block until it is closed."""
    webview = load_webview()
    _set_macos_identity()
    width, height = _window_size(webview)
    webview.create_window(APP_NAME, url, width=width, height=height, min_size=MIN_SIZE)
    webview.start()


def _window_size(webview: ModuleType) -> tuple[int, int]:
    """About 90% of the screen, so the full month view fits without clipping."""
    try:
        screen = webview.screens[0]
        return min(MAX_WIDTH, int(screen.width * SCREEN_FRACTION[0])), int(screen.height * SCREEN_FRACTION[1])
    except (AttributeError, IndexError, TypeError):
        return DEFAULT_SIZE


def _set_macos_identity() -> None:
    """Show the app's own name and icon in the Dock and menu bar instead of 'Python'."""
    if platform.system() != "Darwin":
        return
    try:
        from AppKit import NSApplication, NSImage  # provided by pyobjc, a pywebview dependency
        from Foundation import NSBundle
    except ImportError:
        return
    info = NSBundle.mainBundle().infoDictionary()
    if info is not None:
        info["CFBundleName"] = APP_NAME
    icon = resources.files("patch_monitor.web") / "static" / "icon-512.png"
    with resources.as_file(icon) as path:
        if path.exists():
            image = NSImage.alloc().initWithContentsOfFile_(str(path))
            NSApplication.sharedApplication().setApplicationIconImage_(image)
