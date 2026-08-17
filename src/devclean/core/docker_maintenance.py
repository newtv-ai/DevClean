"""Read-only Docker daemon inventory plus vendor-managed build-cache pruning."""

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
    """Delegate cleanup only to ``docker builder prune``.

    Images, stopped containers, networks and volumes are deliberately outside
    this action because they can represent user-created or persistent state.
    """

    if until_hours < 24:
        raise ValueError("Docker build cache 至少保留最近 24 小时")
    clear_docker_process_cache()
    if docker_process_running():
        raise RuntimeError("Docker/BuildKit 构建正在运行; 请等待构建完成后再清理")

    command = [
        docker_executable(environment),
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
    )


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


def _string(value: object) -> str:
    return "" if value is None else str(value)


__all__ = [
    "DockerBuildPruneResult",
    "DockerStorageInventory",
    "DockerUsageRow",
    "inventory_docker_storage",
    "prune_docker_build_cache",
]
