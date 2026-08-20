from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path, PureWindowsPath

import pytest

from devclean.core import vivaldi_cleanup
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
from devclean.core.user_rules import default_rules
from devclean.core.vivaldi_cleanup import vivaldi_roots

_NOW = datetime(2026, 8, 20, tzinfo=UTC)
_MIB = 1024**2


def _env() -> dict[str, str]:
    return {
        "USERPROFILE": r"C:\Users\alice",
        "APPDATA": r"C:\Users\alice\AppData\Roaming",
        "LOCALAPPDATA": r"C:\Users\alice\AppData\Local",
        "TEMP": r"C:\Users\alice\AppData\Local\Temp",
    }


def test_vivaldi_default_user_data_root_is_discovered() -> None:
    roots = vivaldi_roots(_env())
    assert PureWindowsPath(r"C:\Users\alice\AppData\Local\Vivaldi\User Data") in roots.data_roots


def test_vivaldi_chromium_cache_user_data_and_crash_boundaries() -> None:
    base = r"C:\Users\alice\AppData\Local\Vivaldi\User Data"
    cache = base + r"\Default\Cache\Cache_Data\f_001"
    cache_storage = base + r"\Default\Service Worker\CacheStorage\origin\data"
    script_cache = base + r"\Default\Service Worker\ScriptCache\index"
    history = base + r"\Default\History"
    crash = base + r"\Crashpad\reports\crash.dmp"

    cache_rule = match_application_rule(cache, _env())
    cache_storage_rule = match_application_rule(cache_storage, _env())
    script_rule = match_application_rule(script_cache, _env())
    history_rule = match_application_rule(history, _env())
    crash_rule = match_application_rule(crash, _env())

    assert cache_rule is not None
    assert cache_rule.rule_id == "vivaldi-http-cache"
    assert cache_rule.owner is DecisionOwner.TOOL
    assert cache_storage_rule is not None
    assert cache_storage_rule.rule_id == "vivaldi-site-cache-storage"
    assert cache_storage_rule.owner is DecisionOwner.USER
    assert cache_storage_rule.user_age_buckets == (30, 90, 180)
    assert script_rule is not None
    assert script_rule.rule_id == "vivaldi-service-worker-script-cache"
    assert script_rule.owner is DecisionOwner.TOOL
    assert history_rule is not None
    assert history_rule.rule_id == "vivaldi-profile-state"
    assert history_rule.owner is DecisionOwner.KEEP
    assert crash_rule is not None
    assert crash_rule.rule_id == "vivaldi-crashpad-reports"
    assert crash_rule.owner is DecisionOwner.KEEP

    projected = evaluate_application_path(
        cache_storage,
        logical_size=256 * _MIB,
        last_used=_NOW - timedelta(days=180),
        now=_NOW,
        process_running=False,
        environment=_env(),
    )
    assert projected is not None
    assert projected.action is PolicyAction.KEEP_PROTECTED


def test_vivaldi_crash_reports_stay_protected_regardless_of_age_or_size() -> None:
    crash = (
        r"C:\Users\alice\AppData\Local\Vivaldi\User Data"
        r"\Crashpad\reports\crash.dmp"
    )
    decision = evaluate_application_path(
        crash,
        logical_size=512 * _MIB,
        last_used=_NOW - timedelta(days=3650),
        now=_NOW,
        process_running=False,
        environment=_env(),
    )
    assert decision is not None
    assert decision.action is PolicyAction.KEEP_PROTECTED
    assert (
        whole_tree_application_rule(
            r"C:\Users\alice\AppData\Local\Vivaldi\User Data\Crashpad\reports",
            _env(),
        )
        is None
    )


def test_vivaldi_standalone_user_data_root_is_detected_from_install_layout(
    tmp_path: Path,
) -> None:
    install = tmp_path / "Portable Vivaldi"
    user_data = install / "User Data"
    user_data.mkdir(parents=True)
    executable = install / "Application" / "vivaldi.exe"

    detected = vivaldi_cleanup._standalone_user_data_root(str(executable))
    assert detected == PureWindowsPath(str(user_data))


def test_vivaldi_explicit_disk_cache_is_an_exact_tool_root() -> None:
    env = {**_env(), "DEVCLEAN_VIVALDI_DISK_CACHE_DIR": r"D:\VivaldiCache"}
    path = r"D:\VivaldiCache\Cache_Data\f_001"
    rule = match_application_rule(path, env)
    assert rule is not None
    assert rule.rule_id == "vivaldi-explicit-disk-cache"
    assert rule.owner is DecisionOwner.TOOL
    whole = whole_tree_application_rule(r"D:\VivaldiCache", env)
    assert whole is not None
    assert whole.rule_id == "vivaldi-explicit-disk-cache"


def test_vivaldi_whole_tree_roots_are_catalogued_precisely(tmp_path: Path) -> None:
    local = tmp_path / "Local"
    data = local / "Vivaldi" / "User Data"
    profile = data / "Default"
    cache = profile / "Cache"
    crash = data / "Crashpad" / "reports"
    cache.mkdir(parents=True)
    crash.mkdir(parents=True)
    (profile / "History").write_text("history", encoding="utf-8")

    env = {
        "USERPROFILE": str(tmp_path / "home"),
        "APPDATA": str(tmp_path / "Roaming"),
        "LOCALAPPDATA": str(local),
        "TEMP": str(tmp_path / "Temp"),
    }
    roots = discover_known_cleanup_roots(default_rules().scan, env)
    by_path = {os.path.normcase(str(root.path)): root for root in roots}

    cache_root = by_path[os.path.normcase(str(cache))]
    data_root = by_path[os.path.normcase(str(data))]

    assert cache_root.category is CleanupCategory.BROWSER_CACHE
    assert cache_root.policy is CleanupPolicy.VENDOR_MANAGED
    assert cache_root.delete_root_itself
    assert os.path.normcase(str(crash)) not in by_path
    assert data_root.policy is CleanupPolicy.REPORT_ONLY
    assert not data_root.delete_root_itself
    assert whole_tree_application_rule(crash, env) is None
    assert whole_tree_application_rule(data, env) is None
    assert whole_tree_application_rule(profile, env) is None


def test_vivaldi_process_guard_rechecks_live_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "devclean.core.application_cleanup.vivaldi_process_running",
        lambda: True,
    )
    cache = (
        r"C:\Users\alice\AppData\Local\Vivaldi\User Data"
        r"\Default\Cache\Cache_Data\f_001"
    )
    assert not process_guard_allows(cache, _env())


def test_vivaldi_scan_root_does_not_grant_parent_delete_authority() -> None:
    data = PureWindowsPath(r"C:\Users\alice\AppData\Local\Vivaldi\User Data")
    assert data in set(application_scan_roots(_env()))
    assert whole_tree_application_rule(data, _env()) is None
