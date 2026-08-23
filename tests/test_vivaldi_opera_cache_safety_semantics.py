from __future__ import annotations

from datetime import UTC, datetime, timedelta

from devclean.core.application_cleanup import PolicyAction, evaluate_application_path

_NOW = datetime(2026, 8, 23, tzinfo=UTC)
_MIB = 1024**2


def _env() -> dict[str, str]:
    return {
        "USERPROFILE": r"C:\Users\alice",
        "APPDATA": r"C:\Users\alice\AppData\Roaming",
        "LOCALAPPDATA": r"C:\Users\alice\AppData\Local",
        "TEMP": r"C:\Users\alice\AppData\Local\Temp",
    }


def test_fresh_tiny_vivaldi_http_cache_is_still_safe_cleanup() -> None:
    decision = evaluate_application_path(
        (
            r"C:\Users\alice\AppData\Local\Vivaldi\User Data\Default"
            r"\Cache\Cache_Data\f_001"
        ),
        logical_size=1,
        last_used=_NOW,
        now=_NOW,
        process_running=False,
        environment=_env(),
    )
    assert decision is not None
    assert decision.rule.rule_id == "vivaldi-http-cache"
    assert decision.action is PolicyAction.TOOL_DELETE


def test_vivaldi_unknown_last_use_and_explicit_cache_remain_safe() -> None:
    code = evaluate_application_path(
        (
            r"C:\Users\alice\AppData\Local\Vivaldi\User Data\Profile 2"
            r"\Code Cache\js\index"
        ),
        logical_size=4 * 1024,
        last_used=None,
        now=_NOW,
        process_running=False,
        environment=_env(),
    )
    env = {**_env(), "DEVCLEAN_VIVALDI_DISK_CACHE_DIR": r"E:\VivaldiCache"}
    explicit = evaluate_application_path(
        r"E:\VivaldiCache\Cache_Data\f_001",
        logical_size=1,
        last_used=_NOW,
        now=_NOW,
        process_running=False,
        environment=env,
    )
    assert code is not None
    assert code.rule.rule_id == "vivaldi-code-cache"
    assert code.action is PolicyAction.TOOL_DELETE
    assert explicit is not None
    assert explicit.rule.rule_id == "vivaldi-explicit-disk-cache"
    assert explicit.action is PolicyAction.TOOL_DELETE


def test_vivaldi_process_and_protected_boundaries_are_unchanged() -> None:
    running = evaluate_application_path(
        (
            r"C:\Users\alice\AppData\Local\Vivaldi\User Data\Default"
            r"\GPUCache\data_0"
        ),
        logical_size=500 * _MIB,
        last_used=_NOW - timedelta(days=90),
        now=_NOW,
        process_running=True,
        environment=_env(),
    )
    site = evaluate_application_path(
        (
            r"C:\Users\alice\AppData\Local\Vivaldi\User Data\Default"
            r"\Service Worker\CacheStorage\origin\data"
        ),
        logical_size=500 * _MIB,
        last_used=_NOW - timedelta(days=365),
        now=_NOW,
        process_running=False,
        environment=_env(),
    )
    crash = evaluate_application_path(
        (
            r"C:\Users\alice\AppData\Local\Vivaldi\User Data"
            r"\Crashpad\reports\crash.dmp"
        ),
        logical_size=500 * _MIB,
        last_used=_NOW - timedelta(days=3650),
        now=_NOW,
        process_running=False,
        environment=_env(),
    )
    assert running is not None
    assert running.action is PolicyAction.TOOL_KEEP_IN_USE
    assert site is not None
    assert site.rule.rule_id == "vivaldi-site-cache-storage"
    assert site.action is PolicyAction.USER_DECISION
    assert crash is not None
    assert crash.rule.rule_id == "vivaldi-crashpad-reports"
    assert crash.action is PolicyAction.KEEP_PROTECTED


def test_fresh_tiny_opera_local_http_cache_is_still_safe_cleanup() -> None:
    decision = evaluate_application_path(
        (
            r"C:\Users\alice\AppData\Local\Opera Software\Opera Stable\Default"
            r"\Cache\Cache_Data\f_001"
        ),
        logical_size=1,
        last_used=_NOW,
        now=_NOW,
        process_running=False,
        environment=_env(),
    )
    assert decision is not None
    assert decision.rule.rule_id == "opera-http-cache"
    assert decision.action is PolicyAction.TOOL_DELETE


def test_opera_roaming_generated_cache_and_explicit_cache_remain_safe() -> None:
    code = evaluate_application_path(
        (
            r"C:\Users\alice\AppData\Roaming\Opera Software\Opera Stable\Default"
            r"\Code Cache\js\index"
        ),
        logical_size=4 * 1024,
        last_used=None,
        now=_NOW,
        process_running=False,
        environment=_env(),
    )
    env = {**_env(), "DEVCLEAN_OPERA_DISK_CACHE_DIR": r"E:\OperaCache"}
    explicit = evaluate_application_path(
        r"E:\OperaCache\Cache_Data\f_001",
        logical_size=1,
        last_used=_NOW,
        now=_NOW,
        process_running=False,
        environment=env,
    )
    assert code is not None
    assert code.rule.rule_id == "opera-code-cache"
    assert code.action is PolicyAction.TOOL_DELETE
    assert explicit is not None
    assert explicit.rule.rule_id == "opera-explicit-disk-cache"
    assert explicit.action is PolicyAction.TOOL_DELETE


def test_opera_process_system_cache_and_recovery_boundaries_are_unchanged() -> None:
    running = evaluate_application_path(
        (
            r"C:\Users\alice\AppData\Local\Opera Software\Opera GX Stable\Default"
            r"\GPUCache\data_0"
        ),
        logical_size=500 * _MIB,
        last_used=_NOW - timedelta(days=90),
        now=_NOW,
        process_running=True,
        environment=_env(),
    )
    system_cache = evaluate_application_path(
        (
            r"C:\Users\alice\AppData\Local\Opera Software\Opera Stable\Default"
            r"\System Cache\Cache_Data\data_0"
        ),
        logical_size=500 * _MIB,
        last_used=_NOW - timedelta(days=3650),
        now=_NOW,
        process_running=False,
        environment=_env(),
    )
    recovery = evaluate_application_path(
        (
            r"C:\Users\alice\AppData\Roaming\Opera Software\Opera Stable"
            r"\Default.old\Sessions\Session_123"
        ),
        logical_size=500 * _MIB,
        last_used=_NOW - timedelta(days=3650),
        now=_NOW,
        process_running=False,
        environment=_env(),
    )
    assert running is not None
    assert running.action is PolicyAction.TOOL_KEEP_IN_USE
    assert system_cache is not None
    assert system_cache.rule.rule_id == "opera-system-cache"
    assert system_cache.action is PolicyAction.KEEP_PROTECTED
    assert recovery is not None
    assert recovery.rule.rule_id == "opera-profile-recovery-copy"
    assert recovery.action is PolicyAction.USER_DECISION
