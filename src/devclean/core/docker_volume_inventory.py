"""Read-only Docker volume inventory for persistent-data review."""

# ruff: noqa: RUF001

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass

from devclean.core.docker_cleanup import docker_executable
from devclean.core.docker_maintenance import (
    DockerDaemonIdentity,
    inspect_docker_daemon_target,
)

_BATCH_SIZE = 40


@dataclass(frozen=True, slots=True)
class DockerVolumeEntry:
    name: str
    driver: str
    scope: str
    mountpoint: str
    created_at: str
    labels: tuple[tuple[str, str], ...]
    options: tuple[tuple[str, str], ...]
    container_ids: tuple[str, ...]
    executable: bool
    reason: str

    @property
    def referenced(self) -> bool:
        return bool(self.container_ids)


@dataclass(frozen=True, slots=True)
class DockerVolumeInventory:
    daemon: DockerDaemonIdentity
    volumes: tuple[DockerVolumeEntry, ...]


def inspect_docker_volumes(
    environment: Mapping[str, str] | None = None,
) -> DockerVolumeInventory:
    """Inventory exact Docker volumes without granting volume deletion authority."""

    daemon = _require_local_daemon(environment)
    executable = docker_executable(environment)
    selector = _target_selector(daemon)

    volume_names = _unique_lines(
        _run_docker(
            [executable, *selector, "volume", "ls", "--quiet"],
            environment,
            timeout=60,
        ).stdout
    )
    volume_payloads = _inspect_volumes(
        executable,
        selector,
        volume_names,
        environment,
    )

    container_ids = _unique_lines(
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
    container_payloads = _inspect_containers(
        executable,
        selector,
        container_ids,
        environment,
    )
    references = _volume_references(container_payloads)

    entries: list[DockerVolumeEntry] = []
    for payload in volume_payloads:
        name = _required_string(payload, "Name", "Docker volume inspect")
        driver = _required_string(payload, "Driver", "Docker volume inspect")
        scope = _optional_string(payload.get("Scope"))
        mountpoint = _optional_string(payload.get("Mountpoint"))
        created_at = _optional_string(payload.get("CreatedAt"))
        consumers = tuple(sorted(references.get(name, ())))
        reason = (
            "volume 当前被 container 引用；属于持久数据，仅报告"
            if consumers
            else "volume 当前未被已知 container 引用，但 Docker volume 仍可能包含唯一持久数据；仅报告"
        )
        entries.append(
            DockerVolumeEntry(
                name=name,
                driver=driver,
                scope=scope,
                mountpoint=mountpoint,
                created_at=created_at,
                labels=_string_mapping(payload.get("Labels"), "Labels"),
                options=_string_mapping(payload.get("Options"), "Options"),
                container_ids=consumers,
                executable=False,
                reason=reason,
            )
        )

    entries.sort(key=lambda item: item.name.casefold())
    return DockerVolumeInventory(daemon=daemon, volumes=tuple(entries))


def _inspect_volumes(
    executable: str,
    selector: tuple[str, str],
    names: tuple[str, ...],
    environment: Mapping[str, str] | None,
) -> tuple[dict[str, object], ...]:
    if not names:
        return ()
    payloads: list[dict[str, object]] = []
    for start in range(0, len(names), _BATCH_SIZE):
        batch = names[start : start + _BATCH_SIZE]
        result = _run_docker(
            [executable, *selector, "volume", "inspect", *batch],
            environment,
            timeout=120,
        )
        payloads.extend(_decode_object_array(result.stdout, "docker volume inspect"))
    return tuple(payloads)


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
            [executable, *selector, "container", "inspect", *batch],
            environment,
            timeout=120,
        )
        payloads.extend(_decode_object_array(result.stdout, "docker container inspect"))
    return tuple(payloads)


def _decode_object_array(output: str, label: str) -> tuple[dict[str, object], ...]:
    try:
        decoded = json.loads(output)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"无法解析 {label} JSON") from error
    if not isinstance(decoded, list):
        raise RuntimeError(f"{label} 未返回 JSON array")
    payloads: list[dict[str, object]] = []
    for item in decoded:
        if not isinstance(item, dict):
            raise RuntimeError(f"{label} 返回非 object JSON")
        payloads.append({str(key): value for key, value in item.items()})
    return tuple(payloads)


def _volume_references(
    container_payloads: tuple[dict[str, object], ...],
) -> dict[str, set[str]]:
    references: dict[str, set[str]] = {}
    for payload in container_payloads:
        container_id = _required_string(payload, "Id", "Docker container inspect")
        mounts = payload.get("Mounts")
        if mounts is None:
            continue
        if not isinstance(mounts, list):
            raise RuntimeError("Docker container inspect Mounts 不是 list")
        for mount in mounts:
            if not isinstance(mount, dict):
                raise RuntimeError("Docker container inspect Mounts 包含非 object")
            if str(mount.get("Type", "")).strip() != "volume":
                continue
            name = str(mount.get("Name", "")).strip()
            if name:
                references.setdefault(name, set()).add(container_id)
    return references


def _string_mapping(value: object, label: str) -> tuple[tuple[str, str], ...]:
    if value is None:
        return ()
    if not isinstance(value, dict):
        raise RuntimeError(f"Docker volume inspect {label} 不是 object")
    return tuple(
        sorted((str(key), str(item)) for key, item in value.items())
    )


def _optional_string(value: object) -> str:
    return "" if value is None else str(value).strip()


def _required_string(payload: dict[str, object], key: str, label: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{label} 缺少 {key}")
    return value.strip()


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


def _require_local_daemon(
    environment: Mapping[str, str] | None,
) -> DockerDaemonIdentity:
    daemon = inspect_docker_daemon_target(environment)
    if not daemon.local:
        raise RuntimeError(
            "DevClean 只报告本机 Docker daemon 的 volume 存储；"
            f"当前 endpoint={daemon.endpoint} ({daemon.source})"
        )
    return daemon


def _target_selector(target: DockerDaemonIdentity) -> tuple[str, str]:
    if target.context_name is not None:
        return "--context", target.context_name
    return "--host", target.endpoint


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
        raise RuntimeError(f"无法执行 Docker volume inventory CLI: {error}") from error
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(
            f"Docker volume inventory CLI 失败 (exit {result.returncode}): {detail}"
        )
    return result


__all__ = [
    "DockerVolumeEntry",
    "DockerVolumeInventory",
    "inspect_docker_volumes",
]
