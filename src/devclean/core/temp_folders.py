"""Temporary folders cleaned as one item -- a user decision of 2026-11-10.

The user decided that temporary folders are cleaned whole: the Windows and
user temp folders (``%TEMP%``, ``%TMP%``, ``AppData\\Local\\Temp``,
``C:\\Windows\\Temp``) and folders named ``temp``/``tmp`` in an application's
data folder (``AppData\\Local``, ``AppData\\Roaming``, ``AppData\\LocalLow``).
Each becomes one "safe to clean" row whose size comes from the directory
listing; its files are not classified one by one, and none is read.  Cleaning
empties the folder and keeps the folder itself, which Windows and programs
expect to exist.

This relaxes the 2026-11-07 audit finding that a file's age alone cannot prove
a temporary file unused.  What still protects a file in use is the deletion
itself: a file another program holds open cannot be opened for deletion and is
skipped and reported, without failing the rest of the folder.

Nothing that another rule claims is swept up with the folder: a known cleanup
root inside it, a path an audited application rule decides (Cursor's update
recovery installer, NuGet's scratch folder, Claude's temp folder...), a KEEP
rule, DevClean's own data and, at deletion time, anything a running program or
service uses.  Those entries are left to their own classification.

Other folders are not treated this way however many files they hold.  A
``temp``/``tmp`` folder elsewhere (a project, Documents, ProgramData) is
classified file by file as before; ProgramData is a deny-listed system root,
so offering it here would only have the deletion refuse it.
"""

# Chinese UI-facing reason text uses fullwidth punctuation.
# ruff: noqa: RUF001

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from devclean.core._application_cleanup_impl import snapshot_roots
from devclean.core.application_cleanup import (
    application_display_name,
    evaluate_application_path,
    file_time_utc,
)
from devclean.core.cleanup_catalog import KnownCleanupRoot, known_root_for_path
from devclean.core.paths import data_dir
from devclean.core.system_anchor import system_anchor_refusal
from devclean.core.user_rules import KeepClassification, RuleDecision, UserRules
from devclean.scanner.filesystem import ScanRecord, ScanRecordKind

TEMP_FOLDER_NAMES = frozenset({"temp", "tmp"})

# (path, is_directory, size, last_write_ns) -> why the entry stays out, or None.
EntryGuard = Callable[[str, bool, int, int | None], str | None]


class TempFolderKind(StrEnum):
    WINDOWS_OR_USER = "WINDOWS_OR_USER"
    APPLICATION = "APPLICATION"


def _environment(environment: Mapping[str, str] | None) -> dict[str, str]:
    source = os.environ if environment is None else environment
    return {key.casefold(): value for key, value in source.items() if value}


def _normalized(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def _within(path: str, folder: str) -> bool:
    return path == folder or path.startswith(folder.rstrip(os.sep) + os.sep)


@snapshot_roots
def windows_and_user_temp_folders(
    environment: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """%TEMP%, %TMP%, AppData\\Local\\Temp and the Windows Temp folder, normalised."""

    env = _environment(environment)
    candidates = [env.get("temp"), env.get("tmp")]
    local = env.get("localappdata")
    if local:
        candidates.append(os.path.join(local, "Temp"))
    for key in ("systemroot", "windir"):
        windows = env.get(key)
        if windows:
            candidates.append(os.path.join(windows, "Temp"))
    return tuple(
        dict.fromkeys(
            _normalized(candidate)
            for candidate in candidates
            if candidate and os.path.isabs(candidate)
        )
    )


@snapshot_roots
def application_data_folders(environment: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Per-user application data folders whose temp/tmp folders are cleaned whole."""

    env = _environment(environment)
    candidates = [env.get("localappdata"), env.get("appdata")]
    profile = env.get("userprofile")
    if profile:
        candidates.append(os.path.join(profile, "AppData", "LocalLow"))
    return tuple(
        dict.fromkeys(
            _normalized(candidate)
            for candidate in candidates
            if candidate and os.path.isabs(candidate)
        )
    )


@snapshot_roots
def _broad_folders(environment: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Folders a temp folder must never be or contain, whatever %TEMP% says."""

    env = _environment(environment)
    keys = (
        "userprofile",
        "appdata",
        "localappdata",
        "programdata",
        "programfiles",
        "programfiles(x86)",
        "programw6432",
        "systemroot",
        "windir",
    )
    return tuple(
        dict.fromkeys(
            _normalized(env[key]) for key in keys if env.get(key) and os.path.isabs(env[key])
        )
    )


def temp_folder_kind(
    path: Path,
    known_roots: tuple[KnownCleanupRoot, ...],
    keep_config: KeepClassification,
) -> TempFolderKind | None:
    """Whether *path* is a temporary folder that is cleaned as one item."""

    if path.name.casefold() not in TEMP_FOLDER_NAMES:
        return None
    normalized = _normalized(path)
    if os.path.dirname(normalized) == normalized:
        return None
    if any(_within(folder, normalized) for folder in _broad_folders()):
        return None
    if system_anchor_refusal(path, known_roots, keep_config) is not None:
        return None
    windows_and_user = windows_and_user_temp_folders()
    if normalized in windows_and_user:
        return TempFolderKind.WINDOWS_OR_USER
    if any(_within(normalized, folder) for folder in windows_and_user):
        return None  # part of an enclosing temp folder's item
    if not any(_within(normalized, folder) for folder in application_data_folders()):
        return None
    if known_root_for_path(path, known_roots) is not None:
        # Audited application storage keeps its own rules.
        return None
    return TempFolderKind.APPLICATION


def temp_entry_guard(
    folder: Path,
    known_roots: tuple[KnownCleanupRoot, ...],
    rules: UserRules | None,
    *,
    now: datetime | None = None,
    in_use: Callable[[str, bool], str | None] | None = None,
) -> EntryGuard:
    """Why an entry inside temp *folder* stays out of that folder's cleanup.

    Returns ``None`` for an entry that belongs to the folder's item.  The scan
    and the deletion ask the same question, the deletion additionally whether
    a running program or service uses the entry (*in_use*).
    """

    folder_key = _normalized(folder)
    own_data = _normalized(data_dir())
    inner_roots = tuple(
        (_normalized(root.path), root.label)
        for root in known_roots
        if _normalized(root.path) != folder_key and _within(_normalized(root.path), folder_key)
    )

    def guard(path: str, is_directory: bool, size: int, last_write_ns: int | None) -> str | None:
        key = _normalized(path)
        if _within(key, own_data):
            return "DevClean 自己的数据"
        for root, label in inner_roots:
            if _within(key, root):
                return f"{label}（由它自己的规则处理）"
        if rules is not None:
            if is_directory:
                kept = (
                    rules.directory_decision_for(path) is RuleDecision.KEEP
                    or rules.is_within_kept_directory(path)
                )
            else:
                kept = rules.decision_for(path) is RuleDecision.KEEP
            if kept:
                return "你设为保留"
        decision = evaluate_application_path(
            path,
            logical_size=size,
            last_used=file_time_utc(last_write_ns),
            now=now,
        )
        if decision is not None:
            return f"{application_display_name(decision.rule.app_id)} 的应用规则管理它"
        if in_use is not None:
            return in_use(path, is_directory)
        return None

    return guard


@dataclass(slots=True)
class TempFolderTotals:
    """What one temp folder holds, as the scan listed it."""

    record: ScanRecord
    kind: TempFolderKind
    guard: EntryGuard
    files: int = 0
    directories: int = 0
    logical_bytes: int = 0
    latest_write_ns: int = 0
    kept_out: int = 0


_OUTSIDE = object()


class TempFolderScan:
    """Fold scan records inside temp folders into one total per folder.

    The scanner yields a directory before its contents, so each record's
    parent says where it belongs: outside any temp folder, inside one, or
    inside an entry that was kept out (and is classified as usual).
    """

    def __init__(
        self,
        known_roots: tuple[KnownCleanupRoot, ...],
        keep_config: KeepClassification,
        rules: UserRules | None,
        *,
        now: datetime | None = None,
    ) -> None:
        self._known_roots = known_roots
        self._keep_config = keep_config
        self._rules = rules
        self._now = now
        self._state: dict[str, TempFolderTotals | None] = {}
        self.folders: list[TempFolderTotals] = []

    def take(self, record: ScanRecord) -> bool:
        """True when *record* became part of a temp folder's total."""

        key = os.path.normcase(os.path.normpath(record.path))
        owner = self._state.get(os.path.dirname(key), _OUTSIDE)
        if owner is _OUTSIDE:
            if record.kind is not ScanRecordKind.DIRECTORY:
                return False
            path = Path(record.path)
            kind = temp_folder_kind(path, self._known_roots, self._keep_config)
            if kind is None:
                return False
            totals = TempFolderTotals(
                record=record,
                kind=kind,
                guard=temp_entry_guard(path, self._known_roots, self._rules, now=self._now),
            )
            self._state[key] = totals
            self.folders.append(totals)
            return True
        if owner is None:
            if record.kind is ScanRecordKind.DIRECTORY:
                self._state[key] = None
            return False
        assert isinstance(owner, TempFolderTotals)
        if record.kind is ScanRecordKind.DIRECTORY:
            if owner.guard(record.path, True, 0, record.last_write_time_ns) is not None:
                self._state[key] = None
                owner.kept_out += 1
                return False
            self._state[key] = owner
            owner.directories += 1
            return True
        if record.kind is ScanRecordKind.FILE:
            if (
                owner.guard(
                    record.path, False, record.logical_size, record.last_write_time_ns
                )
                is not None
            ):
                owner.kept_out += 1
                return False
            owner.files += 1
            owner.logical_bytes += record.logical_size
            if record.last_write_time_ns is not None:
                owner.latest_write_ns = max(owner.latest_write_ns, record.last_write_time_ns)
            return True
        # Links, cloud placeholders and unreadable entries inside the folder are
        # not candidates of their own; the folder's cleanup removes links as links.
        return True


__all__ = [
    "TEMP_FOLDER_NAMES",
    "EntryGuard",
    "TempFolderKind",
    "TempFolderScan",
    "TempFolderTotals",
    "application_data_folders",
    "temp_entry_guard",
    "temp_folder_kind",
    "windows_and_user_temp_folders",
]
