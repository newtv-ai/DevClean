from __future__ import annotations

import subprocess
from collections.abc import Mapping
from dataclasses import replace

import pytest

import devclean.core.podman_image_maintenance as images
from devclean.core.podman_container_maintenance import PodmanMachineConnection
from devclean.core.podman_image_maintenance import (
    PodmanImageEntry,
    PodmanImageInventory,
    inspect_podman_images,
    remove_podman_image,
)

_ENV = {"DEVCLEAN_TEST_WINDOWS": "1", "DEVCLEAN_PODMAN_EXE": "podman.exe"}


def _target(name: str = "podman-machine-default") -> PodmanMachineConnection:
    return PodmanMachineConnection(
        executable="podman.exe",
        connection_name=name,
        connection_uri="ssh://user@127.0.0.1:55123/run/user/1000/podman/podman.sock",
        machine_name="podman-machine-default",
        vm_type="wsl",
        running=True,
        rootful=False,
    )


def _id(ch: str) -> str:
    return ch * 64


def _record(
    image_id: str,
    *,
    tags: tuple[str, ...] = ("docker.io/library/demo:latest",),
    parent: str = "",
    size: int = 1024,
) -> images._ImageRecord:
    return images._ImageRecord(
        image_id=image_id,
        repo_tags=tags,
        repo_digests=(f"docker.io/library/demo@sha256:{'f' * 64}",),
        parent_id=parent,
        created="2026-08-01T00:00:00Z",
        size=size,
        manifest_type="application/vnd.oci.image.manifest.v1+json",
    )


def _entry(
    *,
    image_id: str | None = None,
    executable: bool = True,
    tags: tuple[str, ...] = ("docker.io/library/demo:latest",),
) -> PodmanImageEntry:
    iid = image_id or _id("a")
    return PodmanImageEntry(
        image_id=iid,
        repo_tags=tags,
        repo_digests=(f"docker.io/library/demo@sha256:{'f' * 64}",),
        parent_id="",
        child_ids=(),
        created="2026-08-01T00:00:00Z",
        size=1024,
        manifest_type="application/vnd.oci.image.manifest.v1+json",
        read_only=False,
        manifest_list=False,
        podman_container_ids=(),
        external_container_ids=(),
        executable=executable,
        reason="USER_REVIEW" if executable else "protected",
    )


def _completed(stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["podman.exe"], 0, stdout, "")


def test_inventory_separates_exact_image_safety_classes(monkeypatch: pytest.MonkeyPatch) -> None:
    target = _target()
    normal = _id("a")
    read_only = _id("b")
    manifest = _id("c")
    parent = _id("d")
    child = _id("e")
    multi = _id("1")
    ordinary_ref = _id("2")
    external_ref = _id("3")
    all_ids = {normal, read_only, manifest, parent, child, multi, ordinary_ref, external_ref}

    records = {
        normal: _record(normal),
        read_only: _record(read_only),
        parent: _record(parent),
        child: _record(child, parent=parent),
        multi: _record(multi, tags=("example/a:one", "example/a:two")),
        ordinary_ref: _record(ordinary_ref),
        external_ref: _record(external_ref),
    }
    manifest_summary: dict[str, object] = {
        "Id": manifest,
        "RepoTags": ["example/list:latest"],
        "RepoDigests": [],
        "ParentId": None,
        "CreatedAt": "2026-08-01T00:00:00Z",
        "VirtualSize": 2048,
    }
    rows: list[dict[str, object]] = [
        {"Id": image_id} for image_id in all_ids if image_id != manifest
    ]
    rows.append(manifest_summary)

    monkeypatch.setattr(images, "inspect_podman_machine_target", lambda environment=None: target)
    monkeypatch.setattr(images, "_image_list_rows", lambda target, environment: rows)
    monkeypatch.setattr(
        images,
        "_filtered_image_ids",
        lambda target, filter_value, environment: (
            {read_only} if filter_value == "readonly=true" else {manifest}
        ),
    )
    monkeypatch.setattr(
        images,
        "_inspect_regular_images",
        lambda target, ids, environment: {image_id: records[image_id] for image_id in ids},
    )

    def refs(
        target: PodmanMachineConnection,
        *,
        external: bool,
        environment: Mapping[str, str] | None,
    ) -> tuple[dict[str, tuple[str, ...]], bool, str]:
        del target, environment
        if external:
            return {external_ref: ("external-1",)}, True, ""
        return {ordinary_ref: ("container-1",)}, True, ""

    monkeypatch.setattr(images, "_container_image_references", refs)
    monkeypatch.setattr(images, "_system_df", lambda target, environment: "df")

    inventory = inspect_podman_images(_ENV)
    by_id = {item.image_id: item for item in inventory.images}

    assert inventory.reference_proof_complete
    assert by_id[normal].executable
    assert not by_id[read_only].executable
    assert not by_id[manifest].executable
    assert not by_id[parent].executable
    assert by_id[child].executable
    assert not by_id[multi].executable
    assert not by_id[ordinary_ref].executable
    assert not by_id[external_ref].executable
    assert by_id[parent].child_ids == (child,)


def test_inventory_fails_closed_when_external_reference_proof_is_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _target()
    image_id = _id("a")
    monkeypatch.setattr(images, "inspect_podman_machine_target", lambda environment=None: target)
    monkeypatch.setattr(images, "_image_list_rows", lambda target, environment: [{"Id": image_id}])
    monkeypatch.setattr(
        images, "_filtered_image_ids", lambda target, filter_value, environment: set()
    )
    monkeypatch.setattr(
        images,
        "_inspect_regular_images",
        lambda target, ids, environment: {image_id: _record(image_id)},
    )

    def refs(
        target: PodmanMachineConnection,
        *,
        external: bool,
        environment: Mapping[str, str] | None,
    ) -> tuple[dict[str, tuple[str, ...]], bool, str]:
        del target, environment
        return ({}, False, "external reference incomplete") if external else ({}, True, "")

    monkeypatch.setattr(images, "_container_image_references", refs)
    monkeypatch.setattr(images, "_system_df", lambda target, environment: "df")

    inventory = inspect_podman_images(_ENV)

    assert not inventory.reference_proof_complete
    assert not inventory.images[0].executable
    assert "incomplete" in inventory.images[0].reason


def test_container_reference_parser_requires_full_image_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _target()
    valid = _id("a")

    def fake_run(
        command: list[str],
        environment: Mapping[str, str] | None,
        *,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        del environment, timeout
        assert command[1:3] == ["--connection", "podman-machine-default"]
        return _completed(
            '[{"Id":"container-1","Image":"demo:latest","ImageID":"sha256:'
            + valid
            + '"},{"Id":"container-2","Image":"other:latest","ImageID":"short"}]'
        )

    monkeypatch.setattr(images, "_run_podman", fake_run)
    refs, complete, reason = images._container_image_references(
        target,
        external=True,
        environment=_ENV,
    )

    assert refs[valid] == ("container-1",)
    assert not complete
    assert "1 个条目" in reason


def test_filtered_image_query_is_exact_and_connection_pinned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _target()
    image_id = _id("a")
    seen: list[tuple[str, ...]] = []

    def fake_run(
        command: list[str],
        environment: Mapping[str, str] | None,
        *,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        del environment, timeout
        seen.append(tuple(command))
        return _completed("sha256:" + image_id + "\n")

    monkeypatch.setattr(images, "_run_podman", fake_run)
    result = images._filtered_image_ids(target, "readonly=true", _ENV)

    assert result == {image_id}
    assert seen == [
        (
            "podman.exe",
            "--connection",
            "podman-machine-default",
            "images",
            "--all",
            "--no-trunc",
            "--filter",
            "readonly=true",
            "--quiet",
        )
    ]


def test_short_image_id_never_creates_mutation_authority() -> None:
    with pytest.raises(RuntimeError, match="完整 64-hex"):
        images._canonical_image_id("abc123", "test")


def test_remove_uses_exact_image_id_no_prune_and_no_force(monkeypatch: pytest.MonkeyPatch) -> None:
    target = _target()
    expected = _entry()
    before = PodmanImageInventory(target, (expected,), True, "", "before")
    after = PodmanImageInventory(target, (), True, "", "after")
    inventories = iter((before, before, after))
    monkeypatch.setattr(images, "inspect_podman_images", lambda environment=None: next(inventories))
    commands: list[tuple[str, ...]] = []

    def fake_run(
        command: list[str],
        environment: Mapping[str, str] | None,
        *,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        del environment, timeout
        commands.append(tuple(command))
        return _completed("Deleted: " + expected.image_id)

    monkeypatch.setattr(images, "_run_podman", fake_run)
    result = remove_podman_image(expected, target, _ENV)

    assert result.command == (
        "podman.exe",
        "--connection",
        "podman-machine-default",
        "image",
        "rm",
        "--no-prune",
        expected.image_id,
    )
    assert "--force" not in result.command
    assert "--all" not in result.command
    assert commands == [result.command]


def test_remove_refuses_reviewed_target_change(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = _entry()
    reviewed = _target()
    changed = PodmanMachineConnection(
        executable="podman.exe",
        connection_name="other-machine",
        connection_uri="ssh://user@127.0.0.1:55222/run/user/1000/podman/podman.sock",
        machine_name="other-machine",
        vm_type="wsl",
        running=True,
        rootful=False,
    )
    monkeypatch.setattr(
        images,
        "inspect_podman_images",
        lambda environment=None: PodmanImageInventory(changed, (expected,), True, "", "df"),
    )
    monkeypatch.setattr(
        images,
        "_run_podman",
        lambda *args, **kwargs: pytest.fail("image rm must not run on changed target"),
    )

    with pytest.raises(RuntimeError, match="查看/确认的目标已不同"):
        remove_podman_image(expected, reviewed, _ENV)


def test_remove_refuses_image_identity_change(monkeypatch: pytest.MonkeyPatch) -> None:
    target = _target()
    expected = _entry()
    changed = replace(expected, repo_tags=("docker.io/library/demo:changed",))
    inventories = iter(
        (
            PodmanImageInventory(target, (expected,), True, "", "before"),
            PodmanImageInventory(target, (changed,), True, "", "fresh"),
        )
    )
    monkeypatch.setattr(images, "inspect_podman_images", lambda environment=None: next(inventories))

    with pytest.raises(RuntimeError, match="绑定已变化"):
        remove_podman_image(expected, target, _ENV)


def test_remove_requires_postcondition_absence(monkeypatch: pytest.MonkeyPatch) -> None:
    target = _target()
    expected = _entry()
    inventory = PodmanImageInventory(target, (expected,), True, "", "df")
    inventories = iter((inventory, inventory, inventory))
    monkeypatch.setattr(images, "inspect_podman_images", lambda environment=None: next(inventories))
    monkeypatch.setattr(images, "_run_podman", lambda *args, **kwargs: _completed("Deleted"))

    with pytest.raises(RuntimeError, match="仍然存在"):
        remove_podman_image(expected, target, _ENV)
