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
from devclean.core.cleanup_catalog import (
    CleanupCategory,
    CleanupPolicy,
    discover_known_cleanup_roots,
)
from devclean.core.edge_cleanup import _policy_path, edge_roots
from devclean.core.user_rules import default_rules

_NOW = datetime(2026, 8, 16, tzinfo=UTC)
_MIB = 1024**2


def _env() -> dict[str, str]:
    return {
        "USERPROFILE": r"C:\Users\alice",
        "APPDATA": r"C:\Users\alice\AppData\Roaming",
        "LOCALAPPDATA": r"C:\Users\alice\AppData\Local",
        "PROGRAMDATA": r"C:\ProgramData",
        "ProgramFiles(x86)": r"C:\Program Files (x86)",
        "WINDIR": r"C:\Windows",
        "TEMP": r"C:\Users\alice\AppData\Local\Temp",
    }


def test_edge_default_channel_and_updater_roots_are_discovered() -> None:
    roots = edge_roots(_env())
    expected = {
        PureWindowsPath(r"C:\Users\alice\AppData\Local\Microsoft\Edge\User Data"),
        PureWindowsPath(
            r"C:\Users\alice\AppData\Local\Microsoft\Edge Beta\User Data"
        ),
        PureWindowsPath(
            r"C:\Users\alice\AppData\Local\Microsoft\Edge Dev\User Data"
        ),
        PureWindowsPath(
            r"C:\Users\alice\AppData\Local\Microsoft\Edge SxS\User Data"
        ),
    }
    assert expected.issubset(set(roots.data_roots))
    assert PureWindowsPath(
        r"C:\Users\alice\AppData\Local\Microsoft\Edge\Update"
    ) in roots.updater_roots
    assert PureWindowsPath(
        r"C:\Program Files (x86)\Microsoft\EdgeUpdate"
    ) in roots.updater_roots
    assert PureWindowsPath(r"C:\ProgramData\Microsoft\EdgeUpdate") in roots.updater_roots

    scan = set(application_scan_roots(_env()))
    assert PureWindowsPath(r"C:\Users\alice\AppData\Local\Temp") not in scan
    assert PureWindowsPath(r"C:\Windows\Temp") not in scan


def test_edge_policy_path_variables_resolve_only_to_absolute_paths() -> None:
    env = {key.casefold(): value for key, value in _env().items()}
    assert _policy_path(r"${local_app_data}\Microsoft\ManagedEdge", env) == PureWindowsPath(
        r"C:\Users\alice\AppData\Local\Microsoft\ManagedEdge"
    )
    assert _policy_path(r"${users}\${user_name}\ManagedEdge", env) == PureWindowsPath(
        r"C:\Users\alice\ManagedEdge"
    )
    assert _policy_path(r"${unknown}\Edge", env) is None
    assert _policy_path(r"relative\Edge", env) is None


def test_edge_profile_caches_follow_chromium_semantics() -> None:
    tool_paths = {
        (
            r"C:\Users\alice\AppData\Local\Microsoft\Edge\User Data\Default"
            r"\Cache\Cache_Data\f_001"
        ): "edge-http-cache",
        (
            r"C:\Users\alice\AppData\Local\Microsoft\Edge\User Data\Profile 2"
            r"\Code Cache\js\entry"
        ): "edge-code-cache",
        (
            r"C:\Users\alice\AppData\Local\Microsoft\Edge\User Data"
            r"\ShaderCache\GPUCache\data_0"
        ): "edge-shader-cache",
        (
            r"C:\Users\alice\AppData\Local\Microsoft\Edge\User Data\Default"
            r"\Service Worker\ScriptCache\entry"
        ): "edge-service-worker-script-cache",
    }
    for path, rule_id in tool_paths.items():
        rule = match_application_rule(path, _env())
        assert rule is not None
        assert rule.owner is DecisionOwner.TOOL
        assert rule.rule_id == rule_id


def test_edge_site_cache_storage_is_user_owned_and_profile_state_is_keep() -> None:
    cache_storage = (
        r"C:\Users\alice\AppData\Local\Microsoft\Edge\User Data\Default"
        r"\Service Worker\CacheStorage\origin\entry"
    )
    site_rule = match_application_rule(cache_storage, _env())
    assert site_rule is not None
    assert site_rule.rule_id == "edge-site-cache-storage"
    assert site_rule.owner is DecisionOwner.USER
    decision = evaluate_application_path(
        cache_storage,
        logical_size=500 * _MIB,
        last_used=_NOW - timedelta(days=180),
        now=_NOW,
        process_running=False,
        environment=_env(),
    )
    assert decision is not None
    assert decision.action is PolicyAction.KEEP_PROTECTED
    assert whole_tree_application_rule(
        r"C:\Users\alice\AppData\Local\Microsoft\Edge\User Data\Default"
        r"\Service Worker\CacheStorage",
        _env(),
    ) is None

    for path in (
        r"C:\Users\alice\AppData\Local\Microsoft\Edge\User Data\Default\History",
        (
            r"C:\Users\alice\AppData\Local\Microsoft\Edge\User Data\Default"
            r"\Login Data"
        ),
        (
            r"C:\Users\alice\AppData\Local\Microsoft\Edge\User Data\Default"
            r"\Extensions\abc\1.0\manifest.json"
        ),
        (
            r"C:\Users\alice\AppData\Local\Microsoft\Edge\User Data\Default"
            r"\IndexedDB\https_example.indexeddb.leveldb\000003.log"
        ),
    ):
        rule = match_application_rule(path, _env())
        assert rule is not None
        assert rule.rule_id == "edge-profile-state"
        assert rule.owner is DecisionOwner.KEEP


def test_edge_explicit_disk_cache_is_tool_but_user_data_root_is_not() -> None:
    env = {
        **_env(),
        "DEVCLEAN_EDGE_USER_DATA_DIR": r"D:\ManagedEdgeState",
        "DEVCLEAN_EDGE_DISK_CACHE_DIR": r"E:\EdgeDiskCache",
    }
    cache_rule = match_application_rule(r"E:\EdgeDiskCache\Cache_Data\f_001", env)
    assert cache_rule is not None
    assert cache_rule.rule_id == "edge-explicit-disk-cache"
    assert cache_rule.owner is DecisionOwner.TOOL
    assert whole_tree_application_rule(r"E:\EdgeDiskCache", env) is not None
    assert whole_tree_application_rule(r"D:\ManagedEdgeState", env) is None


def test_edge_updater_state_is_protected_but_official_logs_are_tool() -> None:
    updater_state = (
        r"C:\Program Files (x86)\Microsoft\EdgeUpdate"
        r"\1.3.195.43\MicrosoftEdgeUpdate.exe"
    )
    state_rule = match_application_rule(updater_state, _env())
    assert state_rule is not None
    assert state_rule.rule_id == "edge-updater-state"
    assert state_rule.owner is DecisionOwner.KEEP
    assert whole_tree_application_rule(
        r"C:\Program Files (x86)\Microsoft\EdgeUpdate",
        _env(),
    ) is None

    logs = {
        (
            r"C:\ProgramData\Microsoft\EdgeUpdate\Log"
            r"\MicrosoftEdgeUpdate.log"
        ): "edge-update-log",
        (
            r"C:\Users\alice\AppData\Local\Temp"
            r"\MicrosoftEdgeUpdate.log.bak"
        ): "edge-update-log-backup",
        r"C:\Windows\Temp\msedge_installer.log": "edge-installer-log",
    }
    for path, rule_id in logs.items():
        rule = match_application_rule(path, _env())
        assert rule is not None
        assert rule.rule_id == rule_id
        assert rule.owner is DecisionOwner.TOOL

    unrelated = match_application_rule(
        r"C:\Users\alice\AppData\Local\Temp\another-program.tmp",
        _env(),
    )
    assert unrelated is None or unrelated.app_id != "edge"


def test_edge_per_user_update_cache_is_reported_not_raw_deleted() -> None:
    path = r"C:\Users\alice\AppData\Local\Microsoft\Edge\Update\payload.bin"
    decision = evaluate_application_path(
        path,
        logical_size=2 * 1024**3,
        last_used=_NOW - timedelta(days=365),
        now=_NOW,
        process_running=False,
        environment=_env(),
    )
    assert decision is not None
    assert decision.rule.rule_id == "edge-updater-state"
    assert decision.rule.owner is DecisionOwner.KEEP
    assert decision.action is PolicyAction.KEEP_PROTECTED


def test_edge_webview2_user_data_is_not_mistaken_for_browser_profile() -> None:
    path = (
        r"C:\Users\alice\AppData\Local\Contoso\MyApp"
        r"\EBWebView\Default\Cache\Cache_Data\f_001"
    )
    rule = match_application_rule(path, _env())
    assert rule is None or rule.app_id != "edge"


def test_edge_catalog_upgrades_only_exact_browser_cache_roots(tmp_path: Path) -> None:
    local = tmp_path / "Local"
    data = local / "Microsoft" / "Edge" / "User Data"
    profile = data / "Default"
    cache = profile / "Cache"
    cache.mkdir(parents=True)
    (profile / "History").write_text("history", encoding="utf-8")
    update = local / "Microsoft" / "Edge" / "Update"
    update.mkdir(parents=True)
    env = {
        "USERPROFILE": str(tmp_path / "home"),
        "APPDATA": str(tmp_path / "Roaming"),
        "LOCALAPPDATA": str(local),
        "PROGRAMDATA": str(tmp_path / "ProgramData"),
        "ProgramFiles(x86)": str(tmp_path / "Program Files (x86)"),
        "WINDIR": str(tmp_path / "Windows"),
        "TEMP": str(tmp_path / "Temp"),
    }

    discovered = discover_known_cleanup_roots(default_rules().scan, env)
    by_path = {os.path.normcase(str(root.path)): root for root in discovered}
    data_root = by_path[os.path.normcase(str(data))]
    cache_root = by_path[os.path.normcase(str(cache))]
    update_root = by_path[os.path.normcase(str(update))]
    assert data_root.policy is CleanupPolicy.REPORT_ONLY
    assert not data_root.delete_root_itself
    assert cache_root.policy is CleanupPolicy.VENDOR_MANAGED
    assert cache_root.category is CleanupCategory.BROWSER_CACHE
    assert cache_root.delete_root_itself
    assert update_root.policy is CleanupPolicy.REPORT_ONLY
    assert not update_root.delete_root_itself


def test_edge_cache_process_guard_rechecks_live_edge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "devclean.core.application_cleanup.edge_process_running",
        lambda: True,
    )
    cache_file = (
        r"C:\Users\alice\AppData\Local\Microsoft\Edge\User Data\Default"
        r"\Cache\Cache_Data\f_001"
    )
    assert not process_guard_allows(cache_file, _env())
