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
        "PROGRAMDATA": r"C:\ProgramData",
        "TEMP": r"C:\Users\alice\AppData\Local\Temp",
    }


def test_fresh_tiny_firefox_cache2_is_still_safe_cleanup() -> None:
    decision = evaluate_application_path(
        (
            r"C:\Users\alice\AppData\Roaming\Mozilla\Firefox\Profiles"
            r"\abc.default-release\cache2\entries\abcdef"
        ),
        logical_size=1,
        last_used=_NOW,
        now=_NOW,
        process_running=False,
        environment=_env(),
    )
    assert decision is not None
    assert decision.rule.rule_id == "firefox-cache2"
    assert decision.action is PolicyAction.TOOL_DELETE


def test_firefox_cache_with_unknown_last_use_remains_source_backed_safe() -> None:
    decision = evaluate_application_path(
        (
            r"C:\Users\alice\AppData\Roaming\Mozilla\Firefox\Profiles"
            r"\abc.default-release\startupCache\startupCache.8.little"
        ),
        logical_size=4 * 1024,
        last_used=None,
        now=_NOW,
        process_running=False,
        environment=_env(),
    )
    assert decision is not None
    assert decision.rule.rule_id == "firefox-startup-cache"
    assert decision.action is PolicyAction.TOOL_DELETE


def test_fresh_tiny_firefox_profld_root_is_still_safe_cleanup() -> None:
    decision = evaluate_application_path(
        (
            r"C:\Users\alice\AppData\Local\Mozilla\Firefox\Profiles"
            r"\abc.default-release"
        ),
        logical_size=1,
        last_used=_NOW,
        now=_NOW,
        process_running=False,
        environment=_env(),
    )
    assert decision is not None
    assert decision.rule.rule_id == "firefox-local-profile-cache-root"
    assert decision.action is PolicyAction.TOOL_DELETE


def test_firefox_process_activity_still_blocks_cache_cleanup() -> None:
    decision = evaluate_application_path(
        (
            r"C:\Users\alice\AppData\Roaming\Mozilla\Firefox\Profiles"
            r"\abc.default-release\jumpListCache\favicon.ico"
        ),
        logical_size=500 * _MIB,
        last_used=_NOW - timedelta(days=90),
        now=_NOW,
        process_running=True,
        environment=_env(),
    )
    assert decision is not None
    assert decision.rule.rule_id == "firefox-jumplist-cache"
    assert decision.action is PolicyAction.TOOL_KEEP_IN_USE


def test_firefox_persistent_crash_and_update_state_are_unchanged() -> None:
    profile = evaluate_application_path(
        (
            r"C:\Users\alice\AppData\Roaming\Mozilla\Firefox\Profiles"
            r"\abc.default-release\places.sqlite"
        ),
        logical_size=500 * _MIB,
        last_used=_NOW - timedelta(days=3650),
        now=_NOW,
        process_running=False,
        environment=_env(),
    )
    crash = evaluate_application_path(
        (
            r"C:\Users\alice\AppData\Roaming\Mozilla\Firefox\Crash Reports"
            r"\pending\01234567-89ab-cdef-0123-456789abcdef.dmp"
        ),
        logical_size=500 * _MIB,
        last_used=_NOW - timedelta(days=3650),
        now=_NOW,
        process_running=False,
        environment=_env(),
    )
    update = evaluate_application_path(
        r"C:\ProgramData\Mozilla\updates\install-hash\updates\0\update.mar",
        logical_size=500 * _MIB,
        last_used=_NOW - timedelta(days=3650),
        now=_NOW,
        process_running=False,
        environment=_env(),
    )
    assert profile is not None
    assert profile.rule.rule_id == "firefox-persistent-profile-state"
    assert profile.action is PolicyAction.KEEP_PROTECTED
    assert crash is not None
    assert crash.rule.rule_id == "firefox-pending-crash-reports"
    assert crash.action is PolicyAction.KEEP_PROTECTED
    assert update is not None
    assert update.rule.rule_id == "firefox-update-state"
    assert update.action is PolicyAction.KEEP_PROTECTED
