from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path, PureWindowsPath

import pytest

import devclean.core.application_cleanup as application_cleanup
from devclean.core.android_avd_cleanup import android_avd_roots
from devclean.core.application_cleanup import (
    DecisionOwner,
    PolicyAction,
    application_scan_roots,
    evaluate_application_path,
    match_application_rule,
    whole_tree_application_rule,
)

_NOW = datetime(2026, 8, 17, tzinfo=UTC)
_MIB = 1024**2
_GIB = 1024**3


def _layout(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    registry = tmp_path / ".android" / "avd"
    content = registry / "Pixel_9_API_36.avd"
    content.mkdir(parents=True)
    (registry / "Pixel_9_API_36.ini").write_text(
        "\n".join(
            (
                "avd.ini.encoding=UTF-8",
                f"path={content}",
                "path.rel=avd\\Pixel_9_API_36.avd",
                "target=android-36",
            )
        ),
        encoding="utf-8",
    )
    env = {
        "USERPROFILE": str(tmp_path),
        "APPDATA": str(tmp_path / "Roaming"),
        "LOCALAPPDATA": str(tmp_path / "Local"),
        "TEMP": str(tmp_path / "Temp"),
    }
    return env, registry, content


def test_android_avd_default_registry_and_content_are_discovered(tmp_path: Path) -> None:
    env, registry, content = _layout(tmp_path)

    roots = android_avd_roots(env)

    assert PureWindowsPath(str(registry)) in roots.registry_roots
    assert PureWindowsPath(str(content)) in roots.content_roots
    assert PureWindowsPath(str(registry)) in application_scan_roots(env)
    # The content directory is nested below the registry, so scan roots are
    # intentionally minimal and do not traverse it a second time.
    assert PureWindowsPath(str(content)) not in application_scan_roots(env)


def test_android_avd_home_and_root_ini_can_point_to_external_content(
    tmp_path: Path,
) -> None:
    env, _, _ = _layout(tmp_path)
    registry = tmp_path / "CustomAvdRegistry"
    external = tmp_path / "LargeDisk" / "Tablet.avd"
    registry.mkdir()
    external.mkdir(parents=True)
    (registry / "Tablet.ini").write_text(
        f"avd.ini.encoding=UTF-8\npath={external}\ntarget=android-35\n",
        encoding="utf-8",
    )
    env["ANDROID_AVD_HOME"] = str(registry)

    roots = android_avd_roots(env)

    assert PureWindowsPath(str(registry)) in roots.registry_roots
    assert PureWindowsPath(str(external)) in roots.content_roots
    assert PureWindowsPath(str(registry)) in application_scan_roots(env)
    assert PureWindowsPath(str(external)) in application_scan_roots(env)


def test_android_avd_root_ini_relative_path_resolves_from_android_config_home(
    tmp_path: Path,
) -> None:
    env, registry, _ = _layout(tmp_path)
    relative_content = registry / "Relative_Tablet.avd"
    relative_content.mkdir()
    (registry / "Relative_Tablet.ini").write_text(
        "avd.ini.encoding=UTF-8\npath.rel=avd\\Relative_Tablet.avd\ntarget=android-36\n",
        encoding="utf-8",
    )

    roots = android_avd_roots(env)

    assert PureWindowsPath(str(relative_content)) in roots.content_roots


def test_android_avd_user_state_is_never_generic_cache(tmp_path: Path) -> None:
    env, registry, content = _layout(tmp_path)
    cases = {
        registry / "Pixel_9_API_36.ini": (
            "android-avd-registry-state",
            DecisionOwner.KEEP,
        ),
        content / "config.ini": ("android-avd-config-state", DecisionOwner.KEEP),
        content / "encryptionkey.img": (
            "android-avd-encryption-state",
            DecisionOwner.KEEP,
        ),
        content / "userdata-qemu.img": (
            "android-avd-user-data",
            DecisionOwner.USER,
        ),
        content / "userdata-qemu.img.qcow2": (
            "android-avd-user-data",
            DecisionOwner.USER,
        ),
        content / "sdcard.img": (
            "android-avd-sd-card-data",
            DecisionOwner.USER,
        ),
        content / "snapshots" / "default_boot" / "ram.img": (
            "android-avd-snapshot-state",
            DecisionOwner.USER,
        ),
        content / "snapshots.img": (
            "android-avd-snapshot-storage",
            DecisionOwner.USER,
        ),
        content / "system.img.qcow2": (
            "android-avd-mutable-system-state",
            DecisionOwner.USER,
        ),
        content / "vendor.img.qcow2": (
            "android-avd-mutable-vendor-state",
            DecisionOwner.USER,
        ),
        content / "data" / "misc" / "state.db": (
            "android-avd-data-state",
            DecisionOwner.USER,
        ),
        content / "future-state.bin": (
            "android-avd-content-state",
            DecisionOwner.USER,
        ),
    }

    for path, (rule_id, owner) in cases.items():
        rule = match_application_rule(path, env)
        assert rule is not None
        assert rule.rule_id == rule_id
        assert rule.owner is owner


def test_android_avd_uncoupled_default_cache_partition_is_conservative_tool_data(
    tmp_path: Path,
) -> None:
    env, _, content = _layout(tmp_path)
    cache = content / "cache.img"
    cache.write_bytes(b"x")

    recent_huge = evaluate_application_path(
        cache,
        logical_size=8 * _GIB,
        last_used=_NOW - timedelta(days=6),
        now=_NOW,
        process_running=False,
        environment=env,
    )
    stale = evaluate_application_path(
        cache,
        logical_size=128 * _MIB,
        last_used=_NOW - timedelta(days=8),
        now=_NOW,
        process_running=False,
        environment=env,
    )
    small = evaluate_application_path(
        cache,
        logical_size=4 * _MIB,
        last_used=_NOW - timedelta(days=365),
        now=_NOW,
        process_running=False,
        environment=env,
    )
    running = evaluate_application_path(
        cache,
        logical_size=128 * _MIB,
        last_used=_NOW - timedelta(days=365),
        now=_NOW,
        process_running=True,
        environment=env,
    )

    assert recent_huge is not None
    assert recent_huge.effective_idle_days == 7
    assert recent_huge.action is PolicyAction.TOOL_KEEP_RECENT
    assert stale is not None and stale.action is PolicyAction.TOOL_DELETE
    assert small is not None and small.action is PolicyAction.TOOL_KEEP_LOW_BENEFIT
    assert running is not None and running.action is PolicyAction.TOOL_KEEP_IN_USE


def test_android_avd_qcow2_cache_pair_fails_closed(tmp_path: Path) -> None:
    env, _, content = _layout(tmp_path)
    cache = content / "cache.img"
    overlay = content / "cache.img.qcow2"
    cache.write_bytes(b"cache")
    overlay.write_bytes(b"overlay")

    cache_rule = match_application_rule(cache, env)
    overlay_rule = match_application_rule(overlay, env)

    assert cache_rule is not None
    assert cache_rule.rule_id == "android-avd-coupled-cache-state"
    assert cache_rule.owner is DecisionOwner.KEEP
    assert overlay_rule is not None
    assert overlay_rule.rule_id == "android-avd-cache-overlay-state"
    assert overlay_rule.owner is DecisionOwner.KEEP

    decision = evaluate_application_path(
        cache,
        logical_size=128 * _MIB,
        last_used=_NOW - timedelta(days=365),
        now=_NOW,
        process_running=False,
        environment=env,
    )
    assert decision is not None
    assert decision.action is PolicyAction.KEEP_PROTECTED


def test_android_avd_user_owned_data_projects_to_generic_protection(
    tmp_path: Path,
) -> None:
    env, _, content = _layout(tmp_path)
    for path in (
        content / "userdata-qemu.img",
        content / "sdcard.img",
        content / "snapshots" / "quickboot" / "memory.bin",
        content / "system.img.qcow2",
        content / "unknown" / "state.db",
    ):
        decision = evaluate_application_path(
            path,
            logical_size=16 * _GIB,
            last_used=_NOW - timedelta(days=3650),
            now=_NOW,
            process_running=False,
            environment=env,
        )
        assert decision is not None
        assert decision.action is PolicyAction.KEEP_PROTECTED


def test_android_avd_never_grants_whole_tree_deletion(tmp_path: Path) -> None:
    env, registry, content = _layout(tmp_path)
    snapshots = content / "snapshots"
    snapshots.mkdir()

    assert whole_tree_application_rule(registry, env) is None
    assert whole_tree_application_rule(content, env) is None
    assert whole_tree_application_rule(snapshots, env) is None


def test_android_avd_process_guard_applies_only_to_deletable_cache_partition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, _, content = _layout(tmp_path)
    cache = content / "cache.img"
    cache.write_bytes(b"cache")

    monkeypatch.setattr(application_cleanup, "android_avd_process_running", lambda: True)
    assert not application_cleanup.process_guard_allows(cache, env)

    monkeypatch.setattr(application_cleanup, "android_avd_process_running", lambda: False)
    assert application_cleanup.process_guard_allows(cache, env)

    # User data is refused regardless of process state.
    assert not application_cleanup.process_guard_allows(content / "userdata-qemu.img", env)
