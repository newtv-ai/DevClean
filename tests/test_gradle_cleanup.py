from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path, PureWindowsPath

import pytest

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
from devclean.core.gradle_cleanup import gradle_roots
from devclean.core.user_rules import default_rules

_NOW = datetime(2026, 8, 17, tzinfo=UTC)
_MIB = 1024**2
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


def test_gradle_mixed_user_home_protects_shared_and_persistent_state(
    tmp_path: Path,
) -> None:
    env, root = _layout(tmp_path)
    release, snapshot, build_cache = _cache_layout(root)
    daemon_log = root / "daemon" / "9.6.1" / "daemon-123.out.log"
    daemon_log.parent.mkdir(parents=True)
    daemon_log.write_text("log", encoding="utf-8")

    cases = {
        root / "gradle.properties": ("gradle-user-properties", DecisionOwner.KEEP),
        root / "init.d" / "company.gradle": ("gradle-init-scripts", DecisionOwner.KEEP),
        root / "wrapper" / "dists" / "gradle-9.6.1-bin": (
            "gradle-wrapper-distributions",
            DecisionOwner.KEEP,
        ),
        root / "jdks" / "jdk-21": ("gradle-downloaded-jdks", DecisionOwner.KEEP),
        root / "caches" / "modules-2" / "files-2.1" / "dep.jar": (
            "gradle-shared-cache-state",
            DecisionOwner.KEEP,
        ),
        root / "caches" / "transforms-4" / "output.bin": (
            "gradle-shared-cache-state",
            DecisionOwner.KEEP,
        ),
        root / "daemon" / "9.6.1" / "registry.bin": (
            "gradle-daemon-state",
            DecisionOwner.KEEP,
        ),
        daemon_log: ("gradle-daemon-log", DecisionOwner.TOOL),
        release / "fileHashes" / "fileHashes.bin": (
            "gradle-release-version-cache",
            DecisionOwner.TOOL,
        ),
        snapshot / "generated-gradle-jars" / "gradle-api.jar": (
            "gradle-snapshot-version-cache",
            DecisionOwner.TOOL,
        ),
        build_cache / "abc123": ("gradle-local-build-cache", DecisionOwner.TOOL),
        root / "notifications" / "state.bin": (
            "gradle-user-home-state",
            DecisionOwner.KEEP,
        ),
    }

    for path, (rule_id, owner) in cases.items():
        rule = match_application_rule(path, env)
        assert rule is not None
        assert rule.rule_id == rule_id
        assert rule.owner is owner


def test_gradle_vendor_retention_floors_are_not_shortened_by_large_size(
    tmp_path: Path,
) -> None:
    env, root = _layout(tmp_path)
    release, snapshot, build_cache = _cache_layout(root)

    release_recent = evaluate_application_path(
        release,
        logical_size=16 * _GIB,
        last_used=_NOW - timedelta(days=29),
        now=_NOW,
        process_running=False,
        environment=env,
    )
    release_stale = evaluate_application_path(
        release,
        logical_size=2 * _GIB,
        last_used=_NOW - timedelta(days=31),
        now=_NOW,
        process_running=False,
        environment=env,
    )
    snapshot_recent = evaluate_application_path(
        snapshot,
        logical_size=8 * _GIB,
        last_used=_NOW - timedelta(days=6),
        now=_NOW,
        process_running=False,
        environment=env,
    )
    snapshot_stale = evaluate_application_path(
        snapshot,
        logical_size=2 * _GIB,
        last_used=_NOW - timedelta(days=8),
        now=_NOW,
        process_running=False,
        environment=env,
    )
    build_recent = evaluate_application_path(
        build_cache,
        logical_size=4 * _GIB,
        last_used=_NOW - timedelta(days=6),
        now=_NOW,
        process_running=False,
        environment=env,
    )
    build_stale = evaluate_application_path(
        build_cache,
        logical_size=2 * _GIB,
        last_used=_NOW - timedelta(days=8),
        now=_NOW,
        process_running=False,
        environment=env,
    )

    assert release_recent is not None
    assert release_recent.effective_idle_days == 30
    assert release_recent.action is PolicyAction.TOOL_KEEP_RECENT
    assert release_stale is not None
    assert release_stale.action is PolicyAction.TOOL_DELETE

    assert snapshot_recent is not None
    assert snapshot_recent.effective_idle_days == 7
    assert snapshot_recent.action is PolicyAction.TOOL_KEEP_RECENT
    assert snapshot_stale is not None
    assert snapshot_stale.action is PolicyAction.TOOL_DELETE

    assert build_recent is not None
    assert build_recent.effective_idle_days == 7
    assert build_recent.action is PolicyAction.TOOL_KEEP_RECENT
    assert build_stale is not None
    assert build_stale.action is PolicyAction.TOOL_DELETE


def test_gradle_cache_policy_keeps_small_or_in_use_roots(tmp_path: Path) -> None:
    env, root = _layout(tmp_path)
    release, _, build_cache = _cache_layout(root)

    small_release = evaluate_application_path(
        release,
        logical_size=64 * _MIB,
        last_used=_NOW - timedelta(days=365),
        now=_NOW,
        process_running=False,
        environment=env,
    )
    in_use_release = evaluate_application_path(
        release,
        logical_size=2 * _GIB,
        last_used=_NOW - timedelta(days=365),
        now=_NOW,
        process_running=True,
        environment=env,
    )
    small_build_cache = evaluate_application_path(
        build_cache,
        logical_size=32 * _MIB,
        last_used=_NOW - timedelta(days=365),
        now=_NOW,
        process_running=False,
        environment=env,
    )

    assert small_release is not None
    assert small_release.action is PolicyAction.TOOL_KEEP_LOW_BENEFIT
    assert in_use_release is not None
    assert in_use_release.action is PolicyAction.TOOL_KEEP_IN_USE
    assert small_build_cache is not None
    assert small_build_cache.action is PolicyAction.TOOL_KEEP_LOW_BENEFIT


def test_gradle_daemon_logs_follow_fourteen_day_floor(tmp_path: Path) -> None:
    env, root = _layout(tmp_path)
    log = root / "daemon" / "9.6.1" / "daemon-123.out.log"
    log.parent.mkdir(parents=True)
    log.write_text("x", encoding="utf-8")

    recent = evaluate_application_path(
        log,
        logical_size=1024,
        last_used=_NOW - timedelta(days=13),
        now=_NOW,
        process_running=False,
        environment=env,
    )
    stale = evaluate_application_path(
        log,
        logical_size=1024,
        last_used=_NOW - timedelta(days=15),
        now=_NOW,
        process_running=False,
        environment=env,
    )

    assert recent is not None
    assert recent.effective_idle_days == 14
    assert recent.action is PolicyAction.TOOL_KEEP_RECENT
    assert stale is not None and stale.action is PolicyAction.TOOL_DELETE


def test_gradle_custom_cleanup_init_script_outranks_devclean_defaults(
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
    }
}
""".strip(),
        encoding="utf-8",
    )

    release_rule = match_application_rule(release, env)
    build_rule = match_application_rule(build_cache, env)

    assert release_rule is not None
    assert release_rule.rule_id == "gradle-user-home-state"
    assert release_rule.owner is DecisionOwner.KEEP
    assert build_rule is not None and build_rule.owner is DecisionOwner.KEEP
    assert whole_tree_application_rule(release, env) is None
    assert whole_tree_application_rule(build_cache, env) is None

    decision = evaluate_application_path(
        release,
        logical_size=8 * _GIB,
        last_used=_NOW - timedelta(days=365),
        now=_NOW,
        process_running=False,
        environment=env,
    )
    assert decision is not None
    assert decision.action is PolicyAction.KEEP_PROTECTED


def test_gradle_whole_tree_authority_is_exact_and_catalogued(tmp_path: Path) -> None:
    env, root = _layout(tmp_path)
    release, snapshot, build_cache = _cache_layout(root)
    modules = root / "caches" / "modules-2"
    wrapper = root / "wrapper" / "dists"
    modules.mkdir(parents=True)
    wrapper.mkdir(parents=True)

    assert whole_tree_application_rule(release, env) is not None
    assert whole_tree_application_rule(snapshot, env) is not None
    assert whole_tree_application_rule(build_cache, env) is not None
    assert whole_tree_application_rule(root, env) is None
    assert whole_tree_application_rule(root / "caches", env) is None
    assert whole_tree_application_rule(modules, env) is None
    assert whole_tree_application_rule(wrapper, env) is None

    discovered = discover_known_cleanup_roots(default_rules().scan, env)
    by_path = {os.path.normcase(str(item.path)): item for item in discovered}

    root_item = by_path[os.path.normcase(str(root))]
    release_item = by_path[os.path.normcase(str(release))]
    snapshot_item = by_path[os.path.normcase(str(snapshot))]
    build_item = by_path[os.path.normcase(str(build_cache))]

    assert root_item.policy is CleanupPolicy.REPORT_ONLY
    assert not root_item.delete_root_itself
    for item in (release_item, snapshot_item, build_item):
        assert item.category is CleanupCategory.GRADLE_CACHE
        assert item.policy is CleanupPolicy.VENDOR_MANAGED
        assert item.delete_root_itself
        assert item.application_rule is not None


def test_gradle_process_guard_is_independent_from_android_studio(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, root = _layout(tmp_path)
    release, _, _ = _cache_layout(root)

    monkeypatch.setattr(application_cleanup, "gradle_process_running", lambda: True)
    monkeypatch.setattr(
        application_cleanup, "android_studio_process_running", lambda: False
    )
    assert not application_cleanup.process_guard_allows(release, env)

    monkeypatch.setattr(application_cleanup, "gradle_process_running", lambda: False)
    monkeypatch.setattr(
        application_cleanup, "android_studio_process_running", lambda: True
    )
    assert application_cleanup.process_guard_allows(release, env)
