from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path, PureWindowsPath

import pytest

from devclean.core import opera_cleanup
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
from devclean.core.opera_cleanup import opera_roots
from devclean.core.user_rules import default_rules

_NOW = datetime(2026, 8, 16, tzinfo=UTC)
_MIB = 1024**2


def _env() -> dict[str, str]:
    return {
        "USERPROFILE": r"C:\Users\alice",
        "APPDATA": r"C:\Users\alice\AppData\Roaming",
        "LOCALAPPDATA": r"C:\Users\alice\AppData\Local",
        "TEMP": r"C:\Users\alice\AppData\Local\Temp",
    }


def test_opera_stable_developer_and_gx_roots_are_split_roaming_local() -> None:
    roots = opera_roots(_env())
    assert PureWindowsPath(
        r"C:\Users\alice\AppData\Roaming\Opera Software\Opera Stable"
    ) in roots.roaming_roots
    assert PureWindowsPath(
        r"C:\Users\alice\AppData\Roaming\Opera Software\Opera Developer"
    ) in roots.roaming_roots
    assert PureWindowsPath(
        r"C:\Users\alice\AppData\Roaming\Opera Software\Opera GX Stable"
    ) in roots.roaming_roots
    assert PureWindowsPath(
        r"C:\Users\alice\AppData\Local\Opera Software\Opera Stable"
    ) in roots.local_roots


def test_opera_roaming_profile_is_authoritative_but_generated_code_cache_is_tool() -> None:
    base = r"C:\Users\alice\AppData\Roaming\Opera Software\Opera Stable\Default"
    history = base + r"\History"
    sessions = base + r"\Sessions\Session_123"
    local_storage = base + r"\Local Storage\leveldb\000003.log"
    cache_storage = base + r"\Service Worker\CacheStorage\origin\data"
    script_cache = base + r"\Service Worker\ScriptCache\index"
    code_cache = base + r"\Code Cache\js\index"

    for protected in (history, sessions, local_storage):
        rule = match_application_rule(protected, _env())
        assert rule is not None
        assert rule.owner is DecisionOwner.KEEP

    cache_storage_rule = match_application_rule(cache_storage, _env())
    assert cache_storage_rule is not None
    assert cache_storage_rule.rule_id == "opera-site-cache-storage"
    assert cache_storage_rule.owner is DecisionOwner.USER
    assert cache_storage_rule.user_age_buckets == (30, 90, 180)

    script_rule = match_application_rule(script_cache, _env())
    code_rule = match_application_rule(code_cache, _env())
    assert script_rule is not None and script_rule.owner is DecisionOwner.TOOL
    assert script_rule.rule_id == "opera-service-worker-script-cache"
    assert code_rule is not None and code_rule.owner is DecisionOwner.TOOL
    assert code_rule.rule_id == "opera-code-cache"

    projected = evaluate_application_path(
        cache_storage,
        logical_size=512 * _MIB,
        last_used=_NOW - timedelta(days=180),
        now=_NOW,
        process_running=False,
        environment=_env(),
    )
    assert projected is not None
    assert projected.action is PolicyAction.KEEP_PROTECTED


def test_opera_local_cache_supports_legacy_and_default_layouts() -> None:
    root = r"C:\Users\alice\AppData\Local\Opera Software\Opera Stable"
    paths = {
        root + r"\Cache\Cache_Data\f_001": "opera-http-cache",
        root + r"\System Cache\Cache_Data\data_0": "opera-system-cache",
        root + r"\Default\Cache\Cache_Data\f_002": "opera-http-cache",
        root + r"\Default\System Cache\Cache_Data\data_1": "opera-system-cache",
        root + r"\Default\GPUCache\data_0": "opera-profile-gpu-cache",
    }
    for path, rule_id in paths.items():
        rule = match_application_rule(path, _env())
        assert rule is not None
        assert rule.owner is DecisionOwner.TOOL
        assert rule.rule_id == rule_id

    unknown = match_application_rule(root + r"\Default\mystery.db", _env())
    assert unknown is not None
    assert unknown.owner is DecisionOwner.KEEP


def test_opera_profile_recovery_copy_never_becomes_tool() -> None:
    path = (
        r"C:\Users\alice\AppData\Roaming\Opera Software\Opera Stable"
        r"\Default.old\Sessions\Session_123"
    )
    rule = match_application_rule(path, _env())
    assert rule is not None
    assert rule.rule_id == "opera-profile-recovery-copy"
    assert rule.owner is DecisionOwner.USER
    assert rule.user_age_buckets == (30, 90, 180)
    assert not process_guard_allows(path, _env())

    numbered = (
        r"C:\Users\alice\AppData\Roaming\Opera Software\Opera Stable"
        r"\Default.old1\Cache\Cache_Data\f_001"
    )
    numbered_rule = match_application_rule(numbered, _env())
    assert numbered_rule is not None
    # Until the recovery-pattern matcher is widened, the broad KEEP fallback
    # still prevents deletion. It must never inherit nested Cache authority.
    assert numbered_rule.owner is not DecisionOwner.TOOL


def test_opera_explicit_disk_cache_is_exact_tool_root() -> None:
    env = {**_env(), "DEVCLEAN_OPERA_DISK_CACHE_DIR": r"D:\OperaCache"}
    rule = match_application_rule(r"D:\OperaCache\Cache_Data\f_001", env)
    assert rule is not None
    assert rule.rule_id == "opera-explicit-disk-cache"
    assert rule.owner is DecisionOwner.TOOL
    whole = whole_tree_application_rule(r"D:\OperaCache", env)
    assert whole is not None
    assert whole.rule_id == "opera-explicit-disk-cache"


def test_opera_portable_profile_data_root_is_detected_when_present(tmp_path: Path) -> None:
    install = tmp_path / "OperaPortable"
    data = install / "profile" / "data"
    data.mkdir(parents=True)
    exe = install / "launcher.exe"
    assert opera_cleanup._portable_data_root(str(exe)) == PureWindowsPath(str(data))


def test_opera_catalog_grants_only_exact_cache_roots(tmp_path: Path) -> None:
    roaming = tmp_path / "Roaming"
    local = tmp_path / "Local"
    roaming_root = roaming / "Opera Software" / "Opera Stable"
    local_root = local / "Opera Software" / "Opera Stable"
    profile = roaming_root / "Default"
    local_profile = local_root / "Default"
    code_cache = profile / "Code Cache"
    http_cache = local_profile / "Cache"
    system_cache = local_profile / "System Cache"
    code_cache.mkdir(parents=True)
    http_cache.mkdir(parents=True)
    system_cache.mkdir(parents=True)
    (profile / "History").write_text("history", encoding="utf-8")

    env = {
        "USERPROFILE": str(tmp_path / "home"),
        "APPDATA": str(roaming),
        "LOCALAPPDATA": str(local),
        "TEMP": str(tmp_path / "Temp"),
    }
    roots = discover_known_cleanup_roots(default_rules().scan, env)
    by_path = {os.path.normcase(str(root.path)): root for root in roots}

    for cache in (code_cache, http_cache, system_cache):
        item = by_path[os.path.normcase(str(cache))]
        assert item.category is CleanupCategory.BROWSER_CACHE
        assert item.policy is CleanupPolicy.VENDOR_MANAGED
        assert item.delete_root_itself

    assert by_path[os.path.normcase(str(roaming_root))].policy is CleanupPolicy.REPORT_ONLY
    assert by_path[os.path.normcase(str(local_root))].policy is CleanupPolicy.REPORT_ONLY
    assert whole_tree_application_rule(roaming_root, env) is None
    assert whole_tree_application_rule(local_root, env) is None
    assert whole_tree_application_rule(profile, env) is None


def test_opera_process_guard_rechecks_live_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "devclean.core.application_cleanup.opera_process_running",
        lambda: True,
    )
    cache = (
        r"C:\Users\alice\AppData\Local\Opera Software\Opera GX Stable"
        r"\Default\Cache\Cache_Data\f_001"
    )
    assert not process_guard_allows(cache, _env())


def test_opera_scan_roots_include_roaming_and_local_without_parent_authority() -> None:
    scan = set(application_scan_roots(_env()))
    roaming = PureWindowsPath(
        r"C:\Users\alice\AppData\Roaming\Opera Software\Opera GX Stable"
    )
    local = PureWindowsPath(
        r"C:\Users\alice\AppData\Local\Opera Software\Opera GX Stable"
    )
    assert roaming in scan
    assert local in scan
    assert whole_tree_application_rule(roaming, _env()) is None
    assert whole_tree_application_rule(local, _env()) is None
