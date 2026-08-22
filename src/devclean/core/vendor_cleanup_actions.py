"""Sealed cleanup capabilities backed by vendor-supported maintenance commands.

The normal filesystem cleanup planner intentionally cannot execute cache roots
whose safe mutation contract is a package-manager command. This module gives
those operations an equally explicit capability boundary: inventory creates an
opaque candidate from an audited provider, and execution dispatches only that
sealed candidate back to the same provider. There is no raw-delete fallback.

A size/recommendation threshold is never a safety gate here. Once an audited
provider says a cache class is deterministic, any existing non-empty instance
is a safe-clean candidate. ``observed_bytes`` is only the storage currently
occupied by the provider root; it is not advertised as a reclaim estimate for
partial-GC operations such as ``uv cache prune`` or ``pnpm store prune``.
Actual reclaimed bytes come only from the post-command before/after measurement.
"""

# Chinese user-facing reasons use fullwidth punctuation.
# ruff: noqa: RUF001

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from devclean.core import (
    conda_maintenance,
    go_maintenance,
    nuget_maintenance,
    pip_maintenance,
    pnpm_maintenance,
    uv_maintenance,
)

_SEAL = object()
_CAPABILITY_KEY = secrets.token_bytes(32)


class VendorCleanupRefusal(ValueError):
    """An untrusted or altered vendor-cleanup capability was refused."""


class VendorCleanupKind(StrEnum):
    PIP_CACHE_PURGE = "pip-cache-purge"
    UV_CACHE_PRUNE = "uv-cache-prune"
    PNPM_STORE_PRUNE = "pnpm-store-prune"
    GO_BUILD_CACHE_CLEAN = "go-build-cache-clean"
    NUGET_HTTP_CACHE_CLEAR = "nuget-http-cache-clear"
    NUGET_TEMP_CLEAR = "nuget-temp-clear"
    NUGET_PLUGINS_CACHE_CLEAR = "nuget-plugins-cache-clear"
    CONDA_TARBALL_INDEX_CLEAN = "conda-tarball-index-clean"


@dataclass(frozen=True, slots=True)
class VendorCleanupCandidate:
    """Opaque vendor-maintenance capability created by read-only inventory."""

    candidate_id: str
    kind: VendorCleanupKind
    path: Path
    observed_bytes: int
    label: str
    reason: str
    _integrity: str = field(repr=False, compare=False, default="")
    _seal: object = field(repr=False, compare=False, default=None)

    def __post_init__(self) -> None:
        if self._seal is not _SEAL:
            raise VendorCleanupRefusal(
                "vendor cleanup candidates must come from audited inventory"
            )
        if self.observed_bytes <= 0:
            raise VendorCleanupRefusal("vendor cleanup candidate must reference visible storage")
        expected = _candidate_integrity(
            self.candidate_id,
            self.kind,
            self.path,
            self.observed_bytes,
            self.label,
            self.reason,
        )
        if not hmac.compare_digest(self._integrity, expected):
            raise VendorCleanupRefusal("vendor cleanup candidate was altered")


@dataclass(frozen=True, slots=True)
class VendorCleanupInventory:
    candidates: tuple[VendorCleanupCandidate, ...]
    warnings: tuple[str, ...] = ()

    @property
    def observed_bytes(self) -> int:
        """Storage occupied by candidate roots, not a promised reclaim total."""

        return sum(candidate.observed_bytes for candidate in self.candidates)


@dataclass(frozen=True, slots=True)
class VendorCleanupExecutionResult:
    candidate_id: str
    kind: VendorCleanupKind
    path: Path
    before_bytes: int
    after_bytes: int
    command: tuple[str, ...]
    output: str

    @property
    def reclaimed_bytes(self) -> int:
        return max(0, self.before_bytes - self.after_bytes)


def inventory_vendor_cleanup_candidates(
    environment: Mapping[str, str] | None = None,
) -> VendorCleanupInventory:
    """Return every non-empty deterministic vendor action without mutation.

    Provider discovery is isolated so a broken optional tool cannot make the
    whole DevClean scan fail. USER-review resources exposed by a provider are
    deliberately not promoted here; they remain in the user-decision lane.
    """

    candidates: list[VendorCleanupCandidate] = []
    warnings: list[str] = []

    try:
        pip_inventory = pip_maintenance.inventory_pip_storage(environment)
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        warnings.append(f"pip inventory: {error}")
    else:
        for entry in pip_inventory.caches:
            if not entry.exists or entry.logical_bytes <= 0:
                continue
            candidates.append(
                _candidate(
                    VendorCleanupKind.PIP_CACHE_PURGE,
                    entry.path,
                    entry.logical_bytes,
                    "pip 缓存",
                    "由 pip cache purge 清理；执行前会再次确认同一个 cache 根目录",
                )
            )

    try:
        uv_inventory = uv_maintenance.inventory_uv_storage(environment)
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        warnings.append(f"uv inventory: {error}")
    else:
        for entry in uv_inventory.caches:
            if not entry.exists or entry.logical_bytes <= 0:
                continue
            candidates.append(
                _candidate(
                    VendorCleanupKind.UV_CACHE_PRUNE,
                    entry.path,
                    entry.logical_bytes,
                    "uv 缓存垃圾回收",
                    "由 uv cache prune 只清理未使用条目；显示大小仅是 cache 当前占用",
                )
            )

    try:
        pnpm_inventory = pnpm_maintenance.inventory_pnpm_storage(environment)
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        warnings.append(f"pnpm inventory: {error}")
    else:
        for entry in pnpm_inventory.stores:
            if not entry.exists or entry.logical_bytes <= 0:
                continue
            candidates.append(
                _candidate(
                    VendorCleanupKind.PNPM_STORE_PRUNE,
                    entry.path,
                    entry.logical_bytes,
                    "pnpm store 垃圾回收",
                    "由 pnpm store prune 只删除所有已注册项目都不再引用的包；显示大小仅是 store 当前占用",
                )
            )

    try:
        go_inventory = go_maintenance.inventory_go_storage(environment)
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        warnings.append(f"Go inventory: {error}")
    else:
        for entry in go_inventory.caches:
            if (
                not entry.exists
                or entry.logical_bytes <= 0
                or entry.lane is not go_maintenance.GoMaintenanceLane.DETERMINISTIC_CANDIDATE
                or entry.kind is not go_maintenance.GoCacheKind.BUILD
            ):
                continue
            candidates.append(
                _candidate(
                    VendorCleanupKind.GO_BUILD_CACHE_CLEAN,
                    entry.path,
                    entry.logical_bytes,
                    "Go 构建缓存",
                    "由 go clean -cache 清理可重新编译的构建缓存；module cache 不在此自动清理",
                )
            )

    try:
        nuget_inventory = nuget_maintenance.inventory_nuget_storage(environment)
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        warnings.append(f"NuGet inventory: {error}")
    else:
        nuget_kinds = {
            nuget_maintenance.NuGetLocalKind.HTTP_CACHE: VendorCleanupKind.NUGET_HTTP_CACHE_CLEAR,
            nuget_maintenance.NuGetLocalKind.TEMP: VendorCleanupKind.NUGET_TEMP_CLEAR,
            nuget_maintenance.NuGetLocalKind.PLUGINS_CACHE: VendorCleanupKind.NUGET_PLUGINS_CACHE_CLEAR,
        }
        for entry in nuget_inventory.locals:
            kind = nuget_kinds.get(entry.kind)
            if (
                kind is None
                or not entry.exists
                or entry.logical_bytes <= 0
                or entry.lane
                is not nuget_maintenance.NuGetMaintenanceLane.DETERMINISTIC_CANDIDATE
            ):
                continue
            candidates.append(
                _candidate(
                    kind,
                    entry.path,
                    entry.logical_bytes,
                    _nuget_label(entry.kind),
                    entry.reason,
                )
            )

    try:
        conda_inventory = conda_maintenance.inventory_conda_storage(environment)
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        warnings.append(f"Conda inventory: {error}")
    else:
        for entry in conda_inventory.package_caches:
            if not entry.exists or entry.logical_bytes <= 0:
                continue
            candidates.append(
                _candidate(
                    VendorCleanupKind.CONDA_TARBALL_INDEX_CLEAN,
                    entry.path,
                    entry.logical_bytes,
                    "Conda 下载/索引缓存清理",
                    entry.reason + "；显示大小仅是整个 package cache 当前占用",
                )
            )

    unique: dict[tuple[VendorCleanupKind, str], VendorCleanupCandidate] = {}
    for candidate in candidates:
        key = (candidate.kind, _normalized(candidate.path))
        previous = unique.get(key)
        if previous is None or candidate.observed_bytes > previous.observed_bytes:
            unique[key] = candidate
    ordered = tuple(
        sorted(
            unique.values(),
            key=lambda candidate: (-candidate.observed_bytes, candidate.kind, str(candidate.path)),
        )
    )
    return VendorCleanupInventory(ordered, tuple(warnings))


def execute_vendor_cleanup(
    candidate: VendorCleanupCandidate,
    environment: Mapping[str, str] | None = None,
) -> VendorCleanupExecutionResult:
    """Execute one sealed candidate through its original vendor provider only."""

    _require_candidate(candidate)
    if candidate.kind is VendorCleanupKind.PIP_CACHE_PURGE:
        result = pip_maintenance.purge_pip_cache(candidate.path, environment)
        return _execution_result(
            candidate,
            result.cache_path,
            result.before_bytes,
            result.after_bytes,
            result.command,
            result.output,
        )
    if candidate.kind is VendorCleanupKind.UV_CACHE_PRUNE:
        result = uv_maintenance.prune_uv_cache(candidate.path, environment)
        return _execution_result(
            candidate,
            result.cache_path,
            result.before_bytes,
            result.after_bytes,
            result.command,
            result.output,
        )
    if candidate.kind is VendorCleanupKind.PNPM_STORE_PRUNE:
        result = pnpm_maintenance.prune_pnpm_store(candidate.path, environment)
        return _execution_result(
            candidate,
            result.store_path,
            result.before_bytes,
            result.after_bytes,
            result.command,
            result.output,
        )
    if candidate.kind is VendorCleanupKind.GO_BUILD_CACHE_CLEAN:
        result = go_maintenance.clean_go_cache(
            go_maintenance.GoCacheKind.BUILD,
            candidate.path,
            environment,
        )
        return _execution_result(
            candidate,
            result.path,
            result.before_bytes,
            result.after_bytes,
            result.command,
            result.output,
        )
    nuget_kind = _nuget_kind_for_vendor(candidate.kind)
    if nuget_kind is not None:
        result = nuget_maintenance.clear_nuget_local(nuget_kind, candidate.path, environment)
        command = (
            nuget_maintenance.dotnet_executable(environment),
            "nuget",
            "locals",
            nuget_kind.value,
            "--clear",
            "--force-english-output",
        )
        return _execution_result(
            candidate,
            result.path,
            result.before_bytes,
            result.after_bytes,
            command,
            result.stdout,
        )
    if candidate.kind is VendorCleanupKind.CONDA_TARBALL_INDEX_CLEAN:
        result = conda_maintenance.clean_conda_package_cache(candidate.path, environment)
        return _execution_result(
            candidate,
            result.package_cache_path,
            result.before_bytes,
            result.after_bytes,
            result.command,
            result.output,
        )
    raise VendorCleanupRefusal(f"unsupported vendor cleanup kind: {candidate.kind}")


def _execution_result(
    candidate: VendorCleanupCandidate,
    path: Path,
    before_bytes: int,
    after_bytes: int,
    command: tuple[str, ...],
    output: str,
) -> VendorCleanupExecutionResult:
    return VendorCleanupExecutionResult(
        candidate_id=candidate.candidate_id,
        kind=candidate.kind,
        path=path,
        before_bytes=before_bytes,
        after_bytes=after_bytes,
        command=command,
        output=output,
    )


def _nuget_label(kind: nuget_maintenance.NuGetLocalKind) -> str:
    return {
        nuget_maintenance.NuGetLocalKind.HTTP_CACHE: "NuGet HTTP 缓存",
        nuget_maintenance.NuGetLocalKind.TEMP: "NuGet 临时缓存",
        nuget_maintenance.NuGetLocalKind.PLUGINS_CACHE: "NuGet 插件缓存",
        nuget_maintenance.NuGetLocalKind.GLOBAL_PACKAGES: "NuGet global-packages",
    }[kind]


def _nuget_kind_for_vendor(
    kind: VendorCleanupKind,
) -> nuget_maintenance.NuGetLocalKind | None:
    return {
        VendorCleanupKind.NUGET_HTTP_CACHE_CLEAR: nuget_maintenance.NuGetLocalKind.HTTP_CACHE,
        VendorCleanupKind.NUGET_TEMP_CLEAR: nuget_maintenance.NuGetLocalKind.TEMP,
        VendorCleanupKind.NUGET_PLUGINS_CACHE_CLEAR: nuget_maintenance.NuGetLocalKind.PLUGINS_CACHE,
    }.get(kind)


def _candidate(
    kind: VendorCleanupKind,
    path: Path,
    observed_bytes: int,
    label: str,
    reason: str,
) -> VendorCleanupCandidate:
    normalized = _normalized(path)
    candidate_id = "vendor_" + hashlib.sha256(
        f"{kind.value}\0{normalized}".encode("utf-8", "surrogatepass")
    ).hexdigest()[:24]
    integrity = _candidate_integrity(
        candidate_id,
        kind,
        path,
        observed_bytes,
        label,
        reason,
    )
    return VendorCleanupCandidate(
        candidate_id=candidate_id,
        kind=kind,
        path=path,
        observed_bytes=observed_bytes,
        label=label,
        reason=reason,
        _integrity=integrity,
        _seal=_SEAL,
    )


def _require_candidate(candidate: VendorCleanupCandidate) -> None:
    if not isinstance(candidate, VendorCleanupCandidate) or candidate._seal is not _SEAL:
        raise VendorCleanupRefusal("vendor cleanup requires an inventoried candidate")
    expected = _candidate_integrity(
        candidate.candidate_id,
        candidate.kind,
        candidate.path,
        candidate.observed_bytes,
        candidate.label,
        candidate.reason,
    )
    if not hmac.compare_digest(candidate._integrity, expected):
        raise VendorCleanupRefusal("vendor cleanup candidate was altered")


def _candidate_integrity(
    candidate_id: str,
    kind: VendorCleanupKind,
    path: Path,
    observed_bytes: int,
    label: str,
    reason: str,
) -> str:
    payload = "\0".join(
        (
            candidate_id,
            kind.value,
            _normalized(path),
            str(observed_bytes),
            label,
            reason,
        )
    ).encode("utf-8", "surrogatepass")
    return hmac.new(_CAPABILITY_KEY, payload, hashlib.sha256).hexdigest()


def _normalized(path: Path) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


__all__ = [
    "VendorCleanupCandidate",
    "VendorCleanupExecutionResult",
    "VendorCleanupInventory",
    "VendorCleanupKind",
    "VendorCleanupRefusal",
    "execute_vendor_cleanup",
    "inventory_vendor_cleanup_candidates",
]
