from __future__ import annotations

import subprocess
from collections.abc import Mapping, Sequence

import pytest

import devclean.core.docker_maintenance as docker_maintenance
from devclean.core.docker_maintenance import (
    inventory_docker_storage,
    prune_docker_build_cache,
)


def _completed(
    args: Sequence[str],
    *,
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        list(args),
        returncode,
        stdout=stdout,
        stderr=stderr,
    )


def test_docker_inventory_uses_read_only_system_df(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[list[str]] = []

    def fake_run(
        command: Sequence[str],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
        timeout: int,
        env: Mapping[str, str],
    ) -> subprocess.CompletedProcess[str]:
        del check, capture_output, text, timeout, env
        seen.append(list(command))
        return _completed(
            command,
            stdout=(
                '{"Type":"Images","TotalCount":"5","Active":"2",'
                '"Size":"16.43MB","Reclaimable":"11.63MB (70%)"}\n'
                '{"Type":"Build Cache","TotalCount":"12","Active":"0",'
                '"Size":"8GB","Reclaimable":"6GB"}\n'
            ),
        )

    monkeypatch.setattr(docker_maintenance.subprocess, "run", fake_run)

    inventory = inventory_docker_storage({"DEVCLEAN_DOCKER_EXE": "docker-test"})

    assert seen == [["docker-test", "system", "df", "--format", "json"]]
    assert [row.kind for row in inventory.rows] == ["Images", "Build Cache"]
    assert inventory.rows[1].reclaimable == "6GB"


def test_docker_build_prune_targets_only_old_build_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr(docker_maintenance, "clear_docker_process_cache", lambda: None)
    monkeypatch.setattr(docker_maintenance, "docker_process_running", lambda: False)

    def fake_run(
        command: Sequence[str],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
        timeout: int,
        env: Mapping[str, str],
    ) -> subprocess.CompletedProcess[str]:
        del check, capture_output, text, timeout, env
        seen.append(list(command))
        return _completed(command, stdout="Total reclaimed space: 4.2GB\n")

    monkeypatch.setattr(docker_maintenance.subprocess, "run", fake_run)

    result = prune_docker_build_cache(
        {"DEVCLEAN_DOCKER_EXE": "docker-test"},
        until_hours=336,
    )

    assert seen == [
        [
            "docker-test",
            "builder",
            "prune",
            "--force",
            "--filter",
            "until=336h",
        ]
    ]
    command = seen[0]
    assert "system" not in command
    assert "image" not in command
    assert "container" not in command
    assert "volume" not in command
    assert "--all" not in command
    assert "--volumes" not in command
    assert result.until_hours == 336


def test_docker_build_prune_refuses_aggressive_retention() -> None:
    with pytest.raises(ValueError, match="24"):
        prune_docker_build_cache({}, until_hours=12)


def test_docker_build_prune_blocks_active_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(docker_maintenance, "clear_docker_process_cache", lambda: None)
    monkeypatch.setattr(docker_maintenance, "docker_process_running", lambda: True)

    with pytest.raises(RuntimeError, match="构建正在运行"):
        prune_docker_build_cache({}, until_hours=168)


def test_docker_cli_failure_is_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(
        command: Sequence[str],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
        timeout: int,
        env: Mapping[str, str],
    ) -> subprocess.CompletedProcess[str]:
        del check, capture_output, text, timeout, env
        return _completed(command, stderr="daemon unavailable", returncode=1)

    monkeypatch.setattr(docker_maintenance.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="daemon unavailable"):
        inventory_docker_storage({"DEVCLEAN_DOCKER_EXE": "docker-test"})


def test_docker_inventory_rejects_malformed_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(
        command: Sequence[str],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
        timeout: int,
        env: Mapping[str, str],
    ) -> subprocess.CompletedProcess[str]:
        del check, capture_output, text, timeout, env
        return _completed(command, stdout="not-json\n")

    monkeypatch.setattr(docker_maintenance.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="JSON"):
        inventory_docker_storage({"DEVCLEAN_DOCKER_EXE": "docker-test"})
