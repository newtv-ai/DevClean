from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path, PureWindowsPath

import pytest

from devclean.core.application_cleanup import (
    DecisionOwner,
    PolicyAction,
    application_scan_roots,
    audited_dynamic_tool_roots,
    evaluate_application_path,
    match_application_rule,
    process_guard_allows,
    whole_tree_application_rule,
)
from devclean.core.cleanup_catalog import CleanupPolicy, discover_known_cleanup_roots
from devclean.core.user_rules import default_rules
from devclean.core.visual_studio_cleanup import visual_studio_roots

_NOW = datetime(2026, 8, 18, tzinfo=UTC)


def _layout(tmp_path: Path) -> tuple[dict[str, str], Path]:
    temp = tmp_path / "Temp"
    logs = temp / "servicehub" / "logs"
    logs.mkdir(parents=True)
    (logs / "ServiceHub.Host.log").write_text("diagnostic", encoding="utf-8")
    return {"TEMP": str(temp)}, logs


def test_servicehub_logs_are_discovered_without_localappdata(tmp_path: Path) -> None:
    env, logs = _layout(tmp_path)

    roots = visual_studio_roots(env)

    assert roots.component_model_cache_roots == ()
    assert roots.roslyn_cache_roots == ()
    assert roots.web_tools_roots == ()
    assert roots.local_package_roots == ()
    assert roots.servicehub_log_roots == (PureWindowsPath(str(logs)),)
    assert PureWindowsPath(str(logs)) in application_scan_roots(env)


def test_servicehub_logs_use_exact_user_review_rule(tmp_path: Path) -> None:
    env, logs = _layout(tmp_path)

    rule = match_application_rule(logs / "ServiceHub.Host.log", env)

    assert rule is not None
    assert rule.rule_id == "visual-studio-servicehub-logs"
    assert rule.owner is DecisionOwner.USER
    assert rule.requires_process_closed
    assert not rule.allow_whole_tree
    assert match_application_rule(logs.parent / "state.json", env) is None
    assert match_application_rule(logs.parent / "logs-old" / "old.log", env) is None


@pytest.mark.parametrize(
    ("logical_size", "last_used"),
    (
        (1, _NOW),
        (64 * 1024**2, _NOW - timedelta(days=45)),
        (64 * 1024**2, None),
    ),
)
def test_servicehub_age_size_and_unknown_usage_never_create_tool_authority(
    tmp_path: Path,
    logical_size: int,
    last_used: datetime | None,
) -> None:
    env, logs = _layout(tmp_path)

    decision = evaluate_application_path(
        logs,
        logical_size=logical_size,
        last_used=last_used,
        now=_NOW,
        process_running=False,
        environment=env,
    )

    assert decision is not None
    assert decision.rule.owner is DecisionOwner.USER
    assert decision.action is PolicyAction.USER_DECISION


def test_servicehub_process_state_is_execution_guard_not_review_classification(
    tmp_path: Path,
) -> None:
    env, logs = _layout(tmp_path)

    decision = evaluate_application_path(
        logs,
        logical_size=64 * 1024**2,
        last_used=_NOW - timedelta(days=45),
        now=_NOW,
        process_running=True,
        environment=env,
    )

    assert decision is not None
    assert decision.action is PolicyAction.USER_DECISION


def test_servicehub_user_approved_mutation_requires_visual_studio_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, logs = _layout(tmp_path)

    monkeypatch.setattr(
        "devclean.core.application_cleanup.visual_studio_process_running",
        lambda: True,
    )
    assert not process_guard_allows(logs, env)

    monkeypatch.setattr(
        "devclean.core.application_cleanup.visual_studio_process_running",
        lambda: False,
    )
    assert process_guard_allows(logs, env)


def test_servicehub_logs_have_no_deterministic_whole_tree_authority(
    tmp_path: Path,
) -> None:
    env, logs = _layout(tmp_path)

    dynamic = dict(audited_dynamic_tool_roots(env))

    assert PureWindowsPath(str(logs)) not in dynamic
    assert whole_tree_application_rule(logs, env) is None
    assert whole_tree_application_rule(logs.parent, env) is None


def test_servicehub_logs_remain_visible_but_non_executable_in_catalog(
    tmp_path: Path,
) -> None:
    env, logs = _layout(tmp_path)

    discovered = discover_known_cleanup_roots(default_rules().scan, env)
    by_path = {os.path.normcase(str(item.path)): item for item in discovered}
    item = by_path[os.path.normcase(str(logs))]

    assert item.policy is CleanupPolicy.REPORT_ONLY
    assert not item.delete_root_itself
    assert item.application_rule is None


def test_servicehub_logs_fail_closed_without_temp() -> None:
    roots = visual_studio_roots({})

    assert roots.servicehub_log_roots == ()
