from __future__ import annotations

import json
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
    claude_plugin_orphan_audited_tool_roots,
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


def _plugin_versions(cache: Path) -> tuple[Path, Path]:
    parent = cache / "claude-plugins-official" / "frontend-design"
    orphan = parent / "1.0.0"
    active = parent / "2.0.0"
    orphan.mkdir(parents=True)
    active.mkdir()
    return orphan, active


def _mark_orphan(path: Path, now_ms: int, *, days_old: int = 8) -> None:
    marked_ms = now_ms - days_old * 24 * 60 * 60 * 1000
    (path / ".orphaned_at").write_text(str(marked_ms), encoding="utf-8")


def _write_plugin_state(
    cache: Path,
    records: list[dict[str, object]],
    *,
    marketplace_location: Path | None = None,
) -> None:
    plugin_root = cache.parent
    installed = {
        "version": 2,
        "plugins": {"frontend-design@claude-plugins-official": records},
    }
    (plugin_root / "installed_plugins.json").write_text(
        json.dumps(installed),
        encoding="utf-8",
    )
    location = marketplace_location or (
        plugin_root / "marketplaces" / "claude-plugins-official"
    )
    known = {
        "claude-plugins-official": {
            "source": {"source": "github", "repo": "anthropics/claude-plugins-official"},
            "installLocation": str(location),
            "lastUpdated": "2026-08-01T00:00:00Z",
        }
    }
    (plugin_root / "known_marketplaces.json").write_text(
        json.dumps(known),
        encoding="utf-8",
    )


def _record(path: Path, *, scope: str = "user", project: str | None = None) -> dict[str, object]:
    record: dict[str, object] = {
        "scope": scope,
        "installPath": str(path),
        "version": path.name,
    }
    if project is not None:
        record["projectPath"] = project
    return record


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


def test_expired_orphan_with_registered_replacement_is_tool_owned(tmp_path: Path) -> None:
    env, cache = _layout(tmp_path)
    now_ms = time.time_ns() // 1_000_000
    orphan, active = _plugin_versions(cache)
    _mark_orphan(orphan, now_ms)
    _write_plugin_state(cache, [_record(active)])

    roots = claude_plugin_orphan_audited_tool_roots(env, now_ms=now_ms)

    assert tuple(path for path, _rule in roots) == (PureWindowsPath(str(orphan)),)
    rule = match_application_rule(orphan / "skills" / "old.md", env)
    assert rule is not None
    assert rule.rule_id == "claude-plugin-expired-orphan-version"
    assert rule.owner is DecisionOwner.TOOL
    assert rule.allow_whole_tree


def test_orphan_marker_must_be_older_than_documented_seven_day_grace(tmp_path: Path) -> None:
    env, cache = _layout(tmp_path)
    now_ms = time.time_ns() // 1_000_000
    orphan, active = _plugin_versions(cache)
    _mark_orphan(orphan, now_ms, days_old=6)
    _write_plugin_state(cache, [_record(active)])

    assert claude_plugin_orphan_audited_tool_roots(env, now_ms=now_ms) == ()
    rule = match_application_rule(orphan, env)
    assert rule is not None and rule.owner is DecisionOwner.KEEP


def test_malformed_or_missing_orphan_marker_never_gains_delete_authority(
    tmp_path: Path,
) -> None:
    env, cache = _layout(tmp_path)
    now_ms = time.time_ns() // 1_000_000
    orphan, active = _plugin_versions(cache)
    _write_plugin_state(cache, [_record(active)])

    marker = orphan / ".orphaned_at"
    marker.write_text("not-a-timestamp", encoding="utf-8")
    assert claude_plugin_orphan_audited_tool_roots(env, now_ms=now_ms) == ()

    marker.unlink()
    assert claude_plugin_orphan_audited_tool_roots(env, now_ms=now_ms) == ()


def test_any_registry_scope_referencing_orphan_path_blocks_cleanup(tmp_path: Path) -> None:
    env, cache = _layout(tmp_path)
    now_ms = time.time_ns() // 1_000_000
    orphan, active = _plugin_versions(cache)
    _mark_orphan(orphan, now_ms)
    _write_plugin_state(
        cache,
        [
            _record(active, scope="user"),
            _record(orphan, scope="project", project=r"C:\work\legacy-project"),
        ],
    )

    assert claude_plugin_orphan_audited_tool_roots(env, now_ms=now_ms) == ()
    rule = match_application_rule(orphan, env)
    assert rule is not None and rule.owner is DecisionOwner.KEEP


def test_orphan_without_registered_sibling_replacement_stays_protected(
    tmp_path: Path,
) -> None:
    env, cache = _layout(tmp_path)
    now_ms = time.time_ns() // 1_000_000
    orphan, _active = _plugin_versions(cache)
    _mark_orphan(orphan, now_ms)
    _write_plugin_state(cache, [])

    assert claude_plugin_orphan_audited_tool_roots(env, now_ms=now_ms) == ()


def test_malformed_or_missing_registry_fails_closed(tmp_path: Path) -> None:
    env, cache = _layout(tmp_path)
    now_ms = time.time_ns() // 1_000_000
    orphan, active = _plugin_versions(cache)
    _mark_orphan(orphan, now_ms)
    plugin_root = cache.parent
    (plugin_root / "installed_plugins.json").write_text("{broken", encoding="utf-8")
    (plugin_root / "known_marketplaces.json").write_text("{}", encoding="utf-8")

    assert claude_plugin_orphan_audited_tool_roots(env, now_ms=now_ms) == ()

    _write_plugin_state(cache, [_record(active)])
    (plugin_root / "known_marketplaces.json").unlink()
    assert claude_plugin_orphan_audited_tool_roots(env, now_ms=now_ms) == ()


def test_marketplace_install_location_overlapping_candidate_blocks_cleanup(
    tmp_path: Path,
) -> None:
    env, cache = _layout(tmp_path)
    now_ms = time.time_ns() // 1_000_000
    orphan, active = _plugin_versions(cache)
    _mark_orphan(orphan, now_ms)
    marketplace_root = cache / "claude-plugins-official"
    _write_plugin_state(cache, [_record(active)], marketplace_location=marketplace_root)

    assert claude_plugin_orphan_audited_tool_roots(env, now_ms=now_ms) == ()


def test_unmarked_superseded_sibling_remains_protected_for_now(tmp_path: Path) -> None:
    env, cache = _layout(tmp_path)
    now_ms = time.time_ns() // 1_000_000
    stale, active = _plugin_versions(cache)
    _write_plugin_state(cache, [_record(active)])

    assert claude_plugin_orphan_audited_tool_roots(env, now_ms=now_ms) == ()
    rule = match_application_rule(stale, env)
    assert rule is not None and rule.owner is DecisionOwner.KEEP


def test_catalog_surfaces_source_proven_plugin_garbage_as_exact_trees(
    tmp_path: Path,
) -> None:
    env, cache = _layout(tmp_path)
    now_ms = time.time_ns() // 1_000_000
    staging = cache / _staging_name("subdir", now_ms, hours_old=12)
    staging.mkdir()
    orphan, active = _plugin_versions(cache)
    _mark_orphan(orphan, now_ms)
    _write_plugin_state(cache, [_record(active)])

    discovered = discover_known_cleanup_roots(default_rules().scan, env)
    by_path = {os.path.normcase(str(root.path)): root for root in discovered}

    staging_root = by_path[os.path.normcase(str(staging))]
    orphan_root = by_path[os.path.normcase(str(orphan))]
    for root in (staging_root, orphan_root):
        assert root.policy is CleanupPolicy.VENDOR_MANAGED
        assert root.delete_root_itself
        assert root.application_rule is not None
    assert staging_root.application_rule is not None
    assert orphan_root.application_rule is not None
    assert staging_root.application_rule.rule_id == "claude-plugin-stale-staging-clone"
    assert orphan_root.application_rule.rule_id == "claude-plugin-expired-orphan-version"


def test_process_guard_requires_claude_closed_before_plugin_cache_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, cache = _layout(tmp_path)
    now_ms = time.time_ns() // 1_000_000
    staging = cache / _staging_name("git", now_ms, hours_old=24)
    staging.mkdir()
    orphan, active = _plugin_versions(cache)
    _mark_orphan(orphan, now_ms)
    _write_plugin_state(cache, [_record(active)])

    monkeypatch.setattr(application_cleanup, "claude_process_running", lambda: True)
    assert not process_guard_allows(staging, env)
    assert not process_guard_allows(orphan, env)

    monkeypatch.setattr(application_cleanup, "claude_process_running", lambda: False)
    assert process_guard_allows(staging, env)
    assert process_guard_allows(orphan, env)


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
