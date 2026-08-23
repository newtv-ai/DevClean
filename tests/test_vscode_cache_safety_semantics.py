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
    }


def test_fresh_tiny_vscode_generated_cache_is_still_safe_cleanup() -> None:
    decision = evaluate_application_path(
        r"C:\Users\alice\AppData\Roaming\Code\Code Cache\js\entry",
        logical_size=1,
        last_used=_NOW,
        now=_NOW,
        process_running=False,
        environment=_env(),
    )

    assert decision is not None
    assert decision.rule.owner is DecisionOwner.TOOL
    assert decision.rule.rule_id == "vscode-code-cache"
    assert decision.action is PolicyAction.TOOL_DELETE


def test_fresh_tiny_vscode_extension_package_cache_is_safe_cleanup() -> None:
    decision = evaluate_application_path(
        r"C:\Users\alice\AppData\Roaming\Code\CachedExtensionVSIXs\ext.vsix",
        logical_size=8 * 1024,
        last_used=_NOW,
        now=_NOW,
        process_running=False,
        environment=_env(),
    )

    assert decision is not None
    assert decision.rule.rule_id == "vscode-extension-vsix-cache"
    assert decision.action is PolicyAction.TOOL_DELETE


def test_fresh_vscode_remote_wsl_download_cache_is_safe_cleanup() -> None:
    decision = evaluate_application_path(
        r"C:\Users\alice\vscode-remote-wsl\stable\abc123\server.tar.gz",
        logical_size=2 * _MIB,
        last_used=_NOW,
        now=_NOW,
        process_running=False,
        environment=_env(),
    )

    assert decision is not None
    assert decision.rule.rule_id == "vscode-wsl-server-download-cache"
    assert decision.action is PolicyAction.TOOL_DELETE


def test_vscode_process_activity_still_blocks_generated_cache_cleanup() -> None:
    decision = evaluate_application_path(
        r"C:\Users\alice\AppData\Roaming\Code\GPUCache\data_0",
        logical_size=500 * _MIB,
        last_used=_NOW - timedelta(days=90),
        now=_NOW,
        process_running=True,
        environment=_env(),
    )

    assert decision is not None
    assert decision.rule.rule_id == "vscode-gpu-cache"
    assert decision.action is PolicyAction.TOOL_KEEP_IN_USE


def test_vscode_diagnostics_and_portable_tmp_keep_separate_retention_semantics() -> None:
    crash = evaluate_application_path(
        r"C:\Users\alice\AppData\Roaming\Code\Crashpad\reports\recent.dmp",
        logical_size=1,
        last_used=_NOW,
        now=_NOW,
        process_running=False,
        environment=_env(),
    )
    portable = evaluate_application_path(
        r"E:\PortableCode\data\tmp\session\scratch.bin",
        logical_size=100 * _MIB,
        last_used=_NOW - timedelta(minutes=2),
        now=_NOW,
        process_running=False,
        environment={**_env(), "VSCODE_PORTABLE": r"E:\PortableCode\data"},
    )

    assert crash is not None
    assert crash.rule.rule_id == "vscode-crashpad-reports"
    assert crash.action is not PolicyAction.TOOL_DELETE
    assert portable is not None
    assert portable.rule.rule_id == "vscode-portable-temp"
    assert portable.action is PolicyAction.TOOL_KEEP_RECENT
