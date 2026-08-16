"""Read-only VS Code storage inventory for user-owned and persistent data."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from devclean.core.vscode_cleanup import vscode_roots


@dataclass(frozen=True, slots=True)
class VSCodeStorageEntry:
    key: str
    label: str
    path: Path
    logical_bytes: int
    exists: bool
    user_data: bool


@dataclass(frozen=True, slots=True)
class VSCodeStorageInventory:
    entries: tuple[VSCodeStorageEntry, ...]

    @property
    def total_bytes(self) -> int:
        return sum(entry.logical_bytes for entry in self.entries)


def inventory_vscode_storage(
    environment: Mapping[str, str] | None = None,
) -> VSCodeStorageInventory:
    roots = vscode_roots(environment)
    entries: list[VSCodeStorageEntry] = []
    for index, root in enumerate(roots.data_roots):
        base = Path(str(root))
        user = base / "User"
        workspace = user / "workspaceStorage"
        entries.extend(
            (
                _directory_entry(
                    f"data-{index}-workspace",
                    "Workspace state / local chat sessions",
                    workspace,
                    user_data=True,
                ),
                _chat_sessions_entry(index, workspace),
                _directory_entry(
                    f"data-{index}-history",
                    "Local file history",
                    user / "History",
                    user_data=True,
                ),
                _directory_entry(
                    f"data-{index}-backups",
                    "Unsaved editor / hot-exit recovery",
                    base / "Backups",
                    user_data=True,
                ),
                _directory_entry(
                    f"data-{index}-global-storage",
                    "Extension/global persistent state",
                    user / "globalStorage",
                    user_data=False,
                ),
            )
        )
    for index, root in enumerate(roots.extension_roots):
        entries.append(
            _directory_entry(
                f"extensions-{index}",
                "Installed extensions",
                Path(str(root)),
                user_data=False,
            )
        )
    return VSCodeStorageInventory(tuple(entries))


def _chat_sessions_entry(index: int, workspace_root: Path) -> VSCodeStorageEntry:
    total = 0
    exists = False
    try:
        for path in workspace_root.rglob("chatSessions"):
            try:
                if not path.is_dir():
                    continue
            except OSError:
                continue
            exists = True
            total += _directory_bytes(path)
    except OSError:
        pass
    return VSCodeStorageEntry(
        key=f"data-{index}-chat-sessions",
        label="Chat session bodies inside workspaceStorage",
        path=workspace_root,
        logical_bytes=total,
        exists=exists,
        user_data=True,
    )


def _directory_entry(
    key: str,
    label: str,
    path: Path,
    *,
    user_data: bool,
) -> VSCodeStorageEntry:
    try:
        exists = path.is_dir()
    except OSError:
        exists = False
    return VSCodeStorageEntry(
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
    "VSCodeStorageEntry",
    "VSCodeStorageInventory",
    "inventory_vscode_storage",
]
