from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from devclean.core.cleanup_catalog import (
    CleanupCategory,
    CleanupPolicy,
    KnownCleanupRoot,
    SourceDomain,
)
from devclean.core.cleanup_journal import ActionState, CleanupJournal, CleanupMode
from devclean.core.postscan_cleanup import (
    CleanupExecutionProgress,
    ScanCleanupCandidate,
    candidate_from_directory_item,
    candidate_from_triage_item,
    execute_cleanup_batch,
    prepare_cleanup_batch,
)
from devclean.core.triage import (
    Actionability,
    CleanupTargetKind,
    DirectoryScope,
    DirectorySubtreeTotals,
    EvidenceKind,
    ExecutionPolicy,
    RecoveryCapability,
    ReviewLane,
    RiskTier,
    TriageItem,
)
from devclean.core.user_rules import default_rules
from devclean.platform.windows.exact_cleanup import (
    DirectoryPurgeProgress,
    DirectoryPurgeResult,
    ExactMutationResult,
    ExactRootBoundary,
)
from devclean.platform.windows.filesystem import FileSystemMetadata
from devclean.scanner.filesystem import ScanRecord, ScanRecordKind


def _metadata(path: Path, *, directory: bool = False) -> FileSystemMetadata:
    identifier = f"{abs(hash(str(path))):032x}"[-32:]
    return FileSystemMetadata(
        is_directory=directory,
        logical_size=0 if directory else 10,
        allocation_size=0 if directory else 10,
        volume_serial=9,
        file_id=identifier,
        file_id_kind="file_id_128",
        link_count=1,
        attributes=0x10 if directory else 0,
        reparse_tag=None,
        is_reparse_point=False,
        is_cloud_placeholder=False,
        creation_time_ns=100,
        last_write_time_ns=200,
    )


def _item(path: Path, root: Path) -> TriageItem:
    metadata = _metadata(path)
    record = ScanRecord(
        root=str(root),
        path=str(path),
        kind=ScanRecordKind.FILE,
        depth=1,
        logical_size=metadata.logical_size,
        allocated_size=metadata.allocation_size,
        raw_allocated_size=metadata.allocation_size,
        volume_serial=metadata.volume_serial,
        file_id=metadata.file_id,
        file_id_kind=metadata.file_id_kind,
        link_count=metadata.link_count,
        attributes=metadata.attributes,
        creation_time_ns=metadata.creation_time_ns,
        last_write_time_ns=metadata.last_write_time_ns,
    )
    return TriageItem(
        record=record,
        path=str(path),
        logical_size=10,
        allocated_size=10,
        category=CleanupCategory.OTHER,
        source_domain=SourceDomain.GENERAL_STORAGE,
        lane=ReviewLane.AI_REVIEW,
        risk_tier=RiskTier.HIGH,
        evidence_kind=EvidenceKind.FILESYSTEM_OBSERVATION,
        actionability=Actionability.AI_REVIEW,
        execution_policy=ExecutionPolicy.USER_CHOICE_DELETE,
        recovery=RecoveryCapability.UNKNOWN,
        reason="user-approved test item",
    )


def _journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> CleanupJournal:
    import devclean.core.cleanup_journal as journal_module

    monkeypatch.setattr(journal_module, "is_local_fixed_path", lambda _path: True)
    monkeypatch.setattr(journal_module, "secure_private_directory", lambda _path: None)
    monkeypatch.setattr(journal_module, "secure_private_file", lambda _path: None)
    return CleanupJournal(tmp_path / "cleanup.db")


def _candidates(
    paths: tuple[Path, ...],
    root: Path,
    present: dict[str, bool],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[ScanCleanupCandidate, ...]:
    import devclean.core.postscan_cleanup as cleanup

    def read(path: Path) -> FileSystemMetadata:
        if not present.get(str(path), True):
            raise FileNotFoundError(str(path))
        return _metadata(path, directory=path == root)

    monkeypatch.setattr(cleanup, "is_local_fixed_path", lambda _path: True)
    monkeypatch.setattr(cleanup, "read_file_metadata", read)
    rules = default_rules()
    return tuple(
        candidate_from_triage_item(
            _item(path, root),
            delete_config=rules.delete.classification,
            keep_config=rules.keep.classification,
        )
        for path in paths
    )


def test_one_failed_file_does_not_stop_the_next(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = Path(r"G:\scan")
    paths = (root / "locked.bin", root / "next.bin")
    present = {str(path): True for path in (*paths, root)}
    candidates = _candidates(paths, root, present, monkeypatch)
    journal = _journal(tmp_path, monkeypatch)

    def purge(path: Path, _snapshot: object, _boundary: object) -> ExactMutationResult:
        if path == paths[0]:
            raise PermissionError("locked")
        present[str(path)] = False
        return ExactMutationResult(str(path), None, True, False, False)

    rules = default_rules()
    result = execute_cleanup_batch(
        prepare_cleanup_batch(candidates),
        CleanupMode.PERMANENT,
        journal=journal,
        purger=purge,
        delete_config=rules.delete.classification,
        keep_config=rules.keep.classification,
    )

    assert [state for _action, state in result.action_states] == [
        ActionState.UNKNOWN,
        ActionState.PURGED,
    ]
    assert result.completed_paths == (str(paths[1]),)
    assert present[str(paths[0])] is True
    assert present[str(paths[1])] is False


def test_unverified_recycle_is_completed_without_claiming_freed_space(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = Path(r"G:\scan")
    target = root / "too-large.bin"
    present = {str(root): True, str(target): True}
    (candidate,) = _candidates((target,), root, present, monkeypatch)
    journal = _journal(tmp_path, monkeypatch)

    def recycle(
        path: Path, _snapshot: object, _boundary: object
    ) -> ExactMutationResult:
        present[str(path)] = False
        return ExactMutationResult(str(path), None, True, False, False, recycled=False)

    rules = default_rules()
    result = execute_cleanup_batch(
        prepare_cleanup_batch((candidate,)),
        CleanupMode.RECYCLE,
        journal=journal,
        recycler=recycle,
        delete_config=rules.delete.classification,
        keep_config=rules.keep.classification,
    )

    assert result.action_states[0][1] is ActionState.RECYCLED
    assert result.completed_paths == (str(target),)
    assert result.unverified_recycle_paths == (str(target),)
    assert result.purged_logical_bytes == 0


def test_already_absent_retry_completes_without_calling_purger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = Path(r"G:\scan")
    target = root / "already-gone.bin"
    present = {str(root): True, str(target): True}
    (candidate,) = _candidates((target,), root, present, monkeypatch)
    present[str(target)] = False
    journal = _journal(tmp_path, monkeypatch)
    called = False

    def purge(_path: Path, _snapshot: object, _boundary: object) -> ExactMutationResult:
        nonlocal called
        called = True
        raise AssertionError("purger must not run for an absent retry")

    rules = default_rules()
    result = execute_cleanup_batch(
        prepare_cleanup_batch((candidate,)),
        CleanupMode.PERMANENT,
        journal=journal,
        purger=purge,
        delete_config=rules.delete.classification,
        keep_config=rules.keep.classification,
    )

    assert called is False
    assert result.action_states[0][1] is ActionState.PURGED
    assert result.completed_paths == (str(target),)
    assert result.purged_logical_bytes == 0


def test_windows_old_root_itself_reaches_directory_purger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import devclean.core.postscan_cleanup as cleanup

    target = Path(r"G:\Windows.old")
    boundary_root = Path("G:\\")
    known = KnownCleanupRoot(
        path=target,
        category=CleanupCategory.WINDOWS_UPDATE,
        policy=CleanupPolicy.VENDOR_MANAGED,
        label="Windows 旧系统目录",
        allow_inside_system_anchor=True,
        delete_root_itself=True,
    )
    target_metadata = _metadata(target, directory=True)
    root_metadata = _metadata(boundary_root, directory=True)
    record = ScanRecord(
        root=str(target),
        path=str(target),
        kind=ScanRecordKind.DIRECTORY,
        depth=0,
        volume_serial=target_metadata.volume_serial,
        file_id=target_metadata.file_id,
        file_id_kind=target_metadata.file_id_kind,
        link_count=target_metadata.link_count,
        attributes=target_metadata.attributes,
        creation_time_ns=target_metadata.creation_time_ns,
        last_write_time_ns=target_metadata.last_write_time_ns,
    )
    item = TriageItem(
        record=record,
        path=str(target),
        logical_size=0,
        allocated_size=None,
        category=CleanupCategory.WINDOWS_UPDATE,
        source_domain=SourceDomain.WINDOWS_SYSTEM,
        lane=ReviewLane.AI_REVIEW,
        risk_tier=RiskTier.HIGH,
        evidence_kind=EvidenceKind.KNOWN_ROOT_HEURISTIC,
        actionability=Actionability.AI_REVIEW,
        execution_policy=ExecutionPolicy.USER_CHOICE_DELETE,
        recovery=RecoveryCapability.VENDOR_REDOWNLOAD_BEST_EFFORT,
        reason="obsolete Windows installation",
        tags=("whole_directory", "known_cache_root"),
        target_kind=CleanupTargetKind.DIRECTORY,
        directory_scope=DirectoryScope.KNOWN_CACHE_ROOT,
    )

    def read(path: Path) -> FileSystemMetadata:
        return root_metadata if path == boundary_root else target_metadata

    monkeypatch.setattr(cleanup, "is_local_fixed_path", lambda _path: True)
    monkeypatch.setattr(cleanup, "read_file_metadata", read)
    rules = default_rules()
    candidate = candidate_from_directory_item(
        item,
        DirectorySubtreeTotals(files=2, logical_bytes=20, allocated_bytes=20),
        known_roots=(known,),
        delete_config=rules.delete.classification,
        keep_config=rules.keep.classification,
    )
    assert candidate.scan_root == boundary_root
    seen_boundary: list[Path] = []
    progress_updates: list[CleanupExecutionProgress] = []

    def purge_directory(
        path: Path,
        _snapshot: object,
        boundary: ExactRootBoundary,
        *,
        on_progress: Callable[[DirectoryPurgeProgress], None],
    ) -> DirectoryPurgeResult:
        assert path == target
        seen_boundary.append(boundary.path)
        assert callable(on_progress)
        on_progress(
            DirectoryPurgeProgress(
                files_removed=1,
                links_removed=0,
                directories_removed=0,
                bytes_removed=10,
            )
        )
        return DirectoryPurgeResult(
            root_path=str(path),
            files_removed=2,
            links_removed=0,
            directories_removed=1,
            bytes_removed=20,
            root_absent=True,
            completed=True,
        )

    result = execute_cleanup_batch(
        prepare_cleanup_batch((candidate,)),
        CleanupMode.PERMANENT,
        journal=_journal(tmp_path, monkeypatch),
        directory_purger=purge_directory,
        on_progress=progress_updates.append,
        known_roots=(known,),
        delete_config=rules.delete.classification,
        keep_config=rules.keep.classification,
    )

    assert seen_boundary == [boundary_root]
    assert [update.completed for update in progress_updates] == [
        False,
        False,
        True,
    ]
    assert progress_updates[1].files_processed == 1
    assert progress_updates[1].files_total == 2
    assert result.action_states[0][1] is ActionState.PURGED
    assert result.completed_paths == (str(target),)
    assert result.purged_logical_bytes == 20
