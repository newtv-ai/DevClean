"""Read-only Trae storage inventory for user-owned and persistent data."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from devclean.core.trae_cleanup import trae_roots


@dataclass(frozen=True, slots=True)
class TraeStorageEntry:
    key: str
    label: str
    path: Path
    logical_bytes: int
    exists: bool
    user_data: bool


@dataclass(frozen=True, slots=True)
class TraeStorageInventory:
    entries: tuple[TraeStorageEntry, ...]

    @property
    def total_bytes(self) -> int:
        return sum(entry.logical_bytes for entry in self.entries)


def inventory_trae_storage(
    environment: Mapping[str, str] | None = None,
) -> TraeStorageInventory:
    roots = trae_roots(environment)
    entries: list[TraeStorageEntry] = []
    for index, root in enumerate(roots.data_roots):
        base = Path(str(root))
        user = base / "User"
        entries.extend(
            (
                _directory_entry(
                    f"data-{index}-workspace",
                    "Workspace-local state / possible AI session metadata",
                    user / "workspaceStorage",
                    user_data=True,
                ),
                _directory_entry(
                    f"data-{index}-history",
                    "Local file history",
                    user / "History",
                    user_data=True,
                ),
                _directory_entry(
                    f"data-{index}-global-storage",
                    "Global extension / AI persistent state",
                    user / "globalStorage",
                    user_data=False,
                ),
                _directory_entry(
                    f"data-{index}-backups",
                    "Unsaved editor / recovery data",
                    base / "Backups",
                    user_data=True,
                ),
            )
        )
    for index, root in enumerate(roots.home_roots):
        entries.append(
            _directory_entry(
                f"home-{index}",
                "Trae home configuration / persistent data",
                Path(str(root)),
                user_data=False,
            )
        )
    for index, root in enumerate(roots.extension_roots):
        entries.append(
            _directory_entry(
                f"extensions-{index}",
                "Installed Trae extensions",
                Path(str(root)),
                user_data=False,
            )
        )
    return TraeStorageInventory(tuple(entries))


def _directory_entry(
    key: str,
    label: str,
    path: Path,
    *,
    user_data: bool,
) -> TraeStorageEntry:
    try:
        exists = path.is_dir()
    except OSError:
        exists = False
    return TraeStorageEntry(
        key=key,
        label=label,
        path=path,
        logical_bytes=_directory_bytes(path) if exists else 0,
        exists=exists,
        user_data=user_data,
    )


def _directory_bytes(root: Path) -> int:
    total = 0
    try:
        for directory, _subdirs, files in os.walk(root):
            base = Path(directory)
            for name in files:
                try:
                    total += (base / name).stat().st_size
                except OSError:
                    continue
    except OSError:
        return total
    return total


__all__ = [
    "TraeStorageEntry",
    "TraeStorageInventory",
    "inventory_trae_storage",
]
