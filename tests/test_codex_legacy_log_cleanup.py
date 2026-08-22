from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

import devclean.core.application_cleanup as application_cleanup
from devclean.core.application_cleanup import (
    DecisionOwner,
    PolicyAction,
    evaluate_application_path,
    match_application_rule,
    process_guard_allows,
)


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        "USERPROFILE": str(tmp_path / "home"),
        "LOCALAPPDATA": str(tmp_path / "Local"),
    }


def test_codex_legacy_tui_log_is_exact_tool_owned_cleanup(tmp_path: Path) -> None:
    env = _env(tmp_path)
    path = tmp_path / "home" / ".codex" / "log" / "codex-tui.log"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"log")

    rule = match_application_rule(path, env)

    assert rule is not None
    assert rule.rule_id == "codex-legacy-tui-log"
    assert rule.owner is DecisionOwner.TOOL
    assert rule.idle_days == 0
    assert rule.min_reclaim_bytes == 0
    assert rule.requires_process_closed


def test_fresh_tiny_legacy_tui_log_is_safe_without_benefit_gate(tmp_path: Path) -> None:
    env = _env(tmp_path)
    path = tmp_path / "home" / ".codex" / "log" / "codex-tui.log"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"x")
    now = datetime.now(UTC)

    decision = evaluate_application_path(
        path,
        logical_size=1,
        last_used=now,
        now=now,
        process_running=False,
        environment=env,
    )

    assert decision is not None
    assert decision.rule.rule_id == "codex-legacy-tui-log"
    assert decision.action is PolicyAction.TOOL_DELETE


def test_live_codex_process_blocks_legacy_tui_log_execution(tmp_path: Path) -> None:
    env = _env(tmp_path)
    path = tmp_path / "home" / ".codex" / "log" / "codex-tui.log"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"x")
    now = datetime.now(UTC)

    decision = evaluate_application_path(
        path,
        logical_size=1024**3,
        last_used=now,
        now=now,
        process_running=True,
        environment=env,
    )

    assert decision is not None
    assert decision.action is PolicyAction.TOOL_KEEP_IN_USE


def test_process_guard_rechecks_codex_before_legacy_log_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = _env(tmp_path)
    path = tmp_path / "home" / ".codex" / "log" / "codex-tui.log"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"x")

    monkeypatch.setattr(application_cleanup, "application_process_running", lambda _app: True)
    assert not process_guard_allows(path, env)

    monkeypatch.setattr(application_cleanup, "application_process_running", lambda _app: False)
    assert process_guard_allows(path, env)


def test_codex_log_rule_does_not_broaden_other_state(tmp_path: Path) -> None:
    env = _env(tmp_path)
    codex_home = tmp_path / "home" / ".codex"

    other_log = match_application_rule(codex_home / "log" / "custom.log", env)
    state_db = match_application_rule(codex_home / "state_5.sqlite", env)
    sessions = match_application_rule(
        codex_home / "sessions" / "2026" / "08" / "23" / "rollout.jsonl",
        env,
    )

    assert other_log is None
    assert state_db is not None and state_db.owner is DecisionOwner.KEEP
    assert sessions is not None and sessions.owner is DecisionOwner.USER
