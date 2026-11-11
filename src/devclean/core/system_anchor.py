"""The anchored Windows system-root deny-list, shared by classification and deletion.

A path below ``C:\\Windows``, ``C:\\Program Files`` and the other configured
system roots is never deleted, unless a configured cleanup root explicitly
allows its own folder (``C:\\Windows\\Temp``, ``SoftwareDistribution\\Download``...).
Classification asks the same question, so nothing is offered as cleanable that
the deletion step would then refuse.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from devclean.core.cleanup_catalog import KnownCleanupRoot
from devclean.core.user_rules import KeepClassification

SYSTEM_ROOT_REFUSAL = "anchored Windows system-root deny-list blocked the path"
SYSTEM_FILE_REFUSAL = "anchored Windows system-file deny-list blocked the path"


def system_anchor_refusal(
    path: Path,
    known_roots: tuple[KnownCleanupRoot, ...],
    keep_config: KeepClassification,
) -> str | None:
    """Why *path* lies in a protected system location, or ``None``."""

    normalized = _normalized(path)
    refusal: str | None = None
    names = tuple(keep_config.protected_system_root_names)
    for protected in _protected_prefixes(path.anchor, names):
        if normalized == protected or normalized.startswith(protected.rstrip(os.sep) + os.sep):
            refusal = SYSTEM_ROOT_REFUSAL
            break
    if (
        refusal is None
        and path.parent == Path(path.anchor)
        and path.name.casefold() in keep_config.protected_system_file_names
    ):
        refusal = SYSTEM_FILE_REFUSAL
    if refusal is None:
        return None
    if any(
        root.allow_inside_system_anchor and _is_descendant(path, root.path) for root in known_roots
    ):
        return None
    return refusal


@lru_cache(maxsize=64)
def _protected_prefixes(anchor: str, names: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(_normalized(Path(anchor) / name) for name in names)


def _is_descendant(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((_normalized(path), _normalized(root))) == _normalized(root)
    except ValueError:
        return False


def _normalized(path: Path) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


__all__ = ["SYSTEM_FILE_REFUSAL", "SYSTEM_ROOT_REFUSAL", "system_anchor_refusal"]
