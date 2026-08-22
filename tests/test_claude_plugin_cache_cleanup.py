from __future__ import annotations

import os
import time
from pathlib import Path, PureWindowsPath

import pytest

import devclean.core.application_cleanup as application_cleanup
from devclean.core.application_cleanup import (
    DecisionOwner,
    match_application_rule,
    process_guard_allows,
)
from devclean.core.claude_plugin_cache_cleanup import (
    claude_plugin_staging_audited_tool_roots,
)
from devclean.core.cleanup_catalog import CleanupPolicy, discover_known_cleanup_roots
from devclean.core.user_rules import default_rules


def _layout(tmp_path: Path) -> tuple[dict[str, str], Path]:
    plugin_root = tmp_path / "claude-plugins"
    cache = plugin_root / "cache"
    cache.mkdir(parents=True)
    env = {
        "USERPROFILE": str(tmp_path / "home"),
        "CLAUDE_CODE_PLUGIN_CACHE_DIR": str(plugin_root),
        "TEMP": str(tmp_path / "temp"),
    }
    return env, cache


def _staging_name(kind: str, now_ms: int, *, hours_old: int) -> str:
    timestamp = now_ms - hours_old * 60 * 60 * 1000
    if kind == "subdir":
        return f"temp_subdir_{timestamp}_abc123.clone"
    return f"temp_{kind}_{timestamp}_abc123"


def test_only_observed_stale_staging_formats_gain_whole_tree_authority(
    tmp_path: Path,
) -> None:
    env, cache = _layout(tmp_path)
    now_ms = time.time_ns() // 1_000_000
    stale_names = (
        _staging_name("git", now_ms, hours_old=4),
        _staging_name("github", now_ms, hours_old=6),
        _staging_name("subdir", now_ms, hours_old=8),
    )
    for name in stale_names:
        (cache / name).mkdir()

    recent = cache / _staging_name("git", now_ms, hours_old=1)
    recent.mkdir()
    arbitrary = cache / f"temp_local_{now_ms - 10 * 60 * 60 * 1000}_abc123"
    arbitrary.mkdir()
    installed = cache / "claude-plugins-official" / "frontend-design" / "1.2.3"
    installed.mkdir(parents=True)

    roots = claude_plugin_staging_audited_tool_roots(env, now_ms=now_ms)
    paths = {str(path).casefold() for path, _rule in roots}

    assert paths == {str(cache / name).casefold() for name in stale_names}
    assert all(rule.rule_id == "claude-plugin-stale-staging-clone" for _, rule in roots)
    assert all(rule.owner is DecisionOwner.TOOL and rule.allow_whole_tree for _, rule in roots)

    recent_rule = match_application_rule(recent, env)
    installed_rule = match_application_rule(installed, env)
    assert recent_rule is not None and recent_rule.owner is DecisionOwner.KEEP
    assert installed_rule is not None and installed_rule.owner is DecisionOwner.KEEP


def test_staging_timestamp_is_immutable_safety_gate_not_directory_mtime(
    tmp_path: Path,
) -> None:
    env, cache = _layout(tmp_path)
    now_ms = time.time_ns() // 1_000_000
    stale = cache / _staging_name("git", now_ms, hours_old=5)
    stale.mkdir()
    # A failed/partial cleanup can refresh directory mtime. The embedded epoch
    # is the source-backed creation signal used by the rule instead.
    os.utime(stale, None)

    roots = claude_plugin_staging_audited_tool_roots(env, now_ms=now_ms)
    assert tuple(path for path, _rule in roots) == (PureWindowsPath(str(stale)),)


def test_future_malformed_and_non_directory_staging_names_fail_closed(
    tmp_path: Path,
) -> None:
    env, cache = _layout(tmp_path)
    now_ms = time.time_ns() // 1_000_000
    future = cache / f"temp_git_{now_ms + 60_000}_abc123"
    malformed = cache / "temp_git_not-a-timestamp_abc123"
    ordinary_file = cache / _staging_name("github", now_ms, hours_old=10)
    future.mkdir()
    malformed.mkdir()
    ordinary_file.write_text("not a staging directory", encoding="utf-8")

    assert claude_plugin_staging_audited_tool_roots(env, now_ms=now_ms) == ()
    for path in (future, malformed, ordinary_file):
        rule = match_application_rule(path, env)
        assert rule is not None
        assert rule.owner is DecisionOwner.KEEP


def test_catalog_surfaces_stale_staging_as_exact_vendor_managed_tree(
    tmp_path: Path,
) -> None:
    env, cache = _layout(tmp_path)
    now_ms = time.time_ns() // 1_000_000
    stale = cache / _staging_name("subdir", now_ms, hours_old=12)
    stale.mkdir()

    discovered = discover_known_cleanup_roots(default_rules().scan, env)
    matching = [root for root in discovered if os.path.normcase(str(root.path)) == os.path.normcase(str(stale))]

    assert len(matching) == 1
    root = matching[0]
    assert root.policy is CleanupPolicy.VENDOR_MANAGED
    assert root.delete_root_itself
    assert root.application_rule is not None
    assert root.application_rule.rule_id == "claude-plugin-stale-staging-clone"


def test_process_guard_requires_claude_closed_before_staging_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, cache = _layout(tmp_path)
    now_ms = time.time_ns() // 1_000_000
    stale = cache / _staging_name("git", now_ms, hours_old=24)
    stale.mkdir()

    monkeypatch.setattr(application_cleanup, "claude_process_running", lambda: True)
    assert not process_guard_allows(stale, env)

    monkeypatch.setattr(application_cleanup, "claude_process_running", lambda: False)
    assert process_guard_allows(stale, env)


def test_plugin_registry_and_marketplace_state_remain_protected(tmp_path: Path) -> None:
    env, cache = _layout(tmp_path)
    plugin_root = cache.parent
    for path in (
        plugin_root / "installed_plugins.json",
        plugin_root / "known_marketplaces.json",
        plugin_root / "marketplaces" / "claude-plugins-official" / ".git" / "HEAD",
    ):
        rule = match_application_rule(path, env)
        assert rule is not None
        assert rule.owner is DecisionOwner.KEEP
