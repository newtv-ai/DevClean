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
from devclean.core.pip_cleanup import pip_roots
from devclean.core.user_rules import default_rules

_NOW = datetime(2026, 8, 17, tzinfo=UTC)
_MIB = 1024**2


def _layout(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    local = tmp_path / "Local"
    default_cache = local / "pip" / "Cache"
    custom_cache = tmp_path / "shared" / "pip-cache"
    default_cache.mkdir(parents=True)
    custom_cache.mkdir(parents=True)
    env = {
        "USERPROFILE": str(tmp_path / "home"),
        "APPDATA": str(tmp_path / "Roaming"),
        "LOCALAPPDATA": str(local),
        "TEMP": str(tmp_path / "Temp"),
        "PIP_CACHE_DIR": str(custom_cache),
    }
    return env, default_cache, custom_cache


def test_pip_default_and_custom_cache_roots_are_discovered(tmp_path: Path) -> None:
    env, default_cache, custom_cache = _layout(tmp_path)

    roots = pip_roots(env)

    assert PureWindowsPath(str(default_cache)) in roots.managed_cache_roots
    assert PureWindowsPath(str(custom_cache)) in roots.custom_cache_roots
    scan = application_scan_roots(env)
    assert PureWindowsPath(str(default_cache)) in scan
    assert PureWindowsPath(str(custom_cache)) in scan


def test_pip_default_cache_is_tool_but_custom_cache_is_protected(tmp_path: Path) -> None:
    env, default_cache, custom_cache = _layout(tmp_path)

    default_rule = match_application_rule(default_cache / "http-v2" / "entry", env)
    custom_rule = match_application_rule(custom_cache / "wheels" / "pkg.whl", env)

    assert default_rule is not None
    assert default_rule.rule_id == "pip-default-cache"
    assert default_rule.owner is DecisionOwner.TOOL
    assert custom_rule is not None
    assert custom_rule.rule_id == "pip-custom-cache"
    assert custom_rule.owner is DecisionOwner.KEEP

    custom_decision = evaluate_application_path(
        custom_cache,
        logical_size=5 * 1024**3,
        last_used=_NOW - timedelta(days=365),
        now=_NOW,
        process_running=False,
        environment=env,
    )
    assert custom_decision is not None
    assert custom_decision.action is PolicyAction.KEEP_PROTECTED


def test_pip_default_cache_policy_is_conservative_about_recency_size_and_process(
    tmp_path: Path,
) -> None:
    env, default_cache, _ = _layout(tmp_path)

    recent = evaluate_application_path(
        default_cache,
        logical_size=2 * 1024**3,
        last_used=_NOW - timedelta(days=10),
        now=_NOW,
        process_running=False,
        environment=env,
    )
    stale = evaluate_application_path(
        default_cache,
        logical_size=2 * 1024**3,
        last_used=_NOW - timedelta(days=45),
        now=_NOW,
        process_running=False,
        environment=env,
    )
    small = evaluate_application_path(
        default_cache,
        logical_size=16 * _MIB,
        last_used=_NOW - timedelta(days=365),
        now=_NOW,
        process_running=False,
        environment=env,
    )
    running = evaluate_application_path(
        default_cache,
        logical_size=2 * 1024**3,
        last_used=_NOW - timedelta(days=365),
        now=_NOW,
        process_running=True,
        environment=env,
    )

    assert recent is not None
    assert recent.effective_idle_days == 30
    assert recent.action is PolicyAction.TOOL_KEEP_RECENT
    assert stale is not None and stale.action is PolicyAction.TOOL_DELETE
    assert small is not None and small.action is PolicyAction.TOOL_KEEP_LOW_BENEFIT
    assert running is not None and running.action is PolicyAction.TOOL_KEEP_IN_USE


def test_pip_whole_tree_authority_is_exact_and_excludes_custom_cache(
    tmp_path: Path,
) -> None:
    env, default_cache, custom_cache = _layout(tmp_path)

    assert whole_tree_application_rule(default_cache, env) is not None
    assert whole_tree_application_rule(default_cache / "wheels", env) is None
    assert whole_tree_application_rule(custom_cache, env) is None

    discovered = discover_known_cleanup_roots(default_rules().scan, env)
    by_path = {os.path.normcase(str(item.path)): item for item in discovered}

    default_item = by_path[os.path.normcase(str(default_cache))]
    custom_item = by_path[os.path.normcase(str(custom_cache))]

    assert default_item.category is CleanupCategory.PIP_CACHE
    assert default_item.policy is CleanupPolicy.VENDOR_MANAGED
    assert default_item.delete_root_itself
    assert default_item.application_rule is not None
    assert default_item.application_rule.rule_id == "pip-default-cache"
    assert custom_item.policy is CleanupPolicy.REPORT_ONLY
    assert not custom_item.delete_root_itself


def test_pip_explicit_dedicated_hook_can_relocate_whole_tree_authority(
    tmp_path: Path,
) -> None:
    env, _, _ = _layout(tmp_path)
    dedicated = tmp_path / "dedicated" / "pip-cache"
    dedicated.mkdir(parents=True)
    env["DEVCLEAN_PIP_CACHE_DIR"] = str(dedicated)

    roots = pip_roots(env)
    assert PureWindowsPath(str(dedicated)) in roots.managed_cache_roots
    rule = whole_tree_application_rule(dedicated, env)
    assert rule is not None
    assert rule.rule_id == "pip-default-cache"


def test_pip_process_guard_is_independent_from_javascript_package_managers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, default_cache, _ = _layout(tmp_path)

    monkeypatch.setattr(application_cleanup, "pip_process_running", lambda: True)
    monkeypatch.setattr(application_cleanup, "npm_process_running", lambda: False)
    monkeypatch.setattr(application_cleanup, "pnpm_process_running", lambda: False)
    monkeypatch.setattr(application_cleanup, "yarn_process_running", lambda: False)
    monkeypatch.setattr(application_cleanup, "bun_process_running", lambda: False)
    assert not application_cleanup.process_guard_allows(default_cache, env)

    monkeypatch.setattr(application_cleanup, "pip_process_running", lambda: False)
    monkeypatch.setattr(application_cleanup, "npm_process_running", lambda: True)
    monkeypatch.setattr(application_cleanup, "pnpm_process_running", lambda: True)
    monkeypatch.setattr(application_cleanup, "yarn_process_running", lambda: True)
    monkeypatch.setattr(application_cleanup, "bun_process_running", lambda: True)
    assert application_cleanup.process_guard_allows(default_cache, env)
