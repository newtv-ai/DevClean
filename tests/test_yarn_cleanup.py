from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path, PureWindowsPath

from devclean.core.application_cleanup import (
    DecisionOwner,
    PolicyAction,
    application_scan_roots,
    evaluate_application_path,
    match_application_rule,
    whole_tree_application_rule,
)
from devclean.core.cleanup_catalog import CleanupPolicy, discover_known_cleanup_roots
from devclean.core.user_rules import default_rules
from devclean.core.yarn_cleanup import yarn_audited_tool_roots, yarn_roots

_NOW = datetime(2026, 8, 17, tzinfo=UTC)


def _layout(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    local = tmp_path / "Local"
    classic = local / "Yarn" / "Cache"
    global_folder = tmp_path / "YarnBerry"
    classic.mkdir(parents=True)
    (global_folder / "cache").mkdir(parents=True)
    (global_folder / "store").mkdir(parents=True)
    env = {
        "USERPROFILE": str(tmp_path / "home"),
        "APPDATA": str(tmp_path / "Roaming"),
        "LOCALAPPDATA": str(local),
        "TEMP": str(tmp_path / "Temp"),
        "DEVCLEAN_YARN_GLOBAL_FOLDER": str(global_folder),
    }
    return env, classic, global_folder


def test_yarn_machine_cache_roots_are_discovered_without_project_cache_authority(
    tmp_path: Path,
) -> None:
    env, classic, global_folder = _layout(tmp_path)

    roots = yarn_roots(env)

    assert PureWindowsPath(str(classic)) in roots.classic_cache_roots
    assert PureWindowsPath(str(global_folder)) in roots.global_folder_roots
    assert PureWindowsPath(str(global_folder / "cache")) in roots.global_cache_roots
    scan = application_scan_roots(env)
    assert PureWindowsPath(str(classic)) in scan
    assert PureWindowsPath(str(global_folder)) in scan
    assert PureWindowsPath(str(global_folder / "cache")) in scan


def test_yarn_machine_caches_and_global_state_are_protected(
    tmp_path: Path,
) -> None:
    env, classic, global_folder = _layout(tmp_path)
    cases = {
        classic / "v6" / "npm-lodash": "yarn-classic-global-cache",
        global_folder / "cache" / "lodash-npm-4.17.21.zip": "yarn-modern-global-cache",
        global_folder / "store" / "v1" / "ab" / "content.dat": "yarn-modern-global-state",
        global_folder / "index.json": "yarn-modern-global-state",
    }

    for path, rule_id in cases.items():
        rule = match_application_rule(path, env)
        assert rule is not None
        assert rule.rule_id == rule_id
        assert rule.owner is DecisionOwner.KEEP


def test_yarn_project_local_cache_is_user_owned_for_zero_installs(tmp_path: Path) -> None:
    env, _, _ = _layout(tmp_path)
    project = tmp_path / "work" / "project"
    local_cache = project / ".yarn" / "cache" / "react-npm-19.0.0.zip"
    patch = project / ".yarn" / "patches" / "react.patch"
    release = project / ".yarn" / "releases" / "yarn-4.10.3.cjs"
    unplugged = project / ".yarn" / "unplugged" / "native-package" / "node.node"
    lockfile = project / "yarn.lock"
    yarnrc = project / ".yarnrc.yml"
    pnp = project / ".pnp.cjs"

    cache_rule = match_application_rule(local_cache, env)
    assert cache_rule is not None
    assert cache_rule.rule_id == "yarn-project-offline-cache"
    assert cache_rule.owner is DecisionOwner.USER

    for path in (patch, release, unplugged, lockfile, yarnrc, pnp):
        rule = match_application_rule(path, env)
        assert rule is not None
        assert rule.owner is DecisionOwner.KEEP

    decision = evaluate_application_path(
        local_cache,
        logical_size=2 * 1024**3,
        last_used=_NOW - timedelta(days=365),
        now=_NOW,
        process_running=False,
        environment=env,
    )
    assert decision is not None
    assert decision.action is PolicyAction.USER_DECISION


def test_yarn_machine_cache_never_becomes_age_or_size_delete_candidate(
    tmp_path: Path,
) -> None:
    env, classic, global_folder = _layout(tmp_path)
    paths = (classic, global_folder / "cache")

    for path in paths:
        for logical_size, age_days, running in (
            (2 * 1024**3, 10, False),
            (2 * 1024**3, 3650, False),
            (1, 3650, False),
            (2 * 1024**3, 3650, True),
        ):
            decision = evaluate_application_path(
                path,
                logical_size=logical_size,
                last_used=_NOW - timedelta(days=age_days),
                now=_NOW,
                process_running=running,
                environment=env,
            )
            assert decision is not None
            assert decision.action is PolicyAction.KEEP_PROTECTED
            assert decision.effective_idle_days is None


def test_yarn_has_no_generic_whole_tree_authority_and_catalog_is_report_only(
    tmp_path: Path,
) -> None:
    env, classic, global_folder = _layout(tmp_path)
    global_cache = global_folder / "cache"
    global_store = global_folder / "store"

    assert yarn_audited_tool_roots(env) == ()
    assert whole_tree_application_rule(classic, env) is None
    assert whole_tree_application_rule(global_cache, env) is None
    assert whole_tree_application_rule(global_folder, env) is None
    assert whole_tree_application_rule(global_store, env) is None
    assert whole_tree_application_rule(global_cache / "pkg.zip", env) is None

    discovered = discover_known_cleanup_roots(default_rules().scan, env)
    by_path = {os.path.normcase(str(item.path)): item for item in discovered}

    classic_item = by_path[os.path.normcase(str(classic))]
    global_item = by_path[os.path.normcase(str(global_folder))]
    cache_item = by_path[os.path.normcase(str(global_cache))]

    assert classic_item.policy is CleanupPolicy.REPORT_ONLY
    assert not classic_item.delete_root_itself
    assert global_item.policy is CleanupPolicy.REPORT_ONLY
    assert not global_item.delete_root_itself
    assert cache_item.policy is CleanupPolicy.REPORT_ONLY
    assert not cache_item.delete_root_itself
