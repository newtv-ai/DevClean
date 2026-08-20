"""Exact Hugging Face Hub cache inventory and vendor-supported maintenance."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from devclean.core import _application_cleanup_impl as _impl
from devclean.core.huggingface_cleanup import (
    clear_huggingface_process_cache,
    hf_executable,
    huggingface_process_running,
    huggingface_roots,
)
from devclean.platform.windows.filesystem import read_file_metadata
from devclean.platform.windows.volumes import is_local_fixed_path

_REVISION_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_KNOWN_REPO_TYPES = frozenset({"model", "dataset", "space"})


class HuggingFaceCacheKind(StrEnum):
    HUB = "hub"
    XET = "xet"
    ASSETS = "assets"


@dataclass(frozen=True, slots=True)
class HuggingFaceCacheEntry:
    kind: HuggingFaceCacheKind
    path: Path
    logical_bytes: int
    exists: bool


@dataclass(frozen=True, slots=True)
class HuggingFaceStorageInventory:
    caches: tuple[HuggingFaceCacheEntry, ...]

    @property
    def total_cache_bytes(self) -> int:
        return sum(item.logical_bytes for item in self.caches)


@dataclass(frozen=True, slots=True)
class HuggingFacePathIdentity:
    path: Path
    volume_serial: int
    file_id: str
    file_id_kind: str
    creation_time_ns: int | None
    last_write_time_ns: int | None
    is_directory: bool


@dataclass(frozen=True, slots=True)
class HuggingFaceHubRevision:
    cache_id: str
    repo_id: str
    repo_type: str
    commit_hash: str
    snapshot_path: str
    vendor_size: str
    last_modified: str
    refs: tuple[str, ...]
    deletion_supported: bool
    reason: str


@dataclass(frozen=True, slots=True)
class HuggingFaceHubRepo:
    cache_id: str
    repo_id: str
    repo_type: str
    vendor_size: str
    last_accessed: str
    last_modified: str
    refs: tuple[str, ...]
    revisions: tuple[HuggingFaceHubRevision, ...]
    deletion_supported: bool
    reason: str


@dataclass(frozen=True, slots=True)
class HuggingFaceHubInventory:
    hf_tool: HuggingFacePathIdentity
    hub_root: HuggingFacePathIdentity
    repos: tuple[HuggingFaceHubRepo, ...]
    warnings: tuple[str, ...]
    revision_delete_proof_complete: bool


@dataclass(frozen=True, slots=True)
class HuggingFaceDeletePreview:
    target: str
    repos: int
    revisions: int
    vendor_size: str


@dataclass(frozen=True, slots=True)
class HuggingFaceDeleteResult:
    target: str
    repos_deleted: int
    revisions_deleted: int
    vendor_freed: str
    stdout: str


@dataclass(frozen=True, slots=True)
class HuggingFaceHubPrunePreview:
    revisions: int
    incomplete: int
    vendor_size: str
    detached_revision_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class HuggingFacePruneResult:
    path: Path
    before_bytes: int
    after_bytes: int
    stdout: str
    revisions_deleted: int = 0
    incomplete_deleted: int = 0
    vendor_freed: str = ""

    @property
    def reclaimed_bytes(self) -> int:
        return max(0, self.before_bytes - self.after_bytes)


def inventory_huggingface_storage(
    environment: Mapping[str, str] | None = None,
) -> HuggingFaceStorageInventory:
    """Keep the existing coarse cache-root inventory as read-only context.

    Directory accounting never follows symlinks. It is informational only; exact
    Hub maintenance uses the vendor's own per-repository/revision accounting.
    """

    roots = huggingface_roots(environment)
    pairs = (
        (HuggingFaceCacheKind.HUB, roots.hub_cache_roots),
        (HuggingFaceCacheKind.XET, roots.xet_cache_roots),
        (HuggingFaceCacheKind.ASSETS, roots.assets_cache_roots),
    )
    entries: list[HuggingFaceCacheEntry] = []
    seen: set[tuple[HuggingFaceCacheKind, str]] = set()
    for kind, candidates in pairs:
        for raw in candidates:
            path = Path(str(raw))
            key = (kind, os.path.normcase(os.path.normpath(str(path))))
            if key in seen:
                continue
            seen.add(key)
            try:
                exists = path.is_dir()
            except OSError:
                exists = False
            entries.append(
                HuggingFaceCacheEntry(
                    kind=kind,
                    path=path,
                    logical_bytes=_directory_bytes(path) if exists else 0,
                    exists=exists,
                )
            )
    return HuggingFaceStorageInventory(tuple(entries))


def inventory_huggingface_hub_cache(
    environment: Mapping[str, str] | None = None,
) -> HuggingFaceHubInventory:
    """Inventory exact Hub repos/revisions through current ``hf cache ls`` JSON."""

    root = _resolve_hub_root(environment)
    root_identity = _path_identity(root, expect_directory=True, label="Hugging Face Hub cache")
    tool_identity = _resolve_hf_tool(environment)
    env = _hub_environment(root, environment)

    first_repos, first_stderr, _ = _run_hf_json(
        tool_identity,
        ("cache", "ls", "--cache-dir", str(root)),
        env,
        timeout=120,
    )
    revisions_payload, revisions_stderr, _ = _run_hf_json(
        tool_identity,
        ("cache", "ls", "--revisions", "--cache-dir", str(root)),
        env,
        timeout=120,
    )
    second_repos, second_stderr, _ = _run_hf_json(
        tool_identity,
        ("cache", "ls", "--cache-dir", str(root)),
        env,
        timeout=120,
    )

    if first_repos != second_repos:
        raise RuntimeError("Hugging Face Hub cache 在 inventory 期间发生变化; 请重新检查")
    fresh_root = _path_identity(root, expect_directory=True, label="Hugging Face Hub cache")
    fresh_tool = _path_identity(tool_identity.path, expect_directory=False, label="hf CLI")
    if fresh_root != root_identity or fresh_tool != tool_identity:
        raise RuntimeError("Hugging Face cache/tool 身份在 inventory 期间发生变化")

    repo_rows = _require_json_rows(first_repos, "hf cache ls")
    revision_rows = _require_json_rows(revisions_payload, "hf cache ls --revisions")
    warnings = _unique_nonempty_lines((first_stderr, revisions_stderr, second_stderr))
    return _build_hub_inventory(
        tool_identity,
        root_identity,
        repo_rows,
        revision_rows,
        warnings,
    )


def preview_huggingface_repo_removal(
    reviewed: HuggingFaceHubInventory,
    expected: HuggingFaceHubRepo,
    environment: Mapping[str, str] | None = None,
) -> HuggingFaceDeletePreview:
    """Dry-run one exact repo deletion after fresh identity/state validation."""

    current = _validated_current_inventory(reviewed, environment)
    repo = _require_same_repo(current, expected)
    if not repo.deletion_supported:
        raise RuntimeError(repo.reason)
    _require_process_idle()
    payload, _stderr, _stdout = _run_hf_json(
        current.hf_tool,
        (
            "cache",
            "rm",
            repo.cache_id,
            "--cache-dir",
            str(current.hub_root.path),
            "--dry-run",
            "--yes",
        ),
        _hub_environment(current.hub_root.path, environment),
        timeout=120,
    )
    preview = _parse_rm_preview(payload, repo.cache_id)
    if preview.repos != 1 or preview.revisions != len(repo.revisions):
        raise RuntimeError("hf cache rm dry-run 范围与用户审核 repo 不一致; 拒绝执行")
    return preview


def remove_huggingface_repo(
    reviewed: HuggingFaceHubInventory,
    expected: HuggingFaceHubRepo,
    preview: HuggingFaceDeletePreview,
    environment: Mapping[str, str] | None = None,
) -> HuggingFaceDeleteResult:
    """Remove one exact reviewed cached repo through ``hf cache rm``."""

    current = _validated_current_inventory(reviewed, environment)
    repo = _require_same_repo(current, expected)
    if preview.target != repo.cache_id:
        raise RuntimeError("repo dry-run target 与当前审核对象不一致")
    fresh_preview = preview_huggingface_repo_removal(current, repo, environment)
    if fresh_preview != preview:
        raise RuntimeError("repo dry-run 结果已变化; 请重新检查后再删除")
    _require_process_idle()
    payload, _stderr, stdout = _run_hf_json(
        current.hf_tool,
        (
            "cache",
            "rm",
            repo.cache_id,
            "--cache-dir",
            str(current.hub_root.path),
            "--yes",
        ),
        _hub_environment(current.hub_root.path, environment),
        timeout=600,
    )
    result = _parse_rm_result(payload, repo.cache_id, stdout)
    if result.repos_deleted != preview.repos or result.revisions_deleted != preview.revisions:
        raise RuntimeError("hf cache rm 实际删除计数与 dry-run 不一致; 无法确认结果")
    after = inventory_huggingface_hub_cache(environment)
    _require_same_boundaries(current, after)
    if any(item.cache_id.casefold() == repo.cache_id.casefold() for item in after.repos):
        raise RuntimeError("hf 返回成功后目标 repo 仍然存在; 不报告成功")
    return result


def preview_huggingface_revision_removal(
    reviewed: HuggingFaceHubInventory,
    expected: HuggingFaceHubRevision,
    environment: Mapping[str, str] | None = None,
) -> HuggingFaceDeletePreview:
    """Dry-run one globally unique full commit hash before exact revision removal."""

    current = _validated_current_inventory(reviewed, environment)
    revision = _require_same_revision(current, expected)
    if not revision.deletion_supported:
        raise RuntimeError(revision.reason)
    repo = _repo_for_revision(current, revision)
    _require_process_idle()
    payload, _stderr, _stdout = _run_hf_json(
        current.hf_tool,
        (
            "cache",
            "rm",
            revision.commit_hash,
            "--cache-dir",
            str(current.hub_root.path),
            "--dry-run",
            "--yes",
        ),
        _hub_environment(current.hub_root.path, environment),
        timeout=120,
    )
    preview = _parse_rm_preview(payload, revision.commit_hash)
    expected_repos = 1 if len(repo.revisions) == 1 else 0
    if preview.repos != expected_repos or preview.revisions != 1:
        raise RuntimeError("hf cache rm dry-run 范围与用户审核 revision 不一致; 拒绝执行")
    return preview


def remove_huggingface_revision(
    reviewed: HuggingFaceHubInventory,
    expected: HuggingFaceHubRevision,
    preview: HuggingFaceDeletePreview,
    environment: Mapping[str, str] | None = None,
) -> HuggingFaceDeleteResult:
    """Remove one exact globally-unique full revision through the vendor CLI."""

    current = _validated_current_inventory(reviewed, environment)
    revision = _require_same_revision(current, expected)
    if preview.target.casefold() != revision.commit_hash.casefold():
        raise RuntimeError("revision dry-run target 与当前审核对象不一致")
    fresh_preview = preview_huggingface_revision_removal(current, revision, environment)
    if fresh_preview != preview:
        raise RuntimeError("revision dry-run 结果已变化; 请重新检查后再删除")
    _require_process_idle()
    payload, _stderr, stdout = _run_hf_json(
        current.hf_tool,
        (
            "cache",
            "rm",
            revision.commit_hash,
            "--cache-dir",
            str(current.hub_root.path),
            "--yes",
        ),
        _hub_environment(current.hub_root.path, environment),
        timeout=600,
    )
    result = _parse_rm_result(payload, revision.commit_hash, stdout)
    if result.repos_deleted != preview.repos or result.revisions_deleted != 1:
        raise RuntimeError("hf cache rm 实际 revision 删除计数与 dry-run 不一致")
    after = inventory_huggingface_hub_cache(environment)
    _require_same_boundaries(current, after)
    if _find_revision(after, revision.commit_hash) is not None:
        raise RuntimeError("hf 返回成功后目标 revision 仍然存在; 不报告成功")
    return result


def preview_huggingface_hub_prune(
    reviewed: HuggingFaceHubInventory,
    environment: Mapping[str, str] | None = None,
) -> HuggingFaceHubPrunePreview:
    """Dry-run vendor prune; detached revisions remain USER_REVIEW, not automatic."""

    current = _validated_current_inventory(reviewed, environment)
    _require_process_idle()
    payload, _stderr, _stdout = _run_hf_json(
        current.hf_tool,
        (
            "cache",
            "prune",
            "--cache-dir",
            str(current.hub_root.path),
            "--dry-run",
            "--yes",
        ),
        _hub_environment(current.hub_root.path, environment),
        timeout=120,
        allow_empty=True,
    )
    detached = tuple(
        sorted(
            revision.commit_hash
            for repo in current.repos
            for revision in repo.revisions
            if not revision.refs
        )
    )
    if payload is None:
        if detached:
            raise RuntimeError(
                "hf cache prune 未返回 dry-run JSON, 但 inventory 存在 detached revision"
            )
        return HuggingFaceHubPrunePreview(0, 0, "0 B", ())
    mapping = _require_json_mapping(payload, "hf cache prune --dry-run")
    if mapping.get("dry_run") is not True:
        raise RuntimeError("hf cache prune dry-run JSON 缺少 dry_run=true")
    revisions = _require_nonnegative_int(mapping, "revisions")
    incomplete = _require_nonnegative_int(mapping, "incomplete")
    size = _require_text(mapping, "size")
    if revisions != len(detached):
        raise RuntimeError("hf cache prune dry-run revision 范围与 fresh inventory 不一致")
    return HuggingFaceHubPrunePreview(revisions, incomplete, size, detached)


def execute_huggingface_hub_prune(
    reviewed: HuggingFaceHubInventory,
    preview: HuggingFaceHubPrunePreview,
    environment: Mapping[str, str] | None = None,
) -> HuggingFacePruneResult:
    """Execute reviewed prune only when a fresh vendor dry-run is identical."""

    current = _validated_current_inventory(reviewed, environment)
    fresh_preview = preview_huggingface_hub_prune(current, environment)
    if fresh_preview != preview:
        raise RuntimeError("hf cache prune dry-run 结果已变化; 请重新检查后再清理")
    if preview.revisions == 0 and preview.incomplete == 0:
        return HuggingFacePruneResult(
            path=current.hub_root.path,
            before_bytes=0,
            after_bytes=0,
            stdout="Nothing to prune.",
            vendor_freed="0 B",
        )
    _require_process_idle()
    before = _directory_bytes(current.hub_root.path)
    payload, _stderr, stdout = _run_hf_json(
        current.hf_tool,
        (
            "cache",
            "prune",
            "--cache-dir",
            str(current.hub_root.path),
            "--yes",
        ),
        _hub_environment(current.hub_root.path, environment),
        timeout=600,
    )
    mapping = _require_json_mapping(payload, "hf cache prune")
    revisions_deleted = _require_nonnegative_int(mapping, "revisions_deleted")
    incomplete_deleted = _require_nonnegative_int(mapping, "incomplete_deleted")
    freed = _require_text(mapping, "freed")
    if revisions_deleted != preview.revisions or incomplete_deleted != preview.incomplete:
        raise RuntimeError("hf cache prune 实际删除计数与 dry-run 不一致; 无法确认结果")
    after_inventory = inventory_huggingface_hub_cache(environment)
    _require_same_boundaries(current, after_inventory)
    remaining = {
        revision.commit_hash.casefold()
        for repo in after_inventory.repos
        for revision in repo.revisions
    }
    if any(commit.casefold() in remaining for commit in preview.detached_revision_ids):
        raise RuntimeError("hf prune 返回成功后 reviewed detached revision 仍存在")
    after = _directory_bytes(current.hub_root.path)
    return HuggingFacePruneResult(
        path=current.hub_root.path,
        before_bytes=before,
        after_bytes=after,
        stdout=stdout,
        revisions_deleted=revisions_deleted,
        incomplete_deleted=incomplete_deleted,
        vendor_freed=freed,
    )


def prune_huggingface_hub_cache(
    path: Path,
    environment: Mapping[str, str] | None = None,
) -> HuggingFacePruneResult:
    """Compatibility wrapper for the previously exposed vendor prune function.

    It is now strengthened with exact root/tool identities, a vendor dry-run and a
    fresh-state equality check. Product UI should still obtain explicit USER_REVIEW.
    """

    inventory = inventory_huggingface_hub_cache(environment)
    target = _impl._normalize(path)
    if target != _impl._normalize(inventory.hub_root.path):
        raise ValueError(f"不是已审计的 Hugging Face Hub cache 路径: {path}")
    preview = preview_huggingface_hub_prune(inventory, environment)
    return execute_huggingface_hub_prune(inventory, preview, environment)


def _validated_current_inventory(
    reviewed: HuggingFaceHubInventory,
    environment: Mapping[str, str] | None,
) -> HuggingFaceHubInventory:
    _require_process_idle()
    current = inventory_huggingface_hub_cache(environment)
    _require_same_boundaries(reviewed, current)
    if current.repos != reviewed.repos or current.warnings != reviewed.warnings:
        raise RuntimeError("Hugging Face Hub cache 状态自用户审核后已变化; 请刷新")
    _require_process_idle()
    return current


def _require_same_boundaries(
    reviewed: HuggingFaceHubInventory,
    current: HuggingFaceHubInventory,
) -> None:
    if reviewed.hf_tool != current.hf_tool:
        raise RuntimeError("hf CLI 身份自用户审核后已变化")
    if reviewed.hub_root != current.hub_root:
        raise RuntimeError("Hugging Face Hub cache root 身份自用户审核后已变化")


def _require_same_repo(
    inventory: HuggingFaceHubInventory,
    expected: HuggingFaceHubRepo,
) -> HuggingFaceHubRepo:
    matches = [
        repo for repo in inventory.repos if repo.cache_id.casefold() == expected.cache_id.casefold()
    ]
    if len(matches) != 1 or matches[0] != expected:
        raise RuntimeError("Hugging Face cached repo identity/state 已变化")
    return matches[0]


def _require_same_revision(
    inventory: HuggingFaceHubInventory,
    expected: HuggingFaceHubRevision,
) -> HuggingFaceHubRevision:
    matches = [
        revision
        for repo in inventory.repos
        for revision in repo.revisions
        if revision.commit_hash.casefold() == expected.commit_hash.casefold()
    ]
    if len(matches) != 1 or matches[0] != expected:
        raise RuntimeError("Hugging Face cached revision identity/state 已变化或不再唯一")
    return matches[0]


def _repo_for_revision(
    inventory: HuggingFaceHubInventory,
    expected: HuggingFaceHubRevision,
) -> HuggingFaceHubRepo:
    matches = [repo for repo in inventory.repos if expected in repo.revisions]
    if len(matches) != 1:
        raise RuntimeError("无法唯一绑定 revision 到一个 cached repo")
    return matches[0]


def _find_revision(
    inventory: HuggingFaceHubInventory,
    commit_hash: str,
) -> HuggingFaceHubRevision | None:
    matches = [
        revision
        for repo in inventory.repos
        for revision in repo.revisions
        if revision.commit_hash.casefold() == commit_hash.casefold()
    ]
    return matches[0] if len(matches) == 1 else None


def _build_hub_inventory(
    tool: HuggingFacePathIdentity,
    root: HuggingFacePathIdentity,
    repo_rows: list[dict[str, Any]],
    revision_rows: list[dict[str, Any]],
    warnings: tuple[str, ...],
) -> HuggingFaceHubInventory:
    repo_by_id: dict[str, dict[str, Any]] = {}
    for row in repo_rows:
        cache_id = _require_text(row, "id")
        key = cache_id.casefold()
        if key in repo_by_id:
            raise RuntimeError(f"hf cache ls 返回重复 repo id: {cache_id}")
        repo_by_id[key] = row

    revision_counts: Counter[str] = Counter()
    parsed_revision_rows: list[tuple[str, dict[str, Any]]] = []
    for row in revision_rows:
        cache_id = _require_text(row, "id")
        if cache_id.casefold() not in repo_by_id:
            raise RuntimeError(f"revision 引用了 aggregate inventory 中不存在的 repo: {cache_id}")
        commit_hash = _require_text(row, "revision")
        if _REVISION_RE.fullmatch(commit_hash) is None:
            raise RuntimeError(f"hf 返回非完整 40-hex revision: {commit_hash!r}")
        revision_counts[commit_hash.casefold()] += 1
        parsed_revision_rows.append((cache_id.casefold(), row))

    proof_complete = len(warnings) == 0
    revisions_by_repo: dict[str, list[HuggingFaceHubRevision]] = {}
    for cache_key, row in parsed_revision_rows:
        cache_id = _require_text(row, "id")
        repo_id = _require_text(row, "repo_id")
        repo_type = _require_text(row, "repo_type").casefold()
        commit_hash = _require_text(row, "revision")
        refs = _require_text_list(row, "refs")
        unique = revision_counts[commit_hash.casefold()] == 1
        known = repo_type in _KNOWN_REPO_TYPES and _valid_cache_id(cache_id, repo_type)
        supported = proof_complete and unique and known
        if not proof_complete:
            reason = "hf inventory 含 warning; 无法证明 revision hash 在完整 cache 中唯一"
        elif not unique:
            reason = "同一个 40-hex revision hash 在多个 cached repo 中出现; hf CLI 目标解析不唯一"
        elif not known:
            reason = "未知/future Hugging Face repo type; revision 删除 fail closed"
        else:
            reason = "USER_REVIEW: exact full revision hash, vendor dry-run required"
        revision = HuggingFaceHubRevision(
            cache_id=cache_id,
            repo_id=repo_id,
            repo_type=repo_type,
            commit_hash=commit_hash,
            snapshot_path=_require_text(row, "snapshot_path"),
            vendor_size=_require_text(row, "size"),
            last_modified=_require_text(row, "last_modified", allow_empty=True),
            refs=refs,
            deletion_supported=supported,
            reason=reason,
        )
        revisions_by_repo.setdefault(cache_key, []).append(revision)

    repos: list[HuggingFaceHubRepo] = []
    for cache_key, row in repo_by_id.items():
        cache_id = _require_text(row, "id")
        repo_id = _require_text(row, "repo_id")
        repo_type = _require_text(row, "repo_type").casefold()
        revisions = tuple(
            sorted(
                revisions_by_repo.get(cache_key, []),
                key=lambda item: item.commit_hash.casefold(),
            )
        )
        known = repo_type in _KNOWN_REPO_TYPES and _valid_cache_id(cache_id, repo_type)
        supported = known and len(revisions) > 0
        if not known:
            reason = "未知/future Hugging Face repo type; repo 删除 fail closed"
        elif not revisions:
            reason = "repo 没有可验证 revision; 不提供删除"
        else:
            reason = "USER_REVIEW: cached repo 可能是离线/复现实验工作集"
        repos.append(
            HuggingFaceHubRepo(
                cache_id=cache_id,
                repo_id=repo_id,
                repo_type=repo_type,
                vendor_size=_require_text(row, "size"),
                last_accessed=_require_text(row, "last_accessed", allow_empty=True),
                last_modified=_require_text(row, "last_modified", allow_empty=True),
                refs=_require_text_list(row, "refs"),
                revisions=revisions,
                deletion_supported=supported,
                reason=reason,
            )
        )
    repos.sort(key=lambda item: item.cache_id.casefold())
    return HuggingFaceHubInventory(
        hf_tool=tool,
        hub_root=root,
        repos=tuple(repos),
        warnings=warnings,
        revision_delete_proof_complete=proof_complete,
    )


def _valid_cache_id(cache_id: str, repo_type: str) -> bool:
    prefix = {"model": "model/", "dataset": "dataset/", "space": "space/"}.get(repo_type)
    return prefix is not None and cache_id.startswith(prefix) and len(cache_id) > len(prefix)


def _parse_rm_preview(payload: object, target: str) -> HuggingFaceDeletePreview:
    mapping = _require_json_mapping(payload, "hf cache rm --dry-run")
    if mapping.get("dry_run") is not True:
        raise RuntimeError("hf cache rm dry-run JSON 缺少 dry_run=true")
    return HuggingFaceDeletePreview(
        target=target,
        repos=_require_nonnegative_int(mapping, "repos"),
        revisions=_require_nonnegative_int(mapping, "revisions"),
        vendor_size=_require_text(mapping, "size"),
    )


def _parse_rm_result(payload: object, target: str, stdout: str) -> HuggingFaceDeleteResult:
    mapping = _require_json_mapping(payload, "hf cache rm")
    return HuggingFaceDeleteResult(
        target=target,
        repos_deleted=_require_nonnegative_int(mapping, "repos_deleted"),
        revisions_deleted=_require_nonnegative_int(mapping, "revisions_deleted"),
        vendor_freed=_require_text(mapping, "freed"),
        stdout=stdout,
    )


def _run_hf_json(
    tool: HuggingFacePathIdentity,
    arguments: Sequence[str],
    environment: Mapping[str, str],
    *,
    timeout: int,
    allow_empty: bool = False,
) -> tuple[object | None, str, str]:
    command = [str(tool.path), *arguments, "--format", "json"]
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=dict(environment),
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"无法执行 hf cache 命令: {error}") from error
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"hf cache 命令失败 (退出码 {result.returncode}): {detail}")
    stdout = result.stdout.strip()
    if not stdout:
        if allow_empty:
            return None, result.stderr.strip(), stdout
        raise RuntimeError("hf cache JSON 命令没有返回 stdout")
    try:
        return json.loads(stdout), result.stderr.strip(), stdout
    except json.JSONDecodeError as error:
        raise RuntimeError(f"hf cache 返回的 JSON 无法解析: {error}") from error


def _resolve_hub_root(environment: Mapping[str, str] | None) -> Path:
    roots = huggingface_roots(environment).hub_cache_roots
    if len(roots) != 1:
        raise RuntimeError("无法唯一确定 Hugging Face Hub cache root")
    return Path(str(roots[0]))


def _resolve_hf_tool(environment: Mapping[str, str] | None) -> HuggingFacePathIdentity:
    source = os.environ if environment is None else environment
    folded = {key.casefold(): value for key, value in source.items() if value}
    raw = hf_executable(environment)
    candidate = Path(raw)
    if not candidate.is_absolute():
        resolved = shutil.which(raw, path=folded.get("path"))
        if resolved is None:
            raise FileNotFoundError(
                "未找到 Hugging Face `hf` CLI; 需要当前支持 `hf cache ls/rm/prune --format json` 的版本"
            )
        candidate = Path(resolved)
    return _path_identity(candidate, expect_directory=False, label="hf CLI")


def _path_identity(
    path: Path,
    *,
    expect_directory: bool,
    label: str,
) -> HuggingFacePathIdentity:
    candidate = Path(os.path.abspath(path.expanduser()))
    if candidate.is_symlink() or candidate.is_junction():
        raise RuntimeError(f"{label} 不能是 symlink/junction/reparse")
    resolved = candidate.resolve(strict=True)
    if os.path.normcase(os.path.abspath(candidate)) != os.path.normcase(os.path.abspath(resolved)):
        raise RuntimeError(f"{label} 路径包含重定向/reparse")
    if not is_local_fixed_path(resolved):
        raise RuntimeError(f"{label} 不在本地固定磁盘")
    metadata = read_file_metadata(resolved)
    if metadata.is_directory != expect_directory:
        raise RuntimeError(f"{label} 类型与预期不一致")
    if metadata.is_reparse_point or metadata.is_cloud_placeholder:
        raise RuntimeError(f"{label} 是 reparse/cloud placeholder; 不授予维护权限")
    if metadata.volume_serial is None or metadata.file_id is None or metadata.file_id_kind is None:
        raise RuntimeError(f"{label} 缺少稳定文件身份")
    # Directory last-write timestamps legitimately change when the vendor removes
    # direct repo children. Stable directory authority is the exact path + volume
    # + file ID; file timestamps remain bound for the hf executable itself.
    creation_time_ns = None if expect_directory else metadata.creation_time_ns
    last_write_time_ns = None if expect_directory else metadata.last_write_time_ns
    return HuggingFacePathIdentity(
        path=resolved,
        volume_serial=metadata.volume_serial,
        file_id=metadata.file_id,
        file_id_kind=metadata.file_id_kind,
        creation_time_ns=creation_time_ns,
        last_write_time_ns=last_write_time_ns,
        is_directory=metadata.is_directory,
    )


def _hub_environment(
    root: Path,
    environment: Mapping[str, str] | None,
) -> dict[str, str]:
    env = dict(os.environ)
    if environment is not None:
        env.update({str(key): str(value) for key, value in environment.items()})
    env["HF_HUB_CACHE"] = str(root)
    return env


def _require_process_idle() -> None:
    clear_huggingface_process_cache()
    if huggingface_process_running():
        raise RuntimeError(
            "Hugging Face/Transformers/Diffusers 相关进程正在运行或进程状态无法确认; 拒绝修改 Hub cache"
        )


def _require_json_rows(payload: object | None, label: str) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        raise RuntimeError(f"{label} JSON 不是 array")
    rows: list[dict[str, Any]] = []
    for index, item in enumerate(payload):
        if not isinstance(item, dict) or not all(isinstance(key, str) for key in item):
            raise RuntimeError(f"{label} JSON row {index} 不是 string-key object")
        rows.append(dict(item))
    return rows


def _require_json_mapping(payload: object | None, label: str) -> dict[str, Any]:
    if not isinstance(payload, dict) or not all(isinstance(key, str) for key in payload):
        raise RuntimeError(f"{label} JSON 不是 string-key object")
    return dict(payload)


def _require_text(
    mapping: Mapping[str, Any],
    key: str,
    *,
    allow_empty: bool = False,
) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise RuntimeError(f"hf JSON 字段 {key!r} 不是有效字符串")
    return value


def _require_text_list(mapping: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = mapping.get(key)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise RuntimeError(f"hf JSON 字段 {key!r} 不是 string array")
    return tuple(sorted(set(value)))


def _require_nonnegative_int(mapping: Mapping[str, Any], key: str) -> int:
    value = mapping.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError(f"hf JSON 字段 {key!r} 不是非负整数")
    return value


def _unique_nonempty_lines(values: Sequence[str]) -> tuple[str, ...]:
    lines: list[str] = []
    seen: set[str] = set()
    for value in values:
        for raw in value.splitlines():
            line = raw.strip()
            if not line or line in seen:
                continue
            seen.add(line)
            lines.append(line)
    return tuple(lines)


def _directory_bytes(root: Path) -> int:
    total = 0
    try:
        for directory, subdirs, files in os.walk(root, followlinks=False):
            base = Path(directory)
            kept: list[str] = []
            for name in subdirs:
                child = base / name
                try:
                    if child.is_symlink() or child.is_junction():
                        continue
                except OSError:
                    continue
                kept.append(name)
            subdirs[:] = kept
            for name in files:
                child = base / name
                try:
                    total += os.stat(child, follow_symlinks=False).st_size
                except OSError:
                    continue
    except OSError:
        return total
    return total


__all__ = [
    "HuggingFaceCacheEntry",
    "HuggingFaceCacheKind",
    "HuggingFaceDeletePreview",
    "HuggingFaceDeleteResult",
    "HuggingFaceHubInventory",
    "HuggingFaceHubPrunePreview",
    "HuggingFaceHubRepo",
    "HuggingFaceHubRevision",
    "HuggingFacePathIdentity",
    "HuggingFacePruneResult",
    "HuggingFaceStorageInventory",
    "execute_huggingface_hub_prune",
    "inventory_huggingface_hub_cache",
    "inventory_huggingface_storage",
    "preview_huggingface_hub_prune",
    "preview_huggingface_repo_removal",
    "preview_huggingface_revision_removal",
    "prune_huggingface_hub_cache",
    "remove_huggingface_repo",
    "remove_huggingface_revision",
]
