from __future__ import annotations

import http.client
import json
from collections.abc import Iterator
from typing import Any

import pytest

from patch_monitor.web import LocalServer, render_static_report
from patch_monitor.web.server import TOKEN_HEADER


class FakeApi:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.settings: dict[str, Any] = {}

    def snapshot(self) -> dict[str, Any]:
        return {"report": {"generated": "now", "items": [], "errors": []}, "status": self.status_dict()}

    def status_dict(self) -> dict[str, Any]:
        return {"generated": "now", "settings": self.settings}

    def request_check(self) -> None:
        self.calls.append("check")

    def update_settings(self, changes: dict[str, Any]) -> object:
        self.settings.update(changes)
        return self.settings

    def mark_all_seen(self) -> None:
        self.calls.append("seen")


@pytest.fixture
def server() -> Iterator[tuple[LocalServer, FakeApi]]:
    api = FakeApi()
    srv = LocalServer(api)
    srv.start()
    yield srv, api
    srv.stop()


def request(
    srv: LocalServer,
    method: str,
    path: str,
    *,
    body: Any = None,
    token: str | None = None,
    host: str | None = None,
    content_type: str = "application/json",
) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
    headers = {"Host": host or f"127.0.0.1:{srv.port}"}
    if token is not None:
        headers[TOKEN_HEADER] = token
    payload = None
    if body is not None:
        payload = json.dumps(body).encode()
        headers["Content-Type"] = content_type
    conn.request(method, path, body=payload, headers=headers)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, dict(resp.getheaders()), data


def test_index_embeds_token_and_sets_csp(server) -> None:
    srv, _ = server
    status, headers, body = request(srv, "GET", "/")
    assert status == 200
    assert srv.token.encode() in body
    assert "default-src 'none'" in headers["Content-Security-Policy"]


def test_static_assets_are_served(server) -> None:
    srv, _ = server
    for name, ctype in [("app.js", "text/javascript"), ("app.css", "text/css"), ("icon.png", "image/png")]:
        status, headers, _ = request(srv, "GET", f"/{name}")
        assert status == 200
        assert headers["Content-Type"].startswith(ctype)
    assert request(srv, "GET", "/../pyproject.toml")[0] == 404


def test_api_requires_token(server) -> None:
    srv, api = server
    assert request(srv, "GET", "/api/report")[0] == 403
    assert request(srv, "POST", "/api/check", body={}, token="wrong")[0] == 403
    assert api.calls == []
    status, _, body = request(srv, "GET", "/api/report", token=srv.token)
    assert status == 200
    assert json.loads(body)["report"]["items"] == []


def test_rejects_foreign_host_header(server) -> None:
    srv, _ = server
    assert request(srv, "GET", "/", host="evil.example:80")[0] == 403


def test_post_commands(server) -> None:
    srv, api = server
    assert request(srv, "POST", "/api/check", body={}, token=srv.token)[0] == 200
    assert request(srv, "POST", "/api/seen", body={}, token=srv.token)[0] == 200
    status, _, body = request(srv, "POST", "/api/settings", body={"interval_minutes": 10}, token=srv.token)
    assert status == 200
    assert json.loads(body)["settings"] == {"interval_minutes": 10}
    assert api.calls == ["check", "seen"]


def test_post_validation(server) -> None:
    srv, _ = server
    assert request(srv, "POST", "/api/settings", body=[1], token=srv.token)[0] == 400
    assert request(srv, "POST", "/api/settings", body={}, token=srv.token, content_type="text/plain")[0] == 415
    big = {"x": "a" * 20_000}
    assert request(srv, "POST", "/api/settings", body=big, token=srv.token)[0] == 413
    assert request(srv, "POST", "/api/unknown", body={}, token=srv.token)[0] == 404


def test_static_report_is_self_contained() -> None:
    page = render_static_report({"report": {"generated": "now", "items": [], "errors": []}, "status": None})
    assert "window.SPM_STATIC" in page
    assert 'src="app.js"' not in page
    assert 'href="app.css"' not in page
    assert "data:image/png;base64," in page
    assert "{{TOKEN}}" not in page


def test_static_report_escapes_script_breakout() -> None:
    page = render_static_report({"report": {"items": [{"title": "</script><b>x"}]}, "status": None})
    assert "</script><b>x" not in page
