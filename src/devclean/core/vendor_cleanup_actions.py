"""Sealed cleanup capabilities backed by vendor-supported maintenance commands.

The normal filesystem cleanup planner intentionally cannot execute cache roots
whose safe mutation contract is a package-manager command.  This module gives
those operations an equally explicit capability boundary: inventory creates an
opaque candidate from an audited provider, and execution dispatches only that
sealed candidate back to the same provider.  There is no raw-delete fallback.
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

from devclean.core import pip_maintenance, uv_maintenance

_SEAL = object()
_CAPABILITY_KEY = secrets.token_bytes(32)


class VendorCleanupRefusal(ValueError):
    """An untrusted or altered vendor-cleanup capability was refused."""


class VendorCleanupKind(StrEnum):
    PIP_CACHE_PURGE = "pip-cache-purge"
    UV_CACHE_PRUNE = "uv-cache-prune"


@dataclass(frozen=True, slots=True)
class VendorCleanupCandidate:
    """Opaque vendor-maintenance capability created by read-only inventory."""

    candidate_id: str
    kind: VendorCleanupKind
    path: Path
    estimated_bytes: int
    label: str
    reason: str
    _integrity: str = field(repr=False, compare=False, default="")
    _seal: object = field(repr=False, compare=False, default=None)

    def __post_init__(self) -> None:
        if self._seal is not _SEAL:
            raise VendorCleanupRefusal(
                "vendor cleanup candidates must come from audited inventory"
            )
        if self.estimated_bytes <= 0:
            raise VendorCleanupRefusal("vendor cleanup candidate must reclaim visible storage")
        expected = _candidate_integrity(
            self.candidate_id,
            self.kind,
            self.path,
            self.estimated_bytes,
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
    def estimated_bytes(self) -> int:
        return sum(candidate.estimated_bytes for candidate in self.candidates)


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
    """Return worthwhile deterministic vendor actions without mutating storage.

    Provider discovery is isolated so a broken optional tool cannot make the
    whole DevClean scan fail.  Small caches remain visible to their provider's
    own inventory API but are deliberately not promoted into the normal cleanup
    surface: the vendor command would create churn for little reclaim value.
    """

    candidates: list[VendorCleanupCandidate] = []
    warnings: list[str] = []

    try:
        pip_inventory = pip_maintenance.inventory_pip_storage(environment)
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        warnings.append(f"pip inventory: {error}")
    else:
        for entry in pip_inventory.caches:
            if not entry.exists or not entry.recommended or entry.logical_bytes <= 0:
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
            if not entry.exists or not entry.recommended or entry.logical_bytes <= 0:
                continue
            candidates.append(
                _candidate(
                    VendorCleanupKind.UV_CACHE_PRUNE,
                    entry.path,
                    entry.logical_bytes,
                    "uv 缓存",
                    "由 uv cache prune 清理；执行前会再次确认同一个 cache 根目录",
                )
            )

    # A provider may discover the same logical root more than once through
    # environment/default aliases. Keep exactly one capability per operation.
    unique: dict[tuple[VendorCleanupKind, str], VendorCleanupCandidate] = {}
    for candidate in candidates:
        key = (candidate.kind, _normalized(candidate.path))
        previous = unique.get(key)
        if previous is None or candidate.estimated_bytes > previous.estimated_bytes:
            unique[key] = candidate
    ordered = tuple(
        sorted(
            unique.values(),
            key=lambda candidate: (-candidate.estimated_bytes, candidate.kind, str(candidate.path)),
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
        pip_result = pip_maintenance.purge_pip_cache(candidate.path, environment)
        return VendorCleanupExecutionResult(
            candidate_id=candidate.candidate_id,
            kind=candidate.kind,
            path=pip_result.cache_path,
            before_bytes=pip_result.before_bytes,
            after_bytes=pip_result.after_bytes,
            command=pip_result.command,
            output=pip_result.output,
        )
    if candidate.kind is VendorCleanupKind.UV_CACHE_PRUNE:
        uv_result = uv_maintenance.prune_uv_cache(candidate.path, environment)
        return VendorCleanupExecutionResult(
            candidate_id=candidate.candidate_id,
            kind=candidate.kind,
            path=uv_result.cache_path,
            before_bytes=uv_result.before_bytes,
            after_bytes=uv_result.after_bytes,
            command=uv_result.command,
            output=uv_result.output,
        )
    raise VendorCleanupRefusal(f"unsupported vendor cleanup kind: {candidate.kind}")


def _candidate(
    kind: VendorCleanupKind,
    path: Path,
    estimated_bytes: int,
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
        estimated_bytes,
        label,
        reason,
    )
    return VendorCleanupCandidate(
        candidate_id=candidate_id,
        kind=kind,
        path=path,
        estimated_bytes=estimated_bytes,
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
        candidate.estimated_bytes,
        candidate.label,
        candidate.reason,
    )
    if not hmac.compare_digest(candidate._integrity, expected):
        raise VendorCleanupRefusal("vendor cleanup candidate was altered")


def _candidate_integrity(
    candidate_id: str,
    kind: VendorCleanupKind,
    path: Path,
    estimated_bytes: int,
    label: str,
    reason: str,
) -> str:
    payload = "\0".join(
        (
            candidate_id,
            kind.value,
            _normalized(path),
            str(estimated_bytes),
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
