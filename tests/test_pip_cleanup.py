from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path, PureWindowsPath

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


def test_pip_default_and_custom_cache_are_protected_from_raw_deletion(tmp_path: Path) -> None:
    env, default_cache, custom_cache = _layout(tmp_path)

    default_rule = match_application_rule(default_cache / "http-v2" / "entry", env)
    custom_rule = match_application_rule(custom_cache / "wheels" / "pkg.whl", env)

    assert default_rule is not None
    assert default_rule.rule_id == "pip-default-cache"
    assert default_rule.owner is DecisionOwner.KEEP
    assert custom_rule is not None
    assert custom_rule.rule_id == "pip-custom-cache"
    assert custom_rule.owner is DecisionOwner.KEEP

    for path in (default_cache, custom_cache):
        decision = evaluate_application_path(
            path,
            logical_size=5 * 1024**3,
            last_used=_NOW - timedelta(days=365),
            now=_NOW,
            process_running=False,
            environment=env,
        )
        assert decision is not None
        assert decision.action is PolicyAction.KEEP_PROTECTED


def test_pip_has_no_raw_whole_tree_authority(tmp_path: Path) -> None:
    env, default_cache, custom_cache = _layout(tmp_path)

    assert whole_tree_application_rule(default_cache, env) is None
    assert whole_tree_application_rule(default_cache / "wheels", env) is None
    assert whole_tree_application_rule(custom_cache, env) is None

    discovered = discover_known_cleanup_roots(default_rules().scan, env)
    by_path = {os.path.normcase(str(item.path)): item for item in discovered}

    for cache in (default_cache, custom_cache):
        item = by_path[os.path.normcase(str(cache))]
        assert item.category is CleanupCategory.PIP_CACHE
        assert item.policy is CleanupPolicy.REPORT_ONLY
        assert not item.delete_root_itself


def test_pip_explicit_dedicated_hook_stays_report_only(tmp_path: Path) -> None:
    env, _, _ = _layout(tmp_path)
    dedicated = tmp_path / "dedicated" / "pip-cache"
    dedicated.mkdir(parents=True)
    env["DEVCLEAN_PIP_CACHE_DIR"] = str(dedicated)

    roots = pip_roots(env)
    assert PureWindowsPath(str(dedicated)) in roots.managed_cache_roots
    assert whole_tree_application_rule(dedicated, env) is None


def test_generic_process_guard_never_authorizes_raw_pip_cache_mutation(
    tmp_path: Path,
) -> None:
    env, default_cache, custom_cache = _layout(tmp_path)

    assert not application_cleanup.process_guard_allows(default_cache, env)
    assert not application_cleanup.process_guard_allows(custom_cache, env)
