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
from devclean.core.windsurf_cleanup import windsurf_roots

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


def test_windsurf_roots_cover_editor_cascade_plans_extensions_and_system() -> None:
    roots = windsurf_roots(_env())
    stable = PureWindowsPath(r"C:\Users\alice\AppData\Roaming\Windsurf")
    next_root = PureWindowsPath(r"C:\Users\alice\AppData\Roaming\Windsurf - Next")
    config = PureWindowsPath(r"C:\Users\alice\.codeium\windsurf")
    plans = PureWindowsPath(r"C:\Users\alice\.windsurf\plans")
    extensions = PureWindowsPath(r"C:\Users\alice\.windsurf\extensions")
    system = PureWindowsPath(r"C:\ProgramData\Windsurf")

    assert stable in roots.data_roots
    assert next_root in roots.data_roots
    assert roots.config_roots == (config,)
    assert roots.plan_roots == (plans,)
    assert roots.extension_roots == (extensions,)
    assert roots.system_roots == (system,)
    scan_roots = application_scan_roots(_env())
    assert stable in scan_roots
    assert config in scan_roots
    assert plans in scan_roots


def test_windsurf_electron_caches_are_tool_owned_and_process_guarded() -> None:
    paths = {
        r"C:\Users\alice\AppData\Roaming\Windsurf\Cache\data_0": "windsurf-cache",
        (
            r"C:\Users\alice\AppData\Roaming\Windsurf"
            r"\Code Cache\js\entry"
        ): "windsurf-code-cache",
        (
            r"C:\Users\alice\AppData\Roaming\Windsurf"
            r"\GPUCache\data"
        ): "windsurf-gpu-cache",
        (
            r"C:\Users\alice\AppData\Roaming\Windsurf"
            r"\CachedExtensions\index.json"
        ): "windsurf-cached-extensions",
        (
            r"C:\Users\alice\AppData\Roaming\Windsurf"
            r"\CachedExtensionVSIXs\ext.vsix"
        ): "windsurf-extension-vsix-cache",
        (
            r"C:\Users\alice\AppData\Roaming\Windsurf"
            r"\Service Worker\ScriptCache\entry"
        ): "windsurf-service-worker-script-cache",
        (
            r"C:\Users\alice\AppData\Roaming\Windsurf"
            r"\Crashpad\reports\crash.dmp"
        ): "windsurf-crashpad-reports",
    }
    for path, rule_id in paths.items():
        rule = match_application_rule(path, _env())
        assert rule is not None
        assert rule.owner is DecisionOwner.TOOL
        assert rule.rule_id == rule_id

    decision = evaluate_application_path(
        r"C:\Users\alice\AppData\Roaming\Windsurf\Cache\data_0",
        logical_size=200 * _MIB,
        last_used=_NOW - timedelta(days=30),
        now=_NOW,
        process_running=False,
        environment=_env(),
    )
    assert decision is not None
    assert decision.action is PolicyAction.TOOL_DELETE

    running = evaluate_application_path(
        r"C:\Users\alice\AppData\Roaming\Windsurf\Cache\data_0",
        logical_size=200 * _MIB,
        last_used=_NOW - timedelta(days=30),
        now=_NOW,
        process_running=True,
        environment=_env(),
    )
    assert running is not None
    assert running.action is PolicyAction.TOOL_KEEP_IN_USE


def test_windsurf_cache_storage_is_user_owned_persistent_data() -> None:
    path = (
        r"C:\Users\alice\AppData\Roaming\Windsurf"
        r"\Service Worker\CacheStorage\origin\entry"
    )
    rule = match_application_rule(path, _env())
    assert rule is not None
    assert rule.rule_id == "windsurf-site-cache-storage"
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
    assert (
        whole_tree_application_rule(
            r"C:\Users\alice\AppData\Roaming\Windsurf\Service Worker\CacheStorage",
            _env(),
        )
        is None
    )


def test_windsurf_cascade_memories_and_plans_are_user_owned() -> None:
    paths = (
        r"C:\Users\alice\.codeium\windsurf\cascade\conversation.db",
        r"C:\Users\alice\.codeium\windsurf\memories\workspace\memory.md",
        r"C:\Users\alice\.windsurf\plans\migration-plan.md",
        (
            r"C:\Users\alice\AppData\Roaming\Windsurf\User\workspaceStorage"
            r"\abc\state.vscdb"
        ),
        r"C:\Users\alice\AppData\Roaming\Windsurf\User\History\abc\entries.json",
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


def test_windsurf_authored_config_extensions_and_system_policy_are_kept() -> None:
    paths = {
        (
            r"C:\Users\alice\.codeium\windsurf\memories"
            r"\global_rules.md"
        ): "windsurf-global-rules",
        r"C:\Users\alice\.codeium\windsurf\mcp_config.json": "windsurf-mcp-config",
        r"C:\Users\alice\.codeium\windsurf\hooks.json": "windsurf-hooks",
        (
            r"C:\Users\alice\.codeium\windsurf\global_workflows"
            r"\release.md"
        ): "windsurf-global-workflows",
        (
            r"C:\Users\alice\.codeium\windsurf\skills"
            r"\deploy\SKILL.md"
        ): "windsurf-global-skills",
        (
            r"C:\Users\alice\.windsurf\extensions"
            r"\publisher.ext\extension.js"
        ): "windsurf-installed-extensions",
        r"C:\ProgramData\Windsurf\rules\company.md": "windsurf-system-policy",
        (
            r"C:\Users\alice\AppData\Roaming\Windsurf\Backups"
            r"\window\untitled.txt"
        ): "windsurf-hot-exit-backups",
    }
    for path, rule_id in paths.items():
        rule = match_application_rule(path, _env())
        assert rule is not None
        assert rule.owner is DecisionOwner.KEEP
        assert rule.rule_id == rule_id
        assert not process_guard_allows(path, _env())


def test_windsurf_other_service_worker_state_is_not_blanket_deleted() -> None:
    rule = match_application_rule(
        r"C:\Users\alice\AppData\Roaming\Windsurf\Service Worker\Database\000003.log",
        _env(),
    )
    assert rule is not None
    assert rule.rule_id == "windsurf-service-worker-other-state"
    assert rule.owner is DecisionOwner.KEEP


def test_windsurf_whole_tree_delete_is_exact_cache_only() -> None:
    cache = r"C:\Users\alice\AppData\Roaming\Windsurf\Cache"
    rule = whole_tree_application_rule(cache, _env())
    assert rule is not None
    assert rule.owner is DecisionOwner.TOOL
    assert (
        whole_tree_application_rule(
            r"C:\Users\alice\AppData\Roaming\Windsurf",
            _env(),
        )
        is None
    )
    assert (
        whole_tree_application_rule(
            r"C:\Users\alice\.codeium\windsurf",
            _env(),
        )
        is None
    )
    assert (
        whole_tree_application_rule(
            r"C:\Users\alice\.windsurf\plans",
            _env(),
        )
        is None
    )


def test_catalog_upgrades_only_audited_windsurf_cache_subtrees(tmp_path: Path) -> None:
    appdata = tmp_path / "roaming"
    home = tmp_path / "home"
    data_root = appdata / "Windsurf"
    cache = data_root / "Cache"
    cascade = home / ".codeium" / "windsurf" / "cascade"
    cache.mkdir(parents=True)
    cascade.mkdir(parents=True)
    env = {
        "USERPROFILE": str(home),
        "APPDATA": str(appdata),
        "LOCALAPPDATA": str(tmp_path / "local"),
        "PROGRAMDATA": str(tmp_path / "programdata"),
        "TEMP": str(tmp_path / "temp"),
    }

    discovered = discover_known_cleanup_roots(default_rules().scan, env)
    by_path = {os.path.normcase(str(root.path)): root for root in discovered}
    editor = by_path[os.path.normcase(str(data_root))]
    cache_root = by_path[os.path.normcase(str(cache))]
    config = by_path[os.path.normcase(str(home / ".codeium" / "windsurf"))]

    assert editor.policy is CleanupPolicy.REPORT_ONLY
    assert not editor.delete_root_itself
    assert cache_root.policy is CleanupPolicy.VENDOR_MANAGED
    assert cache_root.delete_root_itself
    assert config.policy is CleanupPolicy.REPORT_ONLY
    assert not config.delete_root_itself


def test_windsurf_process_guard_blocks_running_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "devclean.core.application_cleanup.windsurf_process_running",
        lambda: True,
    )
    assert not process_guard_allows(
        r"C:\Users\alice\AppData\Roaming\Windsurf\Cache\data_0",
        _env(),
    )
