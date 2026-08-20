from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path, PureWindowsPath

import pytest

import devclean.core.application_cleanup as application_cleanup
from devclean.core.android_sdk_cleanup import android_sdk_roots
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
_GIB = 1024**3


def _layout(tmp_path: Path) -> tuple[dict[str, str], Path]:
    root = tmp_path / "Local" / "Android" / "Sdk"
    root.mkdir(parents=True)
    env = {
        "USERPROFILE": str(tmp_path),
        "APPDATA": str(tmp_path / "Roaming"),
        "LOCALAPPDATA": str(tmp_path / "Local"),
        "TEMP": str(tmp_path / "Temp"),
    }
    return env, root


def test_android_sdk_default_and_environment_roots_are_discovered(tmp_path: Path) -> None:
    env, default_root = _layout(tmp_path)
    android_home = tmp_path / "AndroidHome"
    deprecated_root = tmp_path / "LegacyAndroidSdkRoot"
    android_home.mkdir()
    deprecated_root.mkdir()
    env["ANDROID_HOME"] = str(android_home)
    env["ANDROID_SDK_ROOT"] = str(deprecated_root)

    roots = android_sdk_roots(env)

    assert PureWindowsPath(str(default_root)) in roots
    assert PureWindowsPath(str(android_home)) in roots
    assert PureWindowsPath(str(deprecated_root)) in roots
    assert PureWindowsPath(str(default_root)) in application_scan_roots(env)
    assert PureWindowsPath(str(android_home)) in application_scan_roots(env)


def test_android_sdk_root_is_installed_payload_except_exact_installer_temp(
    tmp_path: Path,
) -> None:
    env, root = _layout(tmp_path)
    cases = {
        root / "temp" / "package.zip": (
            "android-sdk-install-temp",
            DecisionOwner.TOOL,
        ),
        root / "licenses" / "android-sdk-license": (
            "android-sdk-licenses",
            DecisionOwner.KEEP,
        ),
        root / "system-images" / "android-36" / "google_apis" / "x86_64": (
            "android-sdk-system-images",
            DecisionOwner.KEEP,
        ),
        root / "emulator" / "emulator.exe": (
            "android-sdk-emulator",
            DecisionOwner.KEEP,
        ),
        root / "ndk" / "28.2.13676358" / "source.properties": (
            "android-sdk-ndk",
            DecisionOwner.KEEP,
        ),
        root / "platforms" / "android-36" / "android.jar": (
            "android-sdk-installed-payload",
            DecisionOwner.KEEP,
        ),
        root / "build-tools" / "36.0.0" / "aapt2.exe": (
            "android-sdk-installed-payload",
            DecisionOwner.KEEP,
        ),
        root / "cmdline-tools" / "latest" / "bin" / "sdkmanager.bat": (
            "android-sdk-installed-payload",
            DecisionOwner.KEEP,
        ),
        root / "package.xml": (
            "android-sdk-installed-payload",
            DecisionOwner.KEEP,
        ),
        root / "future-package" / "payload.bin": (
            "android-sdk-installed-payload",
            DecisionOwner.KEEP,
        ),
    }

    for path, (rule_id, owner) in cases.items():
        rule = match_application_rule(path, env)
        assert rule is not None
        assert rule.rule_id == rule_id
        assert rule.owner is owner


def test_android_sdk_installer_temp_uses_conservative_seven_day_floor(
    tmp_path: Path,
) -> None:
    env, root = _layout(tmp_path)
    temp = root / "temp"
    temp.mkdir()

    recent_huge = evaluate_application_path(
        temp,
        logical_size=12 * _GIB,
        last_used=_NOW - timedelta(days=6),
        now=_NOW,
        process_running=False,
        environment=env,
    )
    stale = evaluate_application_path(
        temp,
        logical_size=2 * _GIB,
        last_used=_NOW - timedelta(days=8),
        now=_NOW,
        process_running=False,
        environment=env,
    )
    small = evaluate_application_path(
        temp,
        logical_size=8 * _MIB,
        last_used=_NOW - timedelta(days=365),
        now=_NOW,
        process_running=False,
        environment=env,
    )
    running = evaluate_application_path(
        temp,
        logical_size=2 * _GIB,
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


def test_android_sdk_installed_payload_projects_to_generic_protection(
    tmp_path: Path,
) -> None:
    env, root = _layout(tmp_path)
    for path in (
        root / "licenses" / "android-sdk-license",
        root / "system-images" / "android-36" / "google_apis" / "x86_64" / "system.img",
        root / "platform-tools" / "adb.exe",
        root / "platforms" / "android-36" / "android.jar",
        root / "ndk" / "28.2.13676358" / "source.properties",
    ):
        decision = evaluate_application_path(
            path,
            logical_size=8 * _GIB,
            last_used=_NOW - timedelta(days=3650),
            now=_NOW,
            process_running=False,
            environment=env,
        )
        assert decision is not None
        assert decision.action is PolicyAction.KEEP_PROTECTED


def test_android_sdk_whole_tree_authority_is_only_exact_temp_and_catalogued(
    tmp_path: Path,
) -> None:
    env, root = _layout(tmp_path)
    temp = root / "temp"
    system_images = root / "system-images"
    licenses = root / "licenses"
    platforms = root / "platforms"
    for path in (temp, system_images, licenses, platforms):
        path.mkdir(parents=True, exist_ok=True)

    assert whole_tree_application_rule(temp, env) is not None
    assert whole_tree_application_rule(root, env) is None
    assert whole_tree_application_rule(system_images, env) is None
    assert whole_tree_application_rule(licenses, env) is None
    assert whole_tree_application_rule(platforms, env) is None

    discovered = discover_known_cleanup_roots(default_rules().scan, env)
    by_path = {os.path.normcase(str(item.path)): item for item in discovered}

    root_item = by_path[os.path.normcase(str(root))]
    temp_item = by_path[os.path.normcase(str(temp))]
    image_item = by_path[os.path.normcase(str(system_images))]

    assert root_item.policy is CleanupPolicy.REPORT_ONLY
    assert not root_item.delete_root_itself
    assert temp_item.category is CleanupCategory.INSTALLERS_DOWNLOADS
    assert temp_item.policy is CleanupPolicy.VENDOR_MANAGED
    assert temp_item.delete_root_itself
    assert temp_item.application_rule is not None

    # The static scan catalog is discovery-only. Installed system images are
    # protected here, while the application semantic layer remains authoritative
    # KEEP for their actual payload files.
    assert image_item.category is CleanupCategory.ANDROID_SDK_PAYLOAD
    assert image_item.policy is CleanupPolicy.REPORT_ONLY
    image_rule = match_application_rule(system_images / "android-36" / "system.img", env)
    assert image_rule is not None
    assert image_rule.owner is DecisionOwner.KEEP


def test_android_sdk_process_guard_blocks_sdk_temp_but_not_android_studio_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, root = _layout(tmp_path)
    temp = root / "temp"
    temp.mkdir()

    monkeypatch.setattr(application_cleanup, "android_sdk_process_running", lambda: True)
    monkeypatch.setattr(application_cleanup, "android_studio_process_running", lambda: False)
    assert not application_cleanup.process_guard_allows(temp, env)

    monkeypatch.setattr(application_cleanup, "android_sdk_process_running", lambda: False)
    monkeypatch.setattr(application_cleanup, "android_studio_process_running", lambda: True)
    assert application_cleanup.process_guard_allows(temp, env)
