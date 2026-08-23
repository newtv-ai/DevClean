from __future__ import annotations

import os
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
from devclean.core.cleanup_catalog import CleanupPolicy, discover_known_cleanup_roots
from devclean.core.cursor_updater_cleanup import (
    cursor_update_staging_audited_tool_roots,
)
from devclean.core.user_rules import default_rules


def _env(tmp_path: Path) -> tuple[dict[str, str], Path]:
    local = tmp_path / "Local"
    temp = local / "Temp"
    temp.mkdir(parents=True)
    return (
        {
            "USERPROFILE": str(tmp_path / "home"),
            "APPDATA": str(tmp_path / "Roaming"),
            "LOCALAPPDATA": str(local),
            "TEMP": str(temp),
            "TMP": str(temp),
        },
        temp,
    )


def _stage(temp: Path, family: str, suffix: str, version: str | None) -> Path:
    directory = temp / f"vscode-stable-{family}-x64-{suffix}"
    directory.mkdir()
    if version is not None:
        (directory / f"CodeSetup-stable-{version}.exe").write_bytes(b"installer")
    return directory


def test_older_cursor_update_staging_is_tool_owned_but_latest_recovery_is_kept(
    tmp_path: Path,
) -> None:
    env, temp = _env(tmp_path)
    old_user = _stage(temp, "user", "olduser", "3.15.0")
    latest_user = _stage(temp, "user", "newuser", "3.16.29")
    partial_user = _stage(temp, "user", "partial", None)
    old_system = _stage(temp, "system", "oldsystem", "3.14.0")
    latest_system = _stage(temp, "system", "newsystem", "3.16.0")

    roots = cursor_update_staging_audited_tool_roots(env)
    tool_paths = {os.path.normcase(str(path)) for path, _rule in roots}

    assert tool_paths == {
        os.path.normcase(str(old_user)),
        os.path.normcase(str(partial_user)),
        os.path.normcase(str(old_system)),
    }
    assert all(
        rule.rule_id == "cursor-superseded-windows-update-staging"
        and rule.owner is DecisionOwner.TOOL
        and rule.allow_whole_tree
        for _path, rule in roots
    )

    old_rule = match_application_rule(old_user / "CodeSetup-stable-3.15.0.exe", env)
    latest_rule = match_application_rule(
        latest_user / "CodeSetup-stable-3.16.29.exe",
        env,
    )
    latest_system_rule = match_application_rule(
        latest_system / "CodeSetup-stable-3.16.0.exe",
        env,
    )
    assert old_rule is not None
    assert old_rule.rule_id == "cursor-superseded-windows-update-staging"
    assert old_rule.owner is DecisionOwner.TOOL
    assert latest_rule is not None
    assert latest_rule.rule_id == "cursor-latest-windows-update-recovery"
    assert latest_rule.owner is DecisionOwner.KEEP
    assert latest_system_rule is not None
    assert latest_system_rule.owner is DecisionOwner.KEEP


def test_same_version_duplicate_keeps_only_newest_recovery_copy(tmp_path: Path) -> None:
    env, temp = _env(tmp_path)
    older = _stage(temp, "user", "duplicatea", "3.16.29")
    newer = _stage(temp, "user", "duplicateb", "3.16.29")
    os.utime(older, (1, 1))
    os.utime(newer, (2, 2))

    roots = cursor_update_staging_audited_tool_roots(env)
    tool_paths = {os.path.normcase(str(path)) for path, _rule in roots}

    assert tool_paths == {os.path.normcase(str(older))}
    old_rule = match_application_rule(older, env)
    new_rule = match_application_rule(newer, env)
    assert old_rule is not None and old_rule.owner is DecisionOwner.TOOL
    assert new_rule is not None and new_rule.owner is DecisionOwner.KEEP


def test_new_installer_can_revoke_a_partial_staging_candidate_before_execution(
    tmp_path: Path,
) -> None:
    env, temp = _env(tmp_path)
    partial = _stage(temp, "user", "download", None)

    first = match_application_rule(partial, env)
    assert first is not None
    assert first.owner is DecisionOwner.TOOL

    (partial / "CodeSetup-stable-3.16.29.exe").write_bytes(b"completed download")

    refreshed = match_application_rule(partial, env)
    assert refreshed is not None
    assert refreshed.rule_id == "cursor-latest-windows-update-recovery"
    assert refreshed.owner is DecisionOwner.KEEP


def test_matching_non_directory_fails_closed_as_recovery_state(tmp_path: Path) -> None:
    env, temp = _env(tmp_path)
    fake = temp / "vscode-stable-user-x64-notadir"
    fake.write_text("not a directory", encoding="utf-8")

    rule = match_application_rule(fake, env)

    assert rule is not None
    assert rule.rule_id == "cursor-latest-windows-update-recovery"
    assert rule.owner is DecisionOwner.KEEP
    assert cursor_update_staging_audited_tool_roots(env) == ()


def test_superseded_cursor_staging_is_safe_without_age_or_size_gate(
    tmp_path: Path,
) -> None:
    env, temp = _env(tmp_path)
    old = _stage(temp, "user", "old", "3.15.0")
    _stage(temp, "user", "new", "3.16.29")
    now = datetime.now(UTC)

    decision = evaluate_application_path(
        old,
        logical_size=1,
        last_used=now,
        now=now,
        process_running=False,
        environment=env,
    )

    assert decision is not None
    assert decision.rule.rule_id == "cursor-superseded-windows-update-staging"
    assert decision.rule.owner is DecisionOwner.TOOL
    assert decision.action is PolicyAction.TOOL_DELETE


def test_catalog_surfaces_only_superseded_cursor_updater_trees(
    tmp_path: Path,
) -> None:
    env, temp = _env(tmp_path)
    old = _stage(temp, "user", "old", "3.15.0")
    latest = _stage(temp, "user", "new", "3.16.29")

    discovered = discover_known_cleanup_roots(default_rules().scan, env)
    by_path = {os.path.normcase(str(root.path)): root for root in discovered}

    old_root = by_path[os.path.normcase(str(old))]
    assert old_root.policy is CleanupPolicy.VENDOR_MANAGED
    assert old_root.delete_root_itself
    assert old_root.application_rule is not None
    assert old_root.application_rule.rule_id == "cursor-superseded-windows-update-staging"

    latest_item = by_path.get(os.path.normcase(str(latest)))
    assert latest_item is None or latest_item.application_rule is None


def test_cursor_updater_process_guard_rechecks_before_whole_tree_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, temp = _env(tmp_path)
    old = _stage(temp, "user", "old", "3.15.0")
    _stage(temp, "user", "new", "3.16.29")

    monkeypatch.setattr(application_cleanup, "cursor_process_running", lambda: True)
    assert not process_guard_allows(old, env)

    monkeypatch.setattr(application_cleanup, "cursor_process_running", lambda: False)
    assert process_guard_allows(old, env)


def test_unrelated_temp_directories_do_not_gain_cursor_authority(tmp_path: Path) -> None:
    env, temp = _env(tmp_path)
    unrelated = temp / "vscode-insiders-user-x64-random"
    unrelated.mkdir()

    rule = match_application_rule(unrelated, env)

    assert rule is None or not rule.rule_id.startswith("cursor-")
