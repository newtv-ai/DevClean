from __future__ import annotations

import os
from pathlib import Path, PureWindowsPath

import pytest

from devclean.core.application_cleanup import (
    DecisionOwner,
    application_scan_roots,
    match_application_rule,
    process_guard_allows,
    whole_tree_application_rule,
)
from devclean.core.brave_cleanup import brave_roots
from devclean.core.cleanup_catalog import (
    CleanupCategory,
    CleanupPolicy,
    discover_known_cleanup_roots,
)
from devclean.core.user_rules import default_rules


def _env() -> dict[str, str]:
    return {
        "USERPROFILE": r"C:\Users\alice",
        "APPDATA": r"C:\Users\alice\AppData\Roaming",
        "LOCALAPPDATA": r"C:\Users\alice\AppData\Local",
        "TEMP": r"C:\Users\alice\AppData\Local\Temp",
        "ProgramFiles": r"C:\Program Files",
        "ProgramFiles(x86)": r"C:\Program Files (x86)",
        "ProgramData": r"C:\ProgramData",
    }


def test_brave_channel_and_updater_roots_are_discovered() -> None:
    roots = brave_roots(_env())
    expected = {
        PureWindowsPath(
            r"C:\Users\alice\AppData\Local\BraveSoftware"
            r"\Brave-Browser\User Data"
        ),
        PureWindowsPath(
            r"C:\Users\alice\AppData\Local\BraveSoftware"
            r"\Brave-Browser-Beta\User Data"
        ),
        PureWindowsPath(
            r"C:\Users\alice\AppData\Local\BraveSoftware"
            r"\Brave-Browser-Nightly\User Data"
        ),
    }
    assert expected.issubset(set(roots.data_roots))
    assert PureWindowsPath(
        r"C:\Users\alice\AppData\Local\BraveSoftware\Update"
    ) in roots.updater_roots
    assert PureWindowsPath(
        r"C:\Program Files (x86)\BraveSoftware\Update"
    ) in roots.updater_roots
    assert PureWindowsPath(
        r"C:\ProgramData\BraveSoftware\Update"
    ) in roots.updater_roots


def test_brave_chromium_cache_and_profile_boundaries() -> None:
    cache = (
        r"C:\Users\alice\AppData\Local\BraveSoftware\Brave-Browser\User Data"
        r"\Default\Cache\Cache_Data\f_001"
    )
    cache_storage = (
        r"C:\Users\alice\AppData\Local\BraveSoftware\Brave-Browser\User Data"
        r"\Default\Service Worker\CacheStorage\origin\data"
    )
    history = (
        r"C:\Users\alice\AppData\Local\BraveSoftware\Brave-Browser\User Data"
        r"\Default\History"
    )

    cache_rule = match_application_rule(cache, _env())
    cache_storage_rule = match_application_rule(cache_storage, _env())
    history_rule = match_application_rule(history, _env())

    assert cache_rule is not None
    assert cache_rule.rule_id == "brave-http-cache"
    assert cache_rule.owner is DecisionOwner.TOOL
    assert cache_storage_rule is not None
    assert cache_storage_rule.rule_id == "brave-site-cache-storage"
    assert cache_storage_rule.owner is DecisionOwner.USER
    assert cache_storage_rule.user_age_buckets == (30, 90, 180)
    assert history_rule is not None
    assert history_rule.rule_id == "brave-profile-state"
    assert history_rule.owner is DecisionOwner.KEEP


def test_brave_updater_install_staging_is_tool_but_updater_versions_are_keep() -> None:
    base = r"C:\Program Files (x86)\BraveSoftware\Update"
    staged = base + r"\Install\{A}\brave_installer-delta-x64.exe"
    current = base + r"\1.3.361.143\BraveUpdate.exe"
    log = r"C:\ProgramData\BraveSoftware\Update\Log\BraveUpdate.log"

    staged_rule = match_application_rule(staged, _env())
    current_rule = match_application_rule(current, _env())
    log_rule = match_application_rule(log, _env())

    assert staged_rule is not None
    assert staged_rule.rule_id == "brave-updater-install-staging"
    assert staged_rule.owner is DecisionOwner.TOOL
    assert current_rule is not None
    assert current_rule.rule_id == "brave-updater-state"
    assert current_rule.owner is DecisionOwner.KEEP
    assert log_rule is not None
    assert log_rule.rule_id == "brave-update-log"
    assert log_rule.owner is DecisionOwner.TOOL

    assert whole_tree_application_rule(base + r"\Install", _env()) is not None
    assert whole_tree_application_rule(base, _env()) is None
    assert whole_tree_application_rule(base + r"\1.3.361.143", _env()) is None


def test_brave_whole_tree_roots_are_catalogued_precisely(tmp_path: Path) -> None:
    local = tmp_path / "Local"
    data = local / "BraveSoftware" / "Brave-Browser" / "User Data"
    profile = data / "Default"
    cache = profile / "Cache"
    updater = tmp_path / "PF86" / "BraveSoftware" / "Update"
    install = updater / "Install"
    cache.mkdir(parents=True)
    install.mkdir(parents=True)

    env = {
        "USERPROFILE": str(tmp_path / "home"),
        "APPDATA": str(tmp_path / "Roaming"),
        "LOCALAPPDATA": str(local),
        "TEMP": str(tmp_path / "Temp"),
        "ProgramFiles": str(tmp_path / "PF"),
        "ProgramFiles(x86)": str(tmp_path / "PF86"),
        "ProgramData": str(tmp_path / "ProgramData"),
    }
    roots = discover_known_cleanup_roots(default_rules().scan, env)
    by_path = {os.path.normcase(str(root.path)): root for root in roots}

    cache_root = by_path[os.path.normcase(str(cache))]
    install_root = by_path[os.path.normcase(str(install))]
    data_root = by_path[os.path.normcase(str(data))]
    updater_root = by_path[os.path.normcase(str(updater))]

    assert cache_root.category is CleanupCategory.BROWSER_CACHE
    assert cache_root.policy is CleanupPolicy.VENDOR_MANAGED
    assert cache_root.delete_root_itself
    assert install_root.category is CleanupCategory.INSTALLERS_DOWNLOADS
    assert install_root.policy is CleanupPolicy.VENDOR_MANAGED
    assert install_root.delete_root_itself
    assert data_root.policy is CleanupPolicy.REPORT_ONLY
    assert not data_root.delete_root_itself
    assert updater_root.policy is CleanupPolicy.REPORT_ONLY
    assert not updater_root.delete_root_itself


def test_brave_process_guard_blocks_cache_and_staging_while_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "devclean.core.application_cleanup.brave_process_running",
        lambda: True,
    )
    cache = (
        r"C:\Users\alice\AppData\Local\BraveSoftware\Brave-Browser\User Data"
        r"\Default\Cache\Cache_Data\f_001"
    )
    staged = (
        r"C:\Program Files (x86)\BraveSoftware\Update"
        r"\Install\{A}\brave_installer.exe"
    )
    assert not process_guard_allows(cache, _env())
    assert not process_guard_allows(staged, _env())


def test_brave_scan_roots_do_not_grant_parent_deletion_authority() -> None:
    scan = set(application_scan_roots(_env()))
    data = PureWindowsPath(
        r"C:\Users\alice\AppData\Local\BraveSoftware"
        r"\Brave-Browser\User Data"
    )
    updater = PureWindowsPath(r"C:\Program Files (x86)\BraveSoftware\Update")
    assert data in scan
    assert updater in scan
    assert whole_tree_application_rule(data, _env()) is None
    assert whole_tree_application_rule(updater, _env()) is None


def test_brave_devclean_explicit_disk_cache_is_a_dedicated_tool_root() -> None:
    env = {**_env(), "DEVCLEAN_BRAVE_DISK_CACHE_DIR": r"D:\BraveCache"}
    path = r"D:\BraveCache\Cache_Data\f_001"
    rule = match_application_rule(path, env)
    assert rule is not None
    assert rule.rule_id == "brave-explicit-disk-cache"
    assert rule.owner is DecisionOwner.TOOL
    whole = whole_tree_application_rule(r"D:\BraveCache", env)
    assert whole is not None
    assert whole.rule_id == "brave-explicit-disk-cache"
