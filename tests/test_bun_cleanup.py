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
from devclean.core.bun_cleanup import bun_roots
from devclean.core.cleanup_catalog import (
    CleanupCategory,
    CleanupPolicy,
    discover_known_cleanup_roots,
)
from devclean.core.user_rules import default_rules

_NOW = datetime(2026, 8, 17, tzinfo=UTC)
_MIB = 1024**2


def _layout(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    home = tmp_path / "home" / ".bun"
    cache = home / "install" / "cache"
    global_packages = home / "install" / "global"
    cache.mkdir(parents=True)
    global_packages.mkdir(parents=True)
    (home / "bin").mkdir(parents=True)
    env = {
        "USERPROFILE": str(tmp_path / "home"),
        "APPDATA": str(tmp_path / "Roaming"),
        "LOCALAPPDATA": str(tmp_path / "Local"),
        "TEMP": str(tmp_path / "Temp"),
    }
    return env, home, cache


def test_bun_default_home_and_global_cache_are_discovered(tmp_path: Path) -> None:
    env, home, cache = _layout(tmp_path)

    roots = bun_roots(env)

    assert PureWindowsPath(str(home)) in roots.home_roots
    assert PureWindowsPath(str(cache)) in roots.cache_roots
    scan = application_scan_roots(env)
    assert PureWindowsPath(str(home)) in scan
    assert PureWindowsPath(str(cache)) in scan


def test_bun_exact_global_cache_is_tool_but_surrounding_home_is_protected(
    tmp_path: Path,
) -> None:
    env, home, cache = _layout(tmp_path)
    cases = {
        cache / "@scope" / "pkg@1.0.0": (
            "bun-global-module-cache",
            DecisionOwner.TOOL,
        ),
        home / "install" / "global" / "node_modules" / "typescript": (
            "bun-home-state",
            DecisionOwner.KEEP,
        ),
        home / "bin" / "bunx.exe": ("bun-home-state", DecisionOwner.KEEP),
        home / "bun.exe": ("bun-home-state", DecisionOwner.KEEP),
        home / "unknown-state.db": ("bun-home-state", DecisionOwner.KEEP),
    }

    for path, (rule_id, owner) in cases.items():
        rule = match_application_rule(path, env)
        assert rule is not None
        assert rule.rule_id == rule_id
        assert rule.owner is owner


def test_bun_custom_cache_environment_is_source_backed(tmp_path: Path) -> None:
    env, _, _ = _layout(tmp_path)
    custom = tmp_path / "cache-drive" / "bun-cache"
    custom.mkdir(parents=True)
    env["BUN_INSTALL_CACHE_DIR"] = str(custom)

    roots = bun_roots(env)
    assert PureWindowsPath(str(custom)) in roots.cache_roots

    rule = match_application_rule(custom / "react@19.0.0", env)
    assert rule is not None
    assert rule.rule_id == "bun-global-module-cache"
    assert whole_tree_application_rule(custom, env) is not None


def test_bun_project_local_cache_and_metadata_are_protected(tmp_path: Path) -> None:
    env, _, _ = _layout(tmp_path)
    project = tmp_path / "work" / "project"
    project_cache = project / ".bun" / "cache" / "offline-package"

    cache_rule = match_application_rule(project_cache, env)
    assert cache_rule is not None
    assert cache_rule.rule_id == "bun-project-cache"
    assert cache_rule.owner is DecisionOwner.USER

    for path in (
        project / "bun.lock",
        project / "bun.lockb",
        project / "bunfig.toml",
    ):
        rule = match_application_rule(path, env)
        assert rule is not None
        assert rule.rule_id == "bun-project-metadata"
        assert rule.owner is DecisionOwner.KEEP

    decision = evaluate_application_path(
        project_cache,
        logical_size=2 * 1024**3,
        last_used=_NOW - timedelta(days=365),
        now=_NOW,
        process_running=False,
        environment=env,
    )
    assert decision is not None
    assert decision.action is PolicyAction.KEEP_PROTECTED
    assert whole_tree_application_rule(project / ".bun" / "cache", env) is None


def test_bun_global_cache_policy_is_conservative(tmp_path: Path) -> None:
    env, _, cache = _layout(tmp_path)

    recent = evaluate_application_path(
        cache,
        logical_size=2 * 1024**3,
        last_used=_NOW - timedelta(days=10),
        now=_NOW,
        process_running=False,
        environment=env,
    )
    stale = evaluate_application_path(
        cache,
        logical_size=2 * 1024**3,
        last_used=_NOW - timedelta(days=45),
        now=_NOW,
        process_running=False,
        environment=env,
    )
    small = evaluate_application_path(
        cache,
        logical_size=16 * _MIB,
        last_used=_NOW - timedelta(days=365),
        now=_NOW,
        process_running=False,
        environment=env,
    )
    running = evaluate_application_path(
        cache,
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


def test_bun_whole_tree_authority_is_exact_and_catalogued(tmp_path: Path) -> None:
    env, home, cache = _layout(tmp_path)

    assert whole_tree_application_rule(cache, env) is not None
    assert whole_tree_application_rule(home, env) is None
    assert whole_tree_application_rule(home / "install", env) is None
    assert whole_tree_application_rule(home / "install" / "global", env) is None

    discovered = discover_known_cleanup_roots(default_rules().scan, env)
    by_path = {os.path.normcase(str(item.path)): item for item in discovered}

    home_item = by_path[os.path.normcase(str(home))]
    cache_item = by_path[os.path.normcase(str(cache))]

    assert home_item.policy is CleanupPolicy.REPORT_ONLY
    assert not home_item.delete_root_itself
    assert cache_item.category is CleanupCategory.NPM_CACHE
    assert cache_item.policy is CleanupPolicy.VENDOR_MANAGED
    assert cache_item.delete_root_itself
    assert cache_item.application_rule is not None


def test_bun_process_guard_is_independent_from_other_js_package_managers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, _, cache = _layout(tmp_path)

    monkeypatch.setattr(application_cleanup, "bun_process_running", lambda: True)
    monkeypatch.setattr(application_cleanup, "npm_process_running", lambda: False)
    monkeypatch.setattr(application_cleanup, "pnpm_process_running", lambda: False)
    monkeypatch.setattr(application_cleanup, "yarn_process_running", lambda: False)
    assert not application_cleanup.process_guard_allows(cache, env)

    monkeypatch.setattr(application_cleanup, "bun_process_running", lambda: False)
    monkeypatch.setattr(application_cleanup, "npm_process_running", lambda: True)
    monkeypatch.setattr(application_cleanup, "pnpm_process_running", lambda: True)
    monkeypatch.setattr(application_cleanup, "yarn_process_running", lambda: True)
    assert application_cleanup.process_guard_allows(cache, env)
