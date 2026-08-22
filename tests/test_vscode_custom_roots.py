from __future__ import annotations

from datetime import UTC, datetime, timedelta

from devclean.core.application_cleanup import DecisionOwner, PolicyAction, evaluate_application_path
from devclean.core.vscode_cleanup import vscode_roots

_NOW = datetime(2026, 8, 16, tzinfo=UTC)
_MIB = 1024**2


def test_explicit_roots_apply_when_portable_mode_is_not_active() -> None:
    env = {
        "USERPROFILE": r"C:\Users\alice",
        "APPDATA": r"C:\Users\alice\AppData\Roaming",
        "VSCODE_USER_DATA_DIR": r"D:\VSCodeState",
        "VSCODE_EXTENSIONS_DIR": r"E:\VSCodeExtensions",
    }
    roots = vscode_roots(env)
    assert str(roots.data_roots[0]) == r"D:\VSCodeState"
    assert str(roots.extension_roots[0]) == r"E:\VSCodeExtensions"

    cache = evaluate_application_path(
        r"D:\VSCodeState\Cache\data_0",
        logical_size=100 * _MIB,
        last_used=_NOW - timedelta(days=20),
        now=_NOW,
        process_running=False,
        environment=env,
    )
    extension = evaluate_application_path(
        r"E:\VSCodeExtensions\publisher.ext-1.2.3\extension.js",
        logical_size=50 * _MIB,
        last_used=_NOW - timedelta(days=200),
        now=_NOW,
        process_running=False,
        environment=env,
    )
    assert cache is not None
    assert cache.rule.owner is DecisionOwner.TOOL
    assert cache.action is PolicyAction.TOOL_DELETE
    assert extension is not None
    assert extension.rule.owner is DecisionOwner.KEEP
    assert extension.action is PolicyAction.KEEP_PROTECTED


def test_insiders_workspace_state_is_user_owned() -> None:
    env = {
        "USERPROFILE": r"C:\Users\alice",
        "APPDATA": r"C:\Users\alice\AppData\Roaming",
    }
    decision = evaluate_application_path(
        r"C:\Users\alice\AppData\Roaming\Code - Insiders\User"
        r"\workspaceStorage\abc\chatSessions\thread.jsonl",
        logical_size=200 * _MIB,
        last_used=_NOW - timedelta(days=120),
        now=_NOW,
        process_running=False,
        environment=env,
    )
    assert decision is not None
    assert decision.rule.owner is DecisionOwner.USER
    assert decision.action is PolicyAction.USER_DECISION
