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
from devclean.core.user_rules import default_rules
from devclean.core.uv_cleanup import uv_audited_tool_roots, uv_roots

_NOW = datetime(2026, 8, 17, tzinfo=UTC)


def _layout(tmp_path: Path) -> tuple[dict[str, str], Path, Path, Path, Path]:
    local = tmp_path / "Local"
    roaming = tmp_path / "Roaming"
    programdata = tmp_path / "ProgramData"
    cache = local / "uv" / "cache"
    user_config = roaming / "uv"
    data = user_config / "data"
    system_config = programdata / "uv"
    cache.mkdir(parents=True)
    data.mkdir(parents=True)
    system_config.mkdir(parents=True)
    env = {
        "USERPROFILE": str(tmp_path / "home"),
        "APPDATA": str(roaming),
        "LOCALAPPDATA": str(local),
        "PROGRAMDATA": str(programdata),
        "TEMP": str(tmp_path / "Temp"),
    }
    return env, cache, data, user_config, system_config


def test_uv_default_windows_storage_roots_are_discovered(tmp_path: Path) -> None:
    env, cache, data, user_config, system_config = _layout(tmp_path)

    roots = uv_roots(env)

    assert PureWindowsPath(str(cache)) in roots.cache_roots
    assert PureWindowsPath(str(data)) in roots.data_roots
    assert PureWindowsPath(str(user_config)) in roots.user_config_roots
    assert PureWindowsPath(str(system_config)) in roots.system_config_roots

    scan = application_scan_roots(env)
    assert PureWindowsPath(str(cache)) in scan
    assert PureWindowsPath(str(data)) in scan
    assert PureWindowsPath(str(user_config)) in scan


def test_uv_cache_is_vendor_managed_but_generic_deletion_stays_protected(
    tmp_path: Path,
) -> None:
    env, cache, data, user_config, system_config = _layout(tmp_path)
    cases = {
        cache / "archive-v0" / "package.whl": "uv-cache-vendor-managed",
        data / "python" / "cpython-3.13": "uv-persistent-data",
        user_config / "uv.toml": "uv-user-config",
        system_config / "uv.toml": "uv-system-config",
    }

    for path, rule_id in cases.items():
        rule = match_application_rule(path, env)
        assert rule is not None
        assert rule.rule_id == rule_id
        assert rule.owner is DecisionOwner.KEEP

    decision = evaluate_application_path(
        cache,
        logical_size=8 * 1024**3,
        last_used=_NOW - timedelta(days=365),
        now=_NOW,
        process_running=False,
        environment=env,
    )
    assert decision is not None
    assert decision.action is PolicyAction.KEEP_PROTECTED


def test_uv_never_grants_raw_whole_tree_authority(tmp_path: Path) -> None:
    env, cache, _, _, _ = _layout(tmp_path)

    assert uv_audited_tool_roots(env) == ()
    assert whole_tree_application_rule(cache, env) is None
    assert whole_tree_application_rule(cache / "archive-v0", env) is None
    assert not application_cleanup.process_guard_allows(cache, env)


def test_uv_cache_is_catalogued_report_only_not_vendor_delete_root(tmp_path: Path) -> None:
    env, cache, data, _, _ = _layout(tmp_path)

    discovered = discover_known_cleanup_roots(default_rules().scan, env)
    by_path = {os.path.normcase(str(item.path)): item for item in discovered}

    cache_item = by_path[os.path.normcase(str(cache))]
    data_item = by_path[os.path.normcase(str(data))]

    assert cache_item.category is CleanupCategory.UV_CACHE
    assert cache_item.policy is CleanupPolicy.REPORT_ONLY
    assert not cache_item.delete_root_itself
    assert cache_item.application_rule is None
    assert data_item.policy is CleanupPolicy.REPORT_ONLY
    assert not data_item.delete_root_itself


def test_uv_cache_dir_override_replaces_default_cache_authority(tmp_path: Path) -> None:
    env, default_cache, _, _, _ = _layout(tmp_path)
    custom = tmp_path / "cache-drive" / "uv-cache"
    custom.mkdir(parents=True)
    env["UV_CACHE_DIR"] = str(custom)

    roots = uv_roots(env)

    assert PureWindowsPath(str(custom)) in roots.cache_roots
    assert PureWindowsPath(str(default_cache)) not in roots.cache_roots
    rule = match_application_rule(custom / "wheels-v5" / "package.whl", env)
    assert rule is not None
    assert rule.rule_id == "uv-cache-vendor-managed"
    assert rule.owner is DecisionOwner.KEEP
    assert whole_tree_application_rule(custom, env) is None


def test_uv_process_dispatch_is_independent_from_pip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(application_cleanup, "uv_process_running", lambda: True)
    monkeypatch.setattr(application_cleanup, "pip_process_running", lambda: False)
    assert application_cleanup.application_process_running("uv")

    monkeypatch.setattr(application_cleanup, "uv_process_running", lambda: False)
    monkeypatch.setattr(application_cleanup, "pip_process_running", lambda: True)
    assert not application_cleanup.application_process_running("uv")
