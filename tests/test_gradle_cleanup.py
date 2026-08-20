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
from devclean.core.cleanup_catalog import CleanupPolicy, discover_known_cleanup_roots
from devclean.core.gradle_cleanup import gradle_roots
from devclean.core.user_rules import default_rules

_NOW = datetime(2026, 8, 20, tzinfo=UTC)
_GIB = 1024**3


def _layout(tmp_path: Path) -> tuple[dict[str, str], Path]:
    root = tmp_path / ".gradle"
    root.mkdir(parents=True)
    env = {
        "USERPROFILE": str(tmp_path),
        "APPDATA": str(tmp_path / "Roaming"),
        "LOCALAPPDATA": str(tmp_path / "Local"),
        "TEMP": str(tmp_path / "Temp"),
    }
    return env, root


def _cache_layout(root: Path) -> tuple[Path, Path, Path]:
    release = root / "caches" / "9.6.1"
    snapshot = root / "caches" / "9.7-20260815010101+0000"
    build_cache = root / "caches" / "build-cache-1"
    for path in (release, snapshot, build_cache):
        path.mkdir(parents=True, exist_ok=True)
    return release, snapshot, build_cache


def test_gradle_default_override_and_system_property_roots_are_discovered(
    tmp_path: Path,
) -> None:
    env, default_root = _layout(tmp_path)
    override = tmp_path / "gradle-override"
    system_property = tmp_path / "gradle-system-property"
    override.mkdir()
    system_property.mkdir()
    env["GRADLE_USER_HOME"] = str(override)
    env["GRADLE_OPTS"] = f'-Dgradle.user.home="{system_property}" -Xmx2g'

    roots = gradle_roots(env)

    assert PureWindowsPath(str(default_root)) in roots
    assert PureWindowsPath(str(override)) in roots
    assert PureWindowsPath(str(system_property)) in roots
    assert PureWindowsPath(str(default_root)) in application_scan_roots(env)
    assert PureWindowsPath(str(override)) in application_scan_roots(env)


def test_gradle_mixed_user_home_and_vendor_managed_cache_state_are_protected(
    tmp_path: Path,
) -> None:
    env, root = _layout(tmp_path)
    release, snapshot, build_cache = _cache_layout(root)
    daemon_log = root / "daemon" / "9.6.1" / "daemon-123.out.log"
    daemon_log.parent.mkdir(parents=True)
    daemon_log.write_text("log", encoding="utf-8")

    cases = {
        root / "gradle.properties": "gradle-user-properties",
        root / "init.d" / "company.gradle": "gradle-init-scripts",
        root / "wrapper" / "dists" / "gradle-9.6.1-bin": "gradle-wrapper-distributions",
        root / "jdks" / "jdk-21": "gradle-downloaded-jdks",
        root / "caches" / "modules-2" / "files-2.1" / "dep.jar": "gradle-shared-cache-state",
        root / "caches" / "transforms-4" / "output.bin": "gradle-shared-cache-state",
        root / "daemon" / "9.6.1" / "registry.bin": "gradle-daemon-state",
        daemon_log: "gradle-daemon-log",
        release / "fileHashes" / "fileHashes.bin": "gradle-release-version-cache",
        snapshot / "generated-gradle-jars" / "gradle-api.jar": "gradle-snapshot-version-cache",
        build_cache / "abc123": "gradle-local-build-cache",
        root / "notifications" / "state.bin": "gradle-user-home-state",
    }

    for path, rule_id in cases.items():
        rule = match_application_rule(path, env)
        assert rule is not None
        assert rule.rule_id == rule_id
        assert rule.owner is DecisionOwner.KEEP
        assert not rule.allow_whole_tree


def test_gradle_age_size_and_process_state_never_create_raw_delete_authority(
    tmp_path: Path,
) -> None:
    env, root = _layout(tmp_path)
    release, snapshot, build_cache = _cache_layout(root)
    daemon_log = root / "daemon" / "9.6.1" / "daemon-123.out.log"
    daemon_log.parent.mkdir(parents=True)
    daemon_log.write_text("x", encoding="utf-8")

    for path in (release, snapshot, build_cache, daemon_log):
        decision = evaluate_application_path(
            path,
            logical_size=64 * _GIB,
            last_used=_NOW - timedelta(days=3650),
            now=_NOW,
            process_running=False,
            environment=env,
        )
        assert decision is not None
        assert decision.rule.owner is DecisionOwner.KEEP
        assert decision.effective_idle_days is None
        assert decision.action is PolicyAction.KEEP_PROTECTED


def test_gradle_daemon_log_fourteen_day_default_is_not_a_devclean_threshold(
    tmp_path: Path,
) -> None:
    env, root = _layout(tmp_path)
    log = root / "daemon" / "9.6.1" / "daemon-123.out.log"
    log.parent.mkdir(parents=True)
    log.write_text("x", encoding="utf-8")

    for age_days in (1, 14, 15, 365):
        decision = evaluate_application_path(
            log,
            logical_size=4 * _GIB,
            last_used=_NOW - timedelta(days=age_days),
            now=_NOW,
            process_running=False,
            environment=env,
        )
        assert decision is not None
        assert decision.rule.rule_id == "gradle-daemon-log"
        assert decision.effective_idle_days is None
        assert decision.action is PolicyAction.KEEP_PROTECTED


def test_gradle_init_script_text_does_not_control_devclean_mutation_authority(
    tmp_path: Path,
) -> None:
    env, root = _layout(tmp_path)
    release, _, build_cache = _cache_layout(root)
    init_dir = root / "init.d"
    init_dir.mkdir()
    (init_dir / "cache-settings.init.gradle.kts").write_text(
        """
beforeSettings {
    caches {
        releasedWrappers.setRemoveUnusedEntriesAfterDays(60)
        buildCache.setRemoveUnusedEntriesAfterDays(45)
        cleanup.set(Cleanup.ALWAYS)
    }
}
""".strip(),
        encoding="utf-8",
    )

    for path, rule_id in (
        (release, "gradle-release-version-cache"),
        (build_cache, "gradle-local-build-cache"),
    ):
        rule = match_application_rule(path, env)
        assert rule is not None
        assert rule.rule_id == rule_id
        assert rule.owner is DecisionOwner.KEEP
        assert whole_tree_application_rule(path, env) is None

        decision = evaluate_application_path(
            path,
            logical_size=64 * _GIB,
            last_used=_NOW - timedelta(days=3650),
            now=_NOW,
            process_running=False,
            environment=env,
        )
        assert decision is not None
        assert decision.action is PolicyAction.KEEP_PROTECTED


def test_gradle_whole_tree_authority_is_removed_and_catalog_is_report_only(
    tmp_path: Path,
) -> None:
    env, root = _layout(tmp_path)
    release, snapshot, build_cache = _cache_layout(root)
    modules = root / "caches" / "modules-2"
    wrapper = root / "wrapper" / "dists"
    modules.mkdir(parents=True)
    wrapper.mkdir(parents=True)

    for path in (root, root / "caches", release, snapshot, build_cache, modules, wrapper):
        assert whole_tree_application_rule(path, env) is None

    discovered = discover_known_cleanup_roots(default_rules().scan, env)
    by_path = {os.path.normcase(str(item.path)): item for item in discovered}

    root_item = by_path[os.path.normcase(str(root))]
    assert root_item.policy is CleanupPolicy.REPORT_ONLY
    assert not root_item.delete_root_itself
    assert root_item.application_rule is None

    for path in (release, snapshot, build_cache):
        assert os.path.normcase(str(path)) not in by_path


def test_gradle_protected_paths_fail_closed_before_process_state(
    tmp_path: Path,
) -> None:
    env, root = _layout(tmp_path)
    release, _, _ = _cache_layout(root)

    rule = match_application_rule(release, env)
    assert rule is not None
    assert rule.owner is DecisionOwner.KEEP
    assert not process_guard_allows(release, env)
