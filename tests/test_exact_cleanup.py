"""Handle-bound mutation primitives: recycle, delete, and delete a tree.

Nothing is staged anywhere. ``RECYCLE`` hands the object to the Windows Recycle
Bin, where the user already knows how to get it back, and the permanent modes
delete the verified handle where it stands. A private holding area would not
free the space the user is trying to reclaim, which is the whole point.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from devclean.platform.windows.exact_cleanup import (
    ExactCleanupError,
    ExactFileSnapshot,
    ExactRootBoundary,
    purge_exact_file,
    recycle_exact_object,
)
from devclean.platform.windows.filesystem import read_file_metadata

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows handle-bound mutations")


def _boundary(root: Path) -> ExactRootBoundary:
    metadata = read_file_metadata(root)
    return ExactRootBoundary(
        path=root,
        volume_serial=metadata.volume_serial,
        file_id=metadata.file_id,
        file_id_kind=metadata.file_id_kind,
    )


def _snapshot(path: Path) -> ExactFileSnapshot:
    metadata = read_file_metadata(path)
    return ExactFileSnapshot(
        logical_size=metadata.logical_size,
        volume_serial=metadata.volume_serial,
        file_id=metadata.file_id,
        file_id_kind=metadata.file_id_kind,
        link_count=metadata.link_count,
        attributes=metadata.attributes,
        reparse_tag=metadata.reparse_tag,
        creation_time_ns=metadata.creation_time_ns,
        last_write_time_ns=metadata.last_write_time_ns,
    )


def test_permanent_delete_needs_no_staging_area(tmp_path: Path) -> None:
    target = tmp_path / "cache.bin"
    target.write_bytes(b"payload")

    result = purge_exact_file(target, _snapshot(target), _boundary(tmp_path))

    assert result.source_name_absent
    assert result.destination_path is None
    assert not target.exists()
    # Nothing was created beside it on the way out.
    assert tuple(tmp_path.iterdir()) == ()


def test_a_file_replaced_after_the_snapshot_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "cache.bin"
    target.write_bytes(b"original")
    snapshot = _snapshot(target)
    target.unlink()
    target.write_bytes(b"substituted")

    with pytest.raises(ExactCleanupError):
        purge_exact_file(target, snapshot, _boundary(tmp_path))

    assert target.read_bytes() == b"substituted"


def test_a_target_outside_the_approved_root_is_refused(tmp_path: Path) -> None:
    approved = tmp_path / "approved"
    approved.mkdir()
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"payload")

    with pytest.raises(ExactCleanupError):
        purge_exact_file(outside, _snapshot(outside), _boundary(approved))

    assert outside.exists()


def test_the_approved_root_itself_is_refused(tmp_path: Path) -> None:
    """A boundary bounds its contents; it is never itself the target.

    The file opener deliberately omits backup semantics, so a directory handed to
    it is refused by Windows before any boundary logic runs.  Either refusal is
    acceptable; surviving is not optional.
    """

    with pytest.raises((ExactCleanupError, OSError)):
        purge_exact_file(tmp_path, _snapshot(tmp_path), _boundary(tmp_path))

    assert tmp_path.is_dir()


@pytest.mark.parametrize("prefix", ("\\\\?\\", "\\\\.\\", "\\\\"))
def test_non_ordinary_paths_are_refused(tmp_path: Path, prefix: str) -> None:
    target = tmp_path / "cache.bin"
    target.write_bytes(b"payload")
    snapshot = _snapshot(target)

    with pytest.raises(ExactCleanupError, match="ordinary absolute local path"):
        purge_exact_file(Path(f"{prefix}{target}"), snapshot, _boundary(tmp_path))

    assert target.exists()


def test_recycling_refuses_a_replaced_file_before_calling_the_shell(
    tmp_path: Path,
) -> None:
    target = tmp_path / "cache.bin"
    target.write_bytes(b"original")
    snapshot = _snapshot(target)
    target.unlink()
    target.write_bytes(b"substituted")

    with pytest.raises(ExactCleanupError):
        recycle_exact_object(target, snapshot, _boundary(tmp_path))

    assert target.read_bytes() == b"substituted"


def test_recycling_refuses_a_target_outside_the_approved_root(tmp_path: Path) -> None:
    approved = tmp_path / "approved"
    approved.mkdir()
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"payload")

    with pytest.raises(ExactCleanupError):
        recycle_exact_object(outside, _snapshot(outside), _boundary(approved))

    assert outside.exists()
