from __future__ import annotations

from datetime import UTC, datetime, timedelta

from devclean.core.application_cleanup import DecisionOwner, PolicyAction, evaluate_application_path
from devclean.core.trae_cleanup import trae_roots

_NOW = datetime(2026, 8, 16, tzinfo=UTC)
_MIB = 1024**2


def test_trae_cn_root_uses_same_default_deny_semantics() -> None:
    env = {
        "USERPROFILE": r"C:\Users\alice",
        "APPDATA": r"C:\Users\alice\AppData\Roaming",
        "LOCALAPPDATA": r"C:\Users\alice\AppData\Local",
    }
    cache = evaluate_application_path(
        r"C:\Users\alice\AppData\Roaming\Trae CN\Cache\data_0",
        logical_size=100 * _MIB,
        last_used=_NOW - timedelta(days=20),
        now=_NOW,
        process_running=False,
        environment=env,
    )
    unknown = evaluate_application_path(
        r"C:\Users\alice\AppData\Roaming\Trae CN\AIState\index.db",
        logical_size=500 * _MIB,
        last_used=_NOW - timedelta(days=200),
        now=_NOW,
        process_running=False,
        environment=env,
    )
    assert cache is not None
    assert cache.rule.owner is DecisionOwner.TOOL
    assert cache.action is PolicyAction.TOOL_DELETE
    assert unknown is not None
    assert unknown.rule.owner is DecisionOwner.KEEP
    assert unknown.action is PolicyAction.KEEP_PROTECTED


def test_explicit_trae_root_keeps_extensions_but_cleans_cache() -> None:
    env = {
        "USERPROFILE": r"C:\Users\alice",
        "APPDATA": r"C:\Users\alice\AppData\Roaming",
        "LOCALAPPDATA": r"C:\Users\alice\AppData\Local",
        "TRAE_USER_DATA_DIR": r"D:\TraeState",
        "TRAE_EXTENSIONS_DIR": r"E:\TraeExtensions",
    }
    roots = trae_roots(env)
    assert str(roots.data_roots[0]) == r"D:\TraeState"
    assert str(roots.extension_roots[0]) == r"E:\TraeExtensions"

    cache = evaluate_application_path(
        r"D:\TraeState\GPUCache\data_0",
        logical_size=100 * _MIB,
        last_used=_NOW - timedelta(days=20),
        now=_NOW,
        process_running=False,
        environment=env,
    )
    extension = evaluate_application_path(
        r"E:\TraeExtensions\publisher.ext\extension.js",
        logical_size=100 * _MIB,
        last_used=_NOW - timedelta(days=200),
        now=_NOW,
        process_running=False,
        environment=env,
    )
    assert cache is not None and cache.rule.owner is DecisionOwner.TOOL
    assert extension is not None and extension.rule.owner is DecisionOwner.KEEP
