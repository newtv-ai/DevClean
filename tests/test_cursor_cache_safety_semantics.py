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
        "PROGRAMDATA": r"C:\ProgramData",
        "TEMP": r"C:\Users\alice\AppData\Local\Temp",
    }


def test_fresh_tiny_cursor_generated_cache_is_still_safe_cleanup() -> None:
    decision = evaluate_application_path(
        r"C:\Users\alice\AppData\Roaming\Cursor\Cache\data_0",
        logical_size=1,
        last_used=_NOW,
        now=_NOW,
        process_running=False,
        environment=_env(),
    )

    assert decision is not None
    assert decision.rule.owner is DecisionOwner.TOOL
    assert decision.rule.rule_id == "cursor-roaming-cache"
    assert decision.action is PolicyAction.TOOL_DELETE


def test_fresh_tiny_cursor_extension_package_cache_is_safe_cleanup() -> None:
    decision = evaluate_application_path(
        r"C:\Users\alice\AppData\Roaming\Cursor\CachedExtensionVSIXs\ext.vsix",
        logical_size=8 * 1024,
        last_used=_NOW,
        now=_NOW,
        process_running=False,
        environment=_env(),
    )

    assert decision is not None
    assert decision.rule.rule_id == "cursor-roaming-cached-extension-vsix"
    assert decision.action is PolicyAction.TOOL_DELETE


def test_cursor_generated_cache_is_offered_while_cursor_runs() -> None:
    decision = evaluate_application_path(
        r"C:\Users\alice\AppData\Roaming\Cursor\GPUCache\data_0",
        logical_size=500 * _MIB,
        last_used=_NOW - timedelta(days=90),
        now=_NOW,
        process_running=True,
        environment=_env(),
    )

    # User decision 2026-11-10/11: cleaned while the program runs; what it holds
    # open is skipped one by one at deletion.  Only what a running program would be
    # damaged by losing waits (application_cleanup._MUST_CLOSE_REASONS).
    assert decision is not None
    assert decision.rule.rule_id == "cursor-roaming-gpu-cache"
    assert decision.action is PolicyAction.TOOL_DELETE

    # An extension Cursor may be installing right now still waits for it.
    installing = evaluate_application_path(
        r"C:\Users\alice\AppData\Roaming\Cursor\CachedExtensionVSIXs\ext.vsix",
        logical_size=500 * _MIB,
        last_used=_NOW - timedelta(days=90),
        now=_NOW,
        process_running=True,
        environment=_env(),
    )
    assert installing is not None
    assert installing.rule.rule_id == "cursor-roaming-cached-extension-vsix"
    assert installing.action is PolicyAction.TOOL_KEEP_IN_USE


def test_cursor_diagnostics_are_not_broadened_into_unconditional_cache_cleanup() -> None:
    decision = evaluate_application_path(
        r"C:\Users\alice\AppData\Roaming\Cursor\Crashpad\reports\recent.dmp",
        logical_size=1,
        last_used=_NOW,
        now=_NOW,
        process_running=False,
        environment=_env(),
    )

    assert decision is not None
    assert decision.rule.rule_id == "cursor-roaming-crashpad-reports"
    assert decision.action is not PolicyAction.TOOL_DELETE
