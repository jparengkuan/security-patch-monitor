"""Loopback-only HTTP server for the UI.

Hardening, since any local web page could otherwise talk to it:
  * binds to 127.0.0.1 only;
  * rejects requests whose Host header is not this server (blocks DNS rebinding);
  * every API call must carry a per-session random token (blocks cross-site requests);
  * request bodies are size-limited JSON; responses carry a strict Content-Security-Policy.
"""

from __future__ import annotations

import hmac
import json
import logging
import secrets
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Protocol
from urllib.parse import urlsplit

from patch_monitor.projects import ProjectError
from patch_monitor.web.assets import PUBLIC_ASSETS, content_type, read_asset, render_index

log = logging.getLogger(__name__)

TOKEN_HEADER = "X-SPM-Token"  # noqa: S105 - header name, not a secret
MAX_BODY_BYTES = 16 * 1024
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


class MonitorApi(Protocol):
    """What the web layer needs from the monitor (interface segregation)."""

    def snapshot(self) -> dict[str, Any]: ...

    def status_dict(self) -> dict[str, Any]: ...

    def request_check(self) -> None: ...

    def update_settings(self, changes: dict[str, Any]) -> object: ...

    def mark_all_seen(self) -> None: ...


class ProjectsApi(Protocol):
    """Project management as needed by the web layer; raises ``ProjectError`` for invalid input."""

    def list_projects(self) -> list[dict[str, Any]]: ...

    def add_project(self, name: str, path: str, branch: str | None = None) -> dict[str, Any]: ...

    def update_project(self, project_id: str, name: str, path: str, branch: str | None = None) -> dict[str, Any]: ...

    def remove_project(self, project_id: str) -> None: ...


class FilePicker(Protocol):
    """Native "open file" dialog; only available in the desktop window."""

    @property
    def available(self) -> bool: ...

    def pick_lock_file(self) -> str | None: ...


class NoFilePicker:
    available = False

    def pick_lock_file(self) -> str | None:
        return None


class _RequestError(Exception):
    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status


class LocalServer:
    def __init__(
        self,
        api: MonitorApi,
        projects: ProjectsApi,
        *,
        picker: FilePicker | None = None,
        port: int = 0,
        host: str = "127.0.0.1",
    ) -> None:
        self.token = secrets.token_urlsafe(32)
        self._api = api
        self._projects = projects
        self._picker: FilePicker = picker or NoFilePicker()
        self._httpd = ThreadingHTTPServer((host, port), self._handler_class())
        self._httpd.daemon_threads = True
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self._httpd.server_address[1])

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def start(self) -> None:
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="http", daemon=True)
        self._thread.start()
        log.info("UI server listening on %s", self.url)

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def _handler_class(self) -> type[BaseHTTPRequestHandler]:
        server = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "SecurityPatchMonitor"
            sys_version = ""

            def do_GET(self) -> None:
                self._dispatch(server._get)

            def do_POST(self) -> None:
                self._dispatch(server._post)

            def _dispatch(self, route: Any) -> None:
                try:
                    server._check_host(self.headers.get("Host", ""))
                    status, ctype, body = route(self)
                except _RequestError as exc:
                    status, ctype = exc.status, "application/json"
                    body = json.dumps({"error": str(exc)}).encode()
                except Exception:
                    log.exception("Unhandled error for %s %s", self.command, self.path)
                    status, ctype = HTTPStatus.INTERNAL_SERVER_ERROR, "application/json"
                    body = b'{"error": "internal error"}'
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                if ctype.startswith("text/html"):
                    self.send_header("Content-Security-Policy", CSP)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:
                log.debug("%s - %s", self.address_string(), format % args)

        return Handler

    # -- routing -----------------------------------------------------------------

    def _get(self, req: BaseHTTPRequestHandler) -> tuple[HTTPStatus, str, bytes]:
        path = urlsplit(req.path).path
        if path == "/":
            return HTTPStatus.OK, content_type(".html"), render_index(self.token)
        if path.lstrip("/") in PUBLIC_ASSETS:
            name = path.lstrip("/")
            return HTTPStatus.OK, content_type(name), read_asset(name)
        if path == "/api/report":
            self._check_token(req)
            return self._json(self._api.snapshot())
        if path == "/api/status":
            self._check_token(req)
            return self._json(self._api.status_dict())
        if path == "/api/projects":
            self._check_token(req)
            return self._projects_response()
        raise _RequestError(HTTPStatus.NOT_FOUND, "not found")

    def _post(self, req: BaseHTTPRequestHandler) -> tuple[HTTPStatus, str, bytes]:
        self._check_token(req)
        path = urlsplit(req.path).path
        body = self._read_json(req)
        if path.startswith("/api/projects") or path == "/api/browse":
            return self._post_projects(path, body)
        if path == "/api/settings":
            self._api.update_settings(body)
        elif path == "/api/check":
            self._api.request_check()
        elif path == "/api/seen":
            self._api.mark_all_seen()
        else:
            raise _RequestError(HTTPStatus.NOT_FOUND, "not found")
        return self._json(self._api.status_dict())

    def _post_projects(self, path: str, body: dict[str, Any]) -> tuple[HTTPStatus, str, bytes]:
        try:
            if path == "/api/projects":
                self._projects.add_project(
                    _str_field(body, "name"), _str_field(body, "path"), _str_field(body, "branch") or None
                )
            elif path == "/api/projects/update":
                self._projects.update_project(
                    _str_field(body, "id"),
                    _str_field(body, "name"),
                    _str_field(body, "path"),
                    _str_field(body, "branch") or None,
                )
            elif path == "/api/projects/remove":
                self._projects.remove_project(_str_field(body, "id"))
            elif path == "/api/browse":
                if not self._picker.available:
                    raise _RequestError(HTTPStatus.CONFLICT, "file browsing is only available in the desktop app")
                return self._json({"path": self._picker.pick_lock_file()})
            else:
                raise _RequestError(HTTPStatus.NOT_FOUND, "not found")
        except ProjectError as exc:
            raise _RequestError(HTTPStatus.UNPROCESSABLE_ENTITY, str(exc)) from exc
        return self._projects_response()

    def _projects_response(self) -> tuple[HTTPStatus, str, bytes]:
        return self._json({"projects": self._projects.list_projects(), "can_browse": self._picker.available})

    # -- guards ------------------------------------------------------------------

    def _check_host(self, host: str) -> None:
        if host not in {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}:
            raise _RequestError(HTTPStatus.FORBIDDEN, "invalid host")

    def _check_token(self, req: BaseHTTPRequestHandler) -> None:
        supplied = req.headers.get(TOKEN_HEADER, "")
        if not hmac.compare_digest(supplied.encode(), self.token.encode()):
            raise _RequestError(HTTPStatus.FORBIDDEN, "invalid token")

    @staticmethod
    def _read_json(req: BaseHTTPRequestHandler) -> dict[str, Any]:
        if not req.headers.get("Content-Type", "").startswith("application/json"):
            raise _RequestError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "expected application/json")
        try:
            length = int(req.headers.get("Content-Length") or 0)
        except ValueError as exc:
            raise _RequestError(HTTPStatus.BAD_REQUEST, "invalid Content-Length") from exc
        if length > MAX_BODY_BYTES:
            raise _RequestError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "body too large")
        try:
            data = json.loads(req.rfile.read(length) or b"{}")
        except json.JSONDecodeError as exc:
            raise _RequestError(HTTPStatus.BAD_REQUEST, "invalid JSON") from exc
        if not isinstance(data, dict):
            raise _RequestError(HTTPStatus.BAD_REQUEST, "expected a JSON object")
        return data

    @staticmethod
    def _json(data: Any) -> tuple[HTTPStatus, str, bytes]:
        return HTTPStatus.OK, "application/json", json.dumps(data).encode()


def _str_field(body: dict[str, Any], key: str) -> str:
    value = body.get(key, "")
    if not isinstance(value, str):
        raise _RequestError(HTTPStatus.BAD_REQUEST, f"{key} must be a string")
    return value
