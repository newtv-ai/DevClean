from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path, PureWindowsPath

import pytest

import devclean.core.application_cleanup as application_cleanup
from devclean.core.android_studio_cleanup import android_studio_roots
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
from devclean.core.user_rules import default_rules

_NOW = datetime(2026, 8, 17, tzinfo=UTC)
_MIB = 1024**2


def _layout(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    config = tmp_path / "Roaming" / "Google" / "AndroidStudio2026.2"
    system = tmp_path / "Local" / "Google" / "AndroidStudio2026.2"
    config.mkdir(parents=True)
    system.mkdir(parents=True)
    env = {
        "USERPROFILE": str(tmp_path / "home"),
        "APPDATA": str(tmp_path / "Roaming"),
        "LOCALAPPDATA": str(tmp_path / "Local"),
        "TEMP": str(tmp_path / "Temp"),
    }
    return env, config, system


def test_android_studio_stable_and_preview_roots_are_discovered(tmp_path: Path) -> None:
    env, config, system = _layout(tmp_path)
    preview_config = tmp_path / "Roaming" / "Google" / "AndroidStudioPreview2026.3"
    preview_system = tmp_path / "Local" / "Google" / "AndroidStudioPreview2026.3"
    preview_config.mkdir(parents=True)
    preview_system.mkdir(parents=True)

    roots = android_studio_roots(env)

    assert PureWindowsPath(str(config)) in roots.config_roots
    assert PureWindowsPath(str(system)) in roots.system_roots
    assert PureWindowsPath(str(preview_config)) in roots.config_roots
    assert PureWindowsPath(str(preview_system)) in roots.system_roots
    assert PureWindowsPath(str(config / "plugins")) in roots.plugin_roots
    assert PureWindowsPath(str(system / "log")) in roots.log_roots
    assert PureWindowsPath(str(system)) in application_scan_roots(env)


def test_android_studio_mixed_system_root_only_delegates_exact_platform_caches(
    tmp_path: Path,
) -> None:
    env, config, system = _layout(tmp_path)
    cases = {
        system / "index" / "persistent" / "index.storage": (
            "android-studio-index-cache",
            DecisionOwner.TOOL,
        ),
        system / "tmp" / "studio.tmp": (
            "android-studio-system-temp",
            DecisionOwner.TOOL,
        ),
        system / "vcs-log" / "data": (
            "android-studio-vcs-log-cache",
            DecisionOwner.TOOL,
        ),
        system / "log" / "idea.log": (
            "android-studio-product-logs",
            DecisionOwner.TOOL,
        ),
        system / "LocalHistory" / "storageData": (
            "android-studio-local-history",
            DecisionOwner.USER,
        ),
        system / "jcef_cache" / "Cookies": (
            "android-studio-jcef-browser-state",
            DecisionOwner.USER,
        ),
        system / "caches" / "names.dat": (
            "android-studio-vfs-cache-state",
            DecisionOwner.KEEP,
        ),
        system / "unknown" / "state.db": (
            "android-studio-system-state",
            DecisionOwner.KEEP,
        ),
        config / "options" / "other.xml": (
            "android-studio-config-state",
            DecisionOwner.KEEP,
        ),
        config / "plugins" / "com.example" / "plugin.jar": (
            "android-studio-user-plugins",
            DecisionOwner.KEEP,
        ),
    }

    for path, (rule_id, owner) in cases.items():
        rule = match_application_rule(path, env)
        assert rule is not None
        assert rule.rule_id == rule_id
        assert rule.owner is owner


def test_android_studio_tool_policy_keeps_recent_small_and_in_use_indexes(
    tmp_path: Path,
) -> None:
    env, _, system = _layout(tmp_path)
    index = system / "index"

    recent = evaluate_application_path(
        index,
        logical_size=512 * _MIB,
        last_used=_NOW - timedelta(days=5),
        now=_NOW,
        process_running=False,
        environment=env,
    )
    stale = evaluate_application_path(
        index,
        logical_size=512 * _MIB,
        last_used=_NOW - timedelta(days=45),
        now=_NOW,
        process_running=False,
        environment=env,
    )
    small = evaluate_application_path(
        index,
        logical_size=64 * _MIB,
        last_used=_NOW - timedelta(days=365),
        now=_NOW,
        process_running=False,
        environment=env,
    )
    running = evaluate_application_path(
        index,
        logical_size=512 * _MIB,
        last_used=_NOW - timedelta(days=365),
        now=_NOW,
        process_running=True,
        environment=env,
    )

    assert recent is not None and recent.action is PolicyAction.TOOL_KEEP_RECENT
    assert stale is not None and stale.action is PolicyAction.TOOL_DELETE
    assert small is not None and small.action is PolicyAction.TOOL_KEEP_LOW_BENEFIT
    assert running is not None and running.action is PolicyAction.TOOL_KEEP_IN_USE


def test_android_studio_user_and_keep_state_stays_protected_in_generic_pipeline(
    tmp_path: Path,
) -> None:
    env, config, system = _layout(tmp_path)
    for path in (
        system / "LocalHistory" / "storageData",
        system / "jcef_cache" / "Cookies",
        system / "caches" / "names.dat",
        system / "unknown" / "state.db",
        config / "options" / "other.xml",
        config / "plugins" / "plugin.jar",
    ):
        decision = evaluate_application_path(
            path,
            logical_size=2 * 1024**3,
            last_used=_NOW - timedelta(days=365),
            now=_NOW,
            process_running=False,
            environment=env,
        )
        assert decision is not None
        assert decision.action is PolicyAction.KEEP_PROTECTED


def test_android_studio_whole_tree_authority_is_exact_and_catalogued(
    tmp_path: Path,
) -> None:
    env, config, system = _layout(tmp_path)
    index = system / "index"
    temp = system / "tmp"
    vcs_log = system / "vcs-log"
    logs = system / "log"
    local_history = system / "LocalHistory"
    plugins = config / "plugins"
    for path in (index, temp, vcs_log, logs, local_history, plugins):
        path.mkdir(parents=True, exist_ok=True)

    assert whole_tree_application_rule(index, env) is not None
    assert whole_tree_application_rule(temp, env) is not None
    assert whole_tree_application_rule(vcs_log, env) is not None
    assert whole_tree_application_rule(logs, env) is not None
    assert whole_tree_application_rule(system, env) is None
    assert whole_tree_application_rule(local_history, env) is None
    assert whole_tree_application_rule(plugins, env) is None

    discovered = discover_known_cleanup_roots(default_rules().scan, env)
    by_path = {os.path.normcase(str(item.path)): item for item in discovered}

    system_item = by_path[os.path.normcase(str(system))]
    index_item = by_path[os.path.normcase(str(index))]
    log_item = by_path[os.path.normcase(str(logs))]

    assert system_item.policy is CleanupPolicy.REPORT_ONLY
    assert not system_item.delete_root_itself
    assert index_item.category is CleanupCategory.IDE_CACHE
    assert index_item.policy is CleanupPolicy.VENDOR_MANAGED
    assert index_item.delete_root_itself
    assert index_item.application_rule is not None
    assert log_item.category is CleanupCategory.SYSTEM_LOGS
    assert log_item.policy is CleanupPolicy.VENDOR_MANAGED


def test_android_studio_studio_properties_redirects_are_source_backed(
    tmp_path: Path,
) -> None:
    env, _, _ = _layout(tmp_path)
    custom = tmp_path / "custom"
    properties = tmp_path / "studio.properties"
    properties.write_text(
        "\n".join(
            (
                f"idea.config.path={custom / 'config'}",
                f"idea.system.path={custom / 'system'}",
                f"idea.plugins.path={custom / 'plugins'}",
                f"idea.log.path={custom / 'logs'}",
            )
        ),
        encoding="utf-8",
    )
    env["STUDIO_PROPERTIES"] = str(properties)

    roots = android_studio_roots(env)

    assert PureWindowsPath(str(custom / "config")) in roots.config_roots
    assert PureWindowsPath(str(custom / "system")) in roots.system_roots
    assert PureWindowsPath(str(custom / "plugins")) in roots.plugin_roots
    assert PureWindowsPath(str(custom / "logs")) in roots.log_roots

    rule = match_application_rule(custom / "system" / "index" / "data", env)
    assert rule is not None
    assert rule.rule_id == "android-studio-index-cache"


def test_android_studio_process_guard_is_independent_from_jetbrains_and_toolbox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, _, system = _layout(tmp_path)
    index = system / "index"
    index.mkdir(parents=True)

    monkeypatch.setattr(application_cleanup, "android_studio_process_running", lambda: True)
    monkeypatch.setattr(application_cleanup, "jetbrains_process_running", lambda: False)
    monkeypatch.setattr(application_cleanup, "toolbox_process_running", lambda: False)
    assert not application_cleanup.process_guard_allows(index, env)

    monkeypatch.setattr(application_cleanup, "android_studio_process_running", lambda: False)
    monkeypatch.setattr(application_cleanup, "jetbrains_process_running", lambda: True)
    monkeypatch.setattr(application_cleanup, "toolbox_process_running", lambda: True)
    assert application_cleanup.process_guard_allows(index, env)
