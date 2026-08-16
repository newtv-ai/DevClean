"""Read-only Cursor storage inventory for vendor-aware maintenance guidance."""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from devclean.core.cursor_cleanup import cursor_roots


@dataclass(frozen=True, slots=True)
class CursorStorageEntry:
    key: str
    label: str
    path: Path
    logical_bytes: int
    exists: bool
    user_data: bool


@dataclass(frozen=True, slots=True)
class CursorStorageInventory:
    entries: tuple[CursorStorageEntry, ...]

    @property
    def total_bytes(self) -> int:
        return sum(entry.logical_bytes for entry in self.entries)

    def by_key(self, key: str) -> CursorStorageEntry | None:
        return next((entry for entry in self.entries if entry.key == key), None)


def inventory_cursor_storage(
    environment: Mapping[str, str] | None = None,
) -> CursorStorageInventory:
    """Inventory Cursor's high-value persistent stores without mutating them."""

    roots = cursor_roots(environment)
    entries: list[CursorStorageEntry] = []

    if roots.roaming is not None:
        roaming = Path(str(roots.roaming))
        global_storage = roaming / "User" / "globalStorage"
        entries.append(
            _files_entry(
                "chat_db",
                "Live Cursor chat/agent database",
                global_storage,
                ("state.vscdb", "state.vscdb-wal", "state.vscdb-shm"),
                user_data=True,
            )
        )
        entries.append(
            _files_entry(
                "chat_db_backup",
                "Cursor chat database recovery backup",
                global_storage,
                ("state.vscdb.backup",),
                user_data=True,
            )
        )
        entries.append(
            _glob_files_entry(
                "chat_db_recovery",
                "Cursor corrupted/manual database recovery copies",
                global_storage,
                (
                    "state.vscdb.corrupted.*",
                    "state.vscdb.broken*",
                    "state.vscdb.bak*",
                    "state.vscdb.manual-backup*",
                ),
                user_data=True,
            )
        )
        entries.append(
            _directory_entry(
                "workspace_storage",
                "Workspace state / local chat metadata",
                roaming / "User" / "workspaceStorage",
                user_data=True,
            )
        )
        entries.append(
            _directory_entry(
                "local_history",
                "Local file history / undo snapshots",
                roaming / "User" / "History",
                user_data=True,
            )
        )
        entries.append(
            _directory_entry(
                "commit_checkpoints",
                "AI edit checkpoints",
                global_storage / "anysphere.cursor-commits" / "checkpoints",
                user_data=True,
            )
        )
        entries.append(
            _directory_entry(
                "retrieval_checkpoints",
                "Retrieval/edit checkpoints",
                global_storage / "anysphere.cursor-retrieval" / "checkpoints",
                user_data=True,
            )
        )
        entries.append(
            _directory_entry(
                "hot_exit_backups",
                "Unsaved editor / recovery backups",
                roaming / "Backups",
                user_data=False,
            )
        )

    if roots.program_data is not None:
        program_data = Path(str(roots.program_data))
        entries.append(
            _directory_entry(
                "system_workspace_storage",
                "System-install workspace state",
                program_data / "User" / "workspaceStorage",
                user_data=True,
            )
        )

    if roots.home is not None:
        home = Path(str(roots.home))
        entries.append(
            _directory_entry(
                "agent_projects",
                "Local Agent transcripts / project assets",
                home / "projects",
                user_data=True,
            )
        )
        entries.append(
            _directory_entry(
                "cli_chats",
                "Cursor CLI local chats",
                home / "chats",
                user_data=True,
            )
        )
        entries.append(
            _directory_entry(
                "installed_extensions",
                "Installed Cursor extensions",
                home / "extensions",
                user_data=False,
            )
        )

    return CursorStorageInventory(tuple(entries))


def _files_entry(
    key: str,
    label: str,
    root: Path,
    names: Iterable[str],
    *,
    user_data: bool,
) -> CursorStorageEntry:
    total = 0
    exists = False
    for name in names:
        path = root / name
        try:
            stat = path.stat()
        except OSError:
            continue
        if path.is_file():
            exists = True
            total += stat.st_size
    return CursorStorageEntry(key, label, root, total, exists, user_data)


def _glob_files_entry(
    key: str,
    label: str,
    root: Path,
    patterns: Iterable[str],
    *,
    user_data: bool,
) -> CursorStorageEntry:
    total = 0
    exists = False
    seen: set[Path] = set()
    for pattern in patterns:
        try:
            matches = root.glob(pattern)
        except OSError:
            continue
        for path in matches:
            if path in seen:
                continue
            seen.add(path)
            try:
                stat = path.stat()
            except OSError:
                continue
            if path.is_file():
                exists = True
                total += stat.st_size
    return CursorStorageEntry(key, label, root, total, exists, user_data)


def _directory_entry(
    key: str,
    label: str,
    path: Path,
    *,
    user_data: bool,
) -> CursorStorageEntry:
    try:
        exists = path.is_dir()
    except OSError:
        exists = False
    return CursorStorageEntry(
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
    "CursorStorageEntry",
    "CursorStorageInventory",
    "inventory_cursor_storage",
]
