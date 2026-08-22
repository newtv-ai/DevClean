from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from pytest import MonkeyPatch

from devclean.core import claude_native_cleanup
from devclean.core._application_cleanup_impl import DecisionOwner, PolicyAction
from devclean.core.claude_cleanup import (
    claude_scan_roots,
    evaluate_claude_path,
    match_claude_rule,
)


def _native_layout(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    home = tmp_path / "home"
    versions = home / ".local" / "share" / "claude" / "versions"
    binary_dir = home / ".local" / "bin"
    versions.mkdir(parents=True)
    binary_dir.mkdir(parents=True)
    return {"USERPROFILE": str(home)}, versions, binary_dir


def _large_file(path: Path) -> None:
    with path.open("wb") as stream:
        stream.truncate(9 * 1024 * 1024)


def test_native_scan_roots_are_discovered_directly(tmp_path: Path) -> None:
    environment, versions, binary_dir = _native_layout(tmp_path)

    roots = {str(root).casefold() for root in claude_scan_roots(environment)}

    assert str(versions).casefold() in roots
    assert str(binary_dir).casefold() in roots


def test_native_versions_keep_newest_and_delete_older(tmp_path: Path) -> None:
    environment, versions, _binary_dir = _native_layout(tmp_path)
    for version in ("2.1.237", "2.1.238", "2.1.239"):
        _large_file(versions / version)

    newest = match_claude_rule(versions / "2.1.239", environment)
    older = match_claude_rule(versions / "2.1.238", environment)

    assert newest is not None
    assert newest.owner is DecisionOwner.KEEP
    assert older is not None
    assert older.owner is DecisionOwner.TOOL

    now = datetime.now(UTC)
    decision = evaluate_claude_path(
        versions / "2.1.238",
        logical_size=(versions / "2.1.238").stat().st_size,
        last_used=now,
        now=now,
        process_running=False,
        environment=environment,
    )
    assert decision is not None
    assert decision.action is PolicyAction.TOOL_DELETE


def test_partial_newest_download_is_not_used_as_recovery_copy(tmp_path: Path) -> None:
    environment, versions, _binary_dir = _native_layout(tmp_path)
    _large_file(versions / "2.1.238")
    _large_file(versions / "2.1.239")
    (versions / "2.1.240").write_bytes(b"")

    newest_valid = match_claude_rule(versions / "2.1.239", environment)
    partial = match_claude_rule(versions / "2.1.240", environment)

    assert newest_valid is not None
    assert newest_valid.owner is DecisionOwner.KEEP
    assert partial is not None
    assert partial.owner is DecisionOwner.KEEP


def test_old_windows_launcher_is_tool_owned_only_with_healthy_live_launcher(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    environment, _versions, binary_dir = _native_layout(tmp_path)
    _large_file(binary_dir / "claude.exe")
    _large_file(binary_dir / "claude.exe.old.1787342294461")

    monkeypatch.setattr(claude_native_cleanup, "_healthy_launcher", lambda _path: True)
    old_rule = match_claude_rule(binary_dir / "claude.exe.old.1787342294461", environment)
    assert old_rule is not None
    assert old_rule.owner is DecisionOwner.TOOL

    monkeypatch.setattr(claude_native_cleanup, "_healthy_launcher", lambda _path: False)
    protected = match_claude_rule(binary_dir / "claude.exe.old.1787342294461", environment)
    assert protected is not None
    assert protected.owner is DecisionOwner.KEEP
