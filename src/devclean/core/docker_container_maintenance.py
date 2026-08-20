"""Exact stopped-container inventory and user-directed Docker removal."""

# ruff: noqa: RUF001

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from devclean.core.docker_cleanup import docker_executable
from devclean.core.docker_maintenance import (
    DockerDaemonIdentity,
    inspect_docker_daemon_target,
)

_BATCH_SIZE = 40


@dataclass(frozen=True, slots=True)
class DockerContainerEntry:
    container_id: str
    name: str
    image_id: str
    image_ref: str
    created: str
    status: str
    running: bool
    writable_size: int
    rootfs_size: int
    volume_names: tuple[str, ...]
    executable: bool
    reason: str


@dataclass(frozen=True, slots=True)
class DockerContainerInventory:
    daemon: DockerDaemonIdentity
    containers: tuple[DockerContainerEntry, ...]


@dataclass(frozen=True, slots=True)
class DockerContainerRemoveResult:
    daemon: DockerDaemonIdentity
    container: DockerContainerEntry
    command: tuple[str, ...]
    stdout: str
    system_df_before: str
    system_df_after: str


def inspect_docker_containers(
    environment: Mapping[str, str] | None = None,
) -> DockerContainerInventory:
    """Inventory exact containers and writable-layer sizes on one local daemon."""

    daemon = _require_local_daemon(environment)
    executable = docker_executable(environment)
    selector = _target_selector(daemon)
    ids = _unique_lines(
        _run_docker(
            [
                executable,
                *selector,
                "container",
                "ls",
                "--all",
                "--no-trunc",
                "--quiet",
            ],
            environment,
            timeout=60,
        ).stdout
    )
    payloads = _inspect_containers(executable, selector, ids, environment)
    entries = tuple(
        sorted(
            (_container_entry(payload) for payload in payloads),
            key=lambda item: item.writable_size,
            reverse=True,
        )
    )
    return DockerContainerInventory(daemon=daemon, containers=entries)


def remove_docker_container(
    expected: DockerContainerEntry,
    environment: Mapping[str, str] | None = None,
) -> DockerContainerRemoveResult:
    """Remove one exact stopped container without force and without volumes."""

    initial = inspect_docker_containers(environment)
    current = _exact_container(initial.containers, expected.container_id)
    _require_same_container(expected, current)
    if not current.executable:
        raise RuntimeError(current.reason)

    fresh_inventory = inspect_docker_containers(environment)
    if _daemon_key(fresh_inventory.daemon) != _daemon_key(initial.daemon):
        raise RuntimeError("Docker daemon/context 在执行前发生变化; 请重新检查")
    fresh = _exact_container(fresh_inventory.containers, expected.container_id)
    _require_same_container(expected, fresh)
    if not fresh.executable:
        raise RuntimeError(fresh.reason)

    executable = docker_executable(environment)
    selector = _target_selector(fresh_inventory.daemon)
    before_df = _system_df(executable, selector, environment)
    command = (
        executable,
        *selector,
        "container",
        "rm",
        fresh.container_id,
    )
    result = _run_docker(list(command), environment, timeout=300)

    after_inventory = inspect_docker_containers(environment)
    if _daemon_key(after_inventory.daemon) != _daemon_key(fresh_inventory.daemon):
        raise RuntimeError("Docker daemon/context 在 container 删除后发生变化; 无法确认结果")
    if any(item.container_id == fresh.container_id for item in after_inventory.containers):
        raise RuntimeError("Docker container rm 返回成功，但精确 container ID 仍然存在")
    after_df = _system_df(executable, selector, environment)
    return DockerContainerRemoveResult(
        daemon=fresh_inventory.daemon,
        container=fresh,
        command=command,
        stdout=(result.stdout or result.stderr).strip(),
        system_df_before=before_df,
        system_df_after=after_df,
    )


def _container_entry(payload: dict[str, object]) -> DockerContainerEntry:
    container_id = _required_string(payload, "Id", "Docker container inspect")
    name = _required_string(payload, "Name", "Docker container inspect").lstrip("/")
    image_id = _required_string(payload, "Image", "Docker container inspect")
    created = _required_string(payload, "Created", "Docker container inspect")

    config = payload.get("Config")
    if not isinstance(config, dict):
        raise RuntimeError("Docker container inspect 缺少 Config")
    image_ref_raw = config.get("Image")
    image_ref = str(image_ref_raw).strip() if image_ref_raw is not None else ""

    state = payload.get("State")
    if not isinstance(state, dict):
        raise RuntimeError("Docker container inspect 缺少 State")
    status = str(state.get("Status", "")).strip()
    running = state.get("Running")
    if not status or not isinstance(running, bool):
        raise RuntimeError("Docker container inspect State 缺少 Status/Running")

    writable_size = _optional_nonnegative_int(payload.get("SizeRw"))
    rootfs_size = _optional_nonnegative_int(payload.get("SizeRootFs"))
    volume_names = _volume_names(payload.get("Mounts"))
    executable = not running
    reason = (
        "container 当前正在运行; 不允许删除"
        if running
        else "container 已停止，但 writable layer 可能包含唯一用户状态；仅由用户决定是否删除"
    )
    return DockerContainerEntry(
        container_id=container_id,
        name=name,
        image_id=image_id,
        image_ref=image_ref,
        created=created,
        status=status,
        running=running,
        writable_size=writable_size,
        rootfs_size=rootfs_size,
        volume_names=volume_names,
        executable=executable,
        reason=reason,
    )


def _inspect_containers(
    executable: str,
    selector: tuple[str, str],
    ids: tuple[str, ...],
    environment: Mapping[str, str] | None,
) -> tuple[dict[str, object], ...]:
    if not ids:
        return ()
    payloads: list[dict[str, object]] = []
    for start in range(0, len(ids), _BATCH_SIZE):
        batch = ids[start : start + _BATCH_SIZE]
        result = _run_docker(
            [
                executable,
                *selector,
                "container",
                "inspect",
                "--size",
                *batch,
            ],
            environment,
            timeout=120,
        )
        try:
            decoded = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError("无法解析 docker container inspect JSON") from error
        if not isinstance(decoded, list):
            raise RuntimeError("docker container inspect 未返回 JSON array")
        for item in decoded:
            if not isinstance(item, dict):
                raise RuntimeError("docker container inspect 返回非 object JSON")
            payloads.append({str(key): value for key, value in item.items()})
    return tuple(payloads)


def _volume_names(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise RuntimeError("Docker container inspect Mounts 不是 list")
    names: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise RuntimeError("Docker container inspect Mounts 包含非 object")
        if str(item.get("Type", "")).strip() != "volume":
            continue
        name = str(item.get("Name", "")).strip()
        if name:
            names.add(name)
    return tuple(sorted(names))


def _exact_container(
    containers: Sequence[DockerContainerEntry],
    container_id: str,
) -> DockerContainerEntry:
    matches = [item for item in containers if item.container_id == container_id]
    if len(matches) != 1:
        raise RuntimeError(f"无法唯一确认 Docker container {container_id!r}: found={len(matches)}")
    return matches[0]


def _require_same_container(
    expected: DockerContainerEntry,
    current: DockerContainerEntry,
) -> None:
    if (
        current.container_id != expected.container_id
        or current.name != expected.name
        or current.image_id != expected.image_id
        or current.image_ref != expected.image_ref
        or current.created != expected.created
        or current.status != expected.status
        or current.running != expected.running
        or current.volume_names != expected.volume_names
    ):
        raise RuntimeError("Docker container 身份/image/state/volume 绑定已变化; 请重新检查")


def _system_df(
    executable: str,
    selector: tuple[str, str],
    environment: Mapping[str, str] | None,
) -> str:
    return _run_docker(
        [executable, *selector, "system", "df", "--format", "json"],
        environment,
        timeout=60,
    ).stdout.strip()


def _require_local_daemon(
    environment: Mapping[str, str] | None,
) -> DockerDaemonIdentity:
    daemon = inspect_docker_daemon_target(environment)
    if not daemon.local:
        raise RuntimeError(
            f"DevClean 只维护本机 Docker daemon; 当前 endpoint={daemon.endpoint} ({daemon.source})"
        )
    return daemon


def _target_selector(target: DockerDaemonIdentity) -> tuple[str, str]:
    if target.context_name is not None:
        return "--context", target.context_name
    return "--host", target.endpoint


def _daemon_key(target: DockerDaemonIdentity) -> tuple[str | None, str, str]:
    return target.context_name, target.endpoint, target.source


def _unique_lines(output: str) -> tuple[str, ...]:
    seen: set[str] = set()
    values: list[str] = []
    for raw in output.splitlines():
        value = raw.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        values.append(value)
    return tuple(values)


def _required_string(payload: dict[str, object], key: str, label: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{label} 缺少 {key}")
    return value.strip()


def _optional_nonnegative_int(value: object) -> int:
    if value is None:
        return 0
    try:
        parsed = int(str(value))
    except ValueError as error:
        raise RuntimeError("Docker container inspect 返回无效 size") from error
    return max(0, parsed)


def _run_docker(
    command: list[str],
    environment: Mapping[str, str] | None,
    *,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    if environment is not None:
        env.update(environment)
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"无法执行 Docker container CLI: {error}") from error
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"Docker container CLI 失败 (exit {result.returncode}): {detail}")
    return result


__all__ = [
    "DockerContainerEntry",
    "DockerContainerInventory",
    "DockerContainerRemoveResult",
    "inspect_docker_containers",
    "remove_docker_container",
]
