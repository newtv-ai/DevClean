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
from devclean.core.electron_cleanup import electron_audited_tool_roots, electron_roots
from devclean.core.user_rules import default_rules

_NOW = datetime(2026, 8, 17, tzinfo=UTC)


def _layout(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    home = tmp_path / "home"
    local = home / "AppData" / "Local"
    cache = local / "electron" / "Cache"
    legacy = home / ".electron"
    cache.mkdir(parents=True)
    legacy.mkdir(parents=True)
    env = {"USERPROFILE": str(home), "LOCALAPPDATA": str(local)}
    return env, cache, legacy


def test_electron_default_and_legacy_caches_are_discovered(tmp_path: Path) -> None:
    env, cache, legacy = _layout(tmp_path)

    roots = electron_roots(env)

    assert roots.active_cache_roots == (PureWindowsPath(str(cache)),)
    assert roots.legacy_cache_roots == (PureWindowsPath(str(legacy)),)
    assert not roots.relative_cache_override
    scan = application_scan_roots(env)
    assert PureWindowsPath(str(cache)) in scan
    assert PureWindowsPath(str(legacy)) in scan


def test_electron_absolute_cache_override_replaces_default(tmp_path: Path) -> None:
    env, default_cache, legacy = _layout(tmp_path)
    custom = tmp_path / "artifact-cache" / "electron"
    custom.mkdir(parents=True)
    env["electron_config_cache"] = str(custom)

    roots = electron_roots(env)

    assert roots.active_cache_roots == (PureWindowsPath(str(custom)),)
    assert PureWindowsPath(str(default_cache)) not in roots.active_cache_roots
    assert roots.legacy_cache_roots == (PureWindowsPath(str(legacy)),)


def test_electron_relative_cache_override_is_fail_closed(tmp_path: Path) -> None:
    env, _, legacy = _layout(tmp_path)
    env["electron_config_cache"] = r"cache\electron"

    roots = electron_roots(env)

    assert roots.active_cache_roots == ()
    assert roots.legacy_cache_roots == (PureWindowsPath(str(legacy)),)
    assert roots.relative_cache_override


def test_electron_cache_is_keep_even_when_large_and_old(tmp_path: Path) -> None:
    env, cache, _ = _layout(tmp_path)
    archive = cache / "checksum" / "electron-v44.0.0-win32-x64.zip"

    rule = match_application_rule(archive, env)
    assert rule is not None
    assert rule.rule_id == "electron-download-cache-mixed"
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


def test_electron_legacy_cache_is_keep(tmp_path: Path) -> None:
    env, _, legacy = _layout(tmp_path)

    rule = match_application_rule(legacy / "electron-v9.0.0-win32-x64.zip", env)

    assert rule is not None
    assert rule.rule_id == "electron-legacy-cache-mixed"
    assert rule.owner is DecisionOwner.KEEP


def test_electron_never_grants_raw_whole_tree_authority(tmp_path: Path) -> None:
    env, cache, _ = _layout(tmp_path)

    assert electron_audited_tool_roots(env) == ()
    assert whole_tree_application_rule(cache, env) is None
    assert not application_cleanup.process_guard_allows(cache, env)


def test_electron_cache_is_catalogued_report_only(tmp_path: Path) -> None:
    env, cache, legacy = _layout(tmp_path)

    discovered = discover_known_cleanup_roots(default_rules().scan, env)
    by_path = {os.path.normcase(str(item.path)): item for item in discovered}
    for path in (cache, legacy):
        item = by_path[os.path.normcase(str(path))]
        assert item.category is CleanupCategory.INSTALLERS_DOWNLOADS
        assert item.policy is CleanupPolicy.REPORT_ONLY
        assert not item.delete_root_itself
        assert item.application_rule is None


def test_electron_process_dispatch_does_not_alias_cypress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(application_cleanup, "electron_process_running", lambda: True)
    monkeypatch.setattr(application_cleanup, "cypress_process_running", lambda: False)
    assert application_cleanup.application_process_running("electron")

    monkeypatch.setattr(application_cleanup, "electron_process_running", lambda: False)
    monkeypatch.setattr(application_cleanup, "cypress_process_running", lambda: True)
    assert not application_cleanup.application_process_running("electron")
