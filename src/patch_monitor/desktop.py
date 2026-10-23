"""Native desktop window (pywebview) around the local UI."""

from __future__ import annotations

import logging
import platform
from importlib import resources
from pathlib import Path
from types import ModuleType
from typing import Any

from patch_monitor import APP_NAME

log = logging.getLogger(__name__)

DEFAULT_SIZE = (1280, 820)
MIN_SIZE = (760, 560)
SCREEN_FRACTION = (0.9, 0.88)
MAX_WIDTH = 1600
LOCK_FILE_TYPES = ("Composer lock file (*.lock)", "All files (*.*)")


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


class WebviewFilePicker:
    """Native "open file" dialog of the app window. Unavailable until a window is attached."""

    def __init__(self) -> None:
        self._window: Any = None
        self._webview: ModuleType | None = None

    def attach(self, webview: ModuleType, window: Any) -> None:
        self._webview, self._window = webview, window

    @property
    def available(self) -> bool:
        return self._window is not None

    def pick_composer_lock(self) -> str | None:
        if self._window is None or self._webview is None:
            return None
        selection = self._window.create_file_dialog(
            self._webview.FileDialog.OPEN,
            directory=str(Path.home()),
            allow_multiple=False,
            file_types=LOCK_FILE_TYPES,
        )
        return str(selection[0]) if selection else None


def run_window(url: str, picker: WebviewFilePicker | None = None) -> None:
    """Open the app window and block until it is closed."""
    webview = load_webview()
    _set_macos_identity()
    width, height = _window_size(webview)
    window = webview.create_window(APP_NAME, url, width=width, height=height, min_size=MIN_SIZE)
    if picker is not None:
        picker.attach(webview, window)
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
