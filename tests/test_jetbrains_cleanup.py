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
    process_guard_allows,
    whole_tree_application_rule,
)
from devclean.core.cleanup_catalog import (
    CleanupCategory,
    CleanupPolicy,
    discover_known_cleanup_roots,
)
from devclean.core.jetbrains_cleanup import jetbrains_roots
from devclean.core.user_rules import default_rules

_NOW = datetime(2026, 8, 17, tzinfo=UTC)
_MIB = 1024**2


def _layout(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    roaming = tmp_path / "Roaming" / "JetBrains" / "IntelliJIdea2026.2"
    system = tmp_path / "Local" / "JetBrains" / "IntelliJIdea2026.2"
    roaming.mkdir(parents=True)
    system.mkdir(parents=True)
    env = {
        "USERPROFILE": str(tmp_path / "home"),
        "APPDATA": str(tmp_path / "Roaming"),
        "LOCALAPPDATA": str(tmp_path / "Local"),
        "TEMP": str(tmp_path / "Temp"),
    }
    return env, roaming, system


def test_jetbrains_default_versioned_roots_are_discovered(tmp_path: Path) -> None:
    env, config, system = _layout(tmp_path)
    roots = jetbrains_roots(env)

    assert PureWindowsPath(str(config)) in roots.config_roots
    assert PureWindowsPath(str(system)) in roots.system_roots
    assert PureWindowsPath(str(config / "plugins")) in roots.plugin_roots
    assert PureWindowsPath(str(system / "log")) in roots.log_roots


def test_jetbrains_mixed_system_root_delegates_only_source_backed_subtrees(
    tmp_path: Path,
) -> None:
    env, config, system = _layout(tmp_path)
    paths = {
        system / "index" / "file.idx": ("jetbrains-index-cache", DecisionOwner.TOOL),
        system / "tmp" / "download.tmp": ("jetbrains-system-temp", DecisionOwner.TOOL),
        system / "vcs-log" / "log.db": ("jetbrains-vcs-log-cache", DecisionOwner.TOOL),
        system / "log" / "idea.log": ("jetbrains-product-logs", DecisionOwner.TOOL),
        system / "LocalHistory" / "storageData": (
            "jetbrains-local-history",
            DecisionOwner.USER,
        ),
        system / "jcef_cache" / "Cookies": (
            "jetbrains-jcef-browser-state",
            DecisionOwner.USER,
        ),
        system / "caches" / "names.dat": (
            "jetbrains-vfs-cache-state",
            DecisionOwner.KEEP,
        ),
        system / "unknown" / "state.db": (
            "jetbrains-system-state",
            DecisionOwner.KEEP,
        ),
        config / "options" / "editor.xml": (
            "jetbrains-config-state",
            DecisionOwner.KEEP,
        ),
        config / "plugins" / "my.plugin" / "lib.jar": (
            "jetbrains-user-plugins",
            DecisionOwner.KEEP,
        ),
    }

    for path, (rule_id, owner) in paths.items():
        rule = match_application_rule(path, env)
        assert rule is not None
        assert rule.rule_id == rule_id
        assert rule.owner is owner


def test_jetbrains_user_state_is_reviewable_while_keep_state_is_protected(
    tmp_path: Path,
) -> None:
    env, _config, system = _layout(tmp_path)
    cases = {
        system / "LocalHistory" / "storageData": DecisionOwner.USER,
        system / "jcef_cache" / "Cookies": DecisionOwner.USER,
        system / "caches" / "names.dat": DecisionOwner.KEEP,
    }
    for path, owner in cases.items():
        decision = evaluate_application_path(
            path,
            logical_size=512 * _MIB,
            last_used=_NOW - timedelta(days=365),
            now=_NOW,
            process_running=False,
            environment=env,
        )
        assert decision is not None
        assert decision.rule.owner is owner
        if owner is DecisionOwner.USER:
            assert decision.action is PolicyAction.USER_DECISION
            assert process_guard_allows(path, env)
        else:
            assert decision.action is PolicyAction.KEEP_PROTECTED
            assert not process_guard_allows(path, env)


def test_jetbrains_index_policy_honors_idle_reclaim_and_process_guard(
    tmp_path: Path,
) -> None:
    env, _config, system = _layout(tmp_path)
    index = system / "index"

    stale = evaluate_application_path(
        index,
        logical_size=512 * _MIB,
        last_used=_NOW - timedelta(days=45),
        now=_NOW,
        process_running=False,
        environment=env,
    )
    recent = evaluate_application_path(
        index,
        logical_size=512 * _MIB,
        last_used=_NOW - timedelta(days=2),
        now=_NOW,
        process_running=False,
        environment=env,
    )
    small = evaluate_application_path(
        index,
        logical_size=32 * _MIB,
        last_used=_NOW - timedelta(days=90),
        now=_NOW,
        process_running=False,
        environment=env,
    )
    running = evaluate_application_path(
        index,
        logical_size=512 * _MIB,
        last_used=_NOW - timedelta(days=90),
        now=_NOW,
        process_running=True,
        environment=env,
    )

    assert stale is not None and stale.action is PolicyAction.TOOL_DELETE
    assert recent is not None and recent.action is PolicyAction.TOOL_KEEP_RECENT
    assert small is not None and small.action is PolicyAction.TOOL_KEEP_LOW_BENEFIT
    assert running is not None and running.action is PolicyAction.TOOL_KEEP_IN_USE


def test_jetbrains_whole_tree_authority_is_exact_and_catalogued(tmp_path: Path) -> None:
    env, config, system = _layout(tmp_path)
    index = system / "index"
    temp = system / "tmp"
    vcs_log = system / "vcs-log"
    log = system / "log"
    for path in (index, temp, vcs_log, log, system / "LocalHistory", system / "caches"):
        path.mkdir(parents=True, exist_ok=True)

    assert whole_tree_application_rule(index, env) is not None
    assert whole_tree_application_rule(temp, env) is not None
    assert whole_tree_application_rule(vcs_log, env) is not None
    assert whole_tree_application_rule(log, env) is not None
    assert whole_tree_application_rule(system, env) is None
    assert whole_tree_application_rule(system / "LocalHistory", env) is None
    assert whole_tree_application_rule(system / "caches", env) is None
    assert whole_tree_application_rule(config, env) is None

    discovered = discover_known_cleanup_roots(default_rules().scan, env)
    by_path = {os.path.normcase(str(root.path)): root for root in discovered}
    system_root = by_path[os.path.normcase(str(system))]
    index_root = by_path[os.path.normcase(str(index))]
    log_root = by_path[os.path.normcase(str(log))]

    assert system_root.policy is CleanupPolicy.REPORT_ONLY
    assert not system_root.delete_root_itself
    assert index_root.category is CleanupCategory.IDE_CACHE
    assert index_root.policy is CleanupPolicy.VENDOR_MANAGED
    assert index_root.delete_root_itself
    assert index_root.application_rule is not None
    assert log_root.category is CleanupCategory.SYSTEM_LOGS
    assert log_root.delete_root_itself


def test_idea_properties_redirects_are_discovered_without_widening_parent(
    tmp_path: Path,
) -> None:
    env, config, _system = _layout(tmp_path)
    home = tmp_path / "home"
    custom_system = tmp_path / "custom" / "system"
    custom_plugins = tmp_path / "custom" / "plugins"
    custom_log = tmp_path / "custom" / "logs"
    for path in (home, custom_system, custom_plugins, custom_log):
        path.mkdir(parents=True, exist_ok=True)
    (config / "idea.properties").write_text(
        "\n".join(
            (
                "idea.system.path=${user.home}/../custom/system",
                f"idea.plugins.path={custom_plugins.as_posix()}",
                f"idea.log.path={custom_log.as_posix()}",
            )
        ),
        encoding="utf-8",
    )

    roots = jetbrains_roots(env)
    assert PureWindowsPath(str(custom_system)) in roots.system_roots
    assert PureWindowsPath(str(custom_plugins)) in roots.plugin_roots
    assert PureWindowsPath(str(custom_log)) in roots.log_roots

    plugin_rule = match_application_rule(custom_plugins / "plugin.jar", env)
    log_rule = match_application_rule(custom_log / "idea.log", env)
    sibling_rule = match_application_rule(tmp_path / "custom" / "unrelated" / "Cache", env)
    assert plugin_rule is not None and plugin_rule.owner is DecisionOwner.KEEP
    assert log_rule is not None and log_rule.owner is DecisionOwner.TOOL
    assert sibling_rule is None


def test_unrecognised_and_android_studio_selectors_are_not_claimed(tmp_path: Path) -> None:
    appdata = tmp_path / "Roaming"
    local = tmp_path / "Local"
    for selector in ("RandomTool2026.2", "AndroidStudio2026.2"):
        (appdata / "JetBrains" / selector).mkdir(parents=True)
        (local / "JetBrains" / selector).mkdir(parents=True)
    env = {
        "USERPROFILE": str(tmp_path / "home"),
        "APPDATA": str(appdata),
        "LOCALAPPDATA": str(local),
        "TEMP": str(tmp_path / "Temp"),
    }

    roots = jetbrains_roots(env)
    assert not roots.config_roots
    assert not roots.system_roots
    assert not application_scan_roots(env) or all(
        "RandomTool2026.2" not in str(root) and "AndroidStudio2026.2" not in str(root)
        for root in application_scan_roots(env)
    )
