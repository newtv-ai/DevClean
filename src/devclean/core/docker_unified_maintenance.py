"""Unified Docker maintenance inventory while preserving exact mutation lanes."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from devclean.core.docker_buildx_maintenance import (
    BuildxBuilder,
    BuildxCacheInventory,
    BuildxPruneResult,
    inspect_buildx_cache,
    list_buildx_builders,
    prune_buildx_cache,
)
from devclean.core.docker_container_maintenance import (
    DockerContainerEntry,
    DockerContainerInventory,
    DockerContainerRemoveResult,
    inspect_docker_containers,
    remove_docker_container,
)
from devclean.core.docker_image_maintenance import (
    DockerImageEntry,
    DockerImageInventory,
    DockerImageRemoveResult,
    inspect_docker_images,
    remove_docker_image,
)
from devclean.core.docker_maintenance import (
    DockerBuildPruneResult,
    DockerDaemonIdentity,
    DockerStorageInventory,
    inspect_docker_daemon_target,
    inventory_docker_storage,
    prune_docker_build_cache,
)
from devclean.core.docker_volume_inventory import (
    DockerVolumeInventory,
    inspect_docker_volumes,
)

_DEFAULT_RETENTION_HOURS = 168


@dataclass(frozen=True, slots=True)
class DockerUnifiedInventory:
    """One reviewed local Docker endpoint with all already-audited storage lanes."""

    target: DockerDaemonIdentity
    storage: DockerStorageInventory
    images: DockerImageInventory
    containers: DockerContainerInventory
    volumes: DockerVolumeInventory
    builders: tuple[BuildxBuilder, ...]
    buildx_cache: tuple[BuildxCacheInventory, ...]
    buildx_error: str | None
    retention_hours: int


def inspect_docker_unified(
    environment: Mapping[str, str] | None = None,
    *,
    retention_hours: int = _DEFAULT_RETENTION_HOURS,
) -> DockerUnifiedInventory:
    """Collect one endpoint-pinned snapshot for the unified Docker UI.

    The effective user context is resolved once. Every subsequent read is pinned to
    the exact resulting daemon endpoint through ``DOCKER_HOST`` so changing the
    user's default context cannot redirect this reviewed snapshot.
    """

    if retention_hours < 24:
        raise ValueError("Docker cache 至少保留最近 24 小时")

    target = inspect_docker_daemon_target(environment)
    if not target.local:
        raise RuntimeError(
            "DevClean 的 Docker 维护 UI 只作用于本机 daemon; "
            f"当前 endpoint={target.endpoint} ({target.source})"
        )

    pinned = _pinned_environment(target, environment)
    _require_same_endpoint(target, inspect_docker_daemon_target(pinned), "开始检查")

    storage = inventory_docker_storage(pinned)
    images = inspect_docker_images(pinned)
    _require_same_endpoint(target, images.daemon, "image inventory")
    containers = inspect_docker_containers(pinned)
    _require_same_endpoint(target, containers.daemon, "container inventory")
    volumes = inspect_docker_volumes(pinned)
    _require_same_endpoint(target, volumes.daemon, "volume inventory")

    builders: tuple[BuildxBuilder, ...] = ()
    buildx_cache: list[BuildxCacheInventory] = []
    buildx_error: str | None = None
    try:
        builders = list_buildx_builders(pinned)
        for builder in builders:
            if not builder.executable:
                continue
            buildx_cache.append(
                inspect_buildx_cache(
                    builder.name,
                    pinned,
                    retention_hours=retention_hours,
                )
            )
    except RuntimeError as error:
        buildx_error = str(error)

    _require_same_endpoint(
        target,
        inspect_docker_daemon_target(pinned),
        "完成检查",
    )
    return DockerUnifiedInventory(
        target=target,
        storage=storage,
        images=images,
        containers=containers,
        volumes=volumes,
        builders=builders,
        buildx_cache=tuple(buildx_cache),
        buildx_error=buildx_error,
        retention_hours=retention_hours,
    )


def remove_reviewed_docker_image(
    reviewed_target: DockerDaemonIdentity,
    expected: DockerImageEntry,
    environment: Mapping[str, str] | None = None,
) -> DockerImageRemoveResult:
    """Remove one reviewed image while pinning execution to the reviewed endpoint."""

    pinned = _verified_pinned_environment(reviewed_target, environment)
    result = remove_docker_image(expected, pinned)
    _require_same_endpoint(reviewed_target, result.daemon, "image 删除")
    return result


def remove_reviewed_docker_container(
    reviewed_target: DockerDaemonIdentity,
    expected: DockerContainerEntry,
    environment: Mapping[str, str] | None = None,
) -> DockerContainerRemoveResult:
    """Remove one reviewed stopped container on the exact reviewed endpoint."""

    pinned = _verified_pinned_environment(reviewed_target, environment)
    result = remove_docker_container(expected, pinned)
    _require_same_endpoint(reviewed_target, result.daemon, "container 删除")
    return result


def prune_reviewed_docker_build_cache(
    reviewed_target: DockerDaemonIdentity,
    environment: Mapping[str, str] | None = None,
    *,
    retention_hours: int = _DEFAULT_RETENTION_HOURS,
) -> DockerBuildPruneResult:
    """Run the already-audited classic builder cache prune on the reviewed endpoint."""

    pinned = _verified_pinned_environment(reviewed_target, environment)
    result = prune_docker_build_cache(pinned, until_hours=retention_hours)
    _require_same_endpoint(reviewed_target, result.target, "builder cache prune")
    return result


def prune_reviewed_buildx_cache(
    reviewed_target: DockerDaemonIdentity,
    reviewed: BuildxCacheInventory,
    environment: Mapping[str, str] | None = None,
) -> BuildxPruneResult:
    """Prune one reviewed local Buildx builder only if its identity is still exact."""

    pinned = _verified_pinned_environment(reviewed_target, environment)
    current = inspect_buildx_cache(
        reviewed.builder.name,
        pinned,
        retention_hours=reviewed.retention_hours,
    )
    _require_same_endpoint(reviewed_target, current.daemon, "Buildx 复核")
    if _builder_key(current.builder) != _builder_key(reviewed.builder):
        raise RuntimeError("Buildx builder 身份/node endpoint 已变化; 请重新检查")
    if (
        current.record_count != reviewed.record_count
        or current.aged_reclaimable_bytes != reviewed.aged_reclaimable_bytes
    ):
        raise RuntimeError("Buildx cache 状态已变化; 请重新检查后再清理")

    result = prune_buildx_cache(
        reviewed.builder.name,
        pinned,
        retention_hours=reviewed.retention_hours,
    )
    _require_same_endpoint(reviewed_target, result.daemon, "Buildx prune")
    if _builder_key(result.builder) != _builder_key(reviewed.builder):
        raise RuntimeError("Buildx builder 在 prune 期间发生变化; 无法确认结果")
    return result


def _verified_pinned_environment(
    target: DockerDaemonIdentity,
    environment: Mapping[str, str] | None,
) -> dict[str, str]:
    if not target.local:
        raise RuntimeError("只允许对已经审核为本机的 Docker daemon 执行维护")
    pinned = _pinned_environment(target, environment)
    _require_same_endpoint(target, inspect_docker_daemon_target(pinned), "执行前")
    return pinned


def _pinned_environment(
    target: DockerDaemonIdentity,
    environment: Mapping[str, str] | None,
) -> dict[str, str]:
    pinned = (
        {}
        if environment is None
        else {str(key): str(value) for key, value in environment.items()}
    )
    # Prefer an immutable endpoint binding over a mutable context name. An empty
    # DOCKER_CONTEXT deliberately masks any inherited process setting.
    pinned["DOCKER_CONTEXT"] = ""
    pinned["DOCKER_HOST"] = target.endpoint
    return pinned


def _require_same_endpoint(
    reviewed: DockerDaemonIdentity,
    current: DockerDaemonIdentity,
    phase: str,
) -> None:
    if not current.local or current.endpoint.casefold() != reviewed.endpoint.casefold():
        raise RuntimeError(
            f"Docker daemon endpoint 在{phase}时与用户审核对象不一致: "
            f"reviewed={reviewed.endpoint}, current={current.endpoint}"
        )


def _builder_key(
    builder: BuildxBuilder,
) -> tuple[str, str, tuple[tuple[str, str, str], ...]]:
    return (
        builder.name,
        builder.driver,
        tuple((node.name, node.endpoint, node.status) for node in builder.nodes),
    )


__all__ = [
    "DockerUnifiedInventory",
    "inspect_docker_unified",
    "prune_reviewed_buildx_cache",
    "prune_reviewed_docker_build_cache",
    "remove_reviewed_docker_container",
    "remove_reviewed_docker_image",
]
