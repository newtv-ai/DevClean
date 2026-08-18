"""Read-only Docker daemon inventory plus vendor-managed build-cache pruning."""

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


@dataclass(frozen=True, slots=True)
class DockerDaemonIdentity:
    context_name: str | None
    endpoint: str
    source: str
    local: bool
    reason: str


@dataclass(frozen=True, slots=True)
class DockerUsageRow:
    kind: str
    total: str
    active: str
    size: str
    reclaimable: str


@dataclass(frozen=True, slots=True)
class DockerStorageInventory:
    rows: tuple[DockerUsageRow, ...]
    stdout: str


@dataclass(frozen=True, slots=True)
class DockerBuildPruneResult:
    until_hours: int
    stdout: str
    target: DockerDaemonIdentity


def inspect_docker_daemon_target(
    environment: Mapping[str, str] | None = None,
) -> DockerDaemonIdentity:
    """Resolve the effective Docker daemon target without changing user context."""

    executable = docker_executable(environment)
    env = _casefold_env(environment)
    configured_context = env.get("docker_context", "").strip()
    configured_host = env.get("docker_host", "").strip()

    if configured_context:
        shown = _docker_context_name(executable, environment)
        if shown != configured_context:
            raise RuntimeError(
                "Docker CLI 返回的 effective context 与 DOCKER_CONTEXT 不一致: "
                f"configured={configured_context}, reported={shown}"
            )
        endpoint = _context_endpoint(executable, shown, environment)
        source = "DOCKER_CONTEXT"
        context_name: str | None = shown
    elif configured_host:
        endpoint = configured_host
        source = "DOCKER_HOST"
        context_name = None
    else:
        shown = _docker_context_name(executable, environment)
        endpoint = _context_endpoint(executable, shown, environment)
        source = "docker context"
        context_name = shown

    local = _is_local_docker_endpoint(endpoint)
    reason = (
        "Docker daemon 使用本机 socket，可用于本机磁盘维护"
        if local
        else "Docker daemon endpoint 可能是远程或非本机目标，只允许报告"
    )
    return DockerDaemonIdentity(
        context_name=context_name,
        endpoint=endpoint,
        source=source,
        local=local,
        reason=reason,
    )


def inventory_docker_storage(
    environment: Mapping[str, str] | None = None,
) -> DockerStorageInventory:
    """Ask the Docker daemon for disk accounting without mutating anything."""

    command = [docker_executable(environment), "system", "df", "--format", "json"]
    result = _run_docker(command, environment, timeout=60)
    return DockerStorageInventory(
        rows=_parse_system_df(result.stdout),
        stdout=result.stdout.strip(),
    )


def prune_docker_build_cache(
    environment: Mapping[str, str] | None = None,
    *,
    until_hours: int = 168,
) -> DockerBuildPruneResult:
    """Delegate cleanup only to a verified local ``docker builder prune`` target."""

    if until_hours < 24:
        raise ValueError("Docker build cache 至少保留最近 24 小时")

    initial = _require_local_docker_target(environment)
    clear_docker_process_cache()
    if docker_process_running():
        raise RuntimeError("Docker/BuildKit 构建正在运行; 请等待构建完成后再清理")

    fresh = _require_local_docker_target(environment)
    if _target_key(fresh) != _target_key(initial):
        raise RuntimeError("Docker daemon/context 在执行前发生变化; 请重新检查")

    command = [
        docker_executable(environment),
        *_target_selector(fresh),
        "builder",
        "prune",
        "--force",
        "--filter",
        f"until={until_hours}h",
    ]
    result = _run_docker(command, environment, timeout=600)
    return DockerBuildPruneResult(
        until_hours=until_hours,
        stdout=result.stdout.strip(),
        target=fresh,
    )


def _require_local_docker_target(
    environment: Mapping[str, str] | None,
) -> DockerDaemonIdentity:
    target = inspect_docker_daemon_target(environment)
    if not target.local:
        raise RuntimeError(
            "DevClean 只维护本机 Docker daemon; "
            f"当前 endpoint={target.endpoint} ({target.source})"
        )
    return target


def _docker_context_name(
    executable: str,
    environment: Mapping[str, str] | None,
) -> str:
    result = _run_docker([executable, "context", "show"], environment, timeout=30)
    name = result.stdout.strip()
    if not name or "\n" in name or "\r" in name:
        raise RuntimeError("Docker CLI 未返回唯一 effective context")
    return name


def _context_endpoint(
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
    if _string(item.get("Name")) != context_name:
        raise RuntimeError("docker context inspect 返回了意外的 context 身份")
    endpoints = item.get("Endpoints")
    if not isinstance(endpoints, dict):
        raise RuntimeError("Docker context 缺少 Endpoints")
    docker_endpoint = endpoints.get("docker")
    if not isinstance(docker_endpoint, dict):
        raise RuntimeError("Docker context 缺少 docker endpoint")
    endpoint = _string(docker_endpoint.get("Host")).strip()
    if not endpoint:
        raise RuntimeError("Docker context 缺少 docker endpoint Host")
    return endpoint


def _is_local_docker_endpoint(endpoint: str) -> bool:
    normalized = endpoint.strip().replace("\\", "/").casefold()
    if os.name == "nt":
        if not normalized.startswith("npipe://"):
            return False
        remainder = normalized.removeprefix("npipe://")
        return remainder.startswith("///./pipe/") or remainder.startswith("./pipe/")
    return normalized.startswith("unix:///")


def _target_selector(target: DockerDaemonIdentity) -> tuple[str, str]:
    if target.context_name is not None:
        return "--context", target.context_name
    return "--host", target.endpoint


def _target_key(target: DockerDaemonIdentity) -> tuple[str | None, str, str]:
    return target.context_name, target.endpoint, target.source


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
        raise RuntimeError(f"无法执行 Docker CLI: {error}") from error
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(
            f"Docker CLI 执行失败 (退出码 {result.returncode}): {detail}"
        )
    return result


def _parse_system_df(output: str) -> tuple[DockerUsageRow, ...]:
    text = output.strip()
    if not text:
        return ()

    payloads: list[object] = []
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError:
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                payloads.append(json.loads(line))
            except json.JSONDecodeError as error:
                raise RuntimeError("无法解析 docker system df JSON 输出") from error
    else:
        if isinstance(decoded, list):
            payloads.extend(decoded)
        else:
            payloads.append(decoded)

    rows: list[DockerUsageRow] = []
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        rows.append(
            DockerUsageRow(
                kind=_string(payload.get("Type")),
                total=_string(payload.get("TotalCount", payload.get("Total"))),
                active=_string(payload.get("Active")),
                size=_string(payload.get("Size")),
                reclaimable=_string(payload.get("Reclaimable")),
            )
        )
    return tuple(rows)


def _casefold_env(environment: Mapping[str, str] | None) -> dict[str, str]:
    source = os.environ if environment is None else environment
    return {str(key).casefold(): str(value) for key, value in source.items() if value}


def _string(value: object) -> str:
    return "" if value is None else str(value)


__all__ = [
    "DockerBuildPruneResult",
    "DockerDaemonIdentity",
    "DockerStorageInventory",
    "DockerUsageRow",
    "inspect_docker_daemon_target",
    "inventory_docker_storage",
    "prune_docker_build_cache",
]
