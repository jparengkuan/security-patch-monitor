from __future__ import annotations

import json

import pytest

from patch_monitor.lockfiles import Ecosystem, LockError, lock_format, package_ecosystem, parse_lock_file


def test_composer_lock_includes_dev_packages() -> None:
    text = json.dumps(
        {
            "packages": [{"name": "drupal/core", "version": "11.4.11"}, {"name": "drupal/token", "version": "1.15.0"}],
            "packages-dev": [{"name": "drupal/devel", "version": "5.3.1"}],
        }
    )
    locked = parse_lock_file("composer.lock", text)
    assert locked.ecosystem is Ecosystem.COMPOSER
    assert locked.drupal_core == "11.4.11"
    assert locked.drupal_contrib == {"drupal/token": "1.15.0", "drupal/devel": "5.3.1"}


def test_package_lock_v3_uses_top_level_installs_only() -> None:
    text = json.dumps(
        {
            "lockfileVersion": 3,
            "packages": {
                "": {"name": "app"},
                "node_modules/next": {"version": "16.2.0"},
                "node_modules/@next/env": {"version": "16.2.0"},
                "node_modules/a/node_modules/next": {"version": "9.0.0"},
            },
        }
    )
    assert parse_lock_file("package-lock.json", text).versions == {"next": "16.2.0", "@next/env": "16.2.0"}


def test_package_lock_v1() -> None:
    text = json.dumps({"lockfileVersion": 1, "dependencies": {"next": {"version": "12.3.4"}}})
    assert parse_lock_file("package-lock.json", text).versions == {"next": "12.3.4"}


def test_yarn_classic_and_berry() -> None:
    classic = (
        "# yarn lockfile v1\n\n"
        '"@next/env@15.5.4":\n  version "15.5.4"\n  resolved "https://..."\n\n'
        'next@^15.5.0, next@~15.5.4:\n  version "15.5.4"\n'
    )
    assert parse_lock_file("yarn.lock", classic).versions == {"@next/env": "15.5.4", "next": "15.5.4"}
    berry = '__metadata:\n  version: 8\n\n"next@npm:^16.1.0":\n  version: 16.1.2\n  resolution: "next@npm:16.1.2"\n'
    assert parse_lock_file("yarn.lock", berry).versions["next"] == "16.1.2"


@pytest.mark.parametrize(
    "line",
    [
        "  /next/14.2.3:",  # v5
        "  /next@14.2.3:",  # v6
        "  next@14.2.3(react@18.3.1):",  # v9 snapshots
        "  'next@14.2.3':",
    ],
)
def test_pnpm_versions(line: str) -> None:
    text = (
        f"lockfileVersion: '9.0'\n\nimporters:\n  .:\n    dependencies: {{}}\n\npackages:\n{line}\n    resolution: x\n"
    )
    assert parse_lock_file("pnpm-lock.yaml", text).versions == {"next": "14.2.3"}


def test_pnpm_scoped_package() -> None:
    text = "lockfileVersion: '6.0'\n\npackages:\n  /@next/env@14.2.3:\n    resolution: x\n"
    assert parse_lock_file("pnpm-lock.yaml", text).versions == {"@next/env": "14.2.3"}


@pytest.mark.parametrize(
    ("name", "text", "message"),
    [
        ("composer.lock", "{", "not valid JSON"),
        ("composer.lock", "{}", "no package list"),
        ("package-lock.json", "{}", "no packages"),
        ("yarn.lock", "# empty\n", "no packages"),
        ("pnpm-lock.yaml", "packages:\n", "no packages"),
        ("Gemfile.lock", "", "Unsupported lock file"),
    ],
)
def test_invalid_lock_files(name: str, text: str, message: str) -> None:
    with pytest.raises(LockError, match=message):
        parse_lock_file(name, text)


def test_lock_format_lookup() -> None:
    assert lock_format("yarn.lock").ecosystem is Ecosystem.NPM


@pytest.mark.parametrize(
    ("package", "ecosystem"),
    [("drupal/webform", Ecosystem.COMPOSER), ("next", Ecosystem.NPM), ("@next/env", Ecosystem.NPM)],
)
def test_package_ecosystem(package: str, ecosystem: Ecosystem) -> None:
    assert package_ecosystem(package) is ecosystem
