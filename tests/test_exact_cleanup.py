from __future__ import annotations

import ctypes
from pathlib import Path

import pytest

from devclean.platform.windows import exact_cleanup
from devclean.platform.windows.exact_cleanup import (
    ExactCleanupError,
    ExactFileSnapshot,
    ExactRootBoundary,
    metadata_matches_snapshot,
    quarantine_exact_file,
)
from devclean.platform.windows.filesystem import FileSystemMetadata


def _snapshot() -> ExactFileSnapshot:
    return ExactFileSnapshot(7, 42, "ab" * 16, "file_id_128", 1, 32, None, 100, 200)


def _boundary(root: Path) -> ExactRootBoundary:
    return ExactRootBoundary(root, 42, "10" * 16, "file_id_128")


def _metadata(**changes: object) -> FileSystemMetadata:
    values: dict[str, object] = {
        "is_directory": False,
        "logical_size": 7,
        "allocation_size": 4096,
        "volume_serial": 42,
        "file_id": "ab" * 16,
        "file_id_kind": "file_id_128",
        "link_count": 1,
        "attributes": 32,
        "reparse_tag": None,
        "is_reparse_point": False,
        "is_cloud_placeholder": False,
        "creation_time_ns": 100,
        "last_write_time_ns": 200,
    }
    values.update(changes)
    return FileSystemMetadata(**values)  # type: ignore[arg-type]


def test_rename_layout_uses_file_name_offset_not_padded_structure_size() -> None:
    offset = exact_cleanup._FILE_RENAME_INFO_LAYOUT.file_name.offset
    assert offset == 20
    assert ctypes.sizeof(exact_cleanup._FILE_RENAME_INFO_LAYOUT) > offset


def test_disposition_boolean_is_one_byte_and_mutation_omits_write_delete_share() -> None:
    assert ctypes.sizeof(exact_cleanup._FILE_DISPOSITION_INFO) == 1
    assert exact_cleanup._MUTATION_SHARE_MODE & exact_cleanup._FILE_SHARE_WRITE == 0
    assert exact_cleanup._MUTATION_SHARE_MODE & exact_cleanup._FILE_SHARE_DELETE == 0


def test_final_handle_path_escape_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        exact_cleanup, "_final_path", lambda _handle: r"c:\outside\payload.bin"
    )
    with pytest.raises(ExactCleanupError, match="escaped"):
        exact_cleanup._require_handle_in_boundary(
            object(), r"c:\approved", allow_equal=False
        )


@pytest.mark.parametrize(
    "change",
    [
        {"logical_size": 8},
        {"volume_serial": 43},
        {"file_id": "cd" * 16},
        {"file_id_kind": "file_index_64"},
        {"link_count": 2},
        {"attributes": 33},
        {"reparse_tag": 123, "is_reparse_point": True},
        {"is_cloud_placeholder": True},
        {"creation_time_ns": 101},
        {"last_write_time_ns": 201},
        {"is_directory": True},
    ],
)
def test_exact_snapshot_comparison_fails_closed(change: dict[str, object]) -> None:
    assert not metadata_matches_snapshot(_metadata(**change), _snapshot())


def test_exact_snapshot_comparison_accepts_only_full_match() -> None:
    assert metadata_matches_snapshot(_metadata(), _snapshot())
    assert not metadata_matches_snapshot(None, _snapshot())


def test_quarantine_rejects_same_existing_and_cross_volume_paths(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"fixture")
    with pytest.raises(ExactCleanupError, match="must differ"):
        quarantine_exact_file(source, source, _snapshot(), _boundary(tmp_path))
    destination = tmp_path / "destination.bin"
    destination.write_bytes(b"occupied")
    with pytest.raises(ExactCleanupError, match="already exists"):
        quarantine_exact_file(source, destination, _snapshot(), _boundary(tmp_path))
    with pytest.raises(ExactCleanupError, match="source volume"):
        quarantine_exact_file(
            source,
            Path(r"Z:\quarantine\target"),
            _snapshot(),
            _boundary(tmp_path),
        )
