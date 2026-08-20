from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence

import pytest

import devclean.core.docker_container_maintenance as container_maintenance
from devclean.core.docker_container_maintenance import (
    DockerContainerEntry,
    inspect_docker_containers,
    remove_docker_container,
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


def _daemon(context: str = "default") -> DockerDaemonIdentity:
    return DockerDaemonIdentity(
        context_name=context,
        endpoint="npipe:////./pipe/docker_engine",
        source="docker context",
        local=True,
        reason="local",
    )


def _payload(
    container_id: str,
    *,
    name: str = "demo",
    image_id: str = "sha256:image",
    image_ref: str = "repo:latest",
    running: bool = False,
    status: str = "exited",
    size_rw: int = 123,
    volume_names: tuple[str, ...] = (),
) -> dict[str, object]:
    return {
        "Id": container_id,
        "Name": f"/{name}",
        "Image": image_id,
        "Created": "2026-08-01T00:00:00Z",
        "Config": {"Image": image_ref},
        "State": {"Status": status, "Running": running},
        "SizeRw": size_rw,
        "SizeRootFs": 999,
        "Mounts": [{"Type": "volume", "Name": volume_name} for volume_name in volume_names],
    }


def _install_inventory_fake(
    monkeypatch: pytest.MonkeyPatch,
    payloads: list[dict[str, object]],
    *,
    seen: list[list[str]] | None = None,
) -> None:
    monkeypatch.setattr(
        container_maintenance,
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
        if seen is not None:
            seen.append(cmd)
        if cmd[-5:] == ["container", "ls", "--all", "--no-trunc", "--quiet"]:
            ids = [str(item["Id"]) for item in payloads]
            return _completed(command, stdout="\n".join(ids) + ("\n" if ids else ""))
        if "container" in cmd and "inspect" in cmd:
            assert "--size" in cmd
            return _completed(command, stdout=json.dumps(payloads))
        raise AssertionError(cmd)

    monkeypatch.setattr(
        "devclean.core.docker_container_maintenance.subprocess.run",
        fake_run,
    )


def test_inventory_marks_only_stopped_containers_executable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stopped = "a" * 64
    running = "b" * 64
    seen: list[list[str]] = []
    _install_inventory_fake(
        monkeypatch,
        [
            _payload(stopped, volume_names=("dbdata",), size_rw=500),
            _payload(running, running=True, status="running", size_rw=700),
        ],
        seen=seen,
    )

    inventory = inspect_docker_containers({"DEVCLEAN_DOCKER_EXE": "docker-test"})
    by_id = {entry.container_id: entry for entry in inventory.containers}

    assert by_id[stopped].executable
    assert by_id[stopped].volume_names == ("dbdata",)
    assert not by_id[running].executable
    assert by_id[running].running
    assert any("--size" in command for command in seen)


def test_remote_daemon_is_not_a_container_mutation_target(
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
        container_maintenance,
        "inspect_docker_daemon_target",
        lambda environment=None: remote,
    )

    with pytest.raises(RuntimeError, match="只维护本机 Docker daemon"):
        inspect_docker_containers({})


def test_container_removal_is_exact_no_force_no_volume_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container_id = "c" * 64
    expected = DockerContainerEntry(
        container_id=container_id,
        name="demo",
        image_id="sha256:image",
        image_ref="repo:latest",
        created="2026-08-01T00:00:00Z",
        status="exited",
        running=False,
        writable_size=123,
        rootfs_size=999,
        volume_names=("dbdata",),
        executable=True,
        reason="user review",
    )
    states = iter(
        [
            container_maintenance.DockerContainerInventory(_daemon(), (expected,)),
            container_maintenance.DockerContainerInventory(_daemon(), (expected,)),
            container_maintenance.DockerContainerInventory(_daemon(), ()),
        ]
    )
    monkeypatch.setattr(
        container_maintenance,
        "inspect_docker_containers",
        lambda environment=None: next(states),
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
        if "system" in cmd and "df" in cmd:
            return _completed(command, stdout='{"Type":"Containers"}\n')
        if "container" in cmd and "rm" in cmd:
            return _completed(command, stdout=f"{container_id}\n")
        raise AssertionError(cmd)

    monkeypatch.setattr(
        "devclean.core.docker_container_maintenance.subprocess.run",
        fake_run,
    )

    result = remove_docker_container(
        expected,
        {"DEVCLEAN_DOCKER_EXE": "docker-test"},
    )

    remove = next(command for command in seen if "rm" in command)
    assert remove == [
        "docker-test",
        "--context",
        "default",
        "container",
        "rm",
        container_id,
    ]
    assert "--force" not in remove
    assert "-f" not in remove
    assert "--volumes" not in remove
    assert "-v" not in remove
    assert result.container.volume_names == ("dbdata",)


def test_container_removal_refuses_state_change_to_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container_id = "d" * 64
    expected = DockerContainerEntry(
        container_id=container_id,
        name="demo",
        image_id="sha256:image",
        image_ref="repo:latest",
        created="2026-08-01T00:00:00Z",
        status="exited",
        running=False,
        writable_size=123,
        rootfs_size=999,
        volume_names=(),
        executable=True,
        reason="user review",
    )
    changed = DockerContainerEntry(
        container_id=container_id,
        name=expected.name,
        image_id=expected.image_id,
        image_ref=expected.image_ref,
        created=expected.created,
        status="running",
        running=True,
        writable_size=123,
        rootfs_size=999,
        volume_names=(),
        executable=False,
        reason="running",
    )
    monkeypatch.setattr(
        container_maintenance,
        "inspect_docker_containers",
        lambda environment=None: container_maintenance.DockerContainerInventory(
            _daemon(),
            (changed,),
        ),
    )

    with pytest.raises(RuntimeError, match="已变化"):
        remove_docker_container(expected, {})


def test_container_removal_refuses_volume_binding_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container_id = "e" * 64
    expected = DockerContainerEntry(
        container_id=container_id,
        name="demo",
        image_id="sha256:image",
        image_ref="repo:latest",
        created="2026-08-01T00:00:00Z",
        status="exited",
        running=False,
        writable_size=123,
        rootfs_size=999,
        volume_names=(),
        executable=True,
        reason="user review",
    )
    changed = DockerContainerEntry(
        container_id=container_id,
        name=expected.name,
        image_id=expected.image_id,
        image_ref=expected.image_ref,
        created=expected.created,
        status=expected.status,
        running=False,
        writable_size=123,
        rootfs_size=999,
        volume_names=("new-volume",),
        executable=True,
        reason="user review",
    )
    monkeypatch.setattr(
        container_maintenance,
        "inspect_docker_containers",
        lambda environment=None: container_maintenance.DockerContainerInventory(
            _daemon(),
            (changed,),
        ),
    )

    with pytest.raises(RuntimeError, match="已变化"):
        remove_docker_container(expected, {})
