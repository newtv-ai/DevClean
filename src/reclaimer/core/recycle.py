"""Explicit, identity-checked Recycle Bin workflow for scanned local files only."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from reclaimer.core.paths import data_dir
from reclaimer.platform.windows.filesystem import (
    FILE_ATTRIBUTE_REPARSE_POINT,
    FileSystemMetadata,
    read_file_metadata,
)
from reclaimer.platform.windows.volumes import is_local_fixed_path

MAX_RECYCLE_SELECTION = 32
_PROTECTED_SEGMENTS = frozenset(
    {".git", ".codex", ".claude", "globalstorage", "local history"}
)
_PROTECTED_SUFFIXES = frozenset({".key", ".pem", ".pfx", ".p12", ".kdbx"})


class RecycleRefusal(ValueError):
    """A selected scan record is not safe to send to the Recycle Bin."""


@dataclass(frozen=True, slots=True)
class RecycleTarget:
    candidate_id: str
    path: Path
    logical_size: int
    volume_serial: str
    file_id: str
    file_id_kind: str
    link_count: int
    attributes: int | None
    reparse_tag: int | None
    creation_time_ns: int
    last_write_time_ns: int


def targets_from_records(records: Sequence[Mapping[str, Any]]) -> tuple[RecycleTarget, ...]:
    """Create a bounded recycle selection from exact records in one completed scan."""

    if not records or len(records) > MAX_RECYCLE_SELECTION:
        raise RecycleRefusal(f"select between 1 and {MAX_RECYCLE_SELECTION} scanned files")
    targets = tuple(_target_from_record(record) for record in records)
    if len({target.candidate_id for target in targets}) != len(targets):
        raise RecycleRefusal("selected candidate IDs must be unique")
    if len({os.path.normcase(str(target.path)) for target in targets}) != len(targets):
        raise RecycleRefusal("selected scan records must not resolve to the same path")
    return targets


def preflight_targets(targets: Sequence[RecycleTarget]) -> None:
    """Fail before mutation unless every selected file still matches its scan snapshot."""

    if not targets:
        raise RecycleRefusal("at least one scanned file is required")
    for target in targets:
        _validate_target(target)


def recycle_targets(
    targets: Sequence[RecycleTarget], recycler: Callable[[Path], None]
) -> tuple[RecycleTarget, ...]:
    """Revalidate immediately before each individual Recycle Bin operation.

    A batch is intentionally not atomic: the Windows Recycle Bin has no atomic
    multi-file transaction.  The initial preflight avoids known-bad partial
    batches, while the second verification narrows the path-replacement window.
    """

    preflight_targets(targets)
    recycled: list[RecycleTarget] = []
    for target in targets:
        _validate_target(target)
        recycler(target.path)
        if os.path.lexists(target.path):
            raise RecycleRefusal(
                f"Recycle Bin operation did not remove the selected path: {target.path}"
            )
        recycled.append(target)
    return tuple(recycled)


def _target_from_record(record: Mapping[str, Any]) -> RecycleTarget:
    candidate_id = record.get("candidate_id")
    path_value = record.get("path")
    if (
        record.get("adapter_id") != "filesystem"
        or not isinstance(candidate_id, str)
        or not candidate_id
        or not isinstance(path_value, str)
        or not path_value
    ):
        raise RecycleRefusal("only exact filesystem scan records with a local path can be recycled")
    path = Path(path_value)
    if not path.is_absolute():
        raise RecycleRefusal("stored filesystem path is not absolute")
    _reject_protected_path(path)

    logical_size = _size_value(record.get("logical_size"), "logical_size")
    identity = record.get("identity")
    if not isinstance(identity, Mapping):
        raise RecycleRefusal("scan record has no stable file identity")
    volume_serial = _required_text(identity.get("volume_serial"), "volume_serial")
    file_id = _required_text(identity.get("file_id"), "file_id")
    file_id_kind = _required_text(identity.get("file_id_kind"), "file_id_kind")
    if file_id_kind != "file_id_128":
        raise RecycleRefusal("scan record does not have the required 128-bit file identity")
    link_count = _required_integer(identity.get("link_count"), "link_count")
    if link_count != 1:
        raise RecycleRefusal("hard-linked files cannot be recycled through this command")
    creation_time_ns = _required_integer(identity.get("creation_time_ns"), "creation_time_ns")
    last_write_time_ns = _required_integer(
        identity.get("last_write_time_ns"), "last_write_time_ns"
    )
    attributes = _optional_integer(identity.get("attributes"), "attributes")
    reparse_tag = _optional_integer(identity.get("reparse_tag"), "reparse_tag")
    if (attributes or 0) & FILE_ATTRIBUTE_REPARSE_POINT or reparse_tag is not None:
        raise RecycleRefusal("reparse-point scan records cannot be recycled")
    return RecycleTarget(
        candidate_id=candidate_id,
        path=path,
        logical_size=logical_size,
        volume_serial=volume_serial,
        file_id=file_id,
        file_id_kind=file_id_kind,
        link_count=link_count,
        attributes=attributes,
        reparse_tag=reparse_tag,
        creation_time_ns=creation_time_ns,
        last_write_time_ns=last_write_time_ns,
    )


def _validate_target(target: RecycleTarget) -> None:
    _reject_protected_path(target.path)
    if not is_local_fixed_path(target.path.parent):
        raise RecycleRefusal("selected path is not on a local fixed volume with an ordinary parent")
    try:
        metadata = read_file_metadata(target.path)
    except OSError as error:
        raise RecycleRefusal(f"selected file cannot be revalidated: {error}") from error
    _require_same_snapshot(target, metadata)


def _require_same_snapshot(target: RecycleTarget, metadata: FileSystemMetadata) -> None:
    if metadata.is_directory or metadata.is_reparse_point or metadata.is_cloud_placeholder:
        raise RecycleRefusal("selected path is no longer an ordinary local file")
    if metadata.file_id_kind != "file_id_128" or metadata.volume_serial is None:
        raise RecycleRefusal("selected file no longer has a 128-bit identity")
    if metadata.file_id is None or metadata.link_count != 1:
        raise RecycleRefusal("selected file identity or hard-link count changed")
    if metadata.creation_time_ns is None or metadata.last_write_time_ns is None:
        raise RecycleRefusal("selected file no longer exposes required timestamps")
    actual_volume = f"{metadata.volume_serial:016x}"
    if (
        actual_volume != target.volume_serial
        or metadata.file_id != target.file_id
        or metadata.logical_size != target.logical_size
        or metadata.link_count != target.link_count
        or metadata.attributes != target.attributes
        or metadata.reparse_tag != target.reparse_tag
        or metadata.creation_time_ns != target.creation_time_ns
        or metadata.last_write_time_ns != target.last_write_time_ns
    ):
        raise RecycleRefusal("selected file changed since it was scanned; run a new scan")


def _reject_protected_path(path: Path) -> None:
    normalized = os.path.normcase(os.path.abspath(path))
    protected_root = os.path.normcase(os.path.abspath(data_dir()))
    try:
        if os.path.commonpath((normalized, protected_root)) == protected_root:
            raise RecycleRefusal("Reclaimer state and evidence files cannot be recycled")
    except ValueError:
        # Different drive roots cannot be descendants of each other.
        pass
    parts = {part.casefold() for part in path.parts}
    if parts & _PROTECTED_SEGMENTS:
        raise RecycleRefusal("selected path matches a protected user-asset location")
    name = path.name.casefold()
    if name == ".env" or name.startswith(".env.") or path.suffix.casefold() in _PROTECTED_SUFFIXES:
        raise RecycleRefusal("selected path matches a protected credential pattern")


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise RecycleRefusal(f"scan record {field_name} is missing")
    return value


def _required_integer(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RecycleRefusal(f"scan record {field_name} is missing")
    return value


def _optional_integer(value: object, field_name: str) -> int | None:
    if value is None:
        return None
    return _required_integer(value, field_name)


def _size_value(value: object, field_name: str) -> int:
    if not isinstance(value, Mapping):
        raise RecycleRefusal(f"scan record {field_name} is missing")
    return _required_integer(value.get("value"), field_name)


__all__ = [
    "MAX_RECYCLE_SELECTION",
    "RecycleRefusal",
    "RecycleTarget",
    "preflight_targets",
    "recycle_targets",
    "targets_from_records",
]
