from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence

import pytest

import devclean.core.docker_image_maintenance as image_maintenance
from devclean.core.docker_image_maintenance import (
    DockerImageEntry,
    inspect_docker_images,
    remove_docker_image,
)
from devclean.core.docker_maintenance import DockerDaemonIdentity


def _completed(
    args: Sequence[str],
    *,
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        list(args),
        returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _daemon(context: str = "default") -> DockerDaemonIdentity:
    return DockerDaemonIdentity(
        context_name=context,
        endpoint="npipe:////./pipe/docker_engine",
        source="docker context",
        local=True,
        reason="local",
    )


def _image(
    image_id: str,
    *,
    tags: list[str] | None = None,
    digests: list[str] | None = None,
    size: int = 100,
) -> dict[str, object]:
    return {
        "Id": image_id,
        "RepoTags": [] if tags is None else tags,
        "RepoDigests": [] if digests is None else digests,
        "Created": "2026-08-01T00:00:00Z",
        "Size": size,
    }


def _container(container_id: str, image_id: str) -> dict[str, object]:
    return {"Id": container_id, "Image": image_id}


def _install_inventory_fake(
    monkeypatch: pytest.MonkeyPatch,
    *,
    image_payloads: list[dict[str, object]],
    container_payloads: list[dict[str, object]] | None = None,
    seen: list[list[str]] | None = None,
) -> None:
    monkeypatch.setattr(
        image_maintenance,
        "inspect_docker_daemon_target",
        lambda environment=None: _daemon(),
    )
    containers = [] if container_payloads is None else container_payloads

    def fake_run(
        command: Sequence[str],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
        timeout: int,
        env: Mapping[str, str],
    ) -> subprocess.CompletedProcess[str]:
        del check, capture_output, text, timeout, env
        cmd = list(command)
        if seen is not None:
            seen.append(cmd)
        if cmd[-5:] == ["image", "ls", "--all", "--no-trunc", "--quiet"]:
            ids = [str(item["Id"]) for item in image_payloads]
            return _completed(command, stdout="\n".join(ids) + ("\n" if ids else ""))
        if "image" in cmd and "inspect" in cmd:
            return _completed(command, stdout=json.dumps(image_payloads))
        if cmd[-5:] == ["container", "ls", "--all", "--no-trunc", "--quiet"]:
            ids = [str(item["Id"]) for item in containers]
            return _completed(command, stdout="\n".join(ids) + ("\n" if ids else ""))
        if "container" in cmd and "inspect" in cmd:
            return _completed(command, stdout=json.dumps(containers))
        raise AssertionError(cmd)

    monkeypatch.setattr(
        "devclean.core.docker_image_maintenance.subprocess.run",
        fake_run,
    )


def test_inventory_uses_exact_image_and_container_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_a = "sha256:" + "a" * 64
    image_b = "sha256:" + "b" * 64
    container = "c" * 64
    seen: list[list[str]] = []
    _install_inventory_fake(
        monkeypatch,
        image_payloads=[
            _image(image_a, tags=["repo:latest"], size=200),
            _image(image_b, tags=[], size=300),
        ],
        container_payloads=[_container(container, image_a)],
        seen=seen,
    )

    inventory = inspect_docker_images({"DEVCLEAN_DOCKER_EXE": "docker-test"})
    by_id = {entry.image_id: entry for entry in inventory.images}

    assert by_id[image_a].container_ids == (container,)
    assert not by_id[image_a].executable
    assert by_id[image_b].dangling
    assert by_id[image_b].executable
    assert any("image" in command and "inspect" in command for command in seen)
    assert any("container" in command and "inspect" in command for command in seen)
    image_list = next(command for command in seen if "image" in command and "ls" in command)
    assert "--all" in image_list


def test_multiple_tags_stay_report_only_without_force(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_id = "sha256:" + "d" * 64
    _install_inventory_fake(
        monkeypatch,
        image_payloads=[_image(image_id, tags=["repo:one", "repo:two"])],
    )

    entry = inspect_docker_images({"DEVCLEAN_DOCKER_EXE": "docker-test"}).images[0]

    assert not entry.executable
    assert "多个 tag" in entry.reason


def test_remote_daemon_is_not_an_image_mutation_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote = DockerDaemonIdentity(
        context_name="production",
        endpoint="ssh://docker@example.invalid",
        source="docker context",
        local=False,
        reason="remote",
    )
    monkeypatch.setattr(
        image_maintenance,
        "inspect_docker_daemon_target",
        lambda environment=None: remote,
    )

    with pytest.raises(RuntimeError, match="只维护本机 Docker daemon"):
        inspect_docker_images({})


def test_image_removal_is_exact_no_force_no_parent_prune(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_id = "sha256:" + "e" * 64
    expected = DockerImageEntry(
        image_id=image_id,
        repo_tags=(),
        repo_digests=(),
        created="2026-08-01T00:00:00Z",
        logical_size=500,
        container_ids=(),
        executable=True,
        reason="user review",
    )
    states = iter(
        [
            image_maintenance.DockerImageInventory(_daemon(), (expected,)),
            image_maintenance.DockerImageInventory(_daemon(), (expected,)),
            image_maintenance.DockerImageInventory(_daemon(), ()),
        ]
    )
    monkeypatch.setattr(
        image_maintenance,
        "inspect_docker_images",
        lambda environment=None: next(states),
    )
    monkeypatch.setattr(image_maintenance, "clear_docker_process_cache", lambda: None)
    monkeypatch.setattr(image_maintenance, "docker_process_running", lambda: False)
    seen: list[list[str]] = []

    def fake_run(
        command: Sequence[str],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
        timeout: int,
        env: Mapping[str, str],
    ) -> subprocess.CompletedProcess[str]:
        del check, capture_output, text, timeout, env
        cmd = list(command)
        seen.append(cmd)
        if "system" in cmd and "df" in cmd:
            return _completed(command, stdout='{"Type":"Images"}\n')
        if "image" in cmd and "rm" in cmd:
            return _completed(command, stdout=f"Deleted: {image_id}\n")
        raise AssertionError(cmd)

    monkeypatch.setattr(
        "devclean.core.docker_image_maintenance.subprocess.run",
        fake_run,
    )

    result = remove_docker_image(expected, {"DEVCLEAN_DOCKER_EXE": "docker-test"})

    remove = next(command for command in seen if "rm" in command)
    assert remove == [
        "docker-test",
        "--context",
        "default",
        "image",
        "rm",
        "--no-prune",
        image_id,
    ]
    assert "--force" not in remove
    assert result.image.image_id == image_id


def test_image_removal_refuses_container_reference_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_id = "sha256:" + "f" * 64
    expected = DockerImageEntry(
        image_id=image_id,
        repo_tags=(),
        repo_digests=(),
        created="2026-08-01T00:00:00Z",
        logical_size=500,
        container_ids=(),
        executable=True,
        reason="user review",
    )
    changed = DockerImageEntry(
        image_id=image_id,
        repo_tags=(),
        repo_digests=(),
        created=expected.created,
        logical_size=500,
        container_ids=("container-new",),
        executable=False,
        reason="referenced",
    )
    states = iter(
        [
            image_maintenance.DockerImageInventory(_daemon(), (changed,)),
        ]
    )
    monkeypatch.setattr(
        image_maintenance,
        "inspect_docker_images",
        lambda environment=None: next(states),
    )

    with pytest.raises(RuntimeError, match="已变化"):
        remove_docker_image(expected, {})


def test_image_removal_refuses_active_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_id = "sha256:" + "1" * 64
    expected = DockerImageEntry(
        image_id=image_id,
        repo_tags=(),
        repo_digests=(),
        created="2026-08-01T00:00:00Z",
        logical_size=500,
        container_ids=(),
        executable=True,
        reason="user review",
    )
    monkeypatch.setattr(
        image_maintenance,
        "inspect_docker_images",
        lambda environment=None: image_maintenance.DockerImageInventory(
            _daemon(),
            (expected,),
        ),
    )
    monkeypatch.setattr(image_maintenance, "clear_docker_process_cache", lambda: None)
    monkeypatch.setattr(image_maintenance, "docker_process_running", lambda: True)

    with pytest.raises(RuntimeError, match="构建正在运行"):
        remove_docker_image(expected, {})
