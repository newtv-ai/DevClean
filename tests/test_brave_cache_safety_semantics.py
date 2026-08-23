from __future__ import annotations

from datetime import UTC, datetime, timedelta

from devclean.core.application_cleanup import DecisionOwner, PolicyAction, evaluate_application_path

_NOW = datetime(2026, 8, 23, tzinfo=UTC)
_MIB = 1024**2


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


def test_fresh_tiny_brave_http_cache_is_still_safe_cleanup() -> None:
    decision = evaluate_application_path(
        (
            r"C:\Users\alice\AppData\Local\BraveSoftware\Brave-Browser\User Data"
            r"\Default\Cache\Cache_Data\f_001"
        ),
        logical_size=1,
        last_used=_NOW,
        now=_NOW,
        process_running=False,
        environment=_env(),
    )
    assert decision is not None
    assert decision.rule.owner is DecisionOwner.TOOL
    assert decision.rule.rule_id == "brave-http-cache"
    assert decision.action is PolicyAction.TOOL_DELETE


def test_brave_cache_with_unknown_last_use_remains_source_backed_safe() -> None:
    decision = evaluate_application_path(
        (
            r"C:\Users\alice\AppData\Local\BraveSoftware\Brave-Browser\User Data"
            r"\Profile 2\Code Cache\js\index"
        ),
        logical_size=4 * 1024,
        last_used=None,
        now=_NOW,
        process_running=False,
        environment=_env(),
    )
    assert decision is not None
    assert decision.rule.rule_id == "brave-code-cache"
    assert decision.action is PolicyAction.TOOL_DELETE


def test_fresh_explicit_brave_disk_cache_is_safe_cleanup() -> None:
    env = {**_env(), "DEVCLEAN_BRAVE_DISK_CACHE_DIR": r"E:\BraveDiskCache"}
    decision = evaluate_application_path(
        r"E:\BraveDiskCache\Cache_Data\f_001",
        logical_size=1,
        last_used=_NOW,
        now=_NOW,
        process_running=False,
        environment=env,
    )
    assert decision is not None
    assert decision.rule.rule_id == "brave-explicit-disk-cache"
    assert decision.action is PolicyAction.TOOL_DELETE


def test_brave_process_activity_still_blocks_cache_cleanup() -> None:
    decision = evaluate_application_path(
        (
            r"C:\Users\alice\AppData\Local\BraveSoftware\Brave-Browser\User Data"
            r"\Profile 2\GPUCache\data_0"
        ),
        logical_size=500 * _MIB,
        last_used=_NOW - timedelta(days=90),
        now=_NOW,
        process_running=True,
        environment=_env(),
    )
    assert decision is not None
    assert decision.rule.rule_id == "brave-profile-gpu-cache"
    assert decision.action is PolicyAction.TOOL_KEEP_IN_USE


def test_brave_profile_site_storage_and_updater_state_are_unchanged() -> None:
    site = evaluate_application_path(
        (
            r"C:\Users\alice\AppData\Local\BraveSoftware\Brave-Browser\User Data"
            r"\Default\Service Worker\CacheStorage\origin\data"
        ),
        logical_size=500 * _MIB,
        last_used=_NOW - timedelta(days=365),
        now=_NOW,
        process_running=False,
        environment=_env(),
    )
    history = evaluate_application_path(
        (
            r"C:\Users\alice\AppData\Local\BraveSoftware\Brave-Browser\User Data"
            r"\Default\History"
        ),
        logical_size=500 * _MIB,
        last_used=_NOW - timedelta(days=365),
        now=_NOW,
        process_running=False,
        environment=_env(),
    )
    updater = evaluate_application_path(
        (
            r"C:\Program Files (x86)\BraveSoftware\Update"
            r"\1.3.361.143\BraveUpdate.exe"
        ),
        logical_size=500 * _MIB,
        last_used=_NOW - timedelta(days=365),
        now=_NOW,
        process_running=False,
        environment=_env(),
    )
    assert site is not None
    assert site.rule.rule_id == "brave-site-cache-storage"
    assert site.action is PolicyAction.USER_DECISION
    assert history is not None
    assert history.rule.rule_id == "brave-profile-state"
    assert history.action is PolicyAction.KEEP_PROTECTED
    assert updater is not None
    assert updater.rule.rule_id == "brave-updater-state"
    assert updater.action is PolicyAction.KEEP_PROTECTED
