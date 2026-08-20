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
from devclean.core.user_rules import default_rules
from devclean.core.vscode_cleanup import _argument_value, vscode_roots
from devclean.core.vscode_maintenance import inventory_vscode_storage

_NOW = datetime(2026, 8, 16, tzinfo=UTC)
_MIB = 1024**2


def _env() -> dict[str, str]:
    return {
        "USERPROFILE": r"C:\Users\alice",
        "APPDATA": r"C:\Users\alice\AppData\Roaming",
        "LOCALAPPDATA": r"C:\Users\alice\AppData\Local",
        "TEMP": r"C:\Users\alice\AppData\Local\Temp",
    }


def test_vscode_default_stable_insiders_and_wsl_roots_are_discovered() -> None:
    roots = vscode_roots(_env())
    stable = PureWindowsPath(r"C:\Users\alice\AppData\Roaming\Code")
    insiders = PureWindowsPath(r"C:\Users\alice\AppData\Roaming\Code - Insiders")
    wsl_stable = PureWindowsPath(r"C:\Users\alice\vscode-remote-wsl\stable")
    wsl_insider = PureWindowsPath(r"C:\Users\alice\vscode-remote-wsl\insider")
    wsl_legacy = PureWindowsPath(
        r"C:\Users\alice\AppData\Local\Temp\vscode-remote-wsl"
    )
    assert stable in roots.data_roots
    assert insiders in roots.data_roots
    assert PureWindowsPath(r"C:\Users\alice\.vscode\extensions") in roots.extension_roots
    assert PureWindowsPath(r"C:\Users\alice\.vscode-insiders\extensions") in roots.extension_roots
    assert wsl_stable in roots.wsl_download_roots
    assert wsl_insider in roots.wsl_download_roots
    assert wsl_legacy in roots.wsl_download_roots
    scan_roots = application_scan_roots(_env())
    assert stable in scan_roots
    assert wsl_stable in scan_roots
    assert wsl_legacy in scan_roots


def test_vscode_portable_and_explicit_roots_are_first_class() -> None:
    env = {
        **_env(),
        "VSCODE_PORTABLE": r"E:\PortableCode\data",
        "VSCODE_USER_DATA_DIR": r"D:\VSCodeState",
        "VSCODE_EXTENSIONS_DIR": r"F:\VSCodeExtensions",
    }
    roots = vscode_roots(env)
    assert roots.data_roots[0] == PureWindowsPath(r"D:\VSCodeState")
    assert PureWindowsPath(r"E:\PortableCode\data\user-data") in roots.data_roots
    assert roots.extension_roots[0] == PureWindowsPath(r"F:\VSCodeExtensions")
    assert PureWindowsPath(r"E:\PortableCode\data\extensions") in roots.extension_roots
    assert roots.temp_roots == (PureWindowsPath(r"E:\PortableCode\data\tmp"),)


def test_vscode_running_cli_override_parser_handles_quoted_and_equals_forms() -> None:
    command = (
        r'"C:\Program Files\Microsoft VS Code\Code.exe" '
        r'--user-data-dir "D:\State With Space" --extensions-dir=E:\Exts'
    )
    assert _argument_value(command, "--user-data-dir") == r"D:\State With Space"
    assert _argument_value(command, "--extensions-dir") == r"E:\Exts"


def test_vscode_cache_is_tool_owned_and_process_guarded() -> None:
    path = r"C:\Users\alice\AppData\Roaming\Code\Code Cache\js\entry"
    decision = evaluate_application_path(
        path,
        logical_size=200 * _MIB,
        last_used=_NOW - timedelta(days=20),
        now=_NOW,
        process_running=False,
        environment=_env(),
    )
    assert decision is not None
    assert decision.rule.app_id == "vscode"
    assert decision.rule.owner is DecisionOwner.TOOL
    assert decision.action is PolicyAction.TOOL_DELETE

    running = evaluate_application_path(
        path,
        logical_size=200 * _MIB,
        last_used=_NOW - timedelta(days=20),
        now=_NOW,
        process_running=True,
        environment=_env(),
    )
    assert running is not None
    assert running.action is PolicyAction.TOOL_KEEP_IN_USE


def test_vscode_extended_electron_caches_are_tool_owned() -> None:
    paths = (
        (
            r"C:\Users\alice\AppData\Roaming\Code\CachedExtensions\index.json",
            "vscode-cached-extensions",
        ),
        (
            r"C:\Users\alice\AppData\Roaming\Code\GrShaderCache\data",
            "vscode-grshader-cache",
        ),
        (
            r"C:\Users\alice\AppData\Roaming\Code\ShaderCache\data",
            "vscode-shader-cache",
        ),
        (
            r"C:\Users\alice\AppData\Roaming\Code\Service Worker\ScriptCache\entry",
            "vscode-service-worker-script-cache",
        ),
    )
    for path, rule_id in paths:
        rule = match_application_rule(path, _env())
        assert rule is not None
        assert rule.owner is DecisionOwner.TOOL
        assert rule.rule_id == rule_id


def test_vscode_cache_storage_is_user_owned_persistent_data() -> None:
    path = r"C:\Users\alice\AppData\Roaming\Code\Service Worker\CacheStorage\entry"
    rule = match_application_rule(path, _env())
    assert rule is not None
    assert rule.rule_id == "vscode-site-cache-storage"
    assert rule.owner is DecisionOwner.USER
    decision = evaluate_application_path(
        path,
        logical_size=200 * _MIB,
        last_used=_NOW - timedelta(days=120),
        now=_NOW,
        process_running=False,
        environment=_env(),
    )
    assert decision is not None
    assert decision.action is PolicyAction.KEEP_PROTECTED
    assert whole_tree_application_rule(
        r"C:\Users\alice\AppData\Roaming\Code\Service Worker\CacheStorage",
        _env(),
    ) is None


def test_vscode_other_service_worker_state_is_not_blanket_deleted() -> None:
    rule = match_application_rule(
        r"C:\Users\alice\AppData\Roaming\Code\Service Worker\Database\000003.log",
        _env(),
    )
    assert rule is not None
    assert rule.rule_id == "vscode-service-worker-other-state"
    assert rule.owner is DecisionOwner.KEEP


def test_vscode_wsl_server_download_caches_are_tool_owned_and_guarded() -> None:
    paths = (
        r"C:\Users\alice\vscode-remote-wsl\stable\abc123\server.tar.gz",
        r"C:\Users\alice\vscode-remote-wsl\insider\def456\server.tar.gz",
        (
            r"C:\Users\alice\AppData\Local\Temp\vscode-remote-wsl"
            r"\old123\server.tar.gz"
        ),
    )
    for path in paths:
        decision = evaluate_application_path(
            path,
            logical_size=500 * _MIB,
            last_used=_NOW - timedelta(days=30),
            now=_NOW,
            process_running=False,
            environment=_env(),
        )
        assert decision is not None
        assert decision.rule.rule_id == "vscode-wsl-server-download-cache"
        assert decision.rule.owner is DecisionOwner.TOOL
        assert decision.action is PolicyAction.TOOL_DELETE

    running = evaluate_application_path(
        paths[0],
        logical_size=500 * _MIB,
        last_used=_NOW - timedelta(days=30),
        now=_NOW,
        process_running=True,
        environment=_env(),
    )
    assert running is not None
    assert running.action is PolicyAction.TOOL_KEEP_IN_USE


def test_vscode_workspace_history_and_recovery_are_not_generic_cache() -> None:
    paths = (
        (
            r"C:\Users\alice\AppData\Roaming\Code\User\workspaceStorage"
            r"\abc\chatSessions\thread.jsonl",
            DecisionOwner.USER,
        ),
        (
            r"C:\Users\alice\AppData\Roaming\Code\User\History\abc\entries.json",
            DecisionOwner.USER,
        ),
        (
            r"C:\Users\alice\AppData\Roaming\Code\Backups\window\untitled.txt",
            DecisionOwner.KEEP,
        ),
        (
            r"C:\Users\alice\AppData\Roaming\Code\User\globalStorage\state.vscdb",
            DecisionOwner.KEEP,
        ),
        (
            r"C:\Users\alice\.vscode\extensions\publisher.ext-1.2.3\extension.js",
            DecisionOwner.KEEP,
        ),
    )
    for path, owner in paths:
        decision = evaluate_application_path(
            path,
            logical_size=500 * _MIB,
            last_used=_NOW - timedelta(days=120),
            now=_NOW,
            process_running=False,
            environment=_env(),
        )
        assert decision is not None
        assert decision.rule.owner is owner
        assert decision.action is PolicyAction.KEEP_PROTECTED


def test_vscode_portable_temp_is_tool_owned_but_recent_data_is_kept() -> None:
    env = {**_env(), "VSCODE_PORTABLE": r"E:\PortableCode\data"}
    path = r"E:\PortableCode\data\tmp\session\scratch.bin"
    old = evaluate_application_path(
        path,
        logical_size=100 * _MIB,
        last_used=_NOW - timedelta(minutes=30),
        now=_NOW,
        process_running=False,
        environment=env,
    )
    recent = evaluate_application_path(
        path,
        logical_size=100 * _MIB,
        last_used=_NOW - timedelta(minutes=2),
        now=_NOW,
        process_running=False,
        environment=env,
    )
    assert old is not None and old.action is PolicyAction.TOOL_DELETE
    assert recent is not None and recent.action is PolicyAction.TOOL_KEEP_RECENT


def test_vscode_dynamic_whole_tree_cache_roots_are_exact() -> None:
    env = {**_env(), "VSCODE_USER_DATA_DIR": r"D:\VSCodeState"}
    cache = r"D:\VSCodeState\Cache"
    cache_rule = whole_tree_application_rule(cache, env)
    assert cache_rule is not None
    assert cache_rule.rule_id == "vscode-cache"
    assert cache_rule.owner is DecisionOwner.TOOL

    for wsl_root in (
        r"C:\Users\alice\vscode-remote-wsl\stable",
        r"C:\Users\alice\vscode-remote-wsl\insider",
        r"C:\Users\alice\AppData\Local\Temp\vscode-remote-wsl",
    ):
        wsl_rule = whole_tree_application_rule(wsl_root, env)
        assert wsl_rule is not None
        assert wsl_rule.rule_id == "vscode-wsl-server-download-cache"
    assert whole_tree_application_rule(
        r"C:\Users\alice\AppData\Roaming\Code",
        env,
    ) is None
    assert whole_tree_application_rule(
        r"C:\Users\alice\.vscode\extensions",
        env,
    ) is None


def test_vscode_process_guard_never_allows_workspace_or_backup_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = (
        r"C:\Users\alice\AppData\Roaming\Code\User\workspaceStorage"
        r"\abc\state.vscdb"
    )
    backup = r"C:\Users\alice\AppData\Roaming\Code\Backups\window\untitled.txt"
    assert not process_guard_allows(workspace, _env())
    assert not process_guard_allows(backup, _env())

    monkeypatch.setattr(
        "devclean.core.application_cleanup.vscode_process_running",
        lambda: True,
    )
    assert not process_guard_allows(
        r"C:\Users\alice\AppData\Roaming\Code\Cache\data_0",
        _env(),
    )
    assert not process_guard_allows(
        r"C:\Users\alice\vscode-remote-wsl\stable\abc\server.tar.gz",
        _env(),
    )


def test_catalog_upgrades_vscode_cache_and_wsl_download_roots(tmp_path: Path) -> None:
    appdata = tmp_path / "roaming"
    home = tmp_path / "home"
    temp = tmp_path / "temp"
    code = appdata / "Code"
    cache = code / "Cache"
    workspace = code / "User" / "workspaceStorage"
    wsl_profile_cache = home / "vscode-remote-wsl" / "stable"
    wsl_temp_cache = temp / "vscode-remote-wsl"
    cache.mkdir(parents=True)
    workspace.mkdir(parents=True)
    wsl_profile_cache.mkdir(parents=True)
    wsl_temp_cache.mkdir(parents=True)
    env = {
        "USERPROFILE": str(home),
        "APPDATA": str(appdata),
        "LOCALAPPDATA": str(tmp_path / "local"),
        "TEMP": str(temp),
    }

    discovered = discover_known_cleanup_roots(default_rules().scan, env)
    by_path = {os.path.normcase(str(root.path)): root for root in discovered}
    code_root = by_path[os.path.normcase(str(code))]
    cache_root = by_path[os.path.normcase(str(cache))]
    wsl_profile_root = by_path[os.path.normcase(str(wsl_profile_cache))]
    wsl_temp_root = by_path[os.path.normcase(str(wsl_temp_cache))]
    workspace_root = by_path.get(os.path.normcase(str(workspace)))

    assert code_root.policy is CleanupPolicy.REPORT_ONLY
    assert not code_root.delete_root_itself
    assert cache_root.policy is CleanupPolicy.VENDOR_MANAGED
    assert cache_root.delete_root_itself
    assert wsl_profile_root.policy is CleanupPolicy.VENDOR_MANAGED
    assert wsl_profile_root.delete_root_itself
    assert wsl_temp_root.policy is CleanupPolicy.VENDOR_MANAGED
    assert wsl_temp_root.delete_root_itself
    assert workspace_root is None or not workspace_root.delete_root_itself


def test_vscode_inventory_counts_workspace_chat_history_and_backups(tmp_path: Path) -> None:
    appdata = tmp_path / "roaming"
    home = tmp_path / "home"
    code = appdata / "Code"
    workspace = code / "User" / "workspaceStorage" / "abc"
    chats = workspace / "chatSessions"
    history = code / "User" / "History"
    backups = code / "Backups"
    global_storage = code / "User" / "globalStorage"
    for path in (chats, history, backups, global_storage):
        path.mkdir(parents=True, exist_ok=True)
    (chats / "thread.jsonl").write_bytes(b"c" * 300)
    (history / "entry").write_bytes(b"h" * 100)
    (backups / "untitled").write_bytes(b"b" * 200)
    (global_storage / "state.vscdb").write_bytes(b"g" * 400)
    env = {
        "USERPROFILE": str(home),
        "APPDATA": str(appdata),
        "LOCALAPPDATA": str(tmp_path / "local"),
    }

    inventory = inventory_vscode_storage(env)
    entries = {entry.label: entry for entry in inventory.entries if entry.exists}
    assert entries["Chat session bodies inside workspaceStorage"].logical_bytes == 300
    assert entries["Local file history"].logical_bytes == 100
    assert entries["Unsaved editor / hot-exit recovery"].logical_bytes == 200
    assert entries["Extension/global persistent state"].logical_bytes == 400
