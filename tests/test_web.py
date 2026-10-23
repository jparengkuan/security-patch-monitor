from __future__ import annotations

import http.client
import json
from collections.abc import Iterator
from typing import Any

import pytest

from patch_monitor.projects import ProjectError
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


class FakeProjects:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def list_projects(self) -> list[dict[str, Any]]:
        return list(self.items)

    def add_project(self, name: str, path: str, branch: str | None = None) -> dict[str, Any]:
        if not path.endswith("composer.lock"):
            raise ProjectError("The selected file must be named composer.lock.")
        project = {"id": str(len(self.items) + 1), "name": name, "lock_path": path, "branch": branch}
        self.items.append(project)
        return project

    def update_project(self, project_id: str, name: str, path: str, branch: str | None = None) -> dict[str, Any]:
        for project in self.items:
            if project["id"] == project_id:
                project.update(name=name, lock_path=path, branch=branch)
                return project
        raise ProjectError("Project not found.")

    def remove_project(self, project_id: str) -> None:
        if not any(p["id"] == project_id for p in self.items):
            raise ProjectError("Project not found.")
        self.items = [p for p in self.items if p["id"] != project_id]


class FakePicker:
    available = True

    def pick_composer_lock(self) -> str | None:
        return "/work/site/composer.lock"


@pytest.fixture
def server() -> Iterator[tuple[LocalServer, FakeApi]]:
    api = FakeApi()
    srv = LocalServer(api, FakeProjects())
    srv.start()
    yield srv, api
    srv.stop()


@pytest.fixture
def desktop_server() -> Iterator[LocalServer]:
    srv = LocalServer(FakeApi(), FakeProjects(), picker=FakePicker())
    srv.start()
    yield srv
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
    # Inline scripts cannot be deferred: they must come after the markup they use.
    assert page.index('id="calbody"') < page.index("window.SPM_STATIC") < page.index("</body>")


def test_static_report_escapes_script_breakout() -> None:
    page = render_static_report({"report": {"items": [{"title": "</script><b>x"}]}, "status": None})
    assert "</script><b>x" not in page


def test_project_lifecycle(server) -> None:
    srv, _ = server
    status, _, body = request(srv, "GET", "/api/projects", token=srv.token)
    assert (status, json.loads(body)) == (200, {"projects": [], "can_browse": False})

    status, _, body = request(
        srv, "POST", "/api/projects", body={"name": "Site", "path": "/s/composer.lock"}, token=srv.token
    )
    assert status == 200
    [project] = json.loads(body)["projects"]
    assert (project["name"], project["branch"]) == ("Site", None)  # empty branch means local files

    status, _, body = request(
        srv, "POST", "/api/projects", body={"name": "Dev", "path": "/d/composer.lock", "branch": "develop"},
        token=srv.token,
    )  # fmt: skip
    assert json.loads(body)["projects"][-1]["branch"] == "develop"
    request(srv, "POST", "/api/projects/remove", body={"id": json.loads(body)["projects"][-1]["id"]}, token=srv.token)

    edit = {"id": project["id"], "name": "Site 2", "path": "/s2/composer.lock", "branch": "develop"}
    status, _, body = request(srv, "POST", "/api/projects/update", body=edit, token=srv.token)
    assert {k: json.loads(body)["projects"][0][k] for k in ("name", "lock_path", "branch")} == {
        "name": "Site 2", "lock_path": "/s2/composer.lock", "branch": "develop",
    }  # fmt: skip
    unknown = {"id": "x", "name": "x", "path": "/x/composer.lock", "branch": ""}
    assert request(srv, "POST", "/api/projects/update", body=unknown, token=srv.token)[0] == 422

    status, _, body = request(srv, "POST", "/api/projects/remove", body={"id": project["id"]}, token=srv.token)
    assert (status, json.loads(body)["projects"]) == (200, [])


def test_project_errors_are_reported_to_the_ui(server) -> None:
    srv, _ = server
    status, _, body = request(
        srv, "POST", "/api/projects", body={"name": "x", "path": "/s/composer.json"}, token=srv.token
    )
    assert status == 422
    assert json.loads(body)["error"] == "The selected file must be named composer.lock."
    assert request(srv, "POST", "/api/projects/remove", body={"id": "nope"}, token=srv.token)[0] == 422
    assert request(srv, "POST", "/api/projects", body={"name": 1, "path": ""}, token=srv.token)[0] == 400
    assert request(srv, "POST", "/api/projects", body={"name": "x", "path": "/x"})[0] == 403


def test_browse_only_in_desktop_mode(server, desktop_server) -> None:
    srv, _ = server
    assert request(srv, "POST", "/api/browse", body={}, token=srv.token)[0] == 409
    status, _, body = request(desktop_server, "POST", "/api/browse", body={}, token=desktop_server.token)
    assert (status, json.loads(body)) == (200, {"path": "/work/site/composer.lock"})
    _, _, body = request(desktop_server, "GET", "/api/projects", token=desktop_server.token)
    assert json.loads(body)["can_browse"] is True
