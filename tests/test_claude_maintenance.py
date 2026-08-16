from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from devclean.core.claude_maintenance import (
    ClaudeMaintenanceError,
    claude_plugin_storage_bytes,
    run_claude_plugin_prune,
)


def test_plugin_prune_uses_vendor_dry_run_and_yes_modes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "devclean.core.claude_maintenance.claude_process_running",
        lambda: False,
    )
    calls: list[list[str]] = []

    def fake_runner(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="orphan-a\n", stderr="")

    preview = run_claude_plugin_prune(
        dry_run=True,
        binary=Path("claude.exe"),
        runner=fake_runner,
    )
    applied = run_claude_plugin_prune(
        dry_run=False,
        binary=Path("claude.exe"),
        runner=fake_runner,
    )

    assert preview.dry_run
    assert "--dry-run" in calls[0]
    assert "--yes" not in calls[0]
    assert not applied.dry_run
    assert "--yes" in calls[1]
    assert "--scope" in calls[1]
    assert "user" in calls[1]


def test_plugin_prune_refuses_while_claude_is_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "devclean.core.claude_maintenance.claude_process_running",
        lambda: True,
    )
    with pytest.raises(ClaudeMaintenanceError, match="正在运行"):
        run_claude_plugin_prune(dry_run=True, binary=Path("claude.exe"))


def test_plugin_storage_counts_files_under_redirected_root(tmp_path: Path) -> None:
    plugin_root = tmp_path / "plugins"
    plugin_root.mkdir()
    (plugin_root / "a.bin").write_bytes(b"a" * 100)
    nested = plugin_root / "cache" / "plugin"
    nested.mkdir(parents=True)
    (nested / "b.bin").write_bytes(b"b" * 200)

    total = claude_plugin_storage_bytes(
        {
            "USERPROFILE": str(tmp_path),
            "CLAUDE_CODE_PLUGIN_CACHE_DIR": str(plugin_root),
        }
    )
    assert total == 300
