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
from devclean.core.pnpm_cleanup import pnpm_roots
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


def test_pnpm_default_windows_roots_are_resolved() -> None:
    roots = pnpm_roots(_env())
    home = PureWindowsPath(r"C:\Users\alice\AppData\Local\pnpm")
    assert roots.cache_roots == (
        PureWindowsPath(r"C:\Users\alice\AppData\Local\pnpm-cache"),
    )
    assert roots.state_roots == (
        PureWindowsPath(r"C:\Users\alice\AppData\Local\pnpm-state"),
    )
    assert roots.home_roots == (home,)
    assert roots.store_roots == (home / "store",)
    assert roots.global_roots == (home / "global",)
    assert roots.global_bin_roots == (home / "bin",)


def test_pnpm_explicit_dirs_and_pnpm_home_are_first_class() -> None:
    env = {
        **_env(),
        "PNPM_HOME": r"E:\pnpm-home",
        "PNPM_CONFIG_CACHE_DIR": r"D:\pnpm-cache",
        "PNPM_CONFIG_STATE_DIR": r"D:\pnpm-state",
        "PNPM_CONFIG_STORE_DIR": r"F:\pnpm-store",
        "PNPM_CONFIG_GLOBAL_DIR": r"G:\pnpm-global",
        "PNPM_CONFIG_GLOBAL_BIN_DIR": r"H:\pnpm-bin",
    }
    roots = pnpm_roots(env)
    assert roots.cache_roots[0] == PureWindowsPath(r"D:\pnpm-cache")
    assert roots.state_roots[0] == PureWindowsPath(r"D:\pnpm-state")
    assert roots.home_roots == (PureWindowsPath(r"E:\pnpm-home"),)
    assert roots.store_roots == (PureWindowsPath(r"F:\pnpm-store"),)
    assert roots.global_roots == (PureWindowsPath(r"G:\pnpm-global"),)
    assert roots.global_bin_roots == (PureWindowsPath(r"H:\pnpm-bin"),)
    scan = application_scan_roots(env)
    assert PureWindowsPath(r"F:\pnpm-store") in scan
    assert PureWindowsPath(r"G:\pnpm-global") in scan


def test_pnpm_dlx_cache_uses_source_backed_one_day_default() -> None:
    path = r"C:\Users\alice\AppData\Local\pnpm-cache\dlx\hash\pkg\package.json"
    recent = evaluate_application_path(
        path,
        logical_size=100 * _MIB,
        last_used=_NOW - timedelta(hours=12),
        now=_NOW,
        process_running=False,
        environment=_env(),
    )
    old = evaluate_application_path(
        path,
        logical_size=100 * _MIB,
        last_used=_NOW - timedelta(days=3),
        now=_NOW,
        process_running=False,
        environment=_env(),
    )
    assert recent is not None
    assert recent.rule.rule_id == "pnpm-dlx-cache"
    assert recent.rule.owner is DecisionOwner.TOOL
    assert recent.action is PolicyAction.TOOL_KEEP_RECENT
    assert old is not None
    assert old.action is PolicyAction.TOOL_DELETE


def test_pnpm_metadata_cache_is_tool_owned() -> None:
    path = (
        r"C:\Users\alice\AppData\Local\pnpm-cache"
        r"\metadata-v1.3\registry.npmjs.org\react.json"
    )
    decision = evaluate_application_path(
        path,
        logical_size=20 * _MIB,
        last_used=_NOW - timedelta(days=30),
        now=_NOW,
        process_running=False,
        environment=_env(),
    )
    assert decision is not None
    assert decision.rule.rule_id == "pnpm-metadata-cache"
    assert decision.rule.owner is DecisionOwner.TOOL
    assert decision.action is PolicyAction.TOOL_DELETE


def test_pnpm_update_state_is_regenerable_but_low_value() -> None:
    rule = match_application_rule(
        r"C:\Users\alice\AppData\Local\pnpm-state\pnpm-state.json",
        _env(),
    )
    assert rule is not None
    assert rule.rule_id == "pnpm-update-state"
    assert rule.owner is DecisionOwner.TOOL


def test_pnpm_store_home_and_global_installs_are_protected() -> None:
    env = {
        **_env(),
        "PNPM_HOME": r"E:\pnpm-home",
        "PNPM_CONFIG_STORE_DIR": r"F:\pnpm-store",
        "PNPM_CONFIG_GLOBAL_DIR": r"G:\pnpm-global",
        "PNPM_CONFIG_GLOBAL_BIN_DIR": r"H:\pnpm-bin",
    }
    paths = {
        r"F:\pnpm-store\v10\files\aa\blob": "pnpm-store",
        r"F:\pnpm-store\v10\links\react\index.json": "pnpm-store",
        r"G:\pnpm-global\5\node_modules\typescript\lib\tsc.js": "pnpm-global-install",
        r"H:\pnpm-bin\pnpm.cmd": "pnpm-global-bin",
        r"E:\pnpm-home\pnpm.exe": "pnpm-home",
    }
    for path, rule_id in paths.items():
        decision = evaluate_application_path(
            path,
            logical_size=5 * 1024 * _MIB,
            last_used=_NOW - timedelta(days=365),
            now=_NOW,
            process_running=False,
            environment=env,
        )
        assert decision is not None
        assert decision.rule.rule_id == rule_id
        assert decision.rule.owner is DecisionOwner.KEEP
        assert decision.action is PolicyAction.KEEP_PROTECTED
        assert not process_guard_allows(path, env)


def test_pnpm_lock_and_workspace_files_are_always_protected() -> None:
    for path in (
        r"D:\src\app\pnpm-lock.yaml",
        r"D:\src\app\pnpm-workspace.yaml",
    ):
        rule = match_application_rule(path, _env())
        assert rule is not None
        assert rule.rule_id == "pnpm-project-metadata"
        assert rule.owner is DecisionOwner.KEEP
        assert not process_guard_allows(path, _env())


def test_pnpm_store_never_receives_generic_whole_tree_authority() -> None:
    env = {**_env(), "PNPM_CONFIG_STORE_DIR": r"F:\pnpm-store"}
    assert whole_tree_application_rule(r"F:\pnpm-store", env) is None
    assert whole_tree_application_rule(r"F:\pnpm-store\v10", env) is None
    assert whole_tree_application_rule(r"F:\pnpm-store\v10\links", env) is None


def test_pnpm_dlx_whole_tree_authority_is_exact_cache_child_only() -> None:
    env = {**_env(), "PNPM_CONFIG_CACHE_DIR": r"D:\pnpm-cache"}
    rule = whole_tree_application_rule(r"D:\pnpm-cache\dlx", env)
    assert rule is not None
    assert rule.rule_id == "pnpm-dlx-cache"
    assert rule.owner is DecisionOwner.TOOL
    assert whole_tree_application_rule(r"D:\pnpm-cache", env) is None


def test_catalog_discovers_metadata_dlx_but_protects_store_and_global(
    tmp_path: Path,
) -> None:
    home = tmp_path / "pnpm-home"
    cache = tmp_path / "pnpm-cache"
    state = tmp_path / "pnpm-state"
    store = tmp_path / "pnpm-store"
    global_dir = tmp_path / "pnpm-global"
    global_bin = tmp_path / "pnpm-bin"
    dlx = cache / "dlx"
    metadata = cache / "metadata-v1.3"
    for path in (home, state, store, global_dir, global_bin, dlx, metadata):
        path.mkdir(parents=True)
    env = {
        "USERPROFILE": str(tmp_path / "profile"),
        "APPDATA": str(tmp_path / "roaming"),
        "LOCALAPPDATA": str(tmp_path / "local"),
        "TEMP": str(tmp_path / "temp"),
        "PNPM_HOME": str(home),
        "PNPM_CONFIG_CACHE_DIR": str(cache),
        "PNPM_CONFIG_STATE_DIR": str(state),
        "PNPM_CONFIG_STORE_DIR": str(store),
        "PNPM_CONFIG_GLOBAL_DIR": str(global_dir),
        "PNPM_CONFIG_GLOBAL_BIN_DIR": str(global_bin),
    }

    rules = default_rules()
    discovered = discover_known_cleanup_roots(rules.scan, env)
    by_path = {os.path.normcase(str(root.path)): root for root in discovered}
    cache_root = by_path[os.path.normcase(str(cache))]
    store_root = by_path[os.path.normcase(str(store))]
    global_root = by_path[os.path.normcase(str(global_dir))]
    dlx_root = by_path[os.path.normcase(str(dlx))]
    metadata_root = by_path[os.path.normcase(str(metadata))]

    assert cache_root.policy is CleanupPolicy.REPORT_ONLY
    assert not cache_root.delete_root_itself
    assert store_root.policy is CleanupPolicy.REPORT_ONLY
    assert not store_root.delete_root_itself
    assert global_root.policy is CleanupPolicy.REPORT_ONLY
    assert not global_root.delete_root_itself
    assert dlx_root.policy is CleanupPolicy.VENDOR_MANAGED
    assert dlx_root.delete_root_itself
    assert metadata_root.policy is CleanupPolicy.VENDOR_MANAGED
    assert metadata_root.delete_root_itself


def test_pnpm_global_node_modules_are_not_project_cleanup_output(tmp_path: Path) -> None:
    home = tmp_path / "pnpm-home"
    global_dir = tmp_path / "pnpm-global"
    modules = global_dir / "5" / "node_modules"
    modules.mkdir(parents=True)
    env = {
        "USERPROFILE": str(tmp_path / "profile"),
        "APPDATA": str(tmp_path / "roaming"),
        "LOCALAPPDATA": str(tmp_path / "local"),
        "TEMP": str(tmp_path / "temp"),
        "PNPM_HOME": str(home),
        "PNPM_CONFIG_GLOBAL_DIR": str(global_dir),
    }
    rules = default_rules()
    discovered = discover_known_cleanup_roots(rules.scan, env)
    scope = directory_cleanup_scope(
        modules,
        discovered,
        rules.delete.classification,
        rules.keep.classification,
    )
    assert scope is DirectoryScope.NOT_ELIGIBLE


def test_project_node_modules_remains_regenerable() -> None:
    modules = Path(r"D:\src\repo\node_modules")
    rules = default_rules()
    scope = directory_cleanup_scope(
        modules,
        (),
        rules.delete.classification,
        rules.keep.classification,
    )
    assert scope is DirectoryScope.REGENERABLE_TOOL_OUTPUT


def test_pnpm_cache_is_blocked_when_process_is_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "devclean.core.application_cleanup.pnpm_process_running",
        lambda: True,
    )
    assert not process_guard_allows(
        r"C:\Users\alice\AppData\Local\pnpm-cache\dlx\hash\pkg\index.js",
        _env(),
    )
