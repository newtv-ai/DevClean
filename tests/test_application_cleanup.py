from __future__ import annotations

from datetime import UTC, datetime, timedelta

from devclean.core.application_cleanup import (
    DecisionOwner,
    LastUseStrategy,
    PolicyAction,
    effective_idle_days,
    evaluate_application_path,
    match_application_rule,
)

_ENV = {
    "USERPROFILE": r"C:\Users\alice",
    "LOCALAPPDATA": r"C:\Users\alice\AppData\Local",
}
_NOW = datetime(2026, 8, 16, tzinfo=UTC)


def test_codex_cache_is_tool_decided_but_recent_download_is_kept() -> None:
    path = r"C:\Users\alice\.codex\.tmp\plugins\openai\skill\payload.bin"

    recent = evaluate_application_path(
        path,
        logical_size=700 * 1024**2,
        last_used=_NOW - timedelta(days=5),
        now=_NOW,
        process_running=False,
        environment=_ENV,
    )
    stale = evaluate_application_path(
        path,
        logical_size=700 * 1024**2,
        last_used=_NOW - timedelta(days=45),
        now=_NOW,
        process_running=False,
        environment=_ENV,
    )

    assert recent is not None and stale is not None
    assert recent.rule.owner is DecisionOwner.TOOL
    assert recent.action is PolicyAction.TOOL_KEEP_RECENT
    assert stale.action is PolicyAction.TOOL_DELETE
    assert stale.benefit_score > recent.benefit_score


def test_large_reclaim_shortens_idle_threshold_without_changing_safety() -> None:
    path = r"C:\Users\alice\.codex\.tmp\plugins\marketplace.pack"
    rule = match_application_rule(path, _ENV)

    assert rule is not None
    assert effective_idle_days(rule, 100 * 1024**2) == 30
    assert effective_idle_days(rule, 2 * 1024**3) == 21
    assert effective_idle_days(rule, 8 * 1024**3) == 14
    assert effective_idle_days(rule, 25 * 1024**3) == 7


def test_rebuildable_but_tiny_catalog_is_not_worth_churning() -> None:
    decision = evaluate_application_path(
        r"C:\Users\alice\.codex\models_cache.json",
        logical_size=80 * 1024,
        last_used=_NOW - timedelta(days=90),
        now=_NOW,
        process_running=False,
        environment=_ENV,
    )

    assert decision is not None
    assert decision.rule.owner is DecisionOwner.TOOL
    assert decision.action is PolicyAction.TOOL_KEEP_LOW_BENEFIT


def test_process_guard_overrides_age_and_reclaim_value() -> None:
    decision = evaluate_application_path(
        r"C:\Users\alice\.codex\logs_2.sqlite",
        logical_size=4 * 1024**3,
        last_used=_NOW - timedelta(days=90),
        now=_NOW,
        process_running=True,
        environment=_ENV,
    )

    assert decision is not None
    assert decision.action is PolicyAction.TOOL_KEEP_IN_USE
    assert decision.requires_process_closed


def test_codex_session_history_is_always_user_decided_and_bucketed() -> None:
    path = (
        r"C:\Users\alice\.codex\sessions\2026\01\01"
        r"\rollout-2026-01-01T10-00-00.jsonl"
    )
    decision = evaluate_application_path(
        path,
        logical_size=50 * 1024**2,
        last_used=_NOW - timedelta(days=120),
        now=_NOW,
        process_running=False,
        environment=_ENV,
    )

    assert decision is not None
    assert decision.rule.owner is DecisionOwner.USER
    assert decision.rule.last_use is LastUseStrategy.SESSION_LAST_EVENT
    assert decision.action is PolicyAction.USER_DECISION
    assert decision.age_bucket == "90-180d"


def test_recent_session_is_still_user_decided_not_tool_deleted() -> None:
    path = (
        r"C:\Users\alice\.codex\sessions\2026\08\15"
        r"\rollout-2026-08-15T10-00-00.jsonl"
    )
    decision = evaluate_application_path(
        path,
        logical_size=10 * 1024**2,
        last_used=_NOW - timedelta(days=1),
        now=_NOW,
        process_running=False,
        environment=_ENV,
    )

    assert decision is not None
    assert decision.action is PolicyAction.USER_DECISION
    assert decision.age_bucket == "0-30d"


def test_codex_input_history_is_user_owned() -> None:
    decision = evaluate_application_path(
        r"C:\Users\alice\.codex\history.jsonl",
        logical_size=20 * 1024**2,
        last_used=_NOW - timedelta(days=200),
        now=_NOW,
        environment=_ENV,
    )

    assert decision is not None
    assert decision.rule.owner is DecisionOwner.USER
    assert decision.rule.last_use is LastUseStrategy.JSONL_RECORD_TS
    assert decision.age_bucket == ">=180d"


def test_codex_persistent_state_beats_generic_cache_name() -> None:
    decision = evaluate_application_path(
        r"C:\Users\alice\.codex\plugins\cache\vendor\plugin\1.2.3\plugin.json",
        logical_size=500 * 1024**2,
        last_used=_NOW - timedelta(days=200),
        now=_NOW,
        environment=_ENV,
    )

    assert decision is not None
    assert decision.rule.owner is DecisionOwner.KEEP
    assert decision.action is PolicyAction.KEEP_PROTECTED


def test_codex_state_databases_are_protected() -> None:
    for name in (
        "state_5.sqlite",
        "state_5.sqlite-wal",
        "goals_1.sqlite",
        "memories_1.sqlite",
        "queue_1.sqlite",
        "thread_history_1.sqlite-shm",
    ):
        decision = evaluate_application_path(
            rf"C:\Users\alice\.codex\{name}",
            logical_size=2 * 1024**3,
            last_used=_NOW - timedelta(days=365),
            now=_NOW,
            environment=_ENV,
        )
        assert decision is not None, name
        assert decision.action is PolicyAction.KEEP_PROTECTED, name


def test_codex_desktop_cache_matches_descendants() -> None:
    decision = evaluate_application_path(
        (
            r"C:\Users\alice\AppData\Local\Packages\OpenAI.Codex_2p2nqsd0c76g0"
            r"\LocalCache\Roaming\Codex\web\Codex\Default\Code Cache\js\index.bin"
        ),
        logical_size=300 * 1024**2,
        last_used=_NOW - timedelta(days=45),
        now=_NOW,
        process_running=False,
        environment=_ENV,
    )

    assert decision is not None
    assert decision.rule.owner is DecisionOwner.TOOL
    assert decision.action is PolicyAction.TOOL_DELETE


def test_codex_home_override_is_honoured() -> None:
    env = {**_ENV, "CODEX_HOME": r"D:\CodexState"}
    decision = evaluate_application_path(
        r"D:\CodexState\history.jsonl",
        logical_size=1,
        last_used=_NOW,
        now=_NOW,
        environment=env,
    )

    assert decision is not None
    assert decision.rule.owner is DecisionOwner.USER
