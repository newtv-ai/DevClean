from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping

import pytest

import devclean.core.podman_container_maintenance as podman
from devclean.core.podman_container_maintenance import (
    PodmanContainerEntry,
    PodmanMachineConnection,
    inspect_podman_containers,
    inspect_podman_machine_target,
    remove_podman_container,
)

_ENV = {"DEVCLEAN_TEST_WINDOWS": "1", "DEVCLEAN_PODMAN_EXE": "podman.exe"}
_SAFE_TEST_STATUSES = frozenset({"configured", "created", "exited", "stopped"})


def _completed(stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["podman.exe"], 0, stdout, stderr)


def _target(*, name: str = "podman-machine-default") -> PodmanMachineConnection:
    return PodmanMachineConnection(
        executable="podman.exe",
        connection_name=name,
        connection_uri="ssh://user@127.0.0.1:55123/run/user/1000/podman/podman.sock",
        machine_name="podman-machine-default",
        vm_type="wsl",
        running=True,
        rootful=name.endswith("-root"),
    )


def _container(
    *,
    container_id: str = "a" * 64,
    status: str = "exited",
    running: bool = False,
    paused: bool = False,
    pod_id: str = "",
    is_infra: bool = False,
) -> PodmanContainerEntry:
    executable = (
        status in _SAFE_TEST_STATUSES and not running and not paused and not pod_id and not is_infra
    )
    return PodmanContainerEntry(
        container_id=container_id,
        name="demo",
        image_id="b" * 64,
        image_name="docker.io/library/alpine:latest",
        created="2026-08-01T00:00:00Z",
        status=status,
        running=running,
        paused=paused,
        pod_id=pod_id,
        is_infra=is_infra,
        writable_size=1024,
        rootfs_size=2048,
        volume_names=("data",),
        executable=executable,
        reason="review" if executable else "protected",
    )


def test_target_requires_exact_default_managed_loopback_machine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_run(
        command: list[str],
        environment: Mapping[str, str] | None,
        *,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        del environment, timeout
        calls.append(tuple(command))
        if command[1:5] == ["system", "connection", "list", "--format"]:
            return _completed(
                json.dumps(
                    [
                        {
                            "Name": "podman-machine-default",
                            "URI": "ssh://user@127.0.0.1:55123/run/user/1000/podman/podman.sock",
                            "IsMachine": True,
                            "Default": True,
                            "ReadWrite": True,
                        }
                    ]
                )
            )
        if command[1:4] == ["machine", "list", "--format"]:
            return _completed(
                json.dumps(
                    [
                        {
                            "Name": "podman-machine-default",
                            "VMType": "wsl",
                            "Running": True,
                        }
                    ]
                )
            )
        if command[1:4] == ["--connection", "podman-machine-default", "info"]:
            return _completed(json.dumps({"host": {"os": "linux"}}))
        raise AssertionError(command)

    monkeypatch.setattr(podman, "_run_podman", fake_run)
    target = inspect_podman_machine_target(_ENV)

    assert target.connection_name == "podman-machine-default"
    assert target.machine_name == "podman-machine-default"
    assert target.vm_type == "wsl"
    assert not target.rootful
    assert any("--connection" in call for call in calls)


def test_target_rejects_remote_default(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(
        command: list[str],
        environment: Mapping[str, str] | None,
        *,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        del environment, timeout
        if command[1:5] == ["system", "connection", "list", "--format"]:
            return _completed(
                json.dumps(
                    [
                        {
                            "Name": "prod",
                            "URI": "ssh://root@example.com/run/podman/podman.sock",
                            "IsMachine": False,
                            "Default": True,
                        }
                    ]
                )
            )
        raise AssertionError(command)

    monkeypatch.setattr(podman, "_run_podman", fake_run)
    with pytest.raises(RuntimeError, match="不是 Podman-managed machine"):
        inspect_podman_machine_target(_ENV)


def test_target_accepts_documented_root_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(
        command: list[str],
        environment: Mapping[str, str] | None,
        *,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        del environment, timeout
        if command[1:5] == ["system", "connection", "list", "--format"]:
            return _completed(
                json.dumps(
                    [
                        {
                            "Name": "podman-machine-default-root",
                            "URI": "ssh://root@localhost:55123/run/podman/podman.sock",
                            "IsMachine": True,
                            "Default": True,
                        }
                    ]
                )
            )
        if command[1:4] == ["machine", "list", "--format"]:
            return _completed(
                json.dumps(
                    [
                        {
                            "Name": "podman-machine-default",
                            "VMType": "hyperv",
                            "Running": True,
                        }
                    ]
                )
            )
        if command[1:4] == ["--connection", "podman-machine-default-root", "info"]:
            return _completed(json.dumps({"host": {"os": "linux"}}))
        raise AssertionError(command)

    monkeypatch.setattr(podman, "_run_podman", fake_run)
    target = inspect_podman_machine_target(_ENV)
    assert target.rootful
    assert target.vm_type == "hyperv"


def test_container_entry_protects_running_paused_pod_and_infra() -> None:
    base = {
        "Id": "a" * 64,
        "Name": "demo",
        "Image": "b" * 64,
        "ImageName": "alpine:latest",
        "Created": "2026-08-01T00:00:00Z",
        "Mounts": [],
    }

    running = podman._container_entry(
        {**base, "State": {"Status": "running", "Running": True, "Paused": False}}
    )
    paused = podman._container_entry(
        {**base, "State": {"Status": "paused", "Running": False, "Paused": True}}
    )
    pod_member = podman._container_entry(
        {
            **base,
            "State": {"Status": "exited", "Running": False, "Paused": False},
            "Pod": "c" * 64,
        }
    )
    infra = podman._container_entry(
        {
            **base,
            "State": {"Status": "exited", "Running": False, "Paused": False},
            "IsInfra": True,
        }
    )

    assert not running.executable
    assert not paused.executable
    assert not pod_member.executable
    assert not infra.executable


def test_container_entry_fails_closed_on_transitional_or_unrecognized_status() -> None:
    base = {
        "Id": "a" * 64,
        "Name": "demo",
        "Image": "b" * 64,
        "ImageName": "alpine:latest",
        "Created": "2026-08-01T00:00:00Z",
        "Mounts": [],
    }

    for status in ("stopping", "removing", "initialized", "mystery"):
        entry = podman._container_entry(
            {**base, "State": {"Status": status, "Running": False, "Paused": False}}
        )
        assert not entry.executable
        assert "白名单" in entry.reason

    for status in ("configured", "created", "exited", "stopped"):
        entry = podman._container_entry(
            {**base, "State": {"Status": status, "Running": False, "Paused": False}}
        )
        assert entry.executable


def test_inspect_pins_exact_connection_and_collects_vendor_df(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _target()
    monkeypatch.setattr(podman, "inspect_podman_machine_target", lambda environment=None: target)
    calls: list[tuple[str, ...]] = []

    def fake_run(
        command: list[str],
        environment: Mapping[str, str] | None,
        *,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        del environment, timeout
        calls.append(tuple(command))
        if "ps" in command:
            return _completed("a" * 64 + "\n")
        if "inspect" in command:
            return _completed(
                json.dumps(
                    [
                        {
                            "Id": "a" * 64,
                            "Name": "demo",
                            "Image": "b" * 64,
                            "ImageName": "alpine:latest",
                            "Created": "2026-08-01T00:00:00Z",
                            "State": {"Status": "exited", "Running": False, "Paused": False},
                            "Mounts": [{"Type": "volume", "Name": "data"}],
                            "SizeRw": 123,
                            "SizeRootFs": 456,
                        }
                    ]
                )
            )
        if "df" in command:
            return _completed('[{"Type":"Containers","RawSize":123}]')
        raise AssertionError(command)

    monkeypatch.setattr(podman, "_run_podman", fake_run)
    inventory = inspect_podman_containers(_ENV)

    assert len(inventory.containers) == 1
    assert inventory.containers[0].executable
    assert inventory.containers[0].volume_names == ("data",)
    assert all(call[1:3] == ("--connection", "podman-machine-default") for call in calls)


def test_remove_uses_only_exact_rm_without_force_or_volumes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _target()
    expected = _container()
    before = podman.PodmanContainerInventory(target, (expected,), "before")
    after = podman.PodmanContainerInventory(target, (), "after")
    inventories = iter((before, before, after))
    monkeypatch.setattr(
        podman, "inspect_podman_containers", lambda environment=None: next(inventories)
    )
    commands: list[tuple[str, ...]] = []

    def fake_run(
        command: list[str],
        environment: Mapping[str, str] | None,
        *,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        del environment, timeout
        commands.append(tuple(command))
        return _completed(expected.container_id + "\n")

    monkeypatch.setattr(podman, "_run_podman", fake_run)
    result = remove_podman_container(expected, target, _ENV)

    assert result.command == (
        "podman.exe",
        "--connection",
        "podman-machine-default",
        "rm",
        expected.container_id,
    )
    flat = " ".join(result.command)
    assert "--force" not in flat
    assert "--volumes" not in flat
    assert "--all" not in flat
    assert commands == [result.command]


def test_remove_refuses_reviewed_target_change(monkeypatch: pytest.MonkeyPatch) -> None:
    reviewed_target = _target()
    changed_target = PodmanMachineConnection(
        executable="podman.exe",
        connection_name="other-machine",
        connection_uri="ssh://user@127.0.0.1:55222/run/user/1000/podman/podman.sock",
        machine_name="other-machine",
        vm_type="wsl",
        running=True,
        rootful=False,
    )
    expected = _container()
    inventory = podman.PodmanContainerInventory(changed_target, (expected,), "before")
    monkeypatch.setattr(podman, "inspect_podman_containers", lambda environment=None: inventory)
    monkeypatch.setattr(
        podman,
        "_run_podman",
        lambda *args, **kwargs: pytest.fail("rm must not run on a different reviewed target"),
    )

    with pytest.raises(RuntimeError, match="查看/确认的目标已不同"):
        remove_podman_container(expected, reviewed_target, _ENV)


def test_remove_refuses_identity_change(monkeypatch: pytest.MonkeyPatch) -> None:
    target = _target()
    expected = _container()
    changed = _container(status="stopped")
    inventories = iter(
        (
            podman.PodmanContainerInventory(target, (expected,), "before"),
            podman.PodmanContainerInventory(target, (changed,), "before2"),
        )
    )
    monkeypatch.setattr(
        podman, "inspect_podman_containers", lambda environment=None: next(inventories)
    )
    monkeypatch.setattr(
        podman,
        "_run_podman",
        lambda *args, **kwargs: pytest.fail("rm must not run after state change"),
    )

    with pytest.raises(RuntimeError, match="绑定已变化"):
        remove_podman_container(expected, target, _ENV)


def test_remove_refuses_non_executable_container(monkeypatch: pytest.MonkeyPatch) -> None:
    target = _target()
    expected = _container(running=True, status="running")
    inventory = podman.PodmanContainerInventory(target, (expected,), "before")
    monkeypatch.setattr(podman, "inspect_podman_containers", lambda environment=None: inventory)

    with pytest.raises(RuntimeError):
        remove_podman_container(expected, target, _ENV)
