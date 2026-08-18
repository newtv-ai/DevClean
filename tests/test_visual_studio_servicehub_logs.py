from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path, PureWindowsPath

from devclean.core.application_cleanup import (
    DecisionOwner,
    PolicyAction,
    application_scan_roots,
    audited_dynamic_tool_roots,
    evaluate_application_path,
    match_application_rule,
    whole_tree_application_rule,
)
from devclean.core.cleanup_catalog import (
    CleanupCategory,
    CleanupPolicy,
    discover_known_cleanup_roots,
)
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


def test_servicehub_logs_use_exact_tool_rule(tmp_path: Path) -> None:
    env, logs = _layout(tmp_path)

    rule = match_application_rule(logs / "ServiceHub.Host.log", env)

    assert rule is not None
    assert rule.rule_id == "visual-studio-servicehub-logs"
    assert rule.owner is DecisionOwner.TOOL
    assert match_application_rule(logs.parent / "state.json", env) is None
    assert match_application_rule(logs.parent / "logs-old" / "old.log", env) is None


def test_old_large_servicehub_logs_are_delegated(tmp_path: Path) -> None:
    env, logs = _layout(tmp_path)

    decision = evaluate_application_path(
        logs,
        logical_size=64 * 1024**2,
        last_used=_NOW - timedelta(days=45),
        now=_NOW,
        process_running=False,
        environment=env,
    )

    assert decision is not None
    assert decision.action is PolicyAction.TOOL_DELETE


def test_recent_servicehub_logs_stay_for_diagnostics(tmp_path: Path) -> None:
    env, logs = _layout(tmp_path)

    decision = evaluate_application_path(
        logs,
        logical_size=64 * 1024**2,
        last_used=_NOW - timedelta(days=2),
        now=_NOW,
        process_running=False,
        environment=env,
    )

    assert decision is not None
    assert decision.action is PolicyAction.TOOL_KEEP_RECENT


def test_servicehub_logs_stay_while_visual_studio_is_in_use(tmp_path: Path) -> None:
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
    assert decision.action is PolicyAction.TOOL_KEEP_IN_USE


def test_servicehub_logs_have_exact_whole_tree_authority(tmp_path: Path) -> None:
    env, logs = _layout(tmp_path)

    dynamic = dict(audited_dynamic_tool_roots(env))

    assert PureWindowsPath(str(logs)) in dynamic
    rule = whole_tree_application_rule(logs, env)
    assert rule is not None
    assert rule.rule_id == "visual-studio-servicehub-logs"
    assert whole_tree_application_rule(logs.parent, env) is None


def test_servicehub_logs_are_catalogued_as_vendor_managed_system_logs(
    tmp_path: Path,
) -> None:
    env, logs = _layout(tmp_path)

    discovered = discover_known_cleanup_roots(default_rules().scan, env)
    by_path = {os.path.normcase(str(item.path)): item for item in discovered}
    item = by_path[os.path.normcase(str(logs))]

    assert item.category is CleanupCategory.SYSTEM_LOGS
    assert item.policy is CleanupPolicy.VENDOR_MANAGED
    assert item.delete_root_itself
    assert item.application_rule is not None
    assert item.application_rule.rule_id == "visual-studio-servicehub-logs"


def test_servicehub_logs_fail_closed_without_temp() -> None:
    roots = visual_studio_roots({})

    assert roots.servicehub_log_roots == ()
