"""Exact Docker image inventory and user-directed vendor removal."""

# ruff: noqa: RUF001

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping, Sequence
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

_BATCH_SIZE = 40


@dataclass(frozen=True, slots=True)
class DockerImageEntry:
    image_id: str
    repo_tags: tuple[str, ...]
    repo_digests: tuple[str, ...]
    created: str
    logical_size: int
    container_ids: tuple[str, ...]
    executable: bool
    reason: str

    @property
    def dangling(self) -> bool:
        return not self.repo_tags


@dataclass(frozen=True, slots=True)
class DockerImageInventory:
    daemon: DockerDaemonIdentity
    images: tuple[DockerImageEntry, ...]


@dataclass(frozen=True, slots=True)
class DockerImageRemoveResult:
    daemon: DockerDaemonIdentity
    image: DockerImageEntry
    command: tuple[str, ...]
    stdout: str
    system_df_before: str
    system_df_after: str


def inspect_docker_images(
    environment: Mapping[str, str] | None = None,
) -> DockerImageInventory:
    """Inventory exact local-daemon image IDs and current container references."""

    daemon = _require_local_daemon(environment)
    executable = docker_executable(environment)
    selector = _target_selector(daemon)

    image_ids = _unique_lines(
        _run_docker(
            [
                executable,
                *selector,
                "image",
                "ls",
                "--no-trunc",
                "--quiet",
            ],
            environment,
            timeout=60,
        ).stdout
    )
    image_payloads = _inspect_objects(
        executable,
        selector,
        "image",
        image_ids,
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
    container_payloads = _inspect_objects(
        executable,
        selector,
        "container",
        container_ids,
        environment,
    )
    references = _container_image_references(container_payloads)

    entries: list[DockerImageEntry] = []
    for payload in image_payloads:
        image_id = _required_string(payload, "Id", "Docker image inspect")
        repo_tags = _string_tuple(payload.get("RepoTags"))
        repo_digests = _string_tuple(payload.get("RepoDigests"))
        created = _required_string(payload, "Created", "Docker image inspect")
        logical_size = _nonnegative_int(payload.get("Size"), "Docker image Size")
        consumers = tuple(sorted(references.get(image_id, ())))
        can_remove, reason = _image_decision(repo_tags, consumers)
        entries.append(
            DockerImageEntry(
                image_id=image_id,
                repo_tags=repo_tags,
                repo_digests=repo_digests,
                created=created,
                logical_size=logical_size,
                container_ids=consumers,
                executable=can_remove,
                reason=reason,
            )
        )

    entries.sort(key=lambda item: item.logical_size, reverse=True)
    return DockerImageInventory(daemon=daemon, images=tuple(entries))


def remove_docker_image(
    expected: DockerImageEntry,
    environment: Mapping[str, str] | None = None,
) -> DockerImageRemoveResult:
    """Remove one exact unreferenced image through Docker, never with force."""

    initial = inspect_docker_images(environment)
    current = _exact_image(initial.images, expected.image_id)
    _require_same_image(expected, current)
    if not current.executable:
        raise RuntimeError(current.reason)

    clear_docker_process_cache()
    if docker_process_running():
        raise RuntimeError("Docker/BuildKit 构建正在运行; 请等待构建完成后再删除 image")

    fresh_inventory = inspect_docker_images(environment)
    if _daemon_key(fresh_inventory.daemon) != _daemon_key(initial.daemon):
        raise RuntimeError("Docker daemon/context 在执行前发生变化; 请重新检查")
    fresh = _exact_image(fresh_inventory.images, expected.image_id)
    _require_same_image(expected, fresh)
    if not fresh.executable:
        raise RuntimeError(fresh.reason)

    executable = docker_executable(environment)
    selector = _target_selector(fresh_inventory.daemon)
    before_df = _system_df(executable, selector, environment)
    command = (
        executable,
        *selector,
        "image",
        "rm",
        "--no-prune",
        fresh.image_id,
    )
    result = _run_docker(list(command), environment, timeout=600)

    after_inventory = inspect_docker_images(environment)
    if _daemon_key(after_inventory.daemon) != _daemon_key(fresh_inventory.daemon):
        raise RuntimeError("Docker daemon/context 在 image 删除后发生变化; 无法确认结果")
    if any(item.image_id == fresh.image_id for item in after_inventory.images):
        raise RuntimeError("Docker image rm 返回成功，但精确 image ID 仍然存在")
    after_df = _system_df(executable, selector, environment)
    return DockerImageRemoveResult(
        daemon=fresh_inventory.daemon,
        image=fresh,
        command=command,
        stdout=(result.stdout or result.stderr).strip(),
        system_df_before=before_df,
        system_df_after=after_df,
    )


def _image_decision(
    repo_tags: tuple[str, ...],
    container_ids: tuple[str, ...],
) -> tuple[bool, str]:
    if container_ids:
        return False, "仍有 container 引用该 image; 当前仅报告"
    if len(repo_tags) > 1:
        return (
            False,
            "该 image 有多个 tag。DevClean 不使用 --force，也不自动拆除多个 tag; 当前仅报告",
        )
    if repo_tags:
        return (
            True,
            "该 image 当前无 container 引用；是否保留本地 tag/image 用于离线或快速复用由用户决定",
        )
    return (
        True,
        "该 image 当前无 tag 且无 container 引用；技术状态明确，但仍由用户决定是否删除",
    )


def _inspect_objects(
    executable: str,
    selector: tuple[str, str],
    kind: str,
    ids: tuple[str, ...],
    environment: Mapping[str, str] | None,
) -> tuple[dict[str, object], ...]:
    if not ids:
        return ()
    payloads: list[dict[str, object]] = []
    for start in range(0, len(ids), _BATCH_SIZE):
        batch = ids[start : start + _BATCH_SIZE]
        result = _run_docker(
            [executable, *selector, kind, "inspect", *batch],
            environment,
            timeout=120,
        )
        try:
            decoded = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError(f"无法解析 docker {kind} inspect JSON") from error
        if not isinstance(decoded, list):
            raise RuntimeError(f"docker {kind} inspect 未返回 JSON array")
        for item in decoded:
            if not isinstance(item, dict):
                raise RuntimeError(f"docker {kind} inspect 返回非 object JSON")
            payloads.append({str(key): value for key, value in item.items()})
    return tuple(payloads)


def _container_image_references(
    payloads: tuple[dict[str, object], ...],
) -> dict[str, set[str]]:
    references: dict[str, set[str]] = {}
    for payload in payloads:
        container_id = _required_string(payload, "Id", "Docker container inspect")
        image_id = _required_string(payload, "Image", "Docker container inspect")
        references.setdefault(image_id, set()).add(container_id)
    return references


def _exact_image(
    images: Sequence[DockerImageEntry],
    image_id: str,
) -> DockerImageEntry:
    matches = [item for item in images if item.image_id == image_id]
    if len(matches) != 1:
        raise RuntimeError(
            f"无法唯一确认 Docker image {image_id!r}: found={len(matches)}"
        )
    return matches[0]


def _require_same_image(expected: DockerImageEntry, current: DockerImageEntry) -> None:
    if (
        current.image_id != expected.image_id
        or current.repo_tags != expected.repo_tags
        or current.repo_digests != expected.repo_digests
        or current.created != expected.created
        or current.container_ids != expected.container_ids
    ):
        raise RuntimeError("Docker image 身份/tag/digest/container 引用已变化; 请重新检查")


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


def _string_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise RuntimeError("Docker inspect 返回了非 list 的 tag/digest 字段")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise RuntimeError("Docker inspect tag/digest 字段包含非字符串")
        text = item.strip()
        if text and text not in {"<none>", "<none>:<none>"}:
            result.append(text)
    return tuple(sorted(set(result)))


def _required_string(payload: dict[str, object], key: str, label: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{label} 缺少 {key}")
    return value.strip()


def _nonnegative_int(value: object, label: str) -> int:
    if value is None:
        raise RuntimeError(f"{label} 缺失")
    try:
        parsed = int(str(value))
    except ValueError as error:
        raise RuntimeError(f"{label} 无效") from error
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
        raise RuntimeError(f"无法执行 Docker image CLI: {error}") from error
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(
            f"Docker image CLI 失败 (exit {result.returncode}): {detail}"
        )
    return result


__all__ = [
    "DockerImageEntry",
    "DockerImageInventory",
    "DockerImageRemoveResult",
    "inspect_docker_images",
    "remove_docker_image",
]
