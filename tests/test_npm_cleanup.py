from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path, PureWindowsPath

import pytest

from devclean.core.application_cleanup import (
    DecisionOwner,
    PolicyAction,
    application_scan_roots,
    evaluate_application_path,
    match_application_rule,
    process_guard_allows,
    whole_tree_application_rule,
)
from devclean.core.cleanup_catalog import CleanupPolicy, discover_known_cleanup_roots
from devclean.core.npm_cleanup import npm_roots
from devclean.core.triage import DirectoryScope, directory_cleanup_scope
from devclean.core.user_rules import default_rules

_NOW = datetime(2026, 8, 16, tzinfo=UTC)
_MIB = 1024**2


def _env() -> dict[str, str]:
    return {
        "USERPROFILE": r"C:\Users\alice",
        "APPDATA": r"C:\Users\alice\AppData\Roaming",
        "LOCALAPPDATA": r"C:\Users\alice\AppData\Local",
        "TEMP": r"C:\Users\alice\AppData\Local\Temp",
    }


def test_npm_default_windows_roots_are_resolved() -> None:
    roots = npm_roots(_env())
    assert roots.cache_roots == (
        PureWindowsPath(r"C:\Users\alice\AppData\Local\npm-cache"),
    )
    assert roots.prefix_roots == (
        PureWindowsPath(r"C:\Users\alice\AppData\Roaming\npm"),
    )
    assert roots.user_config_files == (PureWindowsPath(r"C:\Users\alice\.npmrc"),)
    assert roots.external_logs_roots == ()


def test_npm_explicit_cache_prefix_userconfig_and_logs_dir_are_first_class() -> None:
    env = {
        **_env(),
        "NPM_CONFIG_CACHE": r"D:\SharedNpmCache",
        "NPM_CONFIG_PREFIX": r"E:\npm-global",
        "NPM_CONFIG_USERCONFIG": r"F:\npm\account.npmrc",
        "NPM_CONFIG_LOGS_DIR": r"G:\npm-logs",
    }
    roots = npm_roots(env)
    assert roots.cache_roots[0] == PureWindowsPath(r"D:\SharedNpmCache")
    assert PureWindowsPath(r"C:\Users\alice\AppData\Local\npm-cache") in roots.cache_roots
    assert roots.prefix_roots[0] == PureWindowsPath(r"E:\npm-global")
    assert PureWindowsPath(r"C:\Users\alice\AppData\Roaming\npm") in roots.prefix_roots
    assert roots.user_config_files[0] == PureWindowsPath(r"F:\npm\account.npmrc")
    assert roots.external_logs_roots == (PureWindowsPath(r"G:\npm-logs"),)
    scan = application_scan_roots(env)
    assert PureWindowsPath(r"D:\SharedNpmCache") in scan
    assert PureWindowsPath(r"E:\npm-global") in scan
    assert PureWindowsPath(r"G:\npm-logs") in scan

    userconfig = match_application_rule(r"F:\npm\account.npmrc", env)
    assert userconfig is not None
    assert userconfig.rule_id == "npm-user-config"
    assert userconfig.owner is DecisionOwner.KEEP


def test_npm_content_npx_and_default_logs_are_tool_owned() -> None:
    paths = {
        (
            r"C:\Users\alice\AppData\Local\npm-cache"
            r"\_cacache\content-v2\sha512\aa\blob"
        ): "npm-content-cache",
        (
            r"C:\Users\alice\AppData\Local\npm-cache"
            r"\_npx\deadbeef\node_modules\pkg\index.js"
        ): "npm-npx-cache",
        (
            r"C:\Users\alice\AppData\Local\npm-cache"
            r"\_logs\2026-08-01T00_00_00_000Z-debug-0.log"
        ): "npm-default-logs",
    }
    for path, rule_id in paths.items():
        decision = evaluate_application_path(
            path,
            logical_size=100 * _MIB,
            last_used=_NOW - timedelta(days=60),
            now=_NOW,
            process_running=False,
            environment=_env(),
        )
        assert decision is not None
        assert decision.rule.owner is DecisionOwner.TOOL
        assert decision.rule.rule_id == rule_id
        assert decision.action is PolicyAction.TOOL_DELETE


def test_npx_cached_package_metadata_stays_under_npx_tool_semantics() -> None:
    path = (
        r"C:\Users\alice\AppData\Local\npm-cache"
        r"\_npx\deadbeef\package.json"
    )
    rule = match_application_rule(path, _env())
    assert rule is not None
    assert rule.rule_id == "npm-npx-cache"
    assert rule.owner is DecisionOwner.TOOL


def test_npm_cache_items_are_blocked_while_npm_is_running() -> None:
    path = (
        r"C:\Users\alice\AppData\Local\npm-cache"
        r"\_cacache\content-v2\sha512\aa\blob"
    )
    decision = evaluate_application_path(
        path,
        logical_size=100 * _MIB,
        last_used=_NOW - timedelta(days=60),
        now=_NOW,
        process_running=True,
        environment=_env(),
    )
    assert decision is not None
    assert decision.action is PolicyAction.TOOL_KEEP_IN_USE


def test_npm_custom_logs_dir_only_delegates_npm_debug_log_files() -> None:
    env = {**_env(), "NPM_CONFIG_LOGS_DIR": r"G:\shared-logs"}
    npm_log = match_application_rule(
        r"G:\shared-logs\2026-08-01T00_00_00_000Z-debug-0.log",
        env,
    )
    unrelated = match_application_rule(r"G:\shared-logs\application.log", env)
    assert npm_log is not None
    assert npm_log.owner is DecisionOwner.TOOL
    assert npm_log.rule_id == "npm-external-debug-logs"
    assert unrelated is not None
    assert unrelated.owner is DecisionOwner.KEEP
    assert unrelated.rule_id == "npm-external-logs-unclassified"


def test_npm_global_prefix_is_installed_payload_not_node_modules_cache() -> None:
    env = {**_env(), "NPM_CONFIG_PREFIX": r"E:\npm-global"}
    paths = (
        r"E:\npm-global\node_modules\typescript\lib\tsc.js",
        r"E:\npm-global\node_modules\@anthropic-ai\claude-code\cli.js",
        r"E:\npm-global\tsc.cmd",
        r"E:\npm-global\claude.cmd",
    )
    for path in paths:
        decision = evaluate_application_path(
            path,
            logical_size=500 * _MIB,
            last_used=_NOW - timedelta(days=365),
            now=_NOW,
            process_running=False,
            environment=env,
        )
        assert decision is not None
        assert decision.rule.rule_id == "npm-global-prefix"
        assert decision.rule.owner is DecisionOwner.KEEP
        assert decision.action is PolicyAction.KEEP_PROTECTED
        assert not process_guard_allows(path, env)


def test_npm_project_and_config_metadata_are_always_protected() -> None:
    paths = (
        r"C:\Users\alice\.npmrc",
        r"D:\src\app\.npmrc",
        r"D:\src\app\package.json",
        r"D:\src\app\package-lock.json",
        r"D:\src\app\npm-shrinkwrap.json",
    )
    for path in paths:
        rule = match_application_rule(path, _env())
        assert rule is not None
        assert rule.rule_id == "npm-project-metadata"
        assert rule.owner is DecisionOwner.KEEP
        assert not process_guard_allows(path, _env())


def test_npm_unclassified_cache_root_state_is_not_assumed_disposable() -> None:
    for path in (
        r"C:\Users\alice\AppData\Local\npm-cache\_tuf\root.json",
        r"C:\Users\alice\AppData\Local\npm-cache\future-state.db",
    ):
        rule = match_application_rule(path, _env())
        assert rule is not None
        assert rule.rule_id == "npm-cache-unclassified"
        assert rule.owner is DecisionOwner.KEEP


def test_npm_whole_tree_authority_stops_at_owned_cache_subdirectories() -> None:
    env = {**_env(), "NPM_CONFIG_CACHE": r"D:\SharedNpmCache"}
    for child in ("_cacache", "_npx", "_logs"):
        rule = whole_tree_application_rule(rf"D:\SharedNpmCache\{child}", env)
        assert rule is not None
        assert rule.owner is DecisionOwner.TOOL
    assert whole_tree_application_rule(r"D:\SharedNpmCache", env) is None
    assert whole_tree_application_rule(r"D:\SharedNpmCache\_tuf", env) is None


def test_catalog_protects_custom_prefix_and_upgrades_only_npm_cache_children(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    appdata = home / "AppData" / "Roaming"
    localappdata = home / "AppData" / "Local"
    cache = tmp_path / "npm-cache"
    prefix = tmp_path / "npm-global"
    content_cache = cache / "_cacache"
    npx_cache = cache / "_npx"
    global_modules = prefix / "node_modules"
    content_cache.mkdir(parents=True)
    npx_cache.mkdir(parents=True)
    global_modules.mkdir(parents=True)
    env = {
        "USERPROFILE": str(home),
        "APPDATA": str(appdata),
        "LOCALAPPDATA": str(localappdata),
        "TEMP": str(tmp_path / "temp"),
        "NPM_CONFIG_CACHE": str(cache),
        "NPM_CONFIG_PREFIX": str(prefix),
    }

    rules = default_rules()
    discovered = discover_known_cleanup_roots(rules.scan, env)
    by_path = {os.path.normcase(str(root.path)): root for root in discovered}
    cache_root = by_path[os.path.normcase(str(cache))]
    prefix_root = by_path[os.path.normcase(str(prefix))]
    cacache_root = by_path[os.path.normcase(str(content_cache))]
    npx_root = by_path[os.path.normcase(str(npx_cache))]

    assert not cache_root.delete_root_itself
    assert prefix_root.policy is CleanupPolicy.REPORT_ONLY
    assert not prefix_root.delete_root_itself
    assert cacache_root.policy is CleanupPolicy.VENDOR_MANAGED
    assert cacache_root.delete_root_itself
    assert npx_root.policy is CleanupPolicy.VENDOR_MANAGED
    assert npx_root.delete_root_itself

    scope = directory_cleanup_scope(
        global_modules,
        discovered,
        rules.delete.classification,
        rules.keep.classification,
    )
    assert scope is DirectoryScope.NOT_ELIGIBLE


def test_project_node_modules_remains_generic_regenerable_output(tmp_path: Path) -> None:
    project_modules = tmp_path / "repo" / "node_modules"
    project_modules.mkdir(parents=True)
    rules = default_rules()
    scope = directory_cleanup_scope(
        project_modules,
        (),
        rules.delete.classification,
        rules.keep.classification,
    )
    assert scope is DirectoryScope.REGENERABLE_TOOL_OUTPUT


def test_npm_process_guard_rechecks_live_npm_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "devclean.core.application_cleanup.npm_process_running",
        lambda: True,
    )
    assert not process_guard_allows(
        r"C:\Users\alice\AppData\Local\npm-cache\_npx\hash\package.json.tmp",
        _env(),
    )
