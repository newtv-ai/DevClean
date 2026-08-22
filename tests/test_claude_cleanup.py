from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path, PureWindowsPath

import pytest

from devclean.core.application_cleanup import (
    DecisionOwner,
    PolicyAction,
    application_roots,
    application_scan_roots,
    evaluate_application_path,
    match_application_rule,
    process_guard_allows,
)
from devclean.core.claude_cleanup import claude_roots
from devclean.core.cleanup_catalog import CleanupPolicy, discover_known_cleanup_roots
from devclean.core.user_rules import default_rules

_NOW = datetime(2026, 8, 16, tzinfo=UTC)
_MIB = 1024**2
_GIB = 1024**3


def _env() -> dict[str, str]:
    return {
        "USERPROFILE": r"C:\Users\alice",
        "TEMP": r"D:\Temp",
        "CLAUDE_CONFIG_DIR": r"D:\ClaudeState",
        "CLAUDE_CODE_TMPDIR": r"E:\Scratch",
        "CLAUDE_CODE_PLUGIN_CACHE_DIR": r"F:\ClaudePlugins",
    }


def test_claude_redirected_roots_are_resolved_and_scannable() -> None:
    env = _env()
    roots = claude_roots(env)
    assert roots.config == PureWindowsPath(r"D:\ClaudeState")
    assert roots.fallback_config == PureWindowsPath(r"C:\Users\alice\.claude")
    assert roots.temp == PureWindowsPath(r"E:\Scratch\claude")
    assert roots.plugins == PureWindowsPath(r"F:\ClaudePlugins")
    assert roots.profile == PureWindowsPath(r"C:\Users\alice")

    root_map = {root.key: root.path for root in application_roots(env)}
    assert root_map["CLAUDE_HOME"] == roots.config
    assert root_map["CLAUDE_TEMP"] == roots.temp
    assert root_map["CLAUDE_PLUGINS"] == roots.plugins
    assert roots.config in application_scan_roots(env)
    assert roots.fallback_config in application_scan_roots(env)
    assert roots.temp in application_scan_roots(env)


def test_default_claude_home_is_not_duplicated_as_fallback() -> None:
    env = {
        "USERPROFILE": r"C:\Users\alice",
        "TEMP": r"D:\Temp",
    }
    roots = claude_roots(env)
    assert roots.config == PureWindowsPath(r"C:\Users\alice\.claude")
    assert roots.fallback_config is None
    assert application_scan_roots(env).count(roots.config) == 1


def test_claude_tool_cache_uses_idle_benefit_and_process_guard() -> None:
    env = _env()
    old = _NOW - timedelta(days=20)
    path = r"D:\ClaudeState\image-cache\large-image.bin"
    decision = evaluate_application_path(
        path,
        logical_size=500 * _MIB,
        last_used=old,
        now=_NOW,
        process_running=False,
        environment=env,
    )
    assert decision is not None
    assert decision.rule.owner is DecisionOwner.TOOL
    assert decision.rule.app_id == "claude"
    assert decision.action is PolicyAction.TOOL_DELETE

    running = evaluate_application_path(
        path,
        logical_size=500 * _MIB,
        last_used=old,
        now=_NOW,
        process_running=True,
        environment=env,
    )
    assert running is not None
    assert running.action is PolicyAction.TOOL_KEEP_IN_USE


def test_claude_temp_catches_huge_stopped_output_but_not_recent_writes() -> None:
    env = _env()
    path = r"E:\Scratch\claude\D--work\session\tasks\runaway.output"
    stopped = evaluate_application_path(
        path,
        logical_size=50 * _GIB,
        last_used=_NOW - timedelta(minutes=30),
        now=_NOW,
        process_running=False,
        environment=env,
    )
    assert stopped is not None
    assert stopped.rule.rule_id == "claude-temp-scratch"
    assert stopped.action is PolicyAction.TOOL_DELETE

    active = evaluate_application_path(
        path,
        logical_size=50 * _GIB,
        last_used=_NOW - timedelta(minutes=2),
        now=_NOW,
        process_running=False,
        environment=env,
    )
    assert active is not None
    assert active.action is PolicyAction.TOOL_KEEP_RECENT


def test_claude_user_history_is_a_real_user_decision() -> None:
    env = _env()
    paths = (
        r"D:\ClaudeState\projects\D--work\session.jsonl",
        r"D:\ClaudeState\file-history\session\file.txt",
        r"D:\ClaudeState\paste-cache\large-paste.txt",
        r"D:\ClaudeState\history.jsonl",
        r"D:\ClaudeState\stats-cache.json",
        r"D:\ClaudeState\usage-data\report.html",
        r"D:\ClaudeState\feedback-bundles\bug-report.zip",
    )
    for path in paths:
        decision = evaluate_application_path(
            path,
            logical_size=50 * _MIB,
            last_used=_NOW - timedelta(days=120),
            now=_NOW,
            process_running=False,
            environment=env,
        )
        assert decision is not None
        assert decision.rule.owner is DecisionOwner.USER
        assert decision.action is PolicyAction.USER_DECISION


def test_claude_documented_regenerable_state_is_tool_owned() -> None:
    env = _env()
    expected = {
        r"D:\ClaudeState\remote-settings.json": "claude-remote-settings-cache",
        r"D:\ClaudeState\policy-limits.json": "claude-policy-limits-cache",
        r"D:\ClaudeState\cache\changelog.md": "claude-cache",
        r"D:\ClaudeState\statsig\cached-evaluations.json": "claude-legacy-statsig",
        r"D:\ClaudeState\logs\old.log": "claude-legacy-logs",
        r"D:\ClaudeState\todos\old-session.json": "claude-legacy-todos",
        r"D:\ClaudeState\sessions\12345.json": "claude-session-locks",
        r"D:\ClaudeState\daemon.log": "claude-daemon-log",
    }
    for path, rule_id in expected.items():
        rule = match_application_rule(path, env)
        assert rule is not None
        assert rule.owner is DecisionOwner.TOOL
        assert rule.rule_id == rule_id


def test_claude_fallback_home_catches_stray_cache_when_config_is_redirected() -> None:
    env = _env()
    path = r"C:\Users\alice\.claude\statsig\stale-cache.json"
    rule = match_application_rule(path, env)
    assert rule is not None
    assert rule.rule_id == "claude-legacy-statsig"
    assert rule.owner is DecisionOwner.TOOL

    plugin = match_application_rule(
        r"C:\Users\alice\.claude\plugins\cache\market\plugin\1.0\index.js",
        env,
    )
    assert plugin is not None
    assert plugin.rule_id == "claude-home-plugins"
    assert plugin.owner is DecisionOwner.KEEP


def test_claude_auto_memory_plugins_and_agent_state_outrank_cache_like_names() -> None:
    env = _env()
    memory = match_application_rule(
        r"D:\ClaudeState\projects\D--work\memory\MEMORY.md",
        env,
    )
    plugin = match_application_rule(
        r"F:\ClaudePlugins\cache\market\plugin\1.2.3\index.js",
        env,
    )
    workflow = match_application_rule(
        r"D:\ClaudeState\workflows\release.js",
        env,
    )
    schedule = match_application_rule(
        r"D:\ClaudeState\scheduled-tasks\nightly\SKILL.md",
        env,
    )
    job = match_application_rule(
        r"D:\ClaudeState\jobs\123\state.json",
        env,
    )
    assert memory is not None and memory.owner is DecisionOwner.KEEP
    assert memory.rule_id == "claude-project-auto-memory"
    assert plugin is not None and plugin.owner is DecisionOwner.KEEP
    assert plugin.rule_id == "claude-plugins"
    assert workflow is not None and workflow.owner is DecisionOwner.KEEP
    assert workflow.rule_id == "claude-authored-config"
    assert schedule is not None and schedule.owner is DecisionOwner.KEEP
    assert schedule.rule_id == "claude-authored-config"
    assert job is not None and job.owner is DecisionOwner.KEEP
    assert job.rule_id == "claude-agent-view-state"


def test_claude_custom_auto_memory_is_protected(tmp_path: Path) -> None:
    home = tmp_path / "claude-home"
    memory = tmp_path / "my-memory"
    home.mkdir()
    memory.mkdir()
    (home / "settings.json").write_text(
        json.dumps({"autoMemoryDirectory": str(memory)}),
        encoding="utf-8",
    )
    env = {
        "USERPROFILE": str(tmp_path),
        "CLAUDE_CONFIG_DIR": str(home),
        "TEMP": str(tmp_path / "temp"),
    }
    rule = match_application_rule(memory / "MEMORY.md", env)
    assert rule is not None
    assert rule.owner is DecisionOwner.KEEP
    assert rule.rule_id == "claude-custom-auto-memory"


def test_process_guard_refreshes_changed_custom_auto_memory(tmp_path: Path) -> None:
    home = tmp_path / "claude-home"
    memory_a = tmp_path / "memory-a"
    memory_b = tmp_path / "memory-b"
    home.mkdir()
    memory_a.mkdir()
    memory_b.mkdir()
    settings = home / "settings.json"
    settings.write_text(
        json.dumps({"autoMemoryDirectory": str(memory_a)}),
        encoding="utf-8",
    )
    env = {
        "USERPROFILE": str(tmp_path),
        "CLAUDE_CONFIG_DIR": str(home),
        "TEMP": str(tmp_path / "temp"),
    }

    first = match_application_rule(memory_a / "MEMORY.md", env)
    assert first is not None and first.owner is DecisionOwner.KEEP

    settings.write_text(
        json.dumps({"autoMemoryDirectory": str(memory_b)}),
        encoding="utf-8",
    )
    stale = match_application_rule(memory_b / "MEMORY.md", env)
    assert stale is None

    assert not process_guard_allows(memory_b / "MEMORY.md", env)
    refreshed = match_application_rule(memory_b / "MEMORY.md", env)
    assert refreshed is not None
    assert refreshed.rule_id == "claude-custom-auto-memory"
    assert refreshed.owner is DecisionOwner.KEEP


def test_claude_working_directory_temp_marker_is_scoped_by_name() -> None:
    env = _env()
    marker = match_application_rule(r"G:\repo\tmpclaude-a81f-cwd", env)
    ordinary = match_application_rule(r"G:\repo\tmpclaude-not-a-marker.txt", env)
    assert marker is not None
    assert marker.rule_id == "claude-cwd-temp-marker"
    assert ordinary is None


def test_process_guard_allows_user_choice_blocks_keep_and_rechecks_tool_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = _env()
    assert process_guard_allows(r"D:\ClaudeState\history.jsonl", env)
    assert process_guard_allows(r"D:\ClaudeState\paste-cache\large-paste.txt", env)
    assert not process_guard_allows(r"D:\ClaudeState\jobs\123\state.json", env)

    monkeypatch.setattr(
        "devclean.core.application_cleanup.claude_process_running",
        lambda: True,
    )
    assert not process_guard_allows(r"D:\ClaudeState\debug\session.log", env)
    assert not process_guard_allows(r"D:\ClaudeState\sessions\12345.json", env)


def test_catalog_adds_redirected_and_fallback_claude_traversal_roots(
    tmp_path: Path,
) -> None:
    home = tmp_path / "claude-home"
    fallback_home = tmp_path / ".claude"
    image_cache = home / "image-cache"
    temp_base = tmp_path / "scratch"
    temp_root = temp_base / "claude"
    image_cache.mkdir(parents=True)
    fallback_home.mkdir(parents=True)
    temp_root.mkdir(parents=True)
    env = {
        "USERPROFILE": str(tmp_path),
        "CLAUDE_CONFIG_DIR": str(home),
        "CLAUDE_CODE_TMPDIR": str(temp_base),
    }

    discovered = discover_known_cleanup_roots(default_rules().scan, env)
    by_path = {os.path.normcase(str(root.path)): root for root in discovered}
    home_root = by_path[os.path.normcase(str(home))]
    fallback_root = by_path[os.path.normcase(str(fallback_home))]
    cache_root = by_path[os.path.normcase(str(image_cache))]
    scratch_root = by_path[os.path.normcase(str(temp_root))]

    assert home_root.policy is CleanupPolicy.REPORT_ONLY
    assert not home_root.delete_root_itself
    assert fallback_root.policy is CleanupPolicy.REPORT_ONLY
    assert not fallback_root.delete_root_itself
    assert cache_root.policy is CleanupPolicy.VENDOR_MANAGED
    assert cache_root.delete_root_itself
    assert scratch_root.policy is CleanupPolicy.REPORT_ONLY
    assert not scratch_root.delete_root_itself
