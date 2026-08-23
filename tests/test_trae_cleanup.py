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
from devclean.core.trae_cleanup import trae_roots
from devclean.core.trae_maintenance import inventory_trae_storage
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


def test_trae_candidate_roots_cover_global_cn_local_and_home() -> None:
    roots = trae_roots(_env())
    assert PureWindowsPath(r"C:\Users\alice\AppData\Roaming\Trae") in roots.data_roots
    assert PureWindowsPath(r"C:\Users\alice\AppData\Roaming\Trae CN") in roots.data_roots
    assert PureWindowsPath(r"C:\Users\alice\AppData\Local\Trae") in roots.data_roots
    assert PureWindowsPath(r"C:\Users\alice\.trae") in roots.home_roots
    assert PureWindowsPath(r"C:\Users\alice\.trae\extensions") in roots.extension_roots
    assert PureWindowsPath(r"C:\Users\alice\AppData\Roaming\Trae") in application_scan_roots(_env())


def test_trae_explicit_roots_are_supported() -> None:
    env = {
        **_env(),
        "TRAE_USER_DATA_DIR": r"D:\TraeState",
        "TRAE_EXTENSIONS_DIR": r"E:\TraeExtensions",
    }
    roots = trae_roots(env)
    assert roots.data_roots[0] == PureWindowsPath(r"D:\TraeState")
    assert roots.extension_roots[0] == PureWindowsPath(r"E:\TraeExtensions")


def test_trae_known_electron_cache_is_tool_owned_and_process_guarded() -> None:
    path = r"C:\Users\alice\AppData\Roaming\Trae\Code Cache\js\entry"
    decision = evaluate_application_path(
        path,
        logical_size=200 * _MIB,
        last_used=_NOW - timedelta(days=20),
        now=_NOW,
        process_running=False,
        environment=_env(),
    )
    assert decision is not None
    assert decision.rule.app_id == "trae"
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


def test_trae_audited_cache_safety_is_not_revoked_by_heuristics() -> None:
    cases = (
        (r"C:\Users\alice\AppData\Roaming\Trae\Cache\data_0", 1, _NOW),
        (r"C:\Users\alice\AppData\Roaming\Trae\CachedData\entry", 1, None),
        (r"C:\Users\alice\AppData\Roaming\Trae\GPUCache\data_0", 1, _NOW),
        (
            r"C:\Users\alice\AppData\Roaming\Trae\CachedExtensionVSIXs\ext.vsix",
            1,
            _NOW,
        ),
    )
    for path, size, last_used in cases:
        decision = evaluate_application_path(
            path,
            logical_size=size,
            last_used=last_used,
            now=_NOW,
            process_running=False,
            environment=_env(),
        )
        assert decision is not None
        assert decision.rule.owner is DecisionOwner.TOOL
        assert decision.action is PolicyAction.TOOL_DELETE


def test_trae_logs_are_user_review_and_crashpad_is_protected() -> None:
    log = r"C:\Users\alice\AppData\Roaming\Trae CN\logs\20260816\window.log"
    reports = r"C:\Users\alice\AppData\Roaming\Trae\Crashpad\reports\crash.dmp"
    pending = r"C:\Users\alice\AppData\Roaming\Trae\Crashpad\pending\crash.dmp"

    log_rule = match_application_rule(log, _env())
    assert log_rule is not None
    assert log_rule.rule_id == "trae-logs"
    assert log_rule.owner is DecisionOwner.USER
    assert log_rule.requires_process_closed

    log_decision = evaluate_application_path(
        log,
        logical_size=8 * 1024**3,
        last_used=_NOW - timedelta(days=3650),
        now=_NOW,
        process_running=False,
        environment=_env(),
    )
    assert log_decision is not None
    assert log_decision.action is PolicyAction.USER_DECISION

    for path, rule_id in (
        (reports, "trae-crashpad-reports"),
        (pending, "trae-crashpad-pending"),
    ):
        rule = match_application_rule(path, _env())
        assert rule is not None
        assert rule.rule_id == rule_id
        assert rule.owner is DecisionOwner.KEEP
        decision = evaluate_application_path(
            path,
            logical_size=8 * 1024**3,
            last_used=_NOW - timedelta(days=3650),
            now=_NOW,
            process_running=False,
            environment=_env(),
        )
        assert decision is not None
        assert decision.action is PolicyAction.KEEP_PROTECTED

    assert whole_tree_application_rule(
        r"C:\Users\alice\AppData\Roaming\Trae CN\logs", _env()
    ) is None
    assert whole_tree_application_rule(
        r"C:\Users\alice\AppData\Roaming\Trae\Crashpad\reports", _env()
    ) is None
    assert whole_tree_application_rule(
        r"C:\Users\alice\AppData\Roaming\Trae\Crashpad\pending", _env()
    ) is None


def test_trae_user_state_is_reviewable_and_unknown_state_is_protected() -> None:
    paths = (
        (
            r"C:\Users\alice\AppData\Roaming\Trae\User\workspaceStorage"
            r"\abc\state.vscdb",
            DecisionOwner.USER,
        ),
        (
            r"C:\Users\alice\AppData\Roaming\Trae\User\History\abc\entries.json",
            DecisionOwner.USER,
        ),
        (
            r"C:\Users\alice\AppData\Roaming\Trae\User\globalStorage\state.vscdb",
            DecisionOwner.KEEP,
        ),
        (
            r"C:\Users\alice\AppData\Roaming\Trae\Backups\window\untitled.txt",
            DecisionOwner.KEEP,
        ),
        (
            r"C:\Users\alice\.trae\extensions\publisher.ext\extension.js",
            DecisionOwner.KEEP,
        ),
        (
            r"C:\Users\alice\AppData\Roaming\Trae\UnknownAIState\index.db",
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
        expected = (
            PolicyAction.USER_DECISION
            if owner is DecisionOwner.USER
            else PolicyAction.KEEP_PROTECTED
        )
        assert decision.action is expected


def test_trae_dynamic_whole_tree_cache_root_is_exact_only() -> None:
    env = {**_env(), "TRAE_USER_DATA_DIR": r"D:\TraeState"}
    rule = whole_tree_application_rule(r"D:\TraeState\Cache", env)
    assert rule is not None
    assert rule.rule_id == "trae-cache"
    assert rule.owner is DecisionOwner.TOOL
    assert whole_tree_application_rule(r"D:\TraeState", env) is None
    assert whole_tree_application_rule(r"D:\TraeState\User", env) is None
    assert whole_tree_application_rule(r"D:\TraeState\logs", env) is None
    assert whole_tree_application_rule(r"D:\TraeState\Crashpad\reports", env) is None


def test_trae_process_guard_allows_user_choice_and_blocks_live_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = (
        r"C:\Users\alice\AppData\Roaming\Trae\User\workspaceStorage"
        r"\abc\state.vscdb"
    )
    assert process_guard_allows(workspace, _env())

    monkeypatch.setattr(
        "devclean.core.application_cleanup.trae_process_running",
        lambda: True,
    )
    cache = r"C:\Users\alice\AppData\Roaming\Trae\Cache\data_0"
    logs = r"C:\Users\alice\AppData\Roaming\Trae CN\logs\today.log"
    crash = r"C:\Users\alice\AppData\Roaming\Trae\Crashpad\reports\crash.dmp"
    assert not process_guard_allows(cache, _env())
    assert not process_guard_allows(logs, _env())
    assert not process_guard_allows(crash, _env())


def test_catalog_upgrades_trae_cache_without_deleting_data_root(tmp_path: Path) -> None:
    appdata = tmp_path / "roaming"
    home = tmp_path / "home"
    root = appdata / "Trae"
    cache = root / "Cache"
    user = root / "User"
    logs = root / "logs"
    crash = root / "Crashpad" / "reports"
    cache.mkdir(parents=True)
    user.mkdir(parents=True)
    logs.mkdir(parents=True)
    crash.mkdir(parents=True)
    env = {
        "USERPROFILE": str(home),
        "APPDATA": str(appdata),
        "LOCALAPPDATA": str(tmp_path / "local"),
        "TEMP": str(tmp_path / "temp"),
    }

    discovered = discover_known_cleanup_roots(default_rules().scan, env)
    by_path = {os.path.normcase(str(item.path)): item for item in discovered}
    data_root = by_path[os.path.normcase(str(root))]
    cache_root = by_path[os.path.normcase(str(cache))]
    user_root = by_path.get(os.path.normcase(str(user)))
    log_root = by_path[os.path.normcase(str(logs))]
    crash_root = by_path[os.path.normcase(str(crash))]

    assert data_root.policy is CleanupPolicy.REPORT_ONLY
    assert not data_root.delete_root_itself
    assert cache_root.policy is CleanupPolicy.VENDOR_MANAGED
    assert cache_root.delete_root_itself
    assert user_root is None or not user_root.delete_root_itself
    assert log_root.policy is CleanupPolicy.REPORT_ONLY
    assert not log_root.delete_root_itself
    assert crash_root.policy is CleanupPolicy.REPORT_ONLY
    assert not crash_root.delete_root_itself


def test_trae_inventory_counts_workspace_global_history_and_backups(tmp_path: Path) -> None:
    appdata = tmp_path / "roaming"
    home = tmp_path / "home"
    root = appdata / "Trae"
    workspace = root / "User" / "workspaceStorage"
    history = root / "User" / "History"
    global_storage = root / "User" / "globalStorage"
    backups = root / "Backups"
    for path in (workspace, history, global_storage, backups):
        path.mkdir(parents=True, exist_ok=True)
    (workspace / "state.vscdb").write_bytes(b"w" * 100)
    (history / "entry").write_bytes(b"h" * 200)
    (global_storage / "state.vscdb").write_bytes(b"g" * 300)
    (backups / "untitled").write_bytes(b"b" * 400)
    env = {
        "USERPROFILE": str(home),
        "APPDATA": str(appdata),
        "LOCALAPPDATA": str(tmp_path / "local"),
    }

    inventory = inventory_trae_storage(env)
    visible = {entry.label: entry.logical_bytes for entry in inventory.entries if entry.exists}
    assert visible["Workspace-local state / possible AI session metadata"] == 100
    assert visible["Local file history"] == 200
    assert visible["Global extension / AI persistent state"] == 300
    assert visible["Unsaved editor / recovery data"] == 400
