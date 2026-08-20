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
from devclean.core.chrome_cleanup import chrome_roots
from devclean.core.cleanup_catalog import (
    CleanupCategory,
    CleanupPolicy,
    discover_known_cleanup_roots,
)
from devclean.core.user_rules import default_rules

_NOW = datetime(2026, 8, 16, tzinfo=UTC)
_MIB = 1024**2


def _env() -> dict[str, str]:
    return {
        "USERPROFILE": r"C:\Users\alice",
        "APPDATA": r"C:\Users\alice\AppData\Roaming",
        "LOCALAPPDATA": r"C:\Users\alice\AppData\Local",
        "TEMP": r"C:\Users\alice\AppData\Local\Temp",
        "ProgramFiles(x86)": r"C:\Program Files (x86)",
    }


def test_chrome_default_channel_and_updater_roots_are_discovered() -> None:
    roots = chrome_roots(_env())
    expected = {
        PureWindowsPath(r"C:\Users\alice\AppData\Local\Google\Chrome\User Data"),
        PureWindowsPath(
            r"C:\Users\alice\AppData\Local\Google\Chrome Beta\User Data"
        ),
        PureWindowsPath(
            r"C:\Users\alice\AppData\Local\Google\Chrome Dev\User Data"
        ),
        PureWindowsPath(
            r"C:\Users\alice\AppData\Local\Google\Chrome SxS\User Data"
        ),
        PureWindowsPath(
            r"C:\Users\alice\AppData\Local\Google\Chrome for Testing\User Data"
        ),
        PureWindowsPath(r"C:\Users\alice\AppData\Local\Chromium\User Data"),
    }
    assert expected.issubset(set(roots.data_roots))
    assert PureWindowsPath(
        r"C:\Users\alice\AppData\Local\Google\GoogleUpdater"
    ) in roots.updater_roots
    assert PureWindowsPath(
        r"C:\Program Files (x86)\Google\GoogleUpdater"
    ) in roots.updater_roots
    assert PureWindowsPath(
        r"C:\Users\alice\AppData\Local\Google\Update"
    ) in roots.legacy_updater_roots


def test_chrome_profile_caches_are_tool_but_authoritative_profile_state_is_keep() -> None:
    tool_paths = {
        (
            r"C:\Users\alice\AppData\Local\Google\Chrome\User Data\Default"
            r"\Cache\Cache_Data\f_001"
        ): "chrome-http-cache",
        (
            r"C:\Users\alice\AppData\Local\Google\Chrome\User Data\Profile 2"
            r"\Code Cache\js\index"
        ): "chrome-code-cache",
        (
            r"C:\Users\alice\AppData\Local\Google\Chrome\User Data\Default"
            r"\GPUCache\data_0"
        ): "chrome-profile-gpu-cache",
        (
            r"C:\Users\alice\AppData\Local\Google\Chrome\User Data"
            r"\ShaderCache\GPUCache\data_0"
        ): "chrome-shader-cache",
        (
            r"C:\Users\alice\AppData\Local\Google\Chrome\User Data"
            r"\component_crx_cache\abc.crx"
        ): "chrome-component-crx-cache",
    }
    for path, rule_id in tool_paths.items():
        rule = match_application_rule(path, _env())
        assert rule is not None
        assert rule.owner is DecisionOwner.TOOL
        assert rule.rule_id == rule_id

    protected = (
        r"C:\Users\alice\AppData\Local\Google\Chrome\User Data\Default\History",
        r"C:\Users\alice\AppData\Local\Google\Chrome\User Data\Default\Cookies",
        (
            r"C:\Users\alice\AppData\Local\Google\Chrome\User Data\Default"
            r"\Login Data"
        ),
        (
            r"C:\Users\alice\AppData\Local\Google\Chrome\User Data\Default"
            r"\Preferences"
        ),
        (
            r"C:\Users\alice\AppData\Local\Google\Chrome\User Data\Default"
            r"\IndexedDB\https_example.indexeddb.leveldb\000003.log"
        ),
        (
            r"C:\Users\alice\AppData\Local\Google\Chrome\User Data\Default"
            r"\Extensions\abcdefghijklmnop\1.0\manifest.json"
        ),
    )
    for path in protected:
        rule = match_application_rule(path, _env())
        assert rule is not None
        assert rule.rule_id == "chrome-profile-state"
        assert rule.owner is DecisionOwner.KEEP
        assert not process_guard_allows(path, _env())


def test_chrome_cache_storage_is_user_data_but_script_cache_is_tool() -> None:
    cache_storage = (
        r"C:\Users\alice\AppData\Local\Google\Chrome\User Data\Default"
        r"\Service Worker\CacheStorage\https_example\data"
    )
    script_cache = (
        r"C:\Users\alice\AppData\Local\Google\Chrome\User Data\Default"
        r"\Service Worker\ScriptCache\0123456789abcdef_0"
    )
    registration = (
        r"C:\Users\alice\AppData\Local\Google\Chrome\User Data\Default"
        r"\Service Worker\Database\000003.log"
    )

    site_rule = match_application_rule(cache_storage, _env())
    script_rule = match_application_rule(script_cache, _env())
    state_rule = match_application_rule(registration, _env())
    assert site_rule is not None
    assert site_rule.rule_id == "chrome-site-cache-storage"
    assert site_rule.owner is DecisionOwner.USER
    assert script_rule is not None
    assert script_rule.rule_id == "chrome-service-worker-script-cache"
    assert script_rule.owner is DecisionOwner.TOOL
    assert state_rule is not None
    assert state_rule.rule_id == "chrome-service-worker-state"
    assert state_rule.owner is DecisionOwner.KEEP

    site_decision = evaluate_application_path(
        cache_storage,
        logical_size=128 * _MIB,
        last_used=_NOW - timedelta(days=90),
        now=_NOW,
        process_running=False,
        environment=_env(),
    )
    script_decision = evaluate_application_path(
        script_cache,
        logical_size=128 * _MIB,
        last_used=_NOW - timedelta(days=90),
        now=_NOW,
        process_running=False,
        environment=_env(),
    )
    assert site_decision is not None
    assert site_decision.action is PolicyAction.KEEP_PROTECTED
    assert script_decision is not None
    assert script_decision.action is PolicyAction.TOOL_DELETE
    assert whole_tree_application_rule(
        r"C:\Users\alice\AppData\Local\Google\Chrome\User Data\Default"
        r"\Service Worker\CacheStorage",
        _env(),
    ) is None


def test_arbitrary_nested_cache_name_does_not_gain_chrome_deletion_authority() -> None:
    path = (
        r"C:\Users\alice\AppData\Local\Google\Chrome\User Data"
        r"\Not A Profile\Cache\unique.db"
    )
    rule = match_application_rule(path, _env())
    assert rule is not None
    assert rule.rule_id == "chrome-user-data-state"
    assert rule.owner is DecisionOwner.KEEP


def test_explicit_disk_cache_is_a_dedicated_tool_root() -> None:
    env = {**_env(), "DEVCLEAN_CHROME_DISK_CACHE_DIR": r"D:\ChromeDiskCache"}
    rule = match_application_rule(r"D:\ChromeDiskCache\Cache_Data\f_001", env)
    assert rule is not None
    assert rule.rule_id == "chrome-explicit-disk-cache"
    assert rule.owner is DecisionOwner.TOOL
    whole = whole_tree_application_rule(r"D:\ChromeDiskCache", env)
    assert whole is not None
    assert whole.rule_id == "chrome-explicit-disk-cache"


def test_google_updater_only_delegates_download_cache_and_logs() -> None:
    base = r"C:\Users\alice\AppData\Local\Google\GoogleUpdater"
    cache = match_application_rule(base + r"\crx_cache\chrome\payload.crx3", _env())
    prefs = match_application_rule(base + r"\prefs.json", _env())
    binary = match_application_rule(base + r"\140.0.0.0\updater.exe", _env())
    assert cache is not None
    assert cache.rule_id == "chrome-updater-crx-cache"
    assert cache.owner is DecisionOwner.TOOL
    assert prefs is not None and prefs.rule_id == "chrome-updater-state"
    assert prefs.owner is DecisionOwner.KEEP
    assert binary is not None and binary.rule_id == "chrome-updater-state"
    assert binary.owner is DecisionOwner.KEEP

    legacy = match_application_rule(
        r"C:\Users\alice\AppData\Local\Google\Update\Download\package.bin",
        _env(),
    )
    assert legacy is not None
    assert legacy.rule_id == "chrome-legacy-updater-state"
    assert legacy.owner is DecisionOwner.KEEP


def test_chrome_whole_tree_authority_is_exact_and_catalogued(tmp_path: Path) -> None:
    local = tmp_path / "Local"
    data = local / "Google" / "Chrome" / "User Data"
    profile = data / "Default"
    cache = profile / "Cache"
    history = profile / "History"
    updater_cache = local / "Google" / "GoogleUpdater" / "crx_cache"
    cache.mkdir(parents=True)
    updater_cache.mkdir(parents=True)
    history.write_text("history", encoding="utf-8")
    env = {
        "USERPROFILE": str(tmp_path / "home"),
        "APPDATA": str(tmp_path / "Roaming"),
        "LOCALAPPDATA": str(local),
        "TEMP": str(tmp_path / "Temp"),
        "ProgramFiles(x86)": str(tmp_path / "Program Files (x86)"),
    }

    assert whole_tree_application_rule(cache, env) is not None
    assert whole_tree_application_rule(profile, env) is None
    assert whole_tree_application_rule(data, env) is None
    assert whole_tree_application_rule(updater_cache, env) is not None

    rules = default_rules()
    discovered = discover_known_cleanup_roots(rules.scan, env)
    by_path = {os.path.normcase(str(root.path)): root for root in discovered}
    cache_root = by_path[os.path.normcase(str(cache))]
    updater_root = by_path[os.path.normcase(str(updater_cache))]
    data_root = by_path[os.path.normcase(str(data))]
    assert cache_root.category is CleanupCategory.BROWSER_CACHE
    assert cache_root.policy is CleanupPolicy.VENDOR_MANAGED
    assert cache_root.delete_root_itself
    assert updater_root.category is CleanupCategory.INSTALLERS_DOWNLOADS
    assert updater_root.policy is CleanupPolicy.VENDOR_MANAGED
    assert data_root.policy is CleanupPolicy.REPORT_ONLY
    assert not data_root.delete_root_itself


def test_chrome_cache_process_guard_rechecks_live_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "devclean.core.application_cleanup.chrome_process_running",
        lambda: True,
    )
    cache_file = (
        r"C:\Users\alice\AppData\Local\Google\Chrome\User Data\Default"
        r"\Cache\Cache_Data\f_001"
    )
    assert not process_guard_allows(cache_file, _env())


def test_chrome_scan_roots_include_user_data_and_updater_without_whole_tree_authority() -> None:
    scan = set(application_scan_roots(_env()))
    data = PureWindowsPath(r"C:\Users\alice\AppData\Local\Google\Chrome\User Data")
    updater = PureWindowsPath(
        r"C:\Users\alice\AppData\Local\Google\GoogleUpdater"
    )
    assert data in scan
    assert updater in scan
    assert whole_tree_application_rule(data, _env()) is None
    assert whole_tree_application_rule(updater, _env()) is None
