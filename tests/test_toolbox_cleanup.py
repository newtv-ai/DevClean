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
from devclean.core.toolbox_cleanup import toolbox_root
from devclean.core.user_rules import default_rules

_NOW = datetime(2026, 8, 17, tzinfo=UTC)
_MIB = 1024**2
_GIB = 1024**3


def _layout(tmp_path: Path) -> tuple[dict[str, str], Path]:
    root = tmp_path / "Local" / "JetBrains" / "Toolbox"
    root.mkdir(parents=True)
    env = {
        "USERPROFILE": str(tmp_path / "home"),
        "APPDATA": str(tmp_path / "Roaming"),
        "LOCALAPPDATA": str(tmp_path / "Local"),
        "TEMP": str(tmp_path / "Temp"),
    }
    return env, root


def test_toolbox_default_windows_root_is_discovered(tmp_path: Path) -> None:
    env, root = _layout(tmp_path)

    assert toolbox_root(env) == PureWindowsPath(str(root))
    assert PureWindowsPath(str(root)) in application_scan_roots(env)


def test_toolbox_mixed_root_delegates_only_documented_removable_subtrees(
    tmp_path: Path,
) -> None:
    env, root = _layout(tmp_path)
    cases = {
        root / "cache" / "download" / "idea.zip": (
            "toolbox-download-cache",
            DecisionOwner.TOOL,
        ),
        root / "cache" / "temp" / "IU-262" / "product-info.json": (
            "toolbox-install-temp",
            DecisionOwner.TOOL,
        ),
        root / "logs" / "toolbox.latest.log": (
            "toolbox-product-logs",
            DecisionOwner.TOOL,
        ),
        root / ".settings.json": ("toolbox-settings-state", DecisionOwner.KEEP),
        root / "environment.json": ("toolbox-environment-state", DecisionOwner.KEEP),
        root / "bin" / "jetbrains-toolbox.exe": (
            "toolbox-installation-binaries",
            DecisionOwner.KEEP,
        ),
        root / "internal-tools" / "JetBrainsClient" / "client.exe": (
            "toolbox-internal-tools",
            DecisionOwner.KEEP,
        ),
        root / "cache" / "ports" / "471626873.port": (
            "toolbox-ipc-port-state",
            DecisionOwner.KEEP,
        ),
        root / "cache" / "plugins" / "com.example" / "plugin.jar": (
            "toolbox-plugin-state",
            DecisionOwner.KEEP,
        ),
        root / "enterprise-config.json": ("toolbox-root-state", DecisionOwner.KEEP),
        root / "cache" / "future-state" / "state.db": (
            "toolbox-root-state",
            DecisionOwner.KEEP,
        ),
    }

    for path, (rule_id, owner) in cases.items():
        rule = match_application_rule(path, env)
        assert rule is not None
        assert rule.rule_id == rule_id
        assert rule.owner is owner


def test_toolbox_download_cache_preserves_vendor_three_day_retention_floor(
    tmp_path: Path,
) -> None:
    env, root = _layout(tmp_path)
    download = root / "cache" / "download"

    recent_huge = evaluate_application_path(
        download,
        logical_size=8 * _GIB,
        last_used=_NOW - timedelta(days=2),
        now=_NOW,
        process_running=False,
        environment=env,
    )
    stale = evaluate_application_path(
        download,
        logical_size=512 * _MIB,
        last_used=_NOW - timedelta(days=4),
        now=_NOW,
        process_running=False,
        environment=env,
    )
    small = evaluate_application_path(
        download,
        logical_size=4 * _MIB,
        last_used=_NOW - timedelta(days=30),
        now=_NOW,
        process_running=False,
        environment=env,
    )
    running = evaluate_application_path(
        download,
        logical_size=512 * _MIB,
        last_used=_NOW - timedelta(days=30),
        now=_NOW,
        process_running=True,
        environment=env,
    )

    assert recent_huge is not None
    assert recent_huge.effective_idle_days == 3
    assert recent_huge.action is PolicyAction.TOOL_KEEP_RECENT
    assert stale is not None and stale.action is PolicyAction.TOOL_DELETE
    assert small is not None and small.action is PolicyAction.TOOL_KEEP_LOW_BENEFIT
    assert running is not None and running.action is PolicyAction.TOOL_KEEP_IN_USE


def test_toolbox_temp_uses_conservative_seven_day_idle_floor(tmp_path: Path) -> None:
    env, root = _layout(tmp_path)
    temp = root / "cache" / "temp"

    recent = evaluate_application_path(
        temp,
        logical_size=512 * _MIB,
        last_used=_NOW - timedelta(days=6),
        now=_NOW,
        process_running=False,
        environment=env,
    )
    stale = evaluate_application_path(
        temp,
        logical_size=512 * _MIB,
        last_used=_NOW - timedelta(days=8),
        now=_NOW,
        process_running=False,
        environment=env,
    )

    assert recent is not None and recent.action is PolicyAction.TOOL_KEEP_RECENT
    assert stale is not None and stale.action is PolicyAction.TOOL_DELETE


def test_toolbox_keep_state_is_projected_to_generic_protection(tmp_path: Path) -> None:
    env, root = _layout(tmp_path)
    for path in (
        root / ".settings.json",
        root / "environment.json",
        root / "bin" / "jetbrains-toolbox.exe",
        root / "cache" / "ports" / "1.port",
        root / "cache" / "plugins" / "plugin.jar",
        root / "unknown" / "state.db",
    ):
        decision = evaluate_application_path(
            path,
            logical_size=512 * _MIB,
            last_used=_NOW - timedelta(days=365),
            now=_NOW,
            process_running=False,
            environment=env,
        )
        assert decision is not None
        assert decision.action is PolicyAction.KEEP_PROTECTED


def test_toolbox_whole_tree_authority_is_exact_and_catalogued(tmp_path: Path) -> None:
    env, root = _layout(tmp_path)
    download = root / "cache" / "download"
    temp = root / "cache" / "temp"
    logs = root / "logs"
    ports = root / "cache" / "ports"
    plugins = root / "cache" / "plugins"
    for path in (download, temp, logs, ports, plugins, root / "bin"):
        path.mkdir(parents=True, exist_ok=True)

    assert whole_tree_application_rule(download, env) is not None
    assert whole_tree_application_rule(temp, env) is not None
    assert whole_tree_application_rule(logs, env) is not None
    assert whole_tree_application_rule(root, env) is None
    assert whole_tree_application_rule(root / "cache", env) is None
    assert whole_tree_application_rule(ports, env) is None
    assert whole_tree_application_rule(plugins, env) is None
    assert whole_tree_application_rule(root / "bin", env) is None

    discovered = discover_known_cleanup_roots(default_rules().scan, env)
    by_path = {os.path.normcase(str(item.path)): item for item in discovered}

    root_item = by_path[os.path.normcase(str(root))]
    download_item = by_path[os.path.normcase(str(download))]
    temp_item = by_path[os.path.normcase(str(temp))]
    log_item = by_path[os.path.normcase(str(logs))]

    assert root_item.policy is CleanupPolicy.REPORT_ONLY
    assert not root_item.delete_root_itself
    assert download_item.category is CleanupCategory.INSTALLERS_DOWNLOADS
    assert download_item.policy is CleanupPolicy.VENDOR_MANAGED
    assert download_item.delete_root_itself
    assert download_item.application_rule is not None
    assert temp_item.category is CleanupCategory.USER_TEMP
    assert temp_item.policy is CleanupPolicy.VENDOR_MANAGED
    assert log_item.category is CleanupCategory.SYSTEM_LOGS
    assert log_item.policy is CleanupPolicy.VENDOR_MANAGED


def test_toolbox_process_guard_is_independent_from_jetbrains_ide_guard(
    tmp_path: Path,
    monkeypatch,
) -> None:
    env, root = _layout(tmp_path)
    download = root / "cache" / "download"
    download.mkdir(parents=True)

    monkeypatch.setattr(application_cleanup, "toolbox_process_running", lambda: True)
    monkeypatch.setattr(application_cleanup, "jetbrains_process_running", lambda: False)
    assert not application_cleanup.process_guard_allows(download, env)

    monkeypatch.setattr(application_cleanup, "toolbox_process_running", lambda: False)
    monkeypatch.setattr(application_cleanup, "jetbrains_process_running", lambda: True)
    assert application_cleanup.process_guard_allows(download, env)
