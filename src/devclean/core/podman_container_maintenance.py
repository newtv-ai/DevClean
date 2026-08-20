"""Exact stopped-container inventory and user-directed Podman removal on Windows."""

# ruff: noqa: RUF001

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from urllib.parse import urlparse

_BATCH_SIZE = 40
_ALLOWED_VM_TYPES = frozenset({"wsl", "hyperv"})
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_SAFE_REMOVAL_STATUSES = frozenset({"configured", "created", "exited", "stopped"})


@dataclass(frozen=True, slots=True)
class PodmanMachineConnection:
    executable: str
    connection_name: str
    connection_uri: str
    machine_name: str
    vm_type: str
    running: bool
    rootful: bool


@dataclass(frozen=True, slots=True)
class PodmanContainerEntry:
    container_id: str
    name: str
    image_id: str
    image_name: str
    created: str
    status: str
    running: bool
    paused: bool
    pod_id: str
    is_infra: bool
    writable_size: int
    rootfs_size: int
    volume_names: tuple[str, ...]
    executable: bool
    reason: str


@dataclass(frozen=True, slots=True)
class PodmanContainerInventory:
    target: PodmanMachineConnection
    containers: tuple[PodmanContainerEntry, ...]
    system_df: str


@dataclass(frozen=True, slots=True)
class PodmanContainerRemoveResult:
    target: PodmanMachineConnection
    container: PodmanContainerEntry
    command: tuple[str, ...]
    stdout: str
    system_df_before: str
    system_df_after: str


def podman_executable(environment: Mapping[str, str] | None = None) -> str:
    env = _casefold_env(environment)
    configured = env.get("devclean_podman_exe")
    if configured:
        return configured
    if environment is None:
        for name in ("podman.exe", "podman") if os.name == "nt" else ("podman",):
            located = shutil.which(name)
            if located:
                return located
    return "podman.exe" if os.name == "nt" else "podman"


def inspect_podman_machine_target(
    environment: Mapping[str, str] | None = None,
) -> PodmanMachineConnection:
    """Bind the current default Podman connection to one local managed Windows VM."""

    if os.name != "nt" and not _test_windows_override(environment):
        raise RuntimeError("Podman Windows 维护仅支持 Windows")

    executable = podman_executable(environment)
    connections = _json_list(
        _run_podman(
            [executable, "system", "connection", "list", "--format", "json"],
            environment,
            timeout=30,
        ).stdout,
        "podman system connection list",
    )
    defaults = [item for item in connections if item.get("Default") is True]
    if len(defaults) != 1:
        raise RuntimeError(f"无法唯一确认 Podman 默认连接: found={len(defaults)}")
    connection = defaults[0]
    if connection.get("IsMachine") is not True:
        raise RuntimeError("当前 Podman 默认连接不是 Podman-managed machine；仅允许只读检查")

    name = _required_string(connection, "Name", "Podman connection")
    uri = _required_string(connection, "URI", "Podman connection")
    _require_loopback_machine_uri(uri)

    machines = _json_list(
        _run_podman(
            [executable, "machine", "list", "--format", "json"],
            environment,
            timeout=30,
        ).stdout,
        "podman machine list",
    )
    matches: list[tuple[dict[str, object], bool]] = []
    for machine in machines:
        machine_name = str(machine.get("Name", "")).strip()
        if not machine_name:
            continue
        if name == machine_name:
            matches.append((machine, False))
        elif name == f"{machine_name}-root":
            matches.append((machine, True))
    if len(matches) != 1:
        raise RuntimeError("Podman 默认 machine connection 无法唯一绑定到本机 managed machine")
    machine, rootful = matches[0]
    machine_name = _required_string(machine, "Name", "Podman machine")
    vm_type = _required_string(machine, "VMType", "Podman machine").casefold()
    if vm_type not in _ALLOWED_VM_TYPES:
        raise RuntimeError(f"Podman machine provider 不在 Windows 审计范围内: {vm_type}")
    running = machine.get("Running")
    if not isinstance(running, bool):
        raise RuntimeError("Podman machine list 缺少 Running boolean")

    # Pin the exact connection and verify it resolves to a Linux Podman service.
    info = _json_object(
        _run_podman(
            [executable, "--connection", name, "info", "--format", "json"],
            environment,
            timeout=60,
        ).stdout,
        "podman info",
    )
    host = info.get("host")
    if not isinstance(host, dict):
        host = info.get("Host")
    if not isinstance(host, dict):
        raise RuntimeError("podman info 缺少 host")
    host_os = str(host.get("os", host.get("OS", ""))).strip().casefold()
    if host_os != "linux":
        raise RuntimeError(f"Podman machine host OS 非 Linux: {host_os or 'unknown'}")

    return PodmanMachineConnection(
        executable=executable,
        connection_name=name,
        connection_uri=uri,
        machine_name=machine_name,
        vm_type=vm_type,
        running=running,
        rootful=rootful,
    )


def inspect_podman_containers(
    environment: Mapping[str, str] | None = None,
) -> PodmanContainerInventory:
    """Inventory exact containers on one exact Podman-managed Windows machine."""

    target = inspect_podman_machine_target(environment)
    ids = _unique_lines(
        _run_podman(
            [
                target.executable,
                "--connection",
                target.connection_name,
                "ps",
                "--all",
                "--no-trunc",
                "--quiet",
            ],
            environment,
            timeout=60,
        ).stdout
    )
    payloads = _inspect_containers(target, ids, environment)
    entries = tuple(
        sorted(
            (_container_entry(payload) for payload in payloads),
            key=lambda item: item.writable_size,
            reverse=True,
        )
    )
    return PodmanContainerInventory(
        target=target,
        containers=entries,
        system_df=_system_df(target, environment),
    )


def remove_podman_container(
    expected: PodmanContainerEntry,
    expected_target: PodmanMachineConnection,
    environment: Mapping[str, str] | None = None,
) -> PodmanContainerRemoveResult:
    """Remove one reviewed container only from the exact reviewed machine target."""

    initial = inspect_podman_containers(environment)
    if _target_key(initial.target) != _target_key(expected_target):
        raise RuntimeError("Podman machine connection 与用户查看/确认的目标已不同；请重新检查")
    current = _exact_container(initial.containers, expected.container_id)
    _require_same_container(expected, current)
    if not current.executable:
        raise RuntimeError(current.reason)

    fresh_inventory = inspect_podman_containers(environment)
    if _target_key(fresh_inventory.target) != _target_key(initial.target):
        raise RuntimeError("Podman machine connection 在执行前发生变化；请重新检查")
    fresh = _exact_container(fresh_inventory.containers, expected.container_id)
    _require_same_container(expected, fresh)
    if not fresh.executable:
        raise RuntimeError(fresh.reason)

    command = (
        fresh_inventory.target.executable,
        "--connection",
        fresh_inventory.target.connection_name,
        "rm",
        fresh.container_id,
    )
    result = _run_podman(list(command), environment, timeout=300)

    after = inspect_podman_containers(environment)
    if _target_key(after.target) != _target_key(fresh_inventory.target):
        raise RuntimeError("Podman machine connection 在删除后发生变化；无法确认结果")
    if any(item.container_id == fresh.container_id for item in after.containers):
        raise RuntimeError("podman rm 返回成功，但精确 container ID 仍然存在")
    return PodmanContainerRemoveResult(
        target=fresh_inventory.target,
        container=fresh,
        command=command,
        stdout=(result.stdout or result.stderr).strip(),
        system_df_before=fresh_inventory.system_df,
        system_df_after=after.system_df,
    )


def _inspect_containers(
    target: PodmanMachineConnection,
    ids: tuple[str, ...],
    environment: Mapping[str, str] | None,
) -> tuple[dict[str, object], ...]:
    if not ids:
        return ()
    payloads: list[dict[str, object]] = []
    for start in range(0, len(ids), _BATCH_SIZE):
        batch = ids[start : start + _BATCH_SIZE]
        decoded = _json_list(
            _run_podman(
                [
                    target.executable,
                    "--connection",
                    target.connection_name,
                    "container",
                    "inspect",
                    "--size",
                    *batch,
                ],
                environment,
                timeout=120,
            ).stdout,
            "podman container inspect",
        )
        payloads.extend(decoded)
    return tuple(payloads)


def _container_entry(payload: dict[str, object]) -> PodmanContainerEntry:
    container_id = _required_string_any(payload, ("Id", "ID"), "Podman container")
    name = _required_string(payload, "Name", "Podman container").lstrip("/")
    image_id = _required_string_any(payload, ("Image", "ImageID"), "Podman container")
    image_name = str(payload.get("ImageName", payload.get("ImageNameRaw", ""))).strip()
    created = _required_string(payload, "Created", "Podman container")

    state = payload.get("State")
    if not isinstance(state, dict):
        raise RuntimeError("Podman container inspect 缺少 State")
    status = str(state.get("Status", "")).strip().casefold()
    running = state.get("Running")
    paused = state.get("Paused")
    if not status or not isinstance(running, bool) or not isinstance(paused, bool):
        raise RuntimeError("Podman container inspect State 缺少 Status/Running/Paused")

    pod_id = str(payload.get("Pod", "") or "").strip()
    is_infra_raw = payload.get("IsInfra", False)
    if not isinstance(is_infra_raw, bool):
        raise RuntimeError("Podman container inspect IsInfra 不是 boolean")
    writable_size = _optional_nonnegative_int(payload.get("SizeRw", payload.get("SizeRW", 0)))
    rootfs_size = _optional_nonnegative_int(payload.get("SizeRootFs", 0))
    volume_names = _volume_names(payload.get("Mounts"))

    executable = status in _SAFE_REMOVAL_STATUSES
    reason = (
        "container 已处于明确终止状态；writable layer 可能包含唯一用户状态，仅由用户决定是否删除"
        if executable
        else f"container 状态 {status!r} 不在审计过的终止状态白名单内；不允许删除"
    )
    if running:
        executable = False
        reason = "container 当前正在运行；不允许删除"
    elif paused:
        executable = False
        reason = "container 当前处于 paused 状态；不允许删除"
    elif status not in _SAFE_REMOVAL_STATUSES:
        executable = False
        reason = f"container 状态 {status!r} 不在审计过的终止状态白名单内；不允许删除"
    elif is_infra_raw:
        executable = False
        reason = "Pod infra container 属于 pod 生命周期；当前 lane 不删除"
    elif pod_id:
        executable = False
        reason = "container 属于 Pod；当前 lane 不改变 pod 拓扑"

    return PodmanContainerEntry(
        container_id=container_id,
        name=name,
        image_id=image_id,
        image_name=image_name,
        created=created,
        status=status,
        running=running,
        paused=paused,
        pod_id=pod_id,
        is_infra=is_infra_raw,
        writable_size=writable_size,
        rootfs_size=rootfs_size,
        volume_names=volume_names,
        executable=executable,
        reason=reason,
    )


def _volume_names(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise RuntimeError("Podman container inspect Mounts 不是 list")
    names: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise RuntimeError("Podman container inspect Mounts 包含非 object")
        if str(item.get("Type", "")).strip().casefold() != "volume":
            continue
        name = str(item.get("Name", "")).strip()
        if name:
            names.add(name)
    return tuple(sorted(names))


def _system_df(
    target: PodmanMachineConnection,
    environment: Mapping[str, str] | None,
) -> str:
    return _run_podman(
        [
            target.executable,
            "--connection",
            target.connection_name,
            "system",
            "df",
            "--format",
            "json",
        ],
        environment,
        timeout=60,
    ).stdout.strip()


def _require_loopback_machine_uri(uri: str) -> None:
    parsed = urlparse(uri)
    if parsed.scheme.casefold() != "ssh":
        raise RuntimeError("Podman Windows machine connection 必须是 managed SSH loopback")
    host = (parsed.hostname or "").casefold()
    if host not in _LOOPBACK_HOSTS:
        raise RuntimeError(f"Podman connection 指向非本机 endpoint: {host or uri}")


def _exact_container(
    containers: Sequence[PodmanContainerEntry],
    container_id: str,
) -> PodmanContainerEntry:
    matches = [item for item in containers if item.container_id == container_id]
    if len(matches) != 1:
        raise RuntimeError(f"无法唯一确认 Podman container {container_id!r}: found={len(matches)}")
    return matches[0]


def _require_same_container(
    expected: PodmanContainerEntry,
    current: PodmanContainerEntry,
) -> None:
    if (
        current.container_id != expected.container_id
        or current.name != expected.name
        or current.image_id != expected.image_id
        or current.image_name != expected.image_name
        or current.created != expected.created
        or current.status != expected.status
        or current.running != expected.running
        or current.paused != expected.paused
        or current.pod_id != expected.pod_id
        or current.is_infra != expected.is_infra
        or current.volume_names != expected.volume_names
    ):
        raise RuntimeError("Podman container 身份/image/state/pod/volume 绑定已变化；请重新检查")


def _target_key(target: PodmanMachineConnection) -> tuple[str, str, str, str, bool, str]:
    return (
        target.connection_name,
        target.connection_uri,
        target.machine_name,
        target.vm_type,
        target.rootful,
        target.executable,
    )


def _json_list(output: str, label: str) -> list[dict[str, object]]:
    try:
        value = json.loads(output)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"无法解析 {label} JSON") from error
    if not isinstance(value, list):
        raise RuntimeError(f"{label} 未返回 JSON array")
    items: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, dict):
            raise RuntimeError(f"{label} JSON array 包含非 object")
        items.append({str(key): val for key, val in item.items()})
    return items


def _json_object(output: str, label: str) -> dict[str, object]:
    try:
        value = json.loads(output)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"无法解析 {label} JSON") from error
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} 未返回 JSON object")
    return {str(key): val for key, val in value.items()}


def _required_string(payload: dict[str, object], key: str, label: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{label} 缺少 {key}")
    return value.strip()


def _required_string_any(payload: dict[str, object], keys: tuple[str, ...], label: str) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise RuntimeError(f"{label} 缺少 {'/'.join(keys)}")


def _optional_nonnegative_int(value: object) -> int:
    if value is None:
        return 0
    try:
        parsed = int(str(value))
    except ValueError as error:
        raise RuntimeError("Podman container inspect 返回无效 size") from error
    return max(0, parsed)


def _unique_lines(output: str) -> tuple[str, ...]:
    found: list[str] = []
    seen: set[str] = set()
    for line in output.splitlines():
        value = line.strip()
        if value and value not in seen:
            seen.add(value)
            found.append(value)
    return tuple(found)


def _run_podman(
    command: list[str],
    environment: Mapping[str, str] | None,
    *,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    if environment is not None:
        env.update({str(key): str(value) for key, value in environment.items()})
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
        raise RuntimeError(f"无法执行 Podman CLI: {error}") from error
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"Podman CLI 失败 (exit {result.returncode}): {detail}")
    return result


def _casefold_env(environment: Mapping[str, str] | None) -> dict[str, str]:
    source = os.environ if environment is None else environment
    return {str(key).casefold(): str(value) for key, value in source.items()}


def _test_windows_override(environment: Mapping[str, str] | None) -> bool:
    if environment is None:
        return False
    value = _casefold_env(environment).get("devclean_test_windows")
    return value == "1"


__all__ = [
    "PodmanContainerEntry",
    "PodmanContainerInventory",
    "PodmanContainerRemoveResult",
    "PodmanMachineConnection",
    "inspect_podman_containers",
    "inspect_podman_machine_target",
    "podman_executable",
    "remove_podman_container",
]
