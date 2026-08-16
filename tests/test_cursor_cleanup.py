from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path, PureWindowsPath

import pytest

from devclean.core.application_cleanup import (
    DecisionOwner,
    PolicyAction,
    application_roots,
    application_scan_roots,
    evaluate_application_path,
    match_application_rule,
    process_guard_allows,
    whole_tree_application_rule,
)
from devclean.core.cleanup_catalog import CleanupPolicy, discover_known_cleanup_roots
from devclean.core.cursor_cleanup import cursor_roots
from devclean.core.cursor_maintenance import inventory_cursor_storage
from devclean.core.user_rules import default_rules

_NOW = datetime(2026, 8, 16, tzinfo=UTC)
_MIB = 1024**2


def _env() -> dict[str, str]:
    return {
        "USERPROFILE": r"C:\Users\alice",
        "APPDATA": r"C:\Users\alice\AppData\Roaming",
        "LOCALAPPDATA": r"C:\Users\alice\AppData\Local",
        "PROGRAMDATA": r"C:\ProgramData",
        "TEMP": r"C:\Users\alice\AppData\Local\Temp",
    }


def test_cursor_roots_cover_roaming_local_system_and_home() -> None:
    roots = cursor_roots(_env())
    assert roots.roaming == PureWindowsPath(r"C:\Users\alice\AppData\Roaming\Cursor")
    assert roots.local == PureWindowsPath(r"C:\Users\alice\AppData\Local\Cursor")
    assert roots.program_data == PureWindowsPath(r"C:\ProgramData\Cursor")
    assert roots.home == PureWindowsPath(r"C:\Users\alice\.cursor")

    root_map = {root.key: root.path for root in application_roots(_env())}
    assert root_map["CURSOR_ROAMING"] == roots.roaming
    assert root_map["CURSOR_LOCAL"] == roots.local
    assert roots.roaming in application_scan_roots(_env())
    assert roots.home in application_scan_roots(_env())


def test_cursor_known_cache_is_tool_owned_and_process_guarded() -> None:
    path = r"C:\Users\alice\AppData\Roaming\Cursor\Code Cache\js\entry"
    decision = evaluate_application_path(
        path,
        logical_size=100 * _MIB,
        last_used=_NOW - timedelta(days=20),
        now=_NOW,
        process_running=False,
        environment=_env(),
    )
    assert decision is not None
    assert decision.rule.app_id == "cursor"
    assert decision.rule.owner is DecisionOwner.TOOL
    assert decision.action is PolicyAction.TOOL_DELETE

    running = evaluate_application_path(
        path,
        logical_size=100 * _MIB,
        last_used=_NOW - timedelta(days=20),
        now=_NOW,
        process_running=True,
        environment=_env(),
    )
    assert running is not None
    assert running.action is PolicyAction.TOOL_KEEP_IN_USE


def test_cursor_graphics_crash_and_extension_caches_are_tool_owned() -> None:
    paths = {
        r"C:\Users\alice\AppData\Roaming\Cursor\DawnCache\index": "cursor-roaming-dawn-cache",
        r"C:\Users\alice\AppData\Roaming\Cursor\GrShaderCache\data": "cursor-roaming-grshader-cache",
        r"C:\Users\alice\AppData\Roaming\Cursor\ShaderCache\data": "cursor-roaming-shader-cache",
        r"C:\Users\alice\AppData\Roaming\Cursor\CachedExtensions\index.json": "cursor-roaming-cached-extensions",
        r"C:\Users\alice\AppData\Roaming\Cursor\CachedExtensionVSIXs\ext.vsix": "cursor-roaming-cached-extension-vsix",
        r"C:\Users\alice\AppData\Roaming\Cursor\Crashpad\reports\crash.dmp": "cursor-roaming-crashpad-reports",
        r"C:\Users\alice\AppData\Roaming\Cursor\Crashpad\pending\crash.dmp": "cursor-roaming-crashpad-pending",
    }
    for path, rule_id in paths.items():
        rule = match_application_rule(path, _env())
        assert rule is not None
        assert rule.owner is DecisionOwner.TOOL
        assert rule.rule_id == rule_id


def test_cursor_workspace_and_chat_databases_are_user_owned() -> None:
    paths = (
        r"C:\Users\alice\AppData\Roaming\Cursor\User\workspaceStorage\abc\state.vscdb",
        r"C:\Users\alice\AppData\Roaming\Cursor\User\globalStorage\state.vscdb",
        r"C:\Users\alice\AppData\Roaming\Cursor\User\globalStorage\state.vscdb.backup",
        r"C:\Users\alice\AppData\Roaming\Cursor\User\globalStorage\state.vscdb.corrupted.123",
        r"C:\Users\alice\AppData\Roaming\Cursor\User\History\abc\entries.json",
        r"C:\Users\alice\.cursor\projects\repo\agent-transcripts\thread.txt",
        r"C:\Users\alice\.cursor\chats\workspace\chat-id\transcript.jsonl",
    )
    for path in paths:
        decision = evaluate_application_path(
            path,
            logical_size=500 * _MIB,
            last_used=_NOW - timedelta(days=120),
            now=_NOW,
            process_running=False,
            environment=_env(),
        )
        assert decision is not None
        assert decision.rule.owner is DecisionOwner.USER
        assert decision.action is PolicyAction.KEEP_PROTECTED


def test_cursor_recovery_copy_rule_outranks_global_storage_keep() -> None:
    recovery = match_application_rule(
        r"C:\Users\alice\AppData\Roaming\Cursor\User\globalStorage\state.vscdb.corrupted.1767892516529",
        _env(),
    )
    backup = match_application_rule(
        r"C:\Users\alice\AppData\Roaming\Cursor\User\globalStorage\state.vscdb.backup",
        _env(),
    )
    assert recovery is not None
    assert recovery.rule_id == "cursor-global-chat-recovery-files"
    assert recovery.owner is DecisionOwner.USER
    assert backup is not None
    assert backup.rule_id == "cursor-global-chat-backup"
    assert backup.owner is DecisionOwner.USER


def test_cursor_checkpoints_outrank_broad_global_storage_keep() -> None:
    path = (
        r"C:\Users\alice\AppData\Roaming\Cursor\User\globalStorage"
        r"\anysphere.cursor-commits\checkpoints\checkpoint.bin"
    )
    rule = match_application_rule(path, _env())
    assert rule is not None
    assert rule.rule_id == "cursor-commit-checkpoints"
    assert rule.owner is DecisionOwner.USER


def test_cursor_unknown_state_unsaved_backups_and_extensions_are_kept() -> None:
    unknown = match_application_rule(
        r"C:\Users\alice\AppData\Roaming\Cursor\Network\Cookies",
        _env(),
    )
    unsaved = match_application_rule(
        r"C:\Users\alice\AppData\Roaming\Cursor\Backups\window\untitled.txt",
        _env(),
    )
    extension = match_application_rule(
        r"C:\Users\alice\.cursor\extensions\publisher.ext-1.2.3\extension.js",
        _env(),
    )
    assert unknown is not None and unknown.owner is DecisionOwner.KEEP
    assert unsaved is not None and unsaved.owner is DecisionOwner.KEEP
    assert unsaved.rule_id == "cursor-hot-exit-backups"
    assert extension is not None and extension.owner is DecisionOwner.KEEP


def test_cursor_process_guard_never_allows_user_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = r"C:\Users\alice\AppData\Roaming\Cursor\User\globalStorage\state.vscdb"
    recovery = (
        r"C:\Users\alice\AppData\Roaming\Cursor\User\globalStorage"
        r"\state.vscdb.corrupted.123"
    )
    assert not process_guard_allows(db, _env())
    assert not process_guard_allows(recovery, _env())

    monkeypatch.setattr(
        "devclean.core.application_cleanup.cursor_process_running",
        lambda: True,
    )
    cache = r"C:\Users\alice\AppData\Roaming\Cursor\Cache\data_0"
    assert not process_guard_allows(cache, _env())


def test_cursor_whole_tree_delete_is_exact_cache_only() -> None:
    cache = r"C:\Users\alice\AppData\Roaming\Cursor\Cache"
    rule = whole_tree_application_rule(cache, _env())
    assert rule is not None
    assert rule.owner is DecisionOwner.TOOL
    assert whole_tree_application_rule(
        r"C:\Users\alice\AppData\Roaming\Cursor",
        _env(),
    ) is None
    assert whole_tree_application_rule(
        r"C:\Users\alice\AppData\Roaming\Cursor\User",
        _env(),
    ) is None


def test_catalog_exposes_only_audited_cursor_cache_subtrees(tmp_path: Path) -> None:
    roaming = tmp_path / "roaming"
    local = tmp_path / "local"
    home = tmp_path / "home"
    cache = roaming / "Cursor" / "Cache"
    workspace = roaming / "Cursor" / "User" / "workspaceStorage"
    cache.mkdir(parents=True)
    workspace.mkdir(parents=True)
    (local / "Cursor").mkdir(parents=True)
    (home / ".cursor").mkdir(parents=True)
    env = {
        "USERPROFILE": str(home),
        "APPDATA": str(roaming),
        "LOCALAPPDATA": str(local),
        "PROGRAMDATA": str(tmp_path / "programdata"),
        "TEMP": str(tmp_path / "temp"),
    }

    discovered = discover_known_cleanup_roots(default_rules().scan, env)
    by_path = {os.path.normcase(str(root.path)): root for root in discovered}
    cursor_root = by_path[os.path.normcase(str(roaming / "Cursor"))]
    cache_root = by_path[os.path.normcase(str(cache))]
    workspace_root = by_path.get(os.path.normcase(str(workspace)))

    assert cursor_root.policy is CleanupPolicy.REPORT_ONLY
    assert not cursor_root.delete_root_itself
    assert cache_root.policy is CleanupPolicy.VENDOR_MANAGED
    assert cache_root.delete_root_itself
    assert workspace_root is None or not workspace_root.delete_root_itself


def test_cursor_inventory_separates_live_backup_recovery_and_transcripts(
    tmp_path: Path,
) -> None:
    roaming = tmp_path / "roaming"
    home = tmp_path / "home"
    global_storage = roaming / "Cursor" / "User" / "globalStorage"
    global_storage.mkdir(parents=True)
    (global_storage / "state.vscdb").write_bytes(b"x" * 200)
    (global_storage / "state.vscdb.backup").write_bytes(b"b" * 300)
    (global_storage / "state.vscdb.corrupted.123").write_bytes(b"c" * 400)
    transcripts = home / ".cursor" / "projects" / "repo" / "agent-transcripts"
    transcripts.mkdir(parents=True)
    (transcripts / "thread.txt").write_bytes(b"y" * 500)
    env = {
        "USERPROFILE": str(home),
        "APPDATA": str(roaming),
        "LOCALAPPDATA": str(tmp_path / "local"),
        "PROGRAMDATA": str(tmp_path / "programdata"),
    }

    inventory = inventory_cursor_storage(env)
    chat_db = inventory.by_key("chat_db")
    backup = inventory.by_key("chat_db_backup")
    recovery = inventory.by_key("chat_db_recovery")
    projects = inventory.by_key("agent_projects")
    assert chat_db is not None and chat_db.logical_bytes == 200 and chat_db.user_data
    assert backup is not None and backup.logical_bytes == 300 and backup.user_data
    assert recovery is not None and recovery.logical_bytes == 400 and recovery.user_data
    assert projects is not None and projects.logical_bytes == 500 and projects.user_data
