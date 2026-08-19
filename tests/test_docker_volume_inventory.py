from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence

import pytest

import devclean.core.docker_volume_inventory as volume_inventory
from devclean.core.docker_maintenance import DockerDaemonIdentity
from devclean.core.docker_volume_inventory import inspect_docker_volumes


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


def _daemon() -> DockerDaemonIdentity:
    return DockerDaemonIdentity(
        context_name="default",
        endpoint="npipe:////./pipe/docker_engine",
        source="docker context",
        local=True,
        reason="local",
    )


def _volume(
    name: str,
    *,
    driver: str = "local",
    scope: str = "local",
) -> dict[str, object]:
    return {
        "Name": name,
        "Driver": driver,
        "Scope": scope,
        "Mountpoint": f"/var/lib/docker/volumes/{name}/_data",
        "CreatedAt": "2026-08-01T00:00:00Z",
        "Labels": {"project": "demo"},
        "Options": {"type": "none"},
    }


def _container(
    container_id: str,
    volume_names: tuple[str, ...],
) -> dict[str, object]:
    return {
        "Id": container_id,
        "Mounts": [
            {"Type": "volume", "Name": name}
            for name in volume_names
        ],
    }


def test_volume_inventory_is_exact_and_report_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[list[str]] = []
    volumes = [_volume("dbdata"), _volume("cachedata", driver="custom")]
    containers = [_container("c" * 64, ("dbdata",))]
    monkeypatch.setattr(
        volume_inventory,
        "inspect_docker_daemon_target",
        lambda environment=None: _daemon(),
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
        seen.append(cmd)
        if cmd[-3:] == ["volume", "ls", "--quiet"]:
            return _completed(command, stdout="dbdata\ncachedata\n")
        if "volume" in cmd and "inspect" in cmd:
            return _completed(command, stdout=json.dumps(volumes))
        if cmd[-5:] == ["container", "ls", "--all", "--no-trunc", "--quiet"]:
            return _completed(command, stdout=("c" * 64) + "\n")
        if "container" in cmd and "inspect" in cmd:
            return _completed(command, stdout=json.dumps(containers))
        raise AssertionError(cmd)

    monkeypatch.setattr(
        "devclean.core.docker_volume_inventory.subprocess.run",
        fake_run,
    )

    inventory = inspect_docker_volumes({"DEVCLEAN_DOCKER_EXE": "docker-test"})
    by_name = {item.name: item for item in inventory.volumes}

    assert by_name["dbdata"].referenced
    assert by_name["dbdata"].container_ids == ("c" * 64,)
    assert not by_name["dbdata"].executable
    assert not by_name["cachedata"].referenced
    assert not by_name["cachedata"].executable
    assert by_name["cachedata"].driver == "custom"
    assert by_name["dbdata"].labels == (("project", "demo"),)
    assert by_name["dbdata"].options == (("type", "none"),)

    flattened = [argument for command in seen for argument in command]
    assert "rm" not in flattened
    assert "prune" not in flattened
    assert all("system" not in command for command in seen)


def test_volume_inventory_refuses_remote_daemon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote = DockerDaemonIdentity(
        context_name="production",
        endpoint="ssh://docker@example.invalid",
        source="docker context",
        local=False,
        reason="remote",
    )
    monkeypatch.setattr(
        volume_inventory,
        "inspect_docker_daemon_target",
        lambda environment=None: remote,
    )

    with pytest.raises(RuntimeError, match="本机 Docker daemon"):
        inspect_docker_volumes({})


def test_bind_mounts_do_not_create_volume_references(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        volume_inventory,
        "inspect_docker_daemon_target",
        lambda environment=None: _daemon(),
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
        if cmd[-3:] == ["volume", "ls", "--quiet"]:
            return _completed(command, stdout="data\n")
        if "volume" in cmd and "inspect" in cmd:
            return _completed(command, stdout=json.dumps([_volume("data")]))
        if cmd[-5:] == ["container", "ls", "--all", "--no-trunc", "--quiet"]:
            return _completed(command, stdout="container-1\n")
        if "container" in cmd and "inspect" in cmd:
            return _completed(
                command,
                stdout=json.dumps(
                    [
                        {
                            "Id": "container-1",
                            "Mounts": [
                                {"Type": "bind", "Name": "data"},
                            ],
                        }
                    ]
                ),
            )
        raise AssertionError(cmd)

    monkeypatch.setattr(
        "devclean.core.docker_volume_inventory.subprocess.run",
        fake_run,
    )

    entry = inspect_docker_volumes({"DEVCLEAN_DOCKER_EXE": "docker-test"}).volumes[0]
    assert not entry.referenced
    assert entry.container_ids == ()
