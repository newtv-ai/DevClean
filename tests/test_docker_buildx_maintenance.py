from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence

import pytest

import devclean.core.docker_buildx_maintenance as buildx_maintenance
from devclean.core.docker_buildx_maintenance import (
    inspect_buildx_cache,
    list_buildx_builders,
    prune_buildx_cache,
)
from devclean.core.docker_maintenance import DockerDaemonIdentity


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


def _local_daemon(context: str = "default") -> DockerDaemonIdentity:
    return DockerDaemonIdentity(
        context_name=context,
        endpoint="npipe:////./pipe/docker_engine",
        source="docker context",
        local=True,
        reason="local",
    )


def _builder_rows() -> str:
    return (
        "localbuilder\tdocker\t\n"
        "localbuilder0\tdefault\trunning\n"
        "remote-builder\tdocker-container\t\n"
        "remote-builder0\tssh://builder@example.invalid\trunning\n"
        "cluster\tkubernetes\t\n"
        "cluster0\tkubernetes://builder\trunning\n"
    )


def _context_payload(name: str, host: str) -> str:
    return json.dumps(
        [
            {
                "Name": name,
                "Endpoints": {"docker": {"Host": host}},
            }
        ]
    )


def test_buildx_builder_inventory_only_executes_source_proven_local_nodes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    daemon = _local_daemon()
    monkeypatch.setattr(
        buildx_maintenance,
        "inspect_docker_daemon_target",
        lambda environment=None: daemon,
    )

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
        cmd = list(command)
        if "buildx" in cmd and "ls" in cmd:
            return _completed(command, stdout=_builder_rows())
        if cmd == ["docker-test", "context", "inspect", "default"]:
            return _completed(
                command,
                stdout=_context_payload("default", "npipe:////./pipe/docker_engine"),
            )
        raise AssertionError(cmd)

    monkeypatch.setattr(
        "devclean.core.docker_buildx_maintenance.subprocess.run",
        fake_run,
    )

    builders = list_buildx_builders({"DEVCLEAN_DOCKER_EXE": "docker-test"})
    by_name = {builder.name: builder for builder in builders}

    assert by_name["localbuilder"].executable
    assert by_name["localbuilder"].local
    assert not by_name["remote-builder"].executable
    assert not by_name["cluster"].executable


def test_buildx_cache_uses_vendor_age_filter_and_reclaimable_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    daemon = _local_daemon()
    monkeypatch.setattr(
        buildx_maintenance,
        "inspect_docker_daemon_target",
        lambda environment=None: daemon,
    )
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
        cmd = list(command)
        seen.append(cmd)
        if "buildx" in cmd and "ls" in cmd:
            return _completed(
                command,
                stdout="localbuilder\tdocker\t\nlocalbuilder0\tdefault\trunning\n",
            )
        if cmd == ["docker-test", "context", "inspect", "default"]:
            return _completed(
                command,
                stdout=_context_payload("default", "npipe:////./pipe/docker_engine"),
            )
        if "buildx" in cmd and "du" in cmd:
            return _completed(
                command,
                stdout=(
                    '{"ID":"a","Size":"1073741824","Reclaimable":true}\n'
                    '{"ID":"b","Size":"128","Reclaimable":false}\n'
                ),
            )
        raise AssertionError(cmd)

    monkeypatch.setattr(
        "devclean.core.docker_buildx_maintenance.subprocess.run",
        fake_run,
    )

    inventory = inspect_buildx_cache(
        "localbuilder",
        {"DEVCLEAN_DOCKER_EXE": "docker-test"},
        retention_hours=168,
    )

    assert inventory.aged_reclaimable_bytes == 1024**3
    assert inventory.worth_maintaining
    du = next(command for command in seen if "du" in command)
    assert "--builder" in du
    assert "localbuilder" in du
    assert "until=168h" in du
    assert "--format=json" in du


def test_buildx_cache_rejects_remote_builder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    daemon = _local_daemon()
    monkeypatch.setattr(
        buildx_maintenance,
        "inspect_docker_daemon_target",
        lambda environment=None: daemon,
    )

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
        cmd = list(command)
        if "buildx" in cmd and "ls" in cmd:
            return _completed(
                command,
                stdout=(
                    "remote\tdocker-container\t\n"
                    "remote0\tssh://builder@example.invalid\trunning\n"
                ),
            )
        raise AssertionError(cmd)

    monkeypatch.setattr(
        "devclean.core.docker_buildx_maintenance.subprocess.run",
        fake_run,
    )

    with pytest.raises(RuntimeError, match="不是可确认的本机"):
        inspect_buildx_cache(
            "remote",
            {"DEVCLEAN_DOCKER_EXE": "docker-test"},
        )


def test_buildx_prune_is_pinned_and_never_uses_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    daemon = _local_daemon()
    monkeypatch.setattr(
        buildx_maintenance,
        "inspect_docker_daemon_target",
        lambda environment=None: daemon,
    )
    monkeypatch.setattr(buildx_maintenance, "clear_docker_process_cache", lambda: None)
    monkeypatch.setattr(buildx_maintenance, "docker_process_running", lambda: False)
    seen: list[list[str]] = []
    du_calls = 0

    def fake_run(
        command: Sequence[str],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
        timeout: int,
        env: Mapping[str, str],
    ) -> subprocess.CompletedProcess[str]:
        nonlocal du_calls
        del check, capture_output, text, timeout, env
        cmd = list(command)
        seen.append(cmd)
        if "buildx" in cmd and "ls" in cmd:
            return _completed(
                command,
                stdout="localbuilder\tdocker\t\nlocalbuilder0\tdefault\trunning\n",
            )
        if cmd == ["docker-test", "context", "inspect", "default"]:
            return _completed(
                command,
                stdout=_context_payload("default", "npipe:////./pipe/docker_engine"),
            )
        if "buildx" in cmd and "du" in cmd:
            du_calls += 1
            size = 2048 if du_calls < 3 else 512
            return _completed(
                command,
                stdout=f'{{"ID":"a","Size":"{size}","Reclaimable":true}}\n',
            )
        if "buildx" in cmd and "prune" in cmd:
            return _completed(command, stdout="Total: 1.5kB\n")
        raise AssertionError(cmd)

    monkeypatch.setattr(
        "devclean.core.docker_buildx_maintenance.subprocess.run",
        fake_run,
    )

    result = prune_buildx_cache(
        "localbuilder",
        {"DEVCLEAN_DOCKER_EXE": "docker-test"},
        retention_hours=168,
    )

    prune = next(command for command in seen if "prune" in command)
    assert prune[:3] == ["docker-test", "--context", "default"]
    assert "--builder" in prune
    assert "localbuilder" in prune
    assert "until=168h" in prune
    assert "--all" not in prune
    assert result.before_reclaimable_bytes == 2048
    assert result.after_reclaimable_bytes == 512
    assert result.observed_reclaimed_bytes == 1536


def test_buildx_prune_blocks_active_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory = buildx_maintenance.BuildxCacheInventory(
        daemon=_local_daemon(),
        builder=buildx_maintenance.BuildxBuilder(
            "localbuilder",
            "docker",
            (buildx_maintenance.BuildxNode("node0", "default", "running"),),
            True,
            True,
            "local",
        ),
        retention_hours=168,
        record_count=1,
        aged_reclaimable_bytes=1024,
        worth_maintaining=False,
    )
    monkeypatch.setattr(
        buildx_maintenance,
        "inspect_buildx_cache",
        lambda *args, **kwargs: inventory,
    )
    monkeypatch.setattr(buildx_maintenance, "clear_docker_process_cache", lambda: None)
    monkeypatch.setattr(buildx_maintenance, "docker_process_running", lambda: True)

    with pytest.raises(RuntimeError, match="构建正在运行"):
        prune_buildx_cache("localbuilder", {}, retention_hours=168)


def test_buildx_retention_cannot_be_more_aggressive_than_24_hours() -> None:
    with pytest.raises(ValueError, match="24"):
        inspect_buildx_cache("builder", {}, retention_hours=12)
