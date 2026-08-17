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
from devclean.core.cypress_cleanup import cypress_audited_tool_roots, cypress_roots
from devclean.core.user_rules import default_rules

_NOW = datetime(2026, 8, 17, tzinfo=UTC)


def _layout(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    home = tmp_path / "home"
    local = home / "AppData" / "Local"
    roaming = home / "AppData" / "Roaming"
    cache = local / "Cypress" / "Cache"
    app_data = roaming / "Cypress"
    cache.mkdir(parents=True)
    app_data.mkdir(parents=True)
    env = {
        "USERPROFILE": str(home),
        "LOCALAPPDATA": str(local),
        "APPDATA": str(roaming),
    }
    return env, cache, app_data


def test_cypress_default_windows_cache_is_discovered(tmp_path: Path) -> None:
    env, cache, app_data = _layout(tmp_path)

    roots = cypress_roots(env)

    assert roots.binary_cache_roots == (PureWindowsPath(str(cache)),)
    assert roots.app_data_roots == (PureWindowsPath(str(app_data)),)
    assert not roots.relative_cache_override
    assert PureWindowsPath(str(cache)) in application_scan_roots(env)
    assert PureWindowsPath(str(app_data)) not in application_scan_roots(env)


def test_cypress_absolute_and_home_cache_overrides(tmp_path: Path) -> None:
    env, default_cache, _ = _layout(tmp_path)
    custom = tmp_path / "binary-drive" / "Cypress"
    custom.mkdir(parents=True)
    env["CYPRESS_CACHE_FOLDER"] = str(custom)

    roots = cypress_roots(env)
    assert roots.binary_cache_roots == (PureWindowsPath(str(custom)),)
    assert PureWindowsPath(str(default_cache)) not in roots.binary_cache_roots

    env["CYPRESS_CACHE_FOLDER"] = r"~\shared-cypress"
    expected = PureWindowsPath(env["USERPROFILE"]) / "shared-cypress"
    assert cypress_roots(env).binary_cache_roots == (expected,)


def test_cypress_npm_config_cache_fallback_is_honored(tmp_path: Path) -> None:
    env, _, _ = _layout(tmp_path)
    custom = tmp_path / "npm-config" / "Cypress"
    custom.mkdir(parents=True)
    env["npm_config_cypress_cache_folder"] = str(custom)

    assert cypress_roots(env).binary_cache_roots == (PureWindowsPath(str(custom)),)


def test_cypress_relative_cache_override_is_fail_closed(tmp_path: Path) -> None:
    env, _, _ = _layout(tmp_path)
    env["CYPRESS_CACHE_FOLDER"] = r"cache\Cypress"

    roots = cypress_roots(env)

    assert roots.binary_cache_roots == ()
    assert roots.relative_cache_override


def test_cypress_binary_cache_is_keep_even_when_large_and_old(tmp_path: Path) -> None:
    env, cache, _ = _layout(tmp_path)
    binary = cache / "15.16.0" / "Cypress" / "Cypress.exe"

    rule = match_application_rule(binary, env)
    assert rule is not None
    assert rule.rule_id == "cypress-binary-cache-vendor-managed"
    assert rule.owner is DecisionOwner.KEEP

    decision = evaluate_application_path(
        cache,
        logical_size=20 * 1024**3,
        last_used=_NOW - timedelta(days=365),
        now=_NOW,
        process_running=False,
        environment=env,
    )
    assert decision is not None
    assert decision.action is PolicyAction.KEEP_PROTECTED


def test_cypress_app_data_and_external_runtime_are_keep(tmp_path: Path) -> None:
    env, _, app_data = _layout(tmp_path)
    runtime = tmp_path / "runtime" / "Cypress.exe"
    runtime.parent.mkdir(parents=True)
    runtime.touch()
    env["CYPRESS_RUN_BINARY"] = str(runtime)

    app_rule = match_application_rule(app_data / "state.json", env)
    assert app_rule is not None
    assert app_rule.rule_id == "cypress-app-data-state"
    assert app_rule.owner is DecisionOwner.KEEP

    runtime_rule = match_application_rule(runtime, env)
    assert runtime_rule is not None
    assert runtime_rule.rule_id == "cypress-external-run-binary"
    assert runtime_rule.owner is DecisionOwner.KEEP


def test_cypress_never_grants_raw_whole_tree_authority(tmp_path: Path) -> None:
    env, cache, _ = _layout(tmp_path)

    assert cypress_audited_tool_roots(env) == ()
    assert whole_tree_application_rule(cache, env) is None
    assert not application_cleanup.process_guard_allows(cache, env)


def test_cypress_cache_is_catalogued_report_only(tmp_path: Path) -> None:
    env, cache, _ = _layout(tmp_path)

    discovered = discover_known_cleanup_roots(default_rules().scan, env)
    by_path = {os.path.normcase(str(item.path)): item for item in discovered}
    item = by_path[os.path.normcase(str(cache))]

    assert item.category is CleanupCategory.TEST_BROWSER_BINARIES
    assert item.policy is CleanupPolicy.REPORT_ONLY
    assert not item.delete_root_itself
    assert item.application_rule is None


def test_cypress_process_dispatch_does_not_alias_puppeteer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(application_cleanup, "cypress_process_running", lambda: True)
    monkeypatch.setattr(application_cleanup, "puppeteer_process_running", lambda: False)
    assert application_cleanup.application_process_running("cypress")

    monkeypatch.setattr(application_cleanup, "cypress_process_running", lambda: False)
    monkeypatch.setattr(application_cleanup, "puppeteer_process_running", lambda: True)
    assert not application_cleanup.application_process_running("cypress")
