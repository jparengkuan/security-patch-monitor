"""Access to the bundled front-end files and rendering of the HTML entry points."""

from __future__ import annotations

import base64
import html
import json
from functools import cache
from importlib import resources
from typing import Any

STATIC_PACKAGE = "patch_monitor.web"
STATIC_DIR = "static"

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".png": "image/png",
}
PUBLIC_ASSETS = frozenset({"app.css", "app.js", "icon.png"})


@cache
def read_asset(name: str) -> bytes:
    return (resources.files(STATIC_PACKAGE) / STATIC_DIR / name).read_bytes()


def content_type(name: str) -> str:
    return CONTENT_TYPES.get(name[name.rfind(".") :], "application/octet-stream")


def render_index(token: str) -> bytes:
    """The app page served by the local server; the API token is passed via a meta tag."""
    page = read_asset("index.html").decode("utf-8")
    return page.replace("{{TOKEN}}", html.escape(token, quote=True)).encode("utf-8")


def render_static_report(snapshot: dict[str, Any]) -> str:
    """A single self-contained HTML file (styles, script, icon and data inlined)."""
    page = read_asset("index.html").decode("utf-8").replace("{{TOKEN}}", "")
    css = read_asset("app.css").decode("utf-8")
    js = read_asset("app.js").decode("utf-8")
    icon = base64.b64encode(read_asset("icon.png")).decode("ascii")
    data = json.dumps(snapshot).replace("</", "<\\/")
    replacements = {
        '<link rel="stylesheet" href="app.css">': f"<style>\n{css}\n</style>",
        '<script src="app.js" defer></script>': (
            f"<script>window.SPM_STATIC = {data};</script>\n<script>\n{js}\n</script>"
        ),
        'href="icon.png"': f'href="data:image/png;base64,{icon}"',
        'src="icon.png"': f'src="data:image/png;base64,{icon}"',
    }
    for old, new in replacements.items():
        if old not in page:
            raise RuntimeError(f"index.html is missing the expected snippet: {old}")
        page = page.replace(old, new)
    return page
