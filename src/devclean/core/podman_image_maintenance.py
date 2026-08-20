"""Exact Podman image inventory and user-reviewed image removal on Windows."""

# ruff: noqa: RUF001

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from devclean.core.podman_container_maintenance import (
    PodmanMachineConnection,
    inspect_podman_machine_target,
)

_BATCH_SIZE = 40
_FULL_IMAGE_ID = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class PodmanImageEntry:
    image_id: str
    repo_tags: tuple[str, ...]
    repo_digests: tuple[str, ...]
    parent_id: str
    child_ids: tuple[str, ...]
    created: str
    size: int
    manifest_type: str
    read_only: bool
    manifest_list: bool
    podman_container_ids: tuple[str, ...]
    external_container_ids: tuple[str, ...]
    executable: bool
    reason: str


@dataclass(frozen=True, slots=True)
class PodmanImageInventory:
    target: PodmanMachineConnection
    images: tuple[PodmanImageEntry, ...]
    reference_proof_complete: bool
    reference_proof_reason: str
    system_df: str


@dataclass(frozen=True, slots=True)
class PodmanImageRemoveResult:
    target: PodmanMachineConnection
    image: PodmanImageEntry
    command: tuple[str, ...]
    stdout: str
    system_df_before: str
    system_df_after: str


def inspect_podman_images(
    environment: Mapping[str, str] | None = None,
) -> PodmanImageInventory:
    """Inventory exact images and all known container references on one machine."""

    target = inspect_podman_machine_target(environment)
    rows = _image_list_rows(target, environment)
    image_ids = _unique_image_ids(rows)
    read_only_ids = _filtered_image_ids(target, "readonly=true", environment)
    manifest_ids = _filtered_image_ids(target, "manifest=true", environment)
    if not read_only_ids.issubset(image_ids):
        raise RuntimeError("Podman readonly image filter 返回了主镜像清单之外的 ID")
    if not manifest_ids.issubset(image_ids):
        raise RuntimeError("Podman manifest image filter 返回了主镜像清单之外的 ID")

    regular_ids = tuple(sorted(image_ids - manifest_ids))
    inspected = _inspect_regular_images(target, regular_ids, environment)
    if set(inspected) != set(regular_ids):
        raise RuntimeError("Podman image inspect 未完整覆盖主镜像清单")

    summaries = _summaries(rows)
    ordinary_refs, ordinary_complete, ordinary_reason = _container_image_references(
        target,
        external=False,
        environment=environment,
    )
    external_refs, external_complete, external_reason = _container_image_references(
        target,
        external=True,
        environment=environment,
    )
    reference_complete = ordinary_complete and external_complete
    reference_reason = "; ".join(reason for reason in (ordinary_reason, external_reason) if reason)

    parents: dict[str, str] = {}
    records: dict[str, _ImageRecord] = {}
    for image_id in sorted(image_ids):
        if image_id in manifest_ids:
            summary = summaries.get(image_id)
            if summary is None:
                raise RuntimeError(f"Podman manifest image 缺少主清单记录: {image_id}")
            record = _manifest_record(summary)
        else:
            record = inspected[image_id]
        records[image_id] = record
        if record.parent_id:
            parents[image_id] = record.parent_id

    children: dict[str, list[str]] = {image_id: [] for image_id in image_ids}
    for child_id, parent_id in parents.items():
        if parent_id in children:
            children[parent_id].append(child_id)

    images: list[PodmanImageEntry] = []
    for image_id in sorted(image_ids):
        record = records[image_id]
        podman_containers = tuple(sorted(ordinary_refs.get(image_id, ())))
        external_containers = tuple(sorted(external_refs.get(image_id, ())))
        child_ids = tuple(sorted(children.get(image_id, ())))
        read_only = image_id in read_only_ids
        manifest_list = image_id in manifest_ids
        executable, reason = _image_decision(
            record,
            read_only=read_only,
            manifest_list=manifest_list,
            child_ids=child_ids,
            podman_container_ids=podman_containers,
            external_container_ids=external_containers,
            reference_proof_complete=reference_complete,
            reference_proof_reason=reference_reason,
        )
        images.append(
            PodmanImageEntry(
                image_id=image_id,
                repo_tags=record.repo_tags,
                repo_digests=record.repo_digests,
                parent_id=record.parent_id,
                child_ids=child_ids,
                created=record.created,
                size=record.size,
                manifest_type=record.manifest_type,
                read_only=read_only,
                manifest_list=manifest_list,
                podman_container_ids=podman_containers,
                external_container_ids=external_containers,
                executable=executable,
                reason=reason,
            )
        )

    images.sort(key=lambda item: item.size, reverse=True)
    return PodmanImageInventory(
        target=target,
        images=tuple(images),
        reference_proof_complete=reference_complete,
        reference_proof_reason=reference_reason,
        system_df=_system_df(target, environment),
    )


def remove_podman_image(
    expected: PodmanImageEntry,
    expected_target: PodmanMachineConnection,
    environment: Mapping[str, str] | None = None,
) -> PodmanImageRemoveResult:
    """Remove one exact reviewed image without force or parent pruning."""

    initial = inspect_podman_images(environment)
    _require_reviewed_target(expected_target, initial.target)
    current = _exact_image(initial.images, expected.image_id)
    _require_same_image(expected, current)
    if not current.executable:
        raise RuntimeError(current.reason)

    fresh = inspect_podman_images(environment)
    _require_reviewed_target(initial.target, fresh.target)
    current = _exact_image(fresh.images, expected.image_id)
    _require_same_image(expected, current)
    if not current.executable:
        raise RuntimeError(current.reason)

    command = (
        fresh.target.executable,
        "--connection",
        fresh.target.connection_name,
        "image",
        "rm",
        "--no-prune",
        current.image_id,
    )
    result = _run_podman(list(command), environment, timeout=300)

    after = inspect_podman_images(environment)
    _require_reviewed_target(fresh.target, after.target)
    if any(item.image_id == current.image_id for item in after.images):
        raise RuntimeError("podman image rm 返回成功，但精确 image ID 仍然存在")
    return PodmanImageRemoveResult(
        target=fresh.target,
        image=current,
        command=command,
        stdout=(result.stdout or result.stderr).strip(),
        system_df_before=fresh.system_df,
        system_df_after=after.system_df,
    )


@dataclass(frozen=True, slots=True)
class _ImageRecord:
    image_id: str
    repo_tags: tuple[str, ...]
    repo_digests: tuple[str, ...]
    parent_id: str
    created: str
    size: int
    manifest_type: str


def _image_list_rows(
    target: PodmanMachineConnection,
    environment: Mapping[str, str] | None,
) -> list[dict[str, object]]:
    return _json_list(
        _run_podman(
            [
                target.executable,
                "--connection",
                target.connection_name,
                "images",
                "--all",
                "--no-trunc",
                "--format",
                "json",
            ],
            environment,
            timeout=120,
        ).stdout,
        "podman images",
    )


def _unique_image_ids(rows: Sequence[dict[str, object]]) -> set[str]:
    ids: set[str] = set()
    for row in rows:
        raw = row.get("Id", row.get("ID"))
        ids.add(_canonical_image_id(raw, "podman images image ID"))
    return ids


def _filtered_image_ids(
    target: PodmanMachineConnection,
    filter_value: str,
    environment: Mapping[str, str] | None,
) -> set[str]:
    result = _run_podman(
        [
            target.executable,
            "--connection",
            target.connection_name,
            "images",
            "--all",
            "--no-trunc",
            "--filter",
            filter_value,
            "--quiet",
        ],
        environment,
        timeout=120,
    )
    ids: set[str] = set()
    for line in result.stdout.splitlines():
        value = line.strip()
        if value:
            ids.add(_canonical_image_id(value, f"podman images --filter {filter_value}"))
    return ids


def _inspect_regular_images(
    target: PodmanMachineConnection,
    ids: tuple[str, ...],
    environment: Mapping[str, str] | None,
) -> dict[str, _ImageRecord]:
    records: dict[str, _ImageRecord] = {}
    for start in range(0, len(ids), _BATCH_SIZE):
        batch = ids[start : start + _BATCH_SIZE]
        payloads = _json_list(
            _run_podman(
                [
                    target.executable,
                    "--connection",
                    target.connection_name,
                    "image",
                    "inspect",
                    *batch,
                ],
                environment,
                timeout=120,
            ).stdout,
            "podman image inspect",
        )
        for payload in payloads:
            record = _inspect_record(payload)
            if record.image_id in records:
                raise RuntimeError(f"Podman image inspect 重复返回 image ID: {record.image_id}")
            records[record.image_id] = record
    return records


def _inspect_record(payload: dict[str, object]) -> _ImageRecord:
    image_id = _canonical_image_id(payload.get("Id", payload.get("ID")), "image inspect ID")
    return _ImageRecord(
        image_id=image_id,
        repo_tags=_string_tuple(payload.get("RepoTags"), "image inspect RepoTags"),
        repo_digests=_string_tuple(payload.get("RepoDigests"), "image inspect RepoDigests"),
        parent_id=_optional_image_id(payload.get("Parent"), "image inspect Parent"),
        created=_required_string(payload.get("Created"), "image inspect Created"),
        size=_nonnegative_int(payload.get("Size"), "image inspect Size"),
        manifest_type=str(payload.get("ManifestType", "")).strip(),
    )


def _summaries(rows: Sequence[dict[str, object]]) -> dict[str, dict[str, object]]:
    grouped: dict[str, dict[str, object]] = {}
    for row in rows:
        image_id = _canonical_image_id(row.get("Id", row.get("ID")), "podman images image ID")
        if image_id in grouped:
            existing = grouped[image_id]
            if existing != row:
                raise RuntimeError(f"podman images 对同一 image ID 返回不一致记录: {image_id}")
        else:
            grouped[image_id] = row
    return grouped


def _manifest_record(payload: dict[str, object]) -> _ImageRecord:
    image_id = _canonical_image_id(payload.get("Id", payload.get("ID")), "manifest image ID")
    tags = _string_tuple(payload.get("RepoTags", payload.get("Names", [])), "manifest RepoTags")
    digests = _string_tuple(payload.get("RepoDigests", []), "manifest RepoDigests")
    parent_id = _optional_image_id(
        payload.get("ParentId", payload.get("Parent")), "manifest ParentId"
    )
    created_raw = payload.get("CreatedAt", payload.get("Created", "unknown"))
    created = str(created_raw).strip() or "unknown"
    size_raw = payload.get("VirtualSize", payload.get("Size", 0))
    size = _best_effort_size(size_raw)
    return _ImageRecord(
        image_id=image_id,
        repo_tags=tags,
        repo_digests=digests,
        parent_id=parent_id,
        created=created,
        size=size,
        manifest_type="manifest-list/index",
    )


def _container_image_references(
    target: PodmanMachineConnection,
    *,
    external: bool,
    environment: Mapping[str, str] | None,
) -> tuple[dict[str, tuple[str, ...]], bool, str]:
    command = [
        target.executable,
        "--connection",
        target.connection_name,
        "ps",
        "--all",
    ]
    if external:
        command.append("--external")
    command.extend(("--no-trunc", "--format", "json"))
    label = "external container" if external else "Podman container"
    try:
        payloads = _json_list(
            _run_podman(command, environment, timeout=120).stdout,
            f"podman ps ({label})",
        )
    except RuntimeError as error:
        return {}, False, f"无法完整证明 {label} image 引用: {error}"

    refs: dict[str, list[str]] = {}
    incomplete: list[str] = []
    for payload in payloads:
        container_id = str(payload.get("Id", payload.get("ID", ""))).strip() or "unknown"
        raw_image_id = payload.get("ImageID")
        if isinstance(raw_image_id, str) and raw_image_id.strip():
            try:
                image_id = _canonical_image_id(raw_image_id, f"{label} ImageID")
            except RuntimeError:
                incomplete.append(container_id)
                continue
            refs.setdefault(image_id, []).append(container_id)
            continue
        image_name = str(payload.get("Image", "")).strip().casefold()
        if image_name not in {"", "scratch"}:
            incomplete.append(container_id)

    frozen = {image_id: tuple(sorted(set(ids))) for image_id, ids in refs.items()}
    if incomplete:
        return (
            frozen,
            False,
            f"{label} 有 {len(incomplete)} 个条目缺少可验证的完整 ImageID",
        )
    return frozen, True, ""


def _image_decision(
    record: _ImageRecord,
    *,
    read_only: bool,
    manifest_list: bool,
    child_ids: tuple[str, ...],
    podman_container_ids: tuple[str, ...],
    external_container_ids: tuple[str, ...],
    reference_proof_complete: bool,
    reference_proof_reason: str,
) -> tuple[bool, str]:
    if not reference_proof_complete:
        return False, reference_proof_reason or "container image 引用证明不完整"
    if read_only:
        return False, "image 位于 Podman read-only image store；不允许删除"
    if manifest_list:
        return (
            False,
            "image 是 manifest list/image index；使用独立 manifest 生命周期，不在本 lane 删除",
        )
    if podman_container_ids:
        return False, f"image 被 {len(podman_container_ids)} 个 Podman container 引用"
    if external_container_ids:
        return (
            False,
            f"image 被 {len(external_container_ids)} 个 Buildah/CRI-O external container 引用",
        )
    if child_ids:
        return False, f"image 仍有 {len(child_ids)} 个 child image；不删除父镜像"
    if len(record.repo_tags) > 1:
        return (
            False,
            f"image 有 {len(record.repo_tags)} 个 tag；不通过 image ID 一次移除多个用户可见名称",
        )
    return True, "技术边界已证明；image 是否保留取决于用户，属于 USER_REVIEW"


def _exact_image(images: Sequence[PodmanImageEntry], image_id: str) -> PodmanImageEntry:
    matches = [item for item in images if item.image_id == image_id]
    if len(matches) != 1:
        raise RuntimeError(f"无法唯一确认 Podman image {image_id!r}: found={len(matches)}")
    return matches[0]


def _require_same_image(expected: PodmanImageEntry, current: PodmanImageEntry) -> None:
    if (
        current.image_id != expected.image_id
        or current.repo_tags != expected.repo_tags
        or current.repo_digests != expected.repo_digests
        or current.parent_id != expected.parent_id
        or current.child_ids != expected.child_ids
        or current.created != expected.created
        or current.size != expected.size
        or current.manifest_type != expected.manifest_type
        or current.read_only != expected.read_only
        or current.manifest_list != expected.manifest_list
        or current.podman_container_ids != expected.podman_container_ids
        or current.external_container_ids != expected.external_container_ids
    ):
        raise RuntimeError("Podman image identity/tag/parent/reference 绑定已变化；请重新检查")


def _require_reviewed_target(
    expected: PodmanMachineConnection,
    current: PodmanMachineConnection,
) -> None:
    if _target_key(expected) != _target_key(current):
        raise RuntimeError("Podman machine connection 与用户查看/确认的目标已不同；请重新检查")


def _target_key(target: PodmanMachineConnection) -> tuple[str, str, str, str, bool, str]:
    return (
        target.connection_name,
        target.connection_uri,
        target.machine_name,
        target.vm_type,
        target.rootful,
        target.executable,
    )


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


def _canonical_image_id(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise RuntimeError(f"{label} 缺少 string image ID")
    candidate = value.strip().casefold()
    if candidate.startswith("sha256:"):
        candidate = candidate[7:]
    if not _FULL_IMAGE_ID.fullmatch(candidate):
        raise RuntimeError(f"{label} 不是完整 64-hex SHA256 image ID: {value!r}")
    return candidate


def _optional_image_id(value: object, label: str) -> str:
    if value is None:
        return ""
    if isinstance(value, str) and not value.strip():
        return ""
    return _canonical_image_id(value, label)


def _string_tuple(value: object, label: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise RuntimeError(f"{label} 不是 JSON array")
    items: list[str] = []
    for raw in value:
        if not isinstance(raw, str) or not raw.strip():
            raise RuntimeError(f"{label} 包含无效 string")
        items.append(raw.strip())
    return tuple(sorted(set(items)))


def _required_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{label} 缺少非空 string")
    return value.strip()


def _nonnegative_int(value: object, label: str) -> int:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"{label} 不是有效整数") from error
    if parsed < 0:
        raise RuntimeError(f"{label} 不能为负数")
    return parsed


def _best_effort_size(value: object) -> int:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


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


def _run_podman(
    command: list[str],
    environment: Mapping[str, str] | None,
    *,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    import os

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
        raise RuntimeError(f"无法执行 Podman image CLI: {error}") from error
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"Podman image CLI 失败 (exit {result.returncode}): {detail}")
    return result


__all__ = [
    "PodmanImageEntry",
    "PodmanImageInventory",
    "PodmanImageRemoveResult",
    "inspect_podman_images",
    "remove_podman_image",
]
