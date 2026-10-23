from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from patch_monitor.drupal_config import (
    EnabledState,
    enabled_status,
    extension_names,
    load_extension_config,
    parse_core_extension,
)
from patch_monitor.impact import ImpactAnalyzer
from patch_monitor.models import Item, Kind, Product, Report, Severity
from patch_monitor.monitor import report_with_impacts
from patch_monitor.notifications import compose
from patch_monitor.project_files import GitSync, WorkingTreeFiles
from patch_monitor.project_reader import ProjectReader
from patch_monitor.projects import JsonProjectStore, Project, ProjectError, ProjectService, validate_branch
from patch_monitor.version import Version, match_constraint

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


# -- helpers ----------------------------------------------------------------------------


def write_site(
    root: Path,
    packages: dict[str, str],
    *,
    dev: dict[str, str] | None = None,
    enabled: tuple[str, ...] | None = None,
    splits: dict[str, tuple[str, ...]] | None = None,
) -> Path:
    """A minimal Drupal project: composer.lock and (optionally) exported core.extension.yml files."""
    root.mkdir(parents=True, exist_ok=True)
    lock: dict[str, Any] = {
        "packages": [{"name": name, "version": version} for name, version in packages.items()],
        "packages-dev": [{"name": name, "version": version} for name, version in (dev or {}).items()],
    }
    (root / "composer.lock").write_text(json.dumps(lock))
    if enabled is not None:
        write_extensions(root / "config" / "sync", enabled)
    for split, modules in (splits or {}).items():
        write_extensions(root / "config" / "split" / split, modules)
    return root / "composer.lock"


def write_extensions(directory: Path, modules: tuple[str, ...]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    body = "".join(f"  {name}: 0\n" for name in modules)
    (directory / "core.extension.yml").write_text(
        f"_core:\n  default_config_hash: abc\nmodule:\n{body}theme:\n  claro: 0\nprofile: standard\n"
    )


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "-c", "init.defaultBranch=develop",
         *args],
        cwd=cwd, capture_output=True, text=True, check=True,
    )  # fmt: skip
    return result.stdout


def advisory(package: str, affected: str, title: str = "Module - Critical - RCE - SA-CONTRIB-2026-1") -> Item:
    return Item(
        Product.DRUPAL, Kind.ADVISORY, title, date(2026, 10, 21),
        severity=Severity.CRITICAL, package=package, affected=affected,
    )  # fmt: skip


@pytest.fixture
def reader() -> ProjectReader:
    return ProjectReader(GitSync(min_interval=0))


@pytest.fixture
def service(tmp_path: Path, reader: ProjectReader) -> ProjectService:
    return ProjectService(JsonProjectStore(tmp_path / "data" / "projects.json"), reader)


# -- version matching -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("version", "constraint", "affected", "fixed_in"),
    [
        ("4.1.0", "<4.2.1", True, "4.2.1"),
        ("4.2.1", "<4.2.1", False, None),
        ("4.2.1-beta1", "<4.2.1", True, "4.2.1"),
        ("5.0.1", "<4.2.1 || >=5.0.0 <5.0.3", True, "5.0.3"),
        ("5.0.3", "<4.2.1 || >=5.0.0 <5.0.3", False, None),
        ("6.1.1", ">=6.1.0 <6.1.3", True, "6.1.3"),
        ("8.x-1.4", "<8.x-1.5", True, "1.5.0"),
        ("16.2.0", ">= 16.0.0, <= 16.4.0", True, None),
    ],
)
def test_match_constraint(version: str, constraint: str, affected: bool, fixed_in: str | None) -> None:
    match = match_constraint(version, constraint)
    assert match is not None
    assert (match.affected, match.fixed_in) == (affected, fixed_in)


def test_match_constraint_unknown_inputs() -> None:
    assert match_constraint("dev-1.x", "<1.2.0") is None
    assert match_constraint("1.0.0", None) is None
    assert match_constraint("1.0.0", "see advisory") is None


def test_version_ordering() -> None:
    assert Version.parse("1.0.0-alpha1") < Version.parse("1.0.0") < Version.parse("1.0.1")  # type: ignore[operator]
    assert str(Version.parse("v2.1")) == "2.1.0"


# -- core.extension.yml -----------------------------------------------------------------


def test_parse_core_extension_reads_modules_and_themes_only() -> None:
    text = (
        "_core:\n  default_config_hash: x\n"
        "module:\n  node: 0\n  simple_oauth: 10\n"
        "theme:\n  olivero: 0\n"
        "profile: minimal\n"
    )
    assert parse_core_extension(text) == {"node", "simple_oauth", "olivero"}


def test_load_extension_config_prefers_sync_and_reads_splits(tmp_path: Path) -> None:
    write_site(tmp_path, {}, enabled=("node",), splits={"dev": ("devel",)})
    write_extensions(tmp_path / "web" / "profiles" / "custom" / "x" / "config" / "install", ("ignored",))
    config = load_extension_config(WorkingTreeFiles(tmp_path))
    assert config is not None
    assert config.source == "config/sync/core.extension.yml"
    assert config.extensions == {"node", "claro"}
    assert config.splits == {"dev": frozenset({"devel", "claro"})}


def test_extension_names_include_submodules(tmp_path: Path) -> None:
    module = tmp_path / "web" / "modules" / "contrib" / "simple_oauth"
    (module / "modules" / "simple_oauth_extras").mkdir(parents=True)
    (module / "simple_oauth.info.yml").write_text("name: Simple OAuth")
    (module / "modules" / "simple_oauth_extras" / "simple_oauth_extras.info.yml").write_text("name: Extras")
    names = extension_names([WorkingTreeFiles(tmp_path)], "simple_oauth")
    assert names == {"simple_oauth", "simple_oauth_extras"}


def test_enabled_status_states(tmp_path: Path) -> None:
    write_site(tmp_path, {}, enabled=("node", "simple_oauth_extras"), splits={"dev": ("devel",)})
    config = load_extension_config(WorkingTreeFiles(tmp_path))
    enabled = enabled_status(config, frozenset({"simple_oauth", "simple_oauth_extras"}))
    assert (enabled.state, enabled.names) == (EnabledState.ENABLED, ("simple_oauth_extras",))
    split = enabled_status(config, frozenset({"devel"}))
    assert (split.state, split.splits) == (EnabledState.SPLIT_ONLY, ("dev",))
    assert enabled_status(config, frozenset({"webform"})).state is EnabledState.NOT_ENABLED
    assert enabled_status(None, frozenset({"webform"})).state is EnabledState.UNKNOWN


# -- project service ---------------------------------------------------------------------


def test_add_list_and_remove_local_project(service: ProjectService, tmp_path: Path, reader: ProjectReader) -> None:
    lock = write_site(
        tmp_path / "site", {"drupal/core": "10.6.3", "drupal/webform": "6.2.0", "drupal/token": "1.15.0"},
        enabled=("webform",),
    )  # fmt: skip
    added = service.add_project("  Customer   portal ", str(lock.parent))  # a directory is accepted too
    assert added["name"] == "Customer portal"
    assert added["lock_path"] == str(lock.resolve())
    assert added["branch"] is None
    assert added["source"] == "Local files"
    assert added["summary"] == {"ecosystem": "composer", "drupal_core": "10.6.3", "drupal_projects": 2}
    assert added["ecosystem"] == "composer"
    assert added["config"] == {"source": "config/sync/core.extension.yml", "enabled": 2, "splits": []}

    reloaded = ProjectService(JsonProjectStore(tmp_path / "data" / "projects.json"), reader)
    assert [p["name"] for p in reloaded.list_projects()] == ["Customer portal"]

    service.remove_project(added["id"])
    assert service.list_projects() == []
    with pytest.raises(ProjectError, match="not found"):
        service.remove_project(added["id"])


@pytest.mark.parametrize(
    ("name", "path_name", "content", "message"),
    [
        ("", "composer.lock", "{}", "Enter a project name"),
        ("x" * 81, "composer.lock", "{}", "at most 80"),
        ("Site", "composer.json", "{}", "must be one of"),
        ("Site", "composer.lock", "{not json", "not valid JSON"),
        ("Site", "composer.lock", '{"name": "x"}', r"not a valid composer\.lock"),
    ],
)
def test_add_project_validation(
    service: ProjectService, tmp_path: Path, name: str, path_name: str, content: str, message: str
) -> None:
    path = tmp_path / path_name
    path.write_text(content)
    with pytest.raises(ProjectError, match=message):
        service.add_project(name, str(path))


def test_add_project_rejects_missing_file_and_duplicates(service: ProjectService, tmp_path: Path) -> None:
    with pytest.raises(ProjectError, match="No lock file found"):
        service.add_project("Site", str(tmp_path / "missing"))
    lock = write_site(tmp_path / "a", {})
    service.add_project("Site", str(lock))
    with pytest.raises(ProjectError, match="already exists"):
        service.add_project("site", str(write_site(tmp_path / "b", {})))
    with pytest.raises(ProjectError, match="already added"):
        service.add_project("Other", str(lock))


@pytest.mark.parametrize("branch", ["../etc", "-rf", "feature..x", "a b", "x/"])
def test_branch_names_are_validated(branch: str) -> None:
    with pytest.raises(ProjectError, match="not a valid branch"):
        validate_branch(branch)


def test_branch_name_normalisation() -> None:
    assert validate_branch("origin/develop") == "develop"
    assert validate_branch("  ") is None
    assert validate_branch("release/2026.10") == "release/2026.10"


def test_list_reports_unreadable_lock(service: ProjectService, tmp_path: Path) -> None:
    lock = write_site(tmp_path / "site", {})
    service.add_project("Site", str(lock))
    lock.unlink()
    [project] = service.list_projects()
    assert project["summary"] is None
    assert "not found" in project["error"]


# -- git sources ------------------------------------------------------------------------


@pytest.fixture
def git_project(tmp_path: Path) -> tuple[Path, Path, Path]:
    """origin (bare) ← publisher pushes develop; checkout is an older clone with the site in a subdir."""
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", str(origin))
    publisher = tmp_path / "publisher"
    git(tmp_path, "clone", str(origin), str(publisher))
    write_site(publisher / "site", {"drupal/simple_oauth": "6.1.3"}, enabled=("simple_oauth",))
    git(publisher, "add", "-A")
    git(publisher, "commit", "-m", "initial")
    git(publisher, "push", "origin", "HEAD:develop")
    checkout = tmp_path / "checkout"
    git(tmp_path, "clone", "--branch", "develop", str(origin), str(checkout))
    return origin, publisher, checkout


@needs_git
def test_git_project_reads_latest_develop_not_the_working_copy(
    service: ProjectService, reader: ProjectReader, git_project: tuple[Path, Path, Path]
) -> None:
    _, publisher, checkout = git_project
    added = service.add_project("Portal", str(checkout / "site" / "composer.lock"), "develop")
    assert added["source"].startswith("origin/develop @ ")
    assert added["config"]["source"] == "config/sync/core.extension.yml"

    # A colleague pushes a downgrade to develop; the local checkout is not pulled.
    write_site(publisher / "site", {"drupal/simple_oauth": "6.1.1"}, enabled=("simple_oauth",))
    git(publisher, "commit", "-am", "downgrade")
    git(publisher, "push", "origin", "HEAD:develop")
    local = json.loads((checkout / "site" / "composer.lock").read_text())
    assert local["packages"][0]["version"] == "6.1.3"  # working copy is untouched

    analyzer = ImpactAnalyzer(service.projects, reader)
    item = advisory("drupal/simple_oauth", ">=6.1.0 <6.1.3")
    assert analyzer.assess([item]) == {}  # not fetched yet: last known develop is safe
    analyzer.refresh()
    [impact] = analyzer.assess([item])[item.key]
    assert (impact.installed, impact.fixed_in, impact.enabled.state) == ("6.1.1", "6.1.3", EnabledState.ENABLED)
    assert impact.source.startswith("origin/develop @ ")
    assert git(checkout, "rev-parse", "HEAD") != git(checkout, "rev-parse", "origin/develop")


@needs_git
def test_git_project_missing_branch(service: ProjectService, git_project: tuple[Path, Path, Path]) -> None:
    _, _, checkout = git_project
    with pytest.raises(ProjectError, match=r"origin/release not found"):
        service.add_project("Portal", str(checkout / "site" / "composer.lock"), "release")


@needs_git
def test_git_fetch_failure_falls_back_with_warning(
    service: ProjectService, reader: ProjectReader, git_project: tuple[Path, Path, Path]
) -> None:
    origin, _, checkout = git_project
    added = service.add_project("Portal", str(checkout / "site" / "composer.lock"), "develop")
    shutil.rmtree(origin)  # remote unreachable
    reader.sync(service.projects())
    [project] = service.list_projects()
    assert project["error"] is None
    assert project["source"] == added["source"]
    assert "Could not fetch develop" in project["warning"]
    assert "Could not read from remote repository" in project["warning"] or "does not appear" in project["warning"]


@needs_git
def test_refetch_recovers_after_a_failed_fetch(
    service: ProjectService, reader: ProjectReader, git_project: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    origin, _, checkout = git_project
    added = service.add_project("Portal", str(checkout / "site" / "composer.lock"), "develop")
    moved = tmp_path / "moved.git"
    origin.rename(moved)  # remote temporarily unreachable
    assert "Could not fetch develop" in service.refetch_project(added["id"])["warning"]
    moved.rename(origin)
    assert service.refetch_project(added["id"])["warning"] is None


def test_refetch_rejects_local_and_unknown_projects(service: ProjectService, tmp_path: Path) -> None:
    added = service.add_project("Plain", str(write_site(tmp_path / "plain", {})))
    with pytest.raises(ProjectError, match="local files"):
        service.refetch_project(added["id"])
    with pytest.raises(ProjectError, match="not found"):
        service.refetch_project("nope")


def test_not_a_git_repository(service: ProjectService, tmp_path: Path) -> None:
    lock = write_site(tmp_path / "plain", {})
    with pytest.raises(ProjectError, match="not inside a git repository"):
        service.add_project("Plain", str(lock), "develop")


# -- impact analysis --------------------------------------------------------------------


def test_impact_uses_locked_versions_and_enabled_state(
    service: ProjectService, reader: ProjectReader, tmp_path: Path
) -> None:
    write_site(tmp_path / "old", {"drupal/custom_formatters": "4.1.0"}, enabled=("custom_formatters",))
    write_site(tmp_path / "new", {"drupal/custom_formatters": "4.2.1"}, enabled=("custom_formatters",))
    write_site(tmp_path / "off", {"drupal/custom_formatters": "4.0.0"}, enabled=("node",))
    write_site(tmp_path / "noconf", {}, dev={"drupal/custom_formatters": "4.0.0"})
    for name in ("old", "new", "off", "noconf"):
        service.add_project(name.title(), str(tmp_path / name))
    item = advisory("drupal/custom_formatters", "<4.2.1")
    impacts = ImpactAnalyzer(service.projects, reader).assess([item])[item.key]
    assert [(i.project, i.installed, i.enabled.state, i.active) for i in impacts] == [
        ("Old", "4.1.0", EnabledState.ENABLED, True),
        ("Off", "4.0.0", EnabledState.NOT_ENABLED, False),
        ("Noconf", "4.0.0", EnabledState.UNKNOWN, True),
    ]


def test_impact_ignores_unused_modules(service: ProjectService, reader: ProjectReader, tmp_path: Path) -> None:
    service.add_project("Site", str(write_site(tmp_path / "s", {"drupal/core": "10.0.0", "drupal/token": "1.0.0"})))
    items = [advisory("drupal/webform", "<9.9.9"), advisory("drupal/token", "<1.0.0")]
    assert ImpactAnalyzer(service.projects, reader).assess(items) == {}


def test_impact_skips_projects_with_missing_lock(
    service: ProjectService, reader: ProjectReader, tmp_path: Path
) -> None:
    lock = write_site(tmp_path / "s", {"drupal/token": "1.0.0"})
    service.add_project("Site", str(lock))
    lock.unlink()
    assert ImpactAnalyzer(service.projects, reader).assess([advisory("drupal/token", "<2.0.0")]) == {}


def test_local_changes_are_picked_up_after_cache_expiry(tmp_path: Path) -> None:
    now = [0.0]
    reader = ProjectReader(GitSync(min_interval=0), clock=lambda: now[0])
    service = ProjectService(JsonProjectStore(tmp_path / "projects.json"), reader)
    write_site(tmp_path / "s", {"drupal/token": "1.0.0"})
    service.add_project("Site", str(tmp_path / "s"))
    project = service.projects()[0]
    assert reader.read(project).locked.versions["drupal/token"] == "1.0.0"
    write_site(tmp_path / "s", {"drupal/token": "1.1.0"})
    now[0] = 120.0
    assert reader.read(project).locked.versions["drupal/token"] == "1.1.0"


def test_notifications_only_treat_active_impacts_as_urgent(
    service: ProjectService, reader: ProjectReader, tmp_path: Path
) -> None:
    write_site(tmp_path / "p", {"drupal/custom_formatters": "4.1.0", "drupal/webform": "1.0.0"},
               enabled=("custom_formatters",))  # fmt: skip
    service.add_project("Portal", str(tmp_path / "p"))
    hit = advisory("drupal/custom_formatters", "<4.2.1", "Custom Formatters - Critical - RCE - SA-CONTRIB-2026-225")
    dormant = advisory("drupal/webform", "<2.0.0", "Webform - Moderately critical - XSS - SA-CONTRIB-2026-226")
    impacts = ImpactAnalyzer(service.projects, reader).assess([hit, dormant])
    assert set(impacts) == {hit.key, dormant.key}
    notification = compose([dormant, hit], impacts)
    assert (notification.title, notification.subtitle, notification.body) == (
        "⚠ Critical Drupal advisory affects Portal",
        "Custom Formatters · SA-CONTRIB-2026-225",
        "RCE\nPortal: 4.1.0 → update to 4.2.1 (enabled)\n+1 other new item",
    )
    assert notification.thread == "affected"
    only_dormant = {dormant.key: impacts[dormant.key]}
    assert compose([dormant], only_dormant).title == "New critical Drupal advisory"


def test_project_model_round_trip(tmp_path: Path) -> None:
    store = JsonProjectStore(tmp_path / "projects.json")
    project = Project("abc", "Site", tmp_path / "composer.lock", "develop")
    store.save([project])
    assert store.load() == [project]


@needs_git
def test_edit_project_source(
    service: ProjectService, reader: ProjectReader, git_project: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    _, publisher, checkout = git_project
    write_site(publisher / "site", {"drupal/simple_oauth": "6.1.4"}, enabled=("simple_oauth",))
    git(publisher, "commit", "-am", "update")
    git(publisher, "push", "origin", "HEAD:develop")
    lock = str(checkout / "site" / "composer.lock")
    added = service.add_project("Portal", lock)  # local files: 6.1.3
    assert added["source"] == "Local files"

    switched = service.update_project(added["id"], "Portal", lock, "develop")
    assert switched["branch"] == "develop"
    assert switched["source"].startswith("origin/develop @ ")
    assert reader.read(service.projects()[0]).locked.versions["drupal/simple_oauth"] == "6.1.4"

    with pytest.raises(ProjectError, match=r"origin/nope not found"):
        service.update_project(added["id"], "Portal", lock, "nope")
    assert service.projects()[0].branch == "develop"  # a failed edit keeps the previous values

    back = service.update_project(added["id"], "Portal", lock, None)
    assert (back["branch"], back["source"]) == (None, "Local files")
    reloaded = ProjectService(JsonProjectStore(tmp_path / "data" / "projects.json"), reader)
    assert reloaded.projects()[0].branch is None


def test_edit_project_name_and_path(service: ProjectService, tmp_path: Path) -> None:
    first = service.add_project("Site A", str(write_site(tmp_path / "a", {"drupal/token": "1.0.0"})))
    service.add_project("Site B", str(write_site(tmp_path / "b", {})))
    moved = write_site(tmp_path / "a-moved", {"drupal/token": "1.1.0"})

    updated = service.update_project(first["id"], "Site A (renamed)", str(moved), None)
    assert (updated["id"], updated["name"], updated["lock_path"]) == (
        first["id"],
        "Site A (renamed)",
        str(moved.resolve()),
    )
    assert [p.name for p in service.projects()] == ["Site A (renamed)", "Site B"]  # order is kept

    # Keeping its own name is fine; taking another project's name or lock file is not.
    service.update_project(first["id"], "site a (RENAMED)", str(moved), None)
    with pytest.raises(ProjectError, match="already exists"):
        service.update_project(first["id"], "Site B", str(moved), None)
    with pytest.raises(ProjectError, match="already added"):
        service.update_project(first["id"], "Other", str(tmp_path / "b"), None)
    with pytest.raises(ProjectError, match="Enter a project name"):
        service.update_project(first["id"], " ", str(moved), None)


def test_update_unknown_project(service: ProjectService, tmp_path: Path) -> None:
    with pytest.raises(ProjectError, match="not found"):
        service.update_project("missing", "X", str(write_site(tmp_path / "x", {})), None)


def test_drupal_core_advisories_match_the_locked_core_version(
    service: ProjectService, reader: ProjectReader, tmp_path: Path
) -> None:
    write_site(tmp_path / "old", {"drupal/core-recommended": "11.4.9", "drupal/core": "11.4.9"}, enabled=("node",))
    write_site(tmp_path / "new", {"drupal/core": "11.4.11"}, enabled=("node",))
    write_site(tmp_path / "ten", {"drupal/core": "10.6.18"})
    for name in ("old", "new", "ten"):
        service.add_project(name.title(), str(tmp_path / name))
    item = Item(
        Product.DRUPAL, Kind.ADVISORY, "Drupal core - Critical - Remote code execution - SA-CORE-2026-014",
        date(2026, 10, 14), severity=Severity.CRITICAL, package="drupal/core",
        affected=">= 8.9.0 < 10.6.19 || >= 11.0.0 < 11.3.19 || >= 11.4.0 < 11.4.10 || >= 12.0.0-alpha1 < 12.0.0-beta1",
    )  # fmt: skip
    impacts = ImpactAnalyzer(service.projects, reader).assess([item])[item.key]
    assert [(i.project, i.installed, i.fixed_in, i.enabled.state, i.active) for i in impacts] == [
        ("Old", "11.4.9", "11.4.10", EnabledState.ALWAYS, True),
        ("Ten", "10.6.18", "10.6.19", EnabledState.ALWAYS, True),
    ]


# -- Next.js projects -------------------------------------------------------------------


def write_npm_project(root: Path, next_version: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    lock = {
        "name": "web",
        "lockfileVersion": 3,
        "packages": {
            "": {"name": "web", "dependencies": {"next": "^16.0.0"}},
            "node_modules/next": {"version": next_version},
            "node_modules/react": {"version": "19.2.0"},
            "node_modules/some-lib/node_modules/next": {"version": "1.0.0"},  # nested copy is ignored
        },
    }
    (root / "package-lock.json").write_text(json.dumps(lock))
    return root / "package-lock.json"


def nextjs_advisory(affected: str, patched: tuple[str, ...]) -> Item:
    return Item(
        Product.NEXTJS, Kind.ADVISORY, "Next.js: RCE in image optimizer (CVE-2026-1)", date(2026, 10, 22),
        severity=Severity.CRITICAL, package="next", affected=affected, patched=patched,
    )  # fmt: skip


def test_nextjs_project_is_matched_against_next_advisories(
    service: ProjectService, reader: ProjectReader, tmp_path: Path
) -> None:
    added = service.add_project("Frontend", str(write_npm_project(tmp_path / "web", "16.2.0")))
    assert added["ecosystem"] == "npm"
    assert added["summary"] == {"ecosystem": "npm", "next": "16.2.0", "packages": 2}
    assert added["config"] is None  # no core.extension.yml lookup for npm projects
    service.add_project("Safe", str(write_npm_project(tmp_path / "safe", "16.4.1")))
    service.add_project("Drupal", str(write_site(tmp_path / "drupal", {"drupal/core": "11.4.11"})))

    item = nextjs_advisory(">= 16.0.0, <= 16.4.0 || >= 15.0.0, < 15.5.29", ("16.4.1", "15.5.29"))
    [impact] = ImpactAnalyzer(service.projects, reader).assess([item])[item.key]
    assert (impact.project, impact.installed, impact.fixed_in) == ("Frontend", "16.2.0", "16.4.1")
    assert impact.enabled.state is EnabledState.ALWAYS and impact.active


def test_nextjs_fixed_version_prefers_the_installed_major(
    service: ProjectService, reader: ProjectReader, tmp_path: Path
) -> None:
    service.add_project("Old", str(write_npm_project(tmp_path / "old", "15.3.0")))
    item = nextjs_advisory(">= 15.0.0, <= 15.5.28 || >= 16.0.0, <= 16.4.0", ("16.4.1", "15.5.29"))
    [impact] = ImpactAnalyzer(service.projects, reader).assess([item])[item.key]
    assert impact.fixed_in == "15.5.29"


def test_directory_with_several_lock_files_must_be_specific(service: ProjectService, tmp_path: Path) -> None:
    write_site(tmp_path / "mixed", {})
    write_npm_project(tmp_path / "mixed", "16.0.0")
    with pytest.raises(ProjectError, match="choose the lock file"):
        service.add_project("Mixed", str(tmp_path / "mixed"))
    assert service.add_project("Mixed", str(tmp_path / "mixed" / "package-lock.json"))["ecosystem"] == "npm"


def test_not_affected_only_for_projects_of_the_same_type(
    service: ProjectService, reader: ProjectReader, tmp_path: Path
) -> None:
    drupal = advisory("drupal/advanced_filesystem", "<1.0.28")
    nextjs = nextjs_advisory(">= 16.0.0, <= 16.4.0", ("16.4.1",))
    report = Report(datetime(2026, 10, 23, tzinfo=UTC), (drupal, nextjs))
    analyzer = ImpactAnalyzer(service.projects, reader)

    def checked() -> dict[str, bool]:
        data = report_with_impacts(report, analyzer)
        return {item["package"]: item["checked"] for item in data["items"]}

    service.add_project("Frontend", str(write_npm_project(tmp_path / "web", "16.4.1")))
    assert checked() == {"drupal/advanced_filesystem": False, "next": True}
    service.add_project("Site", str(write_site(tmp_path / "site", {"drupal/core": "11.4.11"})))
    assert checked() == {"drupal/advanced_filesystem": True, "next": True}
    assert report_with_impacts(report, None)["items"][0]["checked"] is False
