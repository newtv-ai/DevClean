from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path, PureWindowsPath

import pytest

import devclean.core.application_cleanup as application_cleanup
from devclean.core.application_cleanup import (
    DecisionOwner,
    PolicyAction,
    application_scan_roots,
    evaluate_application_path,
    match_application_rule,
    whole_tree_application_rule,
)
from devclean.core.cleanup_catalog import (
    CleanupCategory,
    CleanupPolicy,
    discover_known_cleanup_roots,
)
from devclean.core.docker_cleanup import docker_audited_tool_roots, docker_roots
from devclean.core.user_rules import default_rules

_NOW = datetime(2026, 8, 17, tzinfo=UTC)


def _layout(tmp_path: Path) -> tuple[dict[str, str], Path, Path, Path]:
    home = tmp_path / "home"
    local = tmp_path / "local"
    roaming = tmp_path / "roaming"
    wsl = local / "Docker" / "wsl"
    cli = home / ".docker"
    settings = roaming / "Docker" / "settings-store.json"
    wsl.mkdir(parents=True)
    cli.mkdir(parents=True)
    settings.parent.mkdir(parents=True)
    settings.write_text('{"theme": "system"}', encoding="utf-8")
    env = {
        "USERPROFILE": str(home),
        "LOCALAPPDATA": str(local),
        "APPDATA": str(roaming),
    }
    return env, wsl, cli, settings


def test_docker_default_windows_roots_are_discovered(tmp_path: Path) -> None:
    env, wsl, cli, settings = _layout(tmp_path)

    roots = docker_roots(env)

    assert roots.desktop_data_roots == (PureWindowsPath(str(wsl)),)
    assert roots.cli_config_roots == (PureWindowsPath(str(cli)),)
    assert roots.desktop_settings_paths == (PureWindowsPath(str(settings)),)
    assert PureWindowsPath(str(wsl)) in application_scan_roots(env)


def test_docker_config_override_replaces_default_cli_directory(tmp_path: Path) -> None:
    env, _, cli, _ = _layout(tmp_path)
    custom = tmp_path / "docker-cli-config"
    custom.mkdir()
    env["DOCKER_CONFIG"] = str(custom)

    roots = docker_roots(env)

    assert roots.cli_config_roots == (PureWindowsPath(str(custom)),)
    assert PureWindowsPath(str(cli)) not in roots.cli_config_roots


def test_docker_desktop_data_is_mixed_keep_storage(tmp_path: Path) -> None:
    env, wsl, _, _ = _layout(tmp_path)
    disk = wsl / "data" / "docker_data.vhdx"

    rule = match_application_rule(disk, env)

    assert rule is not None
    assert rule.rule_id == "docker-desktop-data-mixed"
    assert rule.owner is DecisionOwner.KEEP

    decision = evaluate_application_path(
        disk,
        logical_size=200 * 1024**3,
        last_used=_NOW - timedelta(days=365),
        now=_NOW,
        process_running=False,
        environment=env,
    )
    assert decision is not None
    assert decision.action is PolicyAction.KEEP_PROTECTED


def test_docker_cli_config_and_desktop_settings_are_keep(tmp_path: Path) -> None:
    env, _, cli, settings = _layout(tmp_path)
    cases = {
        cli / "config.json": "docker-cli-configuration",
        cli / "contexts" / "meta" / "context.json": "docker-cli-configuration",
        settings: "docker-desktop-settings",
    }

    for path, rule_id in cases.items():
        rule = match_application_rule(path, env)
        assert rule is not None
        assert rule.rule_id == rule_id
        assert rule.owner is DecisionOwner.KEEP


def test_docker_never_grants_raw_whole_tree_authority(tmp_path: Path) -> None:
    env, wsl, _, _ = _layout(tmp_path)

    assert docker_audited_tool_roots(env) == ()
    assert whole_tree_application_rule(wsl, env) is None
    assert not application_cleanup.process_guard_allows(wsl, env)


def test_docker_wsl_root_is_catalogued_report_only(tmp_path: Path) -> None:
    env, wsl, _, _ = _layout(tmp_path)

    discovered = discover_known_cleanup_roots(default_rules().scan, env)
    by_path = {os.path.normcase(str(item.path)): item for item in discovered}
    item = by_path[os.path.normcase(str(wsl))]

    assert item.category is CleanupCategory.CONTAINER_STORAGE
    assert item.policy is CleanupPolicy.REPORT_ONLY
    assert not item.delete_root_itself
    assert item.application_rule is None


def test_docker_process_dispatch_is_independent_from_ollama(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(application_cleanup, "docker_process_running", lambda: True)
    monkeypatch.setattr(application_cleanup, "ollama_process_running", lambda: False)
    assert application_cleanup.application_process_running("docker")

    monkeypatch.setattr(application_cleanup, "docker_process_running", lambda: False)
    monkeypatch.setattr(application_cleanup, "ollama_process_running", lambda: True)
    assert not application_cleanup.application_process_running("docker")
