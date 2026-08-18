"""Docker Buildx cache inventory and vendor-owned maintenance."""

# ruff: noqa: RUF001

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass

from devclean.core.docker_cleanup import (
    clear_docker_process_cache,
    docker_executable,
    docker_process_running,
)
from devclean.core.docker_maintenance import (
    DockerDaemonIdentity,
    inspect_docker_daemon_target,
)

_MIN_RETENTION_HOURS = 24
_RECOMMEND_RECLAIM_BYTES = 1024**3
_KNOWN_DRIVERS = frozenset({"docker", "docker-container", "kubernetes", "remote"})


@dataclass(frozen=True, slots=True)
class BuildxNode:
    name: str
    endpoint: str
    status: str


@dataclass(frozen=True, slots=True)
class BuildxBuilder:
    name: str
    driver: str
    nodes: tuple[BuildxNode, ...]
    local: bool
    executable: bool
    reason: str


@dataclass(frozen=True, slots=True)
class BuildxCacheInventory:
    daemon: DockerDaemonIdentity
    builder: BuildxBuilder
    retention_hours: int
    record_count: int
    aged_reclaimable_bytes: int
    worth_maintaining: bool


@dataclass(frozen=True, slots=True)
class BuildxPruneResult:
    daemon: DockerDaemonIdentity
    builder: BuildxBuilder
    retention_hours: int
    before_reclaimable_bytes: int
    after_reclaimable_bytes: int
    command: tuple[str, ...]
    stdout: str

    @property
    def observed_reclaimed_bytes(self) -> int:
        return max(0, self.before_reclaimable_bytes - self.after_reclaimable_bytes)


def list_buildx_builders(
    environment: Mapping[str, str] | None = None,
) -> tuple[BuildxBuilder, ...]:
    """List Buildx builders and classify only source-proven local nodes executable."""

    daemon = _require_local_daemon(environment)
    return _list_buildx_builders_for_daemon(daemon, environment)


def inspect_buildx_cache(
    builder_name: str,
    environment: Mapping[str, str] | None = None,
    *,
    retention_hours: int = 168,
) -> BuildxCacheInventory:
    """Inventory cache records Buildx itself says are old enough and reclaimable."""

    _validate_retention(retention_hours)
    daemon = _require_local_daemon(environment)
    builder = _exact_builder(builder_name, daemon, environment)
    if not builder.executable:
        raise RuntimeError(builder.reason)

    records = _buildx_du_records(builder.name, daemon, environment, retention_hours)
    reclaimable = sum(record.size_bytes for record in records if record.reclaimable)
    return BuildxCacheInventory(
        daemon=daemon,
        builder=builder,
        retention_hours=retention_hours,
        record_count=len(records),
        aged_reclaimable_bytes=reclaimable,
        worth_maintaining=reclaimable >= _RECOMMEND_RECLAIM_BYTES,
    )


def prune_buildx_cache(
    builder_name: str,
    environment: Mapping[str, str] | None = None,
    *,
    retention_hours: int = 168,
) -> BuildxPruneResult:
    """Prune only old cache from one freshly revalidated local Buildx builder."""

    before = inspect_buildx_cache(
        builder_name,
        environment,
        retention_hours=retention_hours,
    )
    clear_docker_process_cache()
    if docker_process_running():
        raise RuntimeError("Docker/BuildKit 构建正在运行; 请等待构建完成后再清理")

    fresh = inspect_buildx_cache(
        builder_name,
        environment,
        retention_hours=retention_hours,
    )
    if _daemon_key(fresh.daemon) != _daemon_key(before.daemon):
        raise RuntimeError("Docker daemon/context 在执行前发生变化; 请重新检查")
    if _builder_key(fresh.builder) != _builder_key(before.builder):
        raise RuntimeError("Buildx builder 在执行前发生变化; 请重新检查")

    executable = docker_executable(environment)
    command = (
        executable,
        *_target_selector(fresh.daemon),
        "buildx",
        "prune",
        "--builder",
        fresh.builder.name,
        "--force",
        "--filter",
        f"until={retention_hours}h",
    )
    result = _run_docker(list(command), environment, timeout=1200)

    after = inspect_buildx_cache(
        builder_name,
        environment,
        retention_hours=retention_hours,
    )
    if _daemon_key(after.daemon) != _daemon_key(fresh.daemon):
        raise RuntimeError("Docker daemon/context 在 prune 后发生变化; 无法确认结果")
    if _builder_key(after.builder) != _builder_key(fresh.builder):
        raise RuntimeError("Buildx builder 在 prune 后发生变化; 无法确认结果")
    return BuildxPruneResult(
        daemon=fresh.daemon,
        builder=fresh.builder,
        retention_hours=retention_hours,
        before_reclaimable_bytes=fresh.aged_reclaimable_bytes,
        after_reclaimable_bytes=after.aged_reclaimable_bytes,
        command=command,
        stdout=(result.stdout or result.stderr).strip(),
    )


@dataclass(frozen=True, slots=True)
class _RawBuilder:
    name: str
    driver: str
    nodes: tuple[BuildxNode, ...]


@dataclass(frozen=True, slots=True)
class _DuRecord:
    record_id: str
    size_bytes: int
    reclaimable: bool


def _list_buildx_builders_for_daemon(
    daemon: DockerDaemonIdentity,
    environment: Mapping[str, str] | None,
) -> tuple[BuildxBuilder, ...]:
    executable = docker_executable(environment)
    command = [
        executable,
        *_target_selector(daemon),
        "buildx",
        "ls",
        "--format",
        "{{.Name}}\t{{.DriverEndpoint}}\t{{.Status}}",
    ]
    result = _run_docker(command, environment, timeout=60)
    raw = _parse_builder_rows(result.stdout)
    return tuple(
        _classify_builder(executable, builder, environment)
        for builder in raw
    )


def _parse_builder_rows(output: str) -> tuple[_RawBuilder, ...]:
    builders: list[_RawBuilder] = []
    current_name: str | None = None
    current_driver: str | None = None
    current_nodes: list[BuildxNode] = []

    def finish() -> None:
        nonlocal current_name, current_driver, current_nodes
        if current_name is None or current_driver is None:
            return
        builders.append(_RawBuilder(current_name, current_driver, tuple(current_nodes)))
        current_name = None
        current_driver = None
        current_nodes = []

    for line in output.splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) != 3:
            raise RuntimeError("无法解析 docker buildx ls 格式化输出")
        name = fields[0].strip().removesuffix("*").strip()
        driver_or_endpoint = fields[1].strip()
        status = fields[2].strip()
        if not name or not driver_or_endpoint:
            raise RuntimeError("docker buildx ls 返回不完整 builder/node 身份")
        if driver_or_endpoint in _KNOWN_DRIVERS:
            finish()
            current_name = name
            current_driver = driver_or_endpoint
            continue
        if current_name is None:
            raise RuntimeError("docker buildx ls 在 builder 之前返回了 node")
        current_nodes.append(BuildxNode(name, driver_or_endpoint, status))
    finish()
    return tuple(builders)


def _classify_builder(
    executable: str,
    builder: _RawBuilder,
    environment: Mapping[str, str] | None,
) -> BuildxBuilder:
    if not builder.nodes:
        return BuildxBuilder(
            builder.name,
            builder.driver,
            builder.nodes,
            False,
            False,
            "Buildx builder 没有可验证 node endpoint; 仅报告",
        )
    if builder.driver not in {"docker", "docker-container"}:
        return BuildxBuilder(
            builder.name,
            builder.driver,
            builder.nodes,
            False,
            False,
            f"Buildx driver={builder.driver} 可能是远程/集群后端; 仅报告",
        )

    local_nodes = tuple(
        _buildx_endpoint_is_local(executable, node.endpoint, environment)
        for node in builder.nodes
    )
    local = all(local_nodes)
    if not local:
        reason = "Buildx builder 至少有一个 node 不是可确认的本机 Docker endpoint; 仅报告"
    elif builder.driver == "docker" and len(builder.nodes) != 1:
        local = False
        reason = "docker driver 返回多个 node，超出当前可证明边界; 仅报告"
    else:
        reason = "Buildx builder 的全部 node 都绑定到可确认的本机 Docker endpoint"
    return BuildxBuilder(
        builder.name,
        builder.driver,
        builder.nodes,
        local,
        local,
        reason,
    )


def _buildx_endpoint_is_local(
    executable: str,
    endpoint: str,
    environment: Mapping[str, str] | None,
) -> bool:
    if _is_local_endpoint(endpoint):
        return True
    if "://" in endpoint:
        return False
    try:
        host = _context_host(executable, endpoint, environment)
    except RuntimeError:
        return False
    return _is_local_endpoint(host)


def _context_host(
    executable: str,
    context_name: str,
    environment: Mapping[str, str] | None,
) -> str:
    result = _run_docker(
        [executable, "context", "inspect", context_name],
        environment,
        timeout=30,
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("无法解析 docker context inspect JSON 输出") from error
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        raise RuntimeError("docker context inspect 未返回唯一 context")
    item = payload[0]
    if str(item.get("Name", "")) != context_name:
        raise RuntimeError("docker context inspect 返回了意外 context")
    endpoints = item.get("Endpoints")
    if not isinstance(endpoints, dict):
        raise RuntimeError("Docker context 缺少 Endpoints")
    docker_endpoint = endpoints.get("docker")
    if not isinstance(docker_endpoint, dict):
        raise RuntimeError("Docker context 缺少 docker endpoint")
    host = str(docker_endpoint.get("Host", "")).strip()
    if not host:
        raise RuntimeError("Docker context 缺少 docker endpoint Host")
    return host


def _buildx_du_records(
    builder_name: str,
    daemon: DockerDaemonIdentity,
    environment: Mapping[str, str] | None,
    retention_hours: int,
) -> tuple[_DuRecord, ...]:
    executable = docker_executable(environment)
    command = [
        executable,
        *_target_selector(daemon),
        "buildx",
        "du",
        "--builder",
        builder_name,
        "--filter",
        f"until={retention_hours}h",
        "--format=json",
    ]
    result = _run_docker(command, environment, timeout=120)
    records: list[_DuRecord] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as error:
            raise RuntimeError("无法解析 docker buildx du JSON 输出") from error
        if not isinstance(payload, dict):
            raise RuntimeError("docker buildx du 返回非对象 JSON")
        record_id = str(payload.get("ID", "")).strip()
        if not record_id:
            raise RuntimeError("docker buildx du 记录缺少 ID")
        raw_size = payload.get("Size")
        if raw_size is None:
            raise RuntimeError("docker buildx du 记录缺少 Size")
        try:
            size = int(str(raw_size))
        except ValueError as error:
            raise RuntimeError("docker buildx du 记录含无效 Size") from error
        reclaimable = payload.get("Reclaimable")
        if not isinstance(reclaimable, bool):
            raise RuntimeError("docker buildx du 记录缺少布尔 Reclaimable")
        records.append(_DuRecord(record_id, max(0, size), reclaimable))
    return tuple(records)


def _exact_builder(
    builder_name: str,
    daemon: DockerDaemonIdentity,
    environment: Mapping[str, str] | None,
) -> BuildxBuilder:
    matches = [
        builder
        for builder in _list_buildx_builders_for_daemon(daemon, environment)
        if builder.name == builder_name
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"无法唯一确认 Buildx builder {builder_name!r}: found={len(matches)}"
        )
    return matches[0]


def _validate_retention(retention_hours: int) -> None:
    if retention_hours < _MIN_RETENTION_HOURS:
        raise ValueError("Buildx cache 至少保留最近 24 小时")


def _require_local_daemon(
    environment: Mapping[str, str] | None,
) -> DockerDaemonIdentity:
    daemon = inspect_docker_daemon_target(environment)
    if not daemon.local:
        raise RuntimeError(
            "DevClean 只维护本机 Docker daemon; "
            f"当前 endpoint={daemon.endpoint} ({daemon.source})"
        )
    return daemon


def _target_selector(target: DockerDaemonIdentity) -> tuple[str, str]:
    if target.context_name is not None:
        return "--context", target.context_name
    return "--host", target.endpoint


def _daemon_key(target: DockerDaemonIdentity) -> tuple[str | None, str, str]:
    return target.context_name, target.endpoint, target.source


def _builder_key(
    builder: BuildxBuilder,
) -> tuple[str, str, tuple[tuple[str, str, str], ...]]:
    return (
        builder.name,
        builder.driver,
        tuple((node.name, node.endpoint, node.status) for node in builder.nodes),
    )


def _is_local_endpoint(endpoint: str) -> bool:
    normalized = endpoint.strip().replace("\\", "/").casefold()
    if os.name == "nt":
        if not normalized.startswith("npipe://"):
            return False
        remainder = normalized.removeprefix("npipe://").lstrip("/")
        return remainder.startswith("./pipe/")
    return normalized.startswith("unix:///")


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
        raise RuntimeError(f"无法执行 Docker Buildx CLI: {error}") from error
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(
            f"Docker Buildx CLI 失败 (exit {result.returncode}): {detail}"
        )
    return result


__all__ = [
    "BuildxBuilder",
    "BuildxCacheInventory",
    "BuildxNode",
    "BuildxPruneResult",
    "inspect_buildx_cache",
    "list_buildx_builders",
    "prune_buildx_cache",
]
