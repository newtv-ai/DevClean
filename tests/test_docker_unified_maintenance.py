from __future__ import annotations

from collections.abc import Mapping

import pytest

import devclean.core.docker_unified_maintenance as unified
from devclean.core.docker_buildx_maintenance import (
    BuildxBuilder,
    BuildxCacheInventory,
    BuildxNode,
)
from devclean.core.docker_container_maintenance import DockerContainerInventory
from devclean.core.docker_image_maintenance import (
    DockerImageEntry,
    DockerImageInventory,
    DockerImageRemoveResult,
)
from devclean.core.docker_maintenance import DockerDaemonIdentity, DockerStorageInventory
from devclean.core.docker_volume_inventory import DockerVolumeInventory

_ENDPOINT = "npipe:////./pipe/dockerDesktopLinuxEngine"


def _reviewed_target() -> DockerDaemonIdentity:
    return DockerDaemonIdentity(
        context_name="desktop-linux",
        endpoint=_ENDPOINT,
        source="docker context",
        local=True,
        reason="local",
    )


def _pinned_target(endpoint: str = _ENDPOINT) -> DockerDaemonIdentity:
    return DockerDaemonIdentity(
        context_name=None,
        endpoint=endpoint,
        source="DOCKER_HOST",
        local=True,
        reason="local",
    )


def _image() -> DockerImageEntry:
    return DockerImageEntry(
        image_id="sha256:" + "a" * 64,
        repo_tags=("example:latest",),
        repo_digests=(),
        created="2026-08-01T00:00:00Z",
        logical_size=1024,
        container_ids=(),
        executable=True,
        reason="review",
    )


def _builder() -> BuildxBuilder:
    return BuildxBuilder(
        name="desktop-builder",
        driver="docker-container",
        nodes=(BuildxNode("desktop-builder0", _ENDPOINT, "running"),),
        local=True,
        executable=True,
        reason="local",
    )


def _cache(*, reclaimable: int = 1024) -> BuildxCacheInventory:
    return BuildxCacheInventory(
        daemon=_pinned_target(),
        builder=_builder(),
        retention_hours=168,
        record_count=2,
        aged_reclaimable_bytes=reclaimable,
        worth_maintaining=False,
    )


def test_unified_inventory_pins_every_section_to_reviewed_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reviewed = _reviewed_target()
    pinned = _pinned_target()
    seen_environments: list[dict[str, str]] = []
    target_calls = 0

    def fake_target(environment: Mapping[str, str] | None = None) -> DockerDaemonIdentity:
        nonlocal target_calls
        target_calls += 1
        if target_calls == 1:
            return reviewed
        assert environment is not None
        env = {str(key): str(value) for key, value in environment.items()}
        seen_environments.append(env)
        return pinned

    monkeypatch.setattr(unified, "inspect_docker_daemon_target", fake_target)
    monkeypatch.setattr(
        unified,
        "inventory_docker_storage",
        lambda environment=None: DockerStorageInventory(rows=(), stdout=""),
    )
    monkeypatch.setattr(
        unified,
        "inspect_docker_images",
        lambda environment=None: DockerImageInventory(daemon=pinned, images=()),
    )
    monkeypatch.setattr(
        unified,
        "inspect_docker_containers",
        lambda environment=None: DockerContainerInventory(daemon=pinned, containers=()),
    )
    monkeypatch.setattr(
        unified,
        "inspect_docker_volumes",
        lambda environment=None: DockerVolumeInventory(daemon=pinned, volumes=()),
    )
    monkeypatch.setattr(unified, "list_buildx_builders", lambda environment=None: ())

    result = unified.inspect_docker_unified({"DEVCLEAN_DOCKER_EXE": "docker-test"})

    assert result.target == reviewed
    assert seen_environments
    for environment in seen_environments:
        assert environment["DOCKER_HOST"] == _ENDPOINT
        assert environment["DOCKER_CONTEXT"] == ""


def test_reviewed_image_removal_uses_endpoint_pin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reviewed = _reviewed_target()
    pinned = _pinned_target()
    image = _image()
    seen: dict[str, str] = {}

    monkeypatch.setattr(
        unified,
        "inspect_docker_daemon_target",
        lambda environment=None: pinned,
    )

    def fake_remove(
        expected: DockerImageEntry,
        environment: Mapping[str, str] | None = None,
    ) -> DockerImageRemoveResult:
        assert expected == image
        assert environment is not None
        seen.update({str(key): str(value) for key, value in environment.items()})
        return DockerImageRemoveResult(
            daemon=pinned,
            image=image,
            command=("docker-test", "image", "rm", image.image_id),
            stdout="removed",
            system_df_before="before",
            system_df_after="after",
        )

    monkeypatch.setattr(unified, "remove_docker_image", fake_remove)

    result = unified.remove_reviewed_docker_image(reviewed, image)

    assert result.image == image
    assert seen["DOCKER_HOST"] == _ENDPOINT
    assert seen["DOCKER_CONTEXT"] == ""


def test_reviewed_endpoint_change_fails_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reviewed = _reviewed_target()
    changed = _pinned_target("npipe:////./pipe/otherDockerEngine")
    called = False

    monkeypatch.setattr(
        unified,
        "inspect_docker_daemon_target",
        lambda environment=None: changed,
    )

    def fake_remove(
        expected: DockerImageEntry,
        environment: Mapping[str, str] | None = None,
    ) -> DockerImageRemoveResult:
        del expected, environment
        nonlocal called
        called = True
        raise AssertionError("mutation must not run")

    monkeypatch.setattr(unified, "remove_docker_image", fake_remove)

    with pytest.raises(RuntimeError, match="endpoint"):
        unified.remove_reviewed_docker_image(reviewed, _image())
    assert not called


def test_buildx_review_refuses_changed_cache_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reviewed_target = _reviewed_target()
    reviewed_cache = _cache(reclaimable=4096)
    changed_cache = _cache(reclaimable=2048)
    called = False

    monkeypatch.setattr(
        unified,
        "inspect_docker_daemon_target",
        lambda environment=None: _pinned_target(),
    )
    monkeypatch.setattr(
        unified,
        "inspect_buildx_cache",
        lambda builder_name, environment=None, *, retention_hours=168: changed_cache,
    )

    def fake_prune(*args: object, **kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("prune must not run")

    monkeypatch.setattr(unified, "prune_buildx_cache", fake_prune)

    with pytest.raises(RuntimeError, match="cache 状态已变化"):
        unified.prune_reviewed_buildx_cache(reviewed_target, reviewed_cache)
    assert not called
