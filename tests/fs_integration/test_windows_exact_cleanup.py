from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from devclean.core.cleanup_catalog import (
    CleanupCategory,
    CleanupPolicy,
    KnownCleanupRoot,
)
from devclean.core.cleanup_journal import ActionState, CleanupJournal, CleanupMode
from devclean.core.postscan_cleanup import (
    candidate_from_triage_item,
    confirm_cleanup_batch,
    execute_approved_batch,
    issue_cleanup_confirmation,
    prepare_cleanup_batch,
    restore_quarantined_action,
)
from devclean.core.triage import triage_file
from devclean.platform.windows.exact_cleanup import (
    QUARANTINE_DIRECTORY_NAME,
    ExactCleanupError,
    ExactFileSnapshot,
    ExactRootBoundary,
    prepare_private_quarantine_directory,
    purge_exact_file,
    quarantine_exact_file,
)
from devclean.platform.windows.filesystem import read_file_metadata
from devclean.scanner import ScanOptions, ScanRecordKind, scan_roots

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows exact-handle canary")


def _scan_file(root: Path, path: Path, known: KnownCleanupRoot):
    records = tuple(scan_roots((root,), ScanOptions(include_directories=False)))
    record = next(
        record
        for record in records
        if record.kind is ScanRecordKind.FILE and Path(record.path) == path
    )
    return triage_file(record, known_roots=(known,), now=datetime.now(UTC))


def test_real_handle_quarantine_restore_and_permanent_purge_canary(tmp_path: Path) -> None:
    scan_root = tmp_path / "approved-scan"
    managed_temp = scan_root / "managed-temp"
    managed_temp.mkdir(parents=True)
    known = KnownCleanupRoot(
        managed_temp,
        CleanupCategory.USER_TEMP,
        CleanupPolicy.AGE_BASED_REVIEW,
        "controlled Temp canary",
    )
    old = datetime.now(UTC) - timedelta(days=8)

    recoverable = managed_temp / "recoverable-canary.bin"
    recoverable.write_bytes(b"recoverable exact handle canary")
    os.utime(recoverable, (old.timestamp(), old.timestamp()))
    recoverable_item = _scan_file(scan_root, recoverable, known)
    recoverable_candidate = candidate_from_triage_item(
        recoverable_item, known_roots=(known,)
    )
    recoverable_batch = prepare_cleanup_batch((recoverable_candidate,))
    recoverable_challenge = issue_cleanup_confirmation(recoverable_batch)
    recoverable_approval = confirm_cleanup_batch(
        recoverable_batch, recoverable_challenge, recoverable_challenge.phrase
    )
    journal = CleanupJournal(tmp_path / "journal" / "cleanup.db")
    recoverable_result = execute_approved_batch(
        recoverable_batch,
        recoverable_approval,
        journal=journal,
        recycle_after_quarantine=False,
    )
    recoverable_action_id = recoverable_batch.actions[0].action_id
    assert recoverable_result.action_states == (
        (recoverable_action_id, ActionState.QUARANTINED),
    )
    assert not recoverable.exists()
    assert restore_quarantined_action(journal, recoverable_action_id) is ActionState.RESTORED
    assert recoverable.read_bytes() == b"recoverable exact handle canary"

    permanent = managed_temp / "permanent-canary.bin"
    permanent.write_bytes(b"permanent exact handle canary")
    os.utime(permanent, (old.timestamp(), old.timestamp()))
    permanent_item = _scan_file(scan_root, permanent, known)
    permanent_candidate = candidate_from_triage_item(permanent_item, known_roots=(known,))
    assert permanent_candidate.permanent_eligible
    permanent_batch = prepare_cleanup_batch((permanent_candidate,))
    permanent_challenge = issue_cleanup_confirmation(
        permanent_batch, mode=CleanupMode.PERMANENT
    )
    permanent_approval = confirm_cleanup_batch(
        permanent_batch, permanent_challenge, permanent_challenge.phrase
    )
    permanent_result = execute_approved_batch(
        permanent_batch,
        permanent_approval,
        journal=journal,
    )
    assert permanent_result.action_states == (
        (permanent_batch.actions[0].action_id, ActionState.PURGED),
    )
    assert permanent_result.immediate_reclaim_upper_bound == len(
        b"permanent exact handle canary"
    )
    assert not permanent.exists()

    ai_reviewed = scan_root / "ai-reviewed-canary.bin"
    ai_reviewed.write_bytes(b"explicitly confirmed AI review canary")
    ai_item = _scan_file(scan_root, ai_reviewed, known)
    ai_candidate = candidate_from_triage_item(ai_item, known_roots=(known,))
    assert not ai_candidate.permanent_eligible
    ai_batch = prepare_cleanup_batch((ai_candidate,))
    ai_challenge = issue_cleanup_confirmation(
        ai_batch, mode=CleanupMode.CONFIRMED_PURGE
    )
    ai_approval = confirm_cleanup_batch(ai_batch, ai_challenge, ai_challenge.phrase)
    ai_result = execute_approved_batch(ai_batch, ai_approval, journal=journal)
    assert ai_result.action_states == (
        (ai_batch.actions[0].action_id, ActionState.PURGED),
    )
    assert ai_result.purged_logical_bytes == len(
        b"explicitly confirmed AI review canary"
    )
    assert not ai_reviewed.exists()


def test_conflicting_preopened_delete_handle_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    target = root / "race-canary.bin"
    target.write_bytes(b"race canary")
    target_metadata = read_file_metadata(target)
    root_metadata = read_file_metadata(root)
    snapshot = ExactFileSnapshot(
        target_metadata.logical_size,
        int(target_metadata.volume_serial or 0),
        str(target_metadata.file_id),
        str(target_metadata.file_id_kind),
        int(target_metadata.link_count or 0),
        target_metadata.attributes,
        target_metadata.reparse_tag,
        int(target_metadata.creation_time_ns or 0),
        int(target_metadata.last_write_time_ns or 0),
    )
    boundary = ExactRootBoundary(
        root,
        int(root_metadata.volume_serial or 0),
        str(root_metadata.file_id),
        str(root_metadata.file_id_kind),
    )
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    close = kernel32.CloseHandle
    close.argtypes = (wintypes.HANDLE,)
    close.restype = wintypes.BOOL
    handle = create_file(
        str(target),
        0x00010000,
        0x00000001 | 0x00000002 | 0x00000004,
        None,
        3,
        0x00200000,
        None,
    )
    assert handle != ctypes.c_void_p(-1).value
    try:
        with pytest.raises(OSError):
            purge_exact_file(target, snapshot, boundary)
        assert os.path.lexists(target)
    finally:
        close(handle)
    assert target.read_bytes() == b"race canary"


def test_conflicting_preopened_write_handle_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "approved-write"
    root.mkdir()
    target = root / "writer-race-canary.bin"
    target.write_bytes(b"writer race canary")
    target_metadata = read_file_metadata(target)
    root_metadata = read_file_metadata(root)
    snapshot = ExactFileSnapshot(
        target_metadata.logical_size,
        int(target_metadata.volume_serial or 0),
        str(target_metadata.file_id),
        str(target_metadata.file_id_kind),
        int(target_metadata.link_count or 0),
        target_metadata.attributes,
        target_metadata.reparse_tag,
        int(target_metadata.creation_time_ns or 0),
        int(target_metadata.last_write_time_ns or 0),
    )
    boundary = ExactRootBoundary(
        root,
        int(root_metadata.volume_serial or 0),
        str(root_metadata.file_id),
        str(root_metadata.file_id_kind),
    )
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    close = kernel32.CloseHandle
    close.argtypes = (wintypes.HANDLE,)
    close.restype = wintypes.BOOL
    handle = create_file(
        str(target),
        0x40000000,
        0x00000001 | 0x00000002 | 0x00000004,
        None,
        3,
        0x00200000,
        None,
    )
    assert handle != ctypes.c_void_p(-1).value
    try:
        with pytest.raises(OSError):
            purge_exact_file(target, snapshot, boundary)
    finally:
        close(handle)
    assert target.read_bytes() == b"writer race canary"


def test_preexisting_quarantine_namespace_is_never_taken_over(tmp_path: Path) -> None:
    root = tmp_path / "approved-quarantine"
    root.mkdir()
    root_metadata = read_file_metadata(root)
    boundary = ExactRootBoundary(
        root,
        int(root_metadata.volume_serial or 0),
        str(root_metadata.file_id),
        str(root_metadata.file_id_kind),
    )
    existing = root / f"{QUARANTINE_DIRECTORY_NAME}-batch_preexisting"
    existing.mkdir()

    with pytest.raises(ExactCleanupError, match="refusing takeover"):
        prepare_private_quarantine_directory(existing, boundary)

    assert existing.is_dir()


def test_final_handle_path_blocks_junction_escape_before_disposition(tmp_path: Path) -> None:
    approved = tmp_path / "approved-root"
    outside = tmp_path / "outside-root"
    approved.mkdir()
    outside.mkdir()
    payload = outside / "valuable.bin"
    payload.write_bytes(b"outside payload")
    junction = approved / "swapped-child"
    try:
        os.symlink(outside, junction, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlink unavailable for race canary: {error}")
    target_metadata = read_file_metadata(payload)
    root_metadata = read_file_metadata(approved)
    snapshot = ExactFileSnapshot(
        target_metadata.logical_size,
        int(target_metadata.volume_serial or 0),
        str(target_metadata.file_id),
        str(target_metadata.file_id_kind),
        int(target_metadata.link_count or 0),
        target_metadata.attributes,
        target_metadata.reparse_tag,
        int(target_metadata.creation_time_ns or 0),
        int(target_metadata.last_write_time_ns or 0),
    )
    boundary = ExactRootBoundary(
        approved,
        int(root_metadata.volume_serial or 0),
        str(root_metadata.file_id),
        str(root_metadata.file_id_kind),
    )
    with pytest.raises(
        ExactCleanupError, match=r"final path escaped|ordinary pinned directory"
    ):
        purge_exact_file(junction / payload.name, snapshot, boundary)
    assert payload.read_bytes() == b"outside payload"


def test_destination_parent_final_path_blocks_junction_escape_before_move(
    tmp_path: Path,
) -> None:
    approved = tmp_path / "approved-destination-root"
    outside = tmp_path / "outside-destination-root"
    approved.mkdir()
    outside.mkdir()
    source = approved / "selected.bin"
    source.write_bytes(b"selected payload")
    swapped_parent = approved / "swapped-staging"
    try:
        os.symlink(outside, swapped_parent, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlink unavailable for race canary: {error}")
    source_metadata = read_file_metadata(source)
    root_metadata = read_file_metadata(approved)
    snapshot = ExactFileSnapshot(
        source_metadata.logical_size,
        int(source_metadata.volume_serial or 0),
        str(source_metadata.file_id),
        str(source_metadata.file_id_kind),
        int(source_metadata.link_count or 0),
        source_metadata.attributes,
        source_metadata.reparse_tag,
        int(source_metadata.creation_time_ns or 0),
        int(source_metadata.last_write_time_ns or 0),
    )
    boundary = ExactRootBoundary(
        approved,
        int(root_metadata.volume_serial or 0),
        str(root_metadata.file_id),
        str(root_metadata.file_id_kind),
    )
    with pytest.raises(
        ExactCleanupError, match=r"final path escaped|ordinary pinned directory"
    ):
        quarantine_exact_file(
            source,
            swapped_parent / "quarantined.bin",
            snapshot,
            boundary,
        )
    assert source.read_bytes() == b"selected payload"
    assert not (outside / "quarantined.bin").exists()
