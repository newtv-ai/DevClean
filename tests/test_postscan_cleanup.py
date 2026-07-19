from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from devclean.core import postscan_cleanup as cleanup
from devclean.core.cleanup_catalog import (
    CleanupCategory,
    CleanupPolicy,
    KnownCleanupRoot,
    SourceDomain,
)
from devclean.core.cleanup_journal import (
    ActionState,
    BatchState,
    CleanupJournal,
    CleanupJournalError,
    CleanupMode,
)
from devclean.core.postscan_cleanup import CleanupRefusal
from devclean.core.triage import (
    Actionability,
    EvidenceKind,
    ExecutionPolicy,
    RecoveryCapability,
    ReviewLane,
    RiskTier,
    TriageItem,
)
from devclean.platform.windows.exact_cleanup import ExactMutationResult
from devclean.platform.windows.filesystem import FileSystemMetadata
from devclean.platform.windows.known_folders import (
    CanonicalCleanupKind,
    CanonicalCleanupRoot,
)
from devclean.scanner import ScanRecord, ScanRecordKind


def _metadata(
    *,
    directory: bool = False,
    size: int = 7,
    file_id: str = "ab" * 16,
    volume: int = 42,
    links: int = 1,
    attributes: int | None = None,
    reparse: bool = False,
    cloud: bool = False,
    created: int = 100,
    modified: int = 200,
) -> FileSystemMetadata:
    return FileSystemMetadata(
        is_directory=directory,
        logical_size=0 if directory else size,
        allocation_size=0 if directory else 4096,
        volume_serial=volume,
        file_id=file_id,
        file_id_kind="file_id_128",
        link_count=links,
        attributes=(16 if directory else 32) if attributes is None else attributes,
        reparse_tag=123 if reparse else None,
        is_reparse_point=reparse,
        is_cloud_placeholder=cloud,
        creation_time_ns=created,
        last_write_time_ns=modified,
    )


def _item(
    root: Path,
    path: Path,
    *,
    category: CleanupCategory = CleanupCategory.OTHER,
    lane: ReviewLane = ReviewLane.AI_REVIEW,
    risk: RiskTier = RiskTier.MEDIUM,
    evidence: EvidenceKind = EvidenceKind.FILESYSTEM_OBSERVATION,
    actionability: Actionability = Actionability.AI_REVIEW,
    execution_policy: ExecutionPolicy = ExecutionPolicy.RECYCLE_ONLY,
    size: int = 7,
    file_id: str = "ab" * 16,
    links: int = 1,
    kind: ScanRecordKind = ScanRecordKind.FILE,
) -> TriageItem:
    record = ScanRecord(
        root=str(root),
        path=str(path),
        kind=kind,
        depth=1,
        logical_size=size,
        allocated_size=4096,
        raw_allocated_size=4096,
        volume_serial=42,
        file_id=file_id,
        file_id_kind="file_id_128",
        link_count=links,
        attributes=32,
        creation_time_ns=100,
        last_write_time_ns=200,
    )
    return TriageItem(
        record=record,
        path=str(path),
        logical_size=size,
        allocated_size=4096,
        category=category,
        source_domain=SourceDomain.GENERAL_STORAGE,
        lane=lane,
        risk_tier=risk,
        evidence_kind=evidence,
        actionability=actionability,
        execution_policy=execution_policy,
        recovery=RecoveryCapability.UNKNOWN,
        reason="fixture",
    )


class _FileView:
    def __init__(self, root: Path, source: Path) -> None:
        self.root = root
        self.source = source
        self.values: dict[str, FileSystemMetadata] = {
            self.key(root): _metadata(directory=True, file_id="10" * 16),
            self.key(source): _metadata(),
        }

    @staticmethod
    def key(path: str | Path) -> str:
        return str(Path(path).absolute()).casefold()

    def read(self, path: str | Path) -> FileSystemMetadata:
        try:
            return self.values[self.key(path)]
        except KeyError as error:
            raise FileNotFoundError(path) from error

    def move(
        self, source: Path, destination: Path, _snapshot: object, _boundary: object
    ) -> ExactMutationResult:
        metadata = self.values.pop(self.key(source))
        destination.parent.mkdir(parents=True, exist_ok=True)
        source.rename(destination)
        self.values[self.key(destination)] = metadata
        return ExactMutationResult(str(source), str(destination), True, False, True)

    def purge(
        self, source: Path, _snapshot: object, _boundary: object
    ) -> ExactMutationResult:
        self.values.pop(self.key(source))
        source.unlink()
        return ExactMutationResult(str(source), None, True, False, False)

    def recycle(self, destination: Path) -> None:
        self.values.pop(self.key(destination))
        destination.unlink()


@pytest.fixture
def candidate_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[_FileView, TriageItem, cleanup.ScanCleanupCandidate]:
    root = tmp_path / "scan"
    root.mkdir()
    source = root / "cache.bin"
    source.write_bytes(b"fixture")
    view = _FileView(root, source)
    monkeypatch.setattr(cleanup, "read_file_metadata", view.read)
    monkeypatch.setattr(cleanup, "is_local_fixed_path", lambda _path: True)
    monkeypatch.setattr(
        cleanup,
        "prepare_private_quarantine_directory",
        lambda directory, _boundary: directory.mkdir(),
    )
    monkeypatch.setattr(
        cleanup,
        "verify_private_quarantine_directory",
        lambda _directory, _boundary: None,
    )
    item = _item(root, source)
    candidate = cleanup.candidate_from_triage_item(item)
    return view, item, candidate


def test_candidate_is_bound_to_original_scan_root(
    candidate_fixture: tuple[object, object, object],
) -> None:
    _view, _item_value, candidate = candidate_fixture
    assert isinstance(candidate, cleanup.ScanCleanupCandidate)
    assert candidate.scan_root.name == "scan"
    assert candidate.path.parent == candidate.scan_root
    assert not candidate.permanent_eligible


def test_candidate_rejects_non_file_observation(
    candidate_fixture: tuple[_FileView, TriageItem, cleanup.ScanCleanupCandidate]
) -> None:
    _view, item, _candidate = candidate_fixture
    with pytest.raises(CleanupRefusal, match="exact file"):
        cleanup.candidate_from_triage_item(
            replace(item, record=replace(item.record, kind=ScanRecordKind.DIRECTORY))
        )


@pytest.mark.parametrize(
    ("lane", "actionability", "execution_policy"),
    [
        (ReviewLane.PROTECTED, Actionability.PROTECTED, ExecutionPolicy.NONE),
        (ReviewLane.REPORT_ONLY, Actionability.REPORT_ONLY, ExecutionPolicy.NONE),
        (ReviewLane.VENDOR_MANAGED, Actionability.REVIEW_PLAN, ExecutionPolicy.EXACT_VENDOR),
        (ReviewLane.AI_REVIEW, Actionability.AI_REVIEW, ExecutionPolicy.NONE),
    ],
)
def test_candidate_rejects_non_direct_filesystem_execution_policies(
    candidate_fixture: tuple[_FileView, TriageItem, cleanup.ScanCleanupCandidate],
    lane: ReviewLane,
    actionability: Actionability,
    execution_policy: ExecutionPolicy,
) -> None:
    _view, item, _candidate = candidate_fixture
    with pytest.raises(CleanupRefusal):
        cleanup.candidate_from_triage_item(
            replace(
                item,
                lane=lane,
                actionability=actionability,
                execution_policy=execution_policy,
            )
        )


def test_candidate_rejects_path_outside_recorded_scan_root(
    candidate_fixture: tuple[_FileView, TriageItem, cleanup.ScanCleanupCandidate],
    tmp_path: Path,
) -> None:
    _view, item, _candidate = candidate_fixture
    outside = tmp_path / "outside.bin"
    with pytest.raises(CleanupRefusal, match="strictly below"):
        cleanup.candidate_from_triage_item(
            replace(item, path=str(outside), record=replace(item.record, path=str(outside)))
        )


@pytest.mark.parametrize(
    "relative",
    [
        Path(".git") / "objects" / "aa",
        Path(".codex") / "state.json",
        Path(".claude") / "config.json",
        Path("Code") / "globalStorage" / "state.bin",
        Path("Code") / "Local History" / "history.bin",
        Path("Documents") / "notes.tmp",
        Path(".ssh") / "id_rsa",
        Path("safe") / ".env.production",
        Path("safe") / "signing.pfx",
    ],
)
def test_hard_deny_list_blocks_user_assets(
    candidate_fixture: tuple[_FileView, TriageItem, cleanup.ScanCleanupCandidate],
    relative: Path,
) -> None:
    _view, item, _candidate = candidate_fixture
    path = Path(item.record.root) / relative
    with pytest.raises(CleanupRefusal, match="deny-list"):
        cleanup.candidate_from_triage_item(
            replace(item, path=str(path), record=replace(item.record, path=str(path)))
        )


@pytest.mark.parametrize(
    "path",
    [
        Path(r"C:\Windows\Temp\x.tmp"),
        Path(r"C:\Program Files\App\cache.tmp"),
        Path(r"C:\Program Files (x86)\App\cache.tmp"),
        Path(r"C:\ProgramData\App\cache.tmp"),
        Path(r"C:\Recovery\log.tmp"),
        Path(r"C:\Windows.old\x.tmp"),
    ],
)
def test_anchored_system_roots_are_denied(path: Path) -> None:
    with pytest.raises(CleanupRefusal, match="system-root"):
        cleanup._reject_protected_path(path)


def test_candidate_requires_single_link_and_128_bit_identity(
    candidate_fixture: tuple[_FileView, TriageItem, cleanup.ScanCleanupCandidate]
) -> None:
    _view, item, _candidate = candidate_fixture
    with pytest.raises(CleanupRefusal, match=r"Hard-linked|hard-linked"):
        cleanup.candidate_from_triage_item(
            replace(item, record=replace(item.record, link_count=2))
        )
    with pytest.raises(CleanupRefusal, match="128-bit"):
        cleanup.candidate_from_triage_item(
            replace(item, record=replace(item.record, file_id_kind="file_index_64"))
        )


def test_opaque_candidate_cannot_be_upgraded_with_dataclass_replace(
    candidate_fixture: tuple[_FileView, TriageItem, cleanup.ScanCleanupCandidate]
) -> None:
    _view, _item_value, candidate = candidate_fixture
    with pytest.raises(CleanupRefusal, match="altered"):
        replace(candidate, permanent_eligible=True)


def test_permanent_eligibility_requires_exact_low_risk_temp_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "scan"
    temp = root / "Temp"
    temp.mkdir(parents=True)
    source = temp / "old.tmp"
    source.write_bytes(b"fixture")
    view = _FileView(root, source)
    view.values[view.key(temp)] = _metadata(directory=True, file_id="20" * 16)
    monkeypatch.setattr(cleanup, "read_file_metadata", view.read)
    monkeypatch.setattr(cleanup, "is_local_fixed_path", lambda _path: True)
    monkeypatch.setattr(
        cleanup,
        "prepare_private_quarantine_directory",
        lambda directory, _boundary: directory.mkdir(),
    )
    monkeypatch.setattr(
        cleanup,
        "verify_private_quarantine_directory",
        lambda _directory, _boundary: None,
    )
    monkeypatch.setattr(
        cleanup,
        "canonical_permanent_cleanup_roots",
        lambda: (CanonicalCleanupRoot(temp, CanonicalCleanupKind.USER_TEMP),),
    )
    known = KnownCleanupRoot(
        temp, CleanupCategory.USER_TEMP, CleanupPolicy.AGE_BASED_REVIEW, "Temp"
    )
    item = _item(
        root,
        source,
        category=CleanupCategory.USER_TEMP,
        lane=ReviewLane.DETERMINISTIC_CANDIDATE,
        risk=RiskTier.LOW,
        evidence=EvidenceKind.AGE_AND_APPROVED_ROOT,
        actionability=Actionability.REVIEW_PLAN,
        execution_policy=ExecutionPolicy.PERMANENT_APPROVED_CACHE,
    )
    candidate = cleanup.candidate_from_triage_item(item, known_roots=(known,))
    assert candidate.permanent_eligible
    assert candidate.permanent_root == temp
    assert candidate.permanent_root != root


def test_caller_supplied_temp_catalog_never_grants_permanent_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = project / "valuable-source.tmp"
    source.write_bytes(b"fixture")
    view = _FileView(project, source)
    monkeypatch.setattr(cleanup, "read_file_metadata", view.read)
    monkeypatch.setattr(cleanup, "is_local_fixed_path", lambda _path: True)
    monkeypatch.setattr(cleanup, "canonical_permanent_cleanup_roots", lambda: ())
    forged = KnownCleanupRoot(
        project,
        CleanupCategory.USER_TEMP,
        CleanupPolicy.AGE_BASED_REVIEW,
        "caller-forged Temp",
    )
    item = _item(
        project,
        source,
        category=CleanupCategory.USER_TEMP,
        lane=ReviewLane.DETERMINISTIC_CANDIDATE,
        risk=RiskTier.LOW,
        evidence=EvidenceKind.AGE_AND_APPROVED_ROOT,
        actionability=Actionability.REVIEW_PLAN,
        execution_policy=ExecutionPolicy.PERMANENT_APPROVED_CACHE,
    )
    candidate = cleanup.candidate_from_triage_item(item, known_roots=(forged,))
    assert not candidate.permanent_eligible
    batch = cleanup.prepare_cleanup_batch((candidate,))
    with pytest.raises(CleanupRefusal, match="Temp/CrashDumps"):
        cleanup.issue_cleanup_confirmation(batch, mode=CleanupMode.PERMANENT)


def test_permanent_execution_uses_narrow_temp_root_and_reports_reclaim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "scan"
    temp = root / "Temp"
    temp.mkdir(parents=True)
    source = temp / "old.tmp"
    source.write_bytes(b"fixture")
    view = _FileView(root, source)
    view.values[view.key(temp)] = _metadata(directory=True, file_id="20" * 16)
    monkeypatch.setattr(cleanup, "read_file_metadata", view.read)
    monkeypatch.setattr(cleanup, "is_local_fixed_path", lambda _path: True)
    monkeypatch.setattr(
        cleanup,
        "prepare_private_quarantine_directory",
        lambda directory, _boundary: directory.mkdir(),
    )
    monkeypatch.setattr(
        cleanup,
        "verify_private_quarantine_directory",
        lambda _directory, _boundary: None,
    )
    monkeypatch.setattr(
        cleanup,
        "canonical_permanent_cleanup_roots",
        lambda: (CanonicalCleanupRoot(temp, CanonicalCleanupKind.USER_TEMP),),
    )
    known = KnownCleanupRoot(
        temp, CleanupCategory.USER_TEMP, CleanupPolicy.AGE_BASED_REVIEW, "Temp"
    )
    item = _item(
        root,
        source,
        category=CleanupCategory.USER_TEMP,
        lane=ReviewLane.DETERMINISTIC_CANDIDATE,
        risk=RiskTier.LOW,
        evidence=EvidenceKind.AGE_AND_APPROVED_ROOT,
        actionability=Actionability.REVIEW_PLAN,
        execution_policy=ExecutionPolicy.PERMANENT_APPROVED_CACHE,
    )
    candidate = cleanup.candidate_from_triage_item(item, known_roots=(known,))
    batch = cleanup.prepare_cleanup_batch((candidate,))
    challenge = cleanup.issue_cleanup_confirmation(batch, mode=CleanupMode.PERMANENT)
    approval = cleanup.confirm_cleanup_batch(batch, challenge, challenge.phrase)
    journal = CleanupJournal(tmp_path / "state" / "permanent.db")
    result = cleanup.execute_approved_batch(
        batch, approval, journal=journal, mover=view.move, purger=view.purge
    )
    assert result.action_states[0][1] is ActionState.PURGED
    assert result.immediate_reclaim_upper_bound == 7
    action = journal.action(batch.actions[0].action_id)
    assert action.approved_root == str(temp)
    assert action.approved_root_snapshot.file_id == "20" * 16


def test_batch_is_bounded_and_rejects_duplicates(
    candidate_fixture: tuple[_FileView, TriageItem, cleanup.ScanCleanupCandidate],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _view, _item_value, candidate = candidate_fixture
    with pytest.raises(CleanupRefusal, match="between 1"):
        cleanup.prepare_cleanup_batch(())
    with pytest.raises(CleanupRefusal, match="IDs must be unique"):
        cleanup.prepare_cleanup_batch((candidate, candidate))
    monkeypatch.setattr(cleanup, "MAX_CLEANUP_BATCH_BYTES", 1)
    with pytest.raises(CleanupRefusal, match="per-batch"):
        cleanup.prepare_cleanup_batch((candidate,))


def test_confirmation_defaults_to_recycle_and_is_exact(
    candidate_fixture: tuple[_FileView, TriageItem, cleanup.ScanCleanupCandidate]
) -> None:
    _view, _item_value, candidate = candidate_fixture
    batch = cleanup.prepare_cleanup_batch((candidate,))
    challenge = cleanup.issue_cleanup_confirmation(batch)
    assert challenge.mode is CleanupMode.RECYCLE
    assert challenge.phrase.startswith("移入安全隔离区 1 个文件")
    with pytest.raises(CleanupRefusal, match="exactly match"):
        cleanup.confirm_cleanup_batch(batch, challenge, challenge.phrase + " ")
    approval = cleanup.confirm_cleanup_batch(batch, challenge, challenge.phrase)
    assert approval.mode is CleanupMode.RECYCLE


def test_user_plan_splits_large_selection_but_uses_one_exact_confirmation(
    candidate_fixture: tuple[_FileView, TriageItem, cleanup.ScanCleanupCandidate],
) -> None:
    view, item, first = candidate_fixture
    candidates = [first]
    for index in range(cleanup.MAX_CLEANUP_BATCH_FILES):
        path = first.path.with_name(f"plan-{index:02d}.bin")
        path.write_bytes(b"fixture")
        file_id = f"{index + 100:032x}"
        view.values[view.key(path)] = _metadata(file_id=file_id)
        candidate_item = replace(
            item,
            path=str(path),
            record=replace(item.record, path=str(path), file_id=file_id),
        )
        candidates.append(cleanup.candidate_from_triage_item(candidate_item))

    plan = cleanup.prepare_cleanup_plan(candidates)

    assert len(plan.actions) == cleanup.MAX_CLEANUP_BATCH_FILES + 1
    assert tuple(len(batch.actions) for batch in plan.batches) == (32, 1)
    challenge = cleanup.issue_cleanup_plan_confirmation(
        plan,
        mode=CleanupMode.CONFIRMED_PURGE,
    )
    assert f"{len(plan.actions)} 个文件" in challenge.phrase
    approvals = cleanup.confirm_cleanup_plan(plan, challenge, challenge.phrase)
    assert len(approvals) == 2
    assert tuple(approval.batch_digest for approval in approvals) == tuple(
        batch.digest for batch in plan.batches
    )


def test_ai_candidate_cannot_be_upgraded_to_permanent(
    candidate_fixture: tuple[_FileView, TriageItem, cleanup.ScanCleanupCandidate]
) -> None:
    _view, _item_value, candidate = candidate_fixture
    batch = cleanup.prepare_cleanup_batch((candidate,))
    with pytest.raises(CleanupRefusal, match="Temp/CrashDumps"):
        cleanup.issue_cleanup_confirmation(batch, mode=CleanupMode.PERMANENT)


def test_ai_review_candidate_requires_stronger_confirmed_purge_phrase(
    candidate_fixture: tuple[_FileView, TriageItem, cleanup.ScanCleanupCandidate]
) -> None:
    _view, _item_value, candidate = candidate_fixture
    batch = cleanup.prepare_cleanup_batch((candidate,))
    challenge = cleanup.issue_cleanup_confirmation(
        batch, mode=CleanupMode.CONFIRMED_PURGE
    )
    assert challenge.phrase.startswith("不可恢复清除 1 个文件并释放空间 7 字节")
    approval = cleanup.confirm_cleanup_batch(batch, challenge, challenge.phrase)
    assert approval.mode is CleanupMode.CONFIRMED_PURGE


def test_confirmed_purge_quarantines_then_records_second_intent_and_purges(
    candidate_fixture: tuple[_FileView, TriageItem, cleanup.ScanCleanupCandidate],
    tmp_path: Path,
) -> None:
    view, _item_value, candidate = candidate_fixture
    batch = cleanup.prepare_cleanup_batch((candidate,))
    challenge = cleanup.issue_cleanup_confirmation(
        batch, mode=CleanupMode.CONFIRMED_PURGE
    )
    approval = cleanup.confirm_cleanup_batch(batch, challenge, challenge.phrase)
    journal = CleanupJournal(tmp_path / "state" / "confirmed-purge.db")
    result = cleanup.execute_approved_batch(
        batch,
        approval,
        journal=journal,
        mover=view.move,
        purger=view.purge,
    )
    action_id = batch.actions[0].action_id
    assert result.action_states == ((action_id, ActionState.PURGED),)
    assert result.purged_logical_bytes == 7
    assert result.immediate_reclaim_upper_bound == 7
    assert journal.event_states(action_id) == (
        "INTENT_RECORDED",
        "EXECUTING",
        "QUARANTINED",
        "PURGE_PENDING",
        "PURGED",
    )


def test_purge_started_failure_is_unknown_even_if_quarantine_name_remains(
    candidate_fixture: tuple[_FileView, TriageItem, cleanup.ScanCleanupCandidate],
    tmp_path: Path,
) -> None:
    view, _item_value, candidate = candidate_fixture
    batch = cleanup.prepare_cleanup_batch((candidate,))
    challenge = cleanup.issue_cleanup_confirmation(
        batch, mode=CleanupMode.CONFIRMED_PURGE
    )
    approval = cleanup.confirm_cleanup_batch(batch, challenge, challenge.phrase)
    journal = CleanupJournal(tmp_path / "state" / "purge-unknown.db")

    def disposition_maybe_started(
        _source: Path, _snapshot: object, _boundary: object
    ) -> ExactMutationResult:
        raise OSError("disposition result lost")

    result = cleanup.execute_approved_batch(
        batch,
        approval,
        journal=journal,
        mover=view.move,
        purger=disposition_maybe_started,
    )
    action_id = batch.actions[0].action_id
    assert result.action_states == ((action_id, ActionState.UNKNOWN),)
    assert result.purged_logical_bytes == 0
    assert Path(journal.action(action_id).quarantine_path or "").exists()
    cleanup.reconcile_unfinished_actions(journal)
    assert journal.action(action_id).state is ActionState.UNKNOWN


def test_purge_intent_log_failure_leaves_recoverable_quarantine_and_never_purges(
    candidate_fixture: tuple[_FileView, TriageItem, cleanup.ScanCleanupCandidate],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    view, _item_value, candidate = candidate_fixture
    batch = cleanup.prepare_cleanup_batch((candidate,))
    challenge = cleanup.issue_cleanup_confirmation(
        batch, mode=CleanupMode.CONFIRMED_PURGE
    )
    approval = cleanup.confirm_cleanup_batch(batch, challenge, challenge.phrase)
    journal = CleanupJournal(tmp_path / "state" / "intent-log-failure.db")
    original_transition = journal.transition
    purge_called = False

    def transition(*args: object, **kwargs: object) -> None:
        if kwargs.get("new_state") is ActionState.PURGE_PENDING:
            raise CleanupJournalError("injected durable-log failure")
        original_transition(*args, **kwargs)  # type: ignore[arg-type]

    def must_not_purge(*_args: object) -> ExactMutationResult:
        nonlocal purge_called
        purge_called = True
        raise AssertionError("purger must not run without durable PURGE_PENDING")

    monkeypatch.setattr(journal, "transition", transition)
    result = cleanup.execute_approved_batch(
        batch,
        approval,
        journal=journal,
        mover=view.move,
        purger=must_not_purge,
    )
    action_id = batch.actions[0].action_id
    assert not purge_called
    assert result.action_states == ((action_id, ActionState.QUARANTINED),)
    assert Path(journal.action(action_id).quarantine_path or "").exists()


def test_disposition_accepted_then_result_lost_is_unknown_not_claimed_purged(
    candidate_fixture: tuple[_FileView, TriageItem, cleanup.ScanCleanupCandidate],
    tmp_path: Path,
) -> None:
    view, _item_value, candidate = candidate_fixture
    batch = cleanup.prepare_cleanup_batch((candidate,))
    challenge = cleanup.issue_cleanup_confirmation(
        batch, mode=CleanupMode.CONFIRMED_PURGE
    )
    approval = cleanup.confirm_cleanup_batch(batch, challenge, challenge.phrase)
    journal = CleanupJournal(tmp_path / "state" / "accepted-result-lost.db")

    def accepted_then_lost(
        source: Path, _snapshot: object, _boundary: object
    ) -> ExactMutationResult:
        view.values.pop(view.key(source))
        source.unlink()
        raise OSError("result channel lost after disposition acceptance")

    result = cleanup.execute_approved_batch(
        batch,
        approval,
        journal=journal,
        mover=view.move,
        purger=accepted_then_lost,
    )
    action_id = batch.actions[0].action_id
    assert result.action_states == ((action_id, ActionState.UNKNOWN),)
    assert result.purged_logical_bytes == 0
    assert result.immediate_reclaim_upper_bound == 0
    assert cleanup.reconcile_unfinished_actions(journal) == (
        (action_id, ActionState.UNKNOWN),
    )


def test_partial_batch_reports_only_actions_durably_verified_purged(
    candidate_fixture: tuple[_FileView, TriageItem, cleanup.ScanCleanupCandidate],
    tmp_path: Path,
) -> None:
    view, item, first = candidate_fixture
    second_path = first.path.with_name("second-purge.bin")
    second_path.write_bytes(b"fixture")
    view.values[view.key(second_path)] = _metadata(file_id="ef" * 16)
    second_item = replace(
        item,
        path=str(second_path),
        record=replace(item.record, path=str(second_path), file_id="ef" * 16),
    )
    second = cleanup.candidate_from_triage_item(second_item)
    batch = cleanup.prepare_cleanup_batch((first, second))
    challenge = cleanup.issue_cleanup_confirmation(
        batch, mode=CleanupMode.CONFIRMED_PURGE
    )
    approval = cleanup.confirm_cleanup_batch(batch, challenge, challenge.phrase)
    journal = CleanupJournal(tmp_path / "state" / "partial-purge.db")
    purge_calls = 0

    def partial_purger(
        source: Path, snapshot: object, boundary: object
    ) -> ExactMutationResult:
        nonlocal purge_calls
        purge_calls += 1
        if purge_calls == 2:
            raise OSError("second disposition result unknown")
        return view.purge(source, snapshot, boundary)

    result = cleanup.execute_approved_batch(
        batch,
        approval,
        journal=journal,
        mover=view.move,
        purger=partial_purger,
    )
    assert result.action_states == (
        (batch.actions[0].action_id, ActionState.PURGED),
        (batch.actions[1].action_id, ActionState.UNKNOWN),
    )
    assert result.selected_logical_bytes == 14
    assert result.purged_logical_bytes == 7
    assert result.immediate_reclaim_upper_bound == 7


def test_expired_confirmation_is_refused(
    candidate_fixture: tuple[_FileView, TriageItem, cleanup.ScanCleanupCandidate]
) -> None:
    _view, _item_value, candidate = candidate_fixture
    batch = cleanup.prepare_cleanup_batch((candidate,))
    challenge = cleanup.issue_cleanup_confirmation(batch)
    expired = replace(challenge, expires_at=datetime.now(UTC) - timedelta(seconds=1))
    with pytest.raises(CleanupRefusal, match="expired"):
        cleanup.confirm_cleanup_batch(batch, expired, expired.phrase)


def test_recoverable_execution_stages_privately_and_records_zero_reclaim(
    candidate_fixture: tuple[_FileView, TriageItem, cleanup.ScanCleanupCandidate],
    tmp_path: Path,
) -> None:
    view, _item_value, candidate = candidate_fixture
    batch = cleanup.prepare_cleanup_batch((candidate,))
    challenge = cleanup.issue_cleanup_confirmation(batch)
    approval = cleanup.confirm_cleanup_batch(batch, challenge, challenge.phrase)
    journal = CleanupJournal(tmp_path / "state" / "journal.db")
    result = cleanup.execute_approved_batch(
        batch,
        approval,
        journal=journal,
        mover=view.move,
    )
    assert result.batch_state is BatchState.NEEDS_REVIEW
    assert result.action_states[0][1] is ActionState.QUARANTINED
    assert result.immediate_reclaim_upper_bound == 0
    assert journal.event_states(batch.actions[0].action_id) == (
        "INTENT_RECORDED",
        "EXECUTING",
        "QUARANTINED",
    )


def test_quarantine_only_execution_remains_recoverable(
    candidate_fixture: tuple[_FileView, TriageItem, cleanup.ScanCleanupCandidate],
    tmp_path: Path,
) -> None:
    view, _item_value, candidate = candidate_fixture
    batch = cleanup.prepare_cleanup_batch((candidate,))
    challenge = cleanup.issue_cleanup_confirmation(batch)
    approval = cleanup.confirm_cleanup_batch(batch, challenge, challenge.phrase)
    journal = CleanupJournal(tmp_path / "state" / "journal.db")
    result = cleanup.execute_approved_batch(
        batch,
        approval,
        journal=journal,
        recycle_after_quarantine=False,
        mover=view.move,
    )
    assert result.action_states[0][1] is ActionState.QUARANTINED
    action = journal.action(batch.actions[0].action_id)
    assert action.quarantine_path is not None
    assert Path(action.quarantine_path).exists()


def test_automatic_recycle_forwarding_is_refused_before_intent(
    candidate_fixture: tuple[_FileView, TriageItem, cleanup.ScanCleanupCandidate],
    tmp_path: Path,
) -> None:
    view, _item_value, candidate = candidate_fixture
    batch = cleanup.prepare_cleanup_batch((candidate,))
    challenge = cleanup.issue_cleanup_confirmation(batch)
    approval = cleanup.confirm_cleanup_batch(batch, challenge, challenge.phrase)
    journal = CleanupJournal(tmp_path / "state" / "journal.db")

    with pytest.raises(CleanupRefusal, match="forced-recycle"):
        cleanup.execute_approved_batch(
            batch,
            approval,
            journal=journal,
            recycle_after_quarantine=True,
            mover=view.move,
        )
    with pytest.raises(CleanupJournalError):
        journal.batch(batch.batch_id)


def test_change_between_batch_preflight_and_action_execution_never_mutates(
    candidate_fixture: tuple[_FileView, TriageItem, cleanup.ScanCleanupCandidate],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    view, _item_value, candidate = candidate_fixture
    batch = cleanup.prepare_cleanup_batch((candidate,))
    challenge = cleanup.issue_cleanup_confirmation(batch)
    approval = cleanup.confirm_cleanup_batch(batch, challenge, challenge.phrase)
    source_calls = 0

    def racing_read(path: str | Path) -> FileSystemMetadata:
        nonlocal source_calls
        if view.key(path) == view.key(candidate.path):
            source_calls += 1
            if source_calls >= 2:
                return _metadata(size=8, file_id="cd" * 16)
        return view.read(path)

    monkeypatch.setattr(cleanup, "read_file_metadata", racing_read)
    moved = False

    def must_not_move(*_args: object) -> ExactMutationResult:
        nonlocal moved
        moved = True
        raise AssertionError("mover should not be reached")

    journal = CleanupJournal(tmp_path / "state" / "race.db")
    result = cleanup.execute_approved_batch(
        batch, approval, journal=journal, mover=must_not_move
    )
    assert not moved
    assert result.action_states[0][1] is ActionState.UNKNOWN
    assert result.batch_state is BatchState.NEEDS_REVIEW


def test_first_failure_stops_batch_and_later_intent_is_not_replayed(
    candidate_fixture: tuple[_FileView, TriageItem, cleanup.ScanCleanupCandidate],
    tmp_path: Path,
) -> None:
    view, item, first = candidate_fixture
    second_path = first.path.with_name("second.bin")
    second_path.write_bytes(b"fixture")
    view.values[view.key(second_path)] = _metadata(file_id="cd" * 16)
    second_item = replace(
        item,
        path=str(second_path),
        record=replace(item.record, path=str(second_path), file_id="cd" * 16),
    )
    second = cleanup.candidate_from_triage_item(second_item)
    batch = cleanup.prepare_cleanup_batch((first, second))
    challenge = cleanup.issue_cleanup_confirmation(batch)
    approval = cleanup.confirm_cleanup_batch(batch, challenge, challenge.phrase)
    calls = 0

    def fail(
        _source: Path, _destination: Path, _snapshot: object, _boundary: object
    ) -> ExactMutationResult:
        nonlocal calls
        calls += 1
        raise RuntimeError("controlled mover failure")

    journal = CleanupJournal(tmp_path / "state" / "stop.db")
    result = cleanup.execute_approved_batch(batch, approval, journal=journal, mover=fail)
    assert calls == 1
    assert result.action_states == (
        (batch.actions[0].action_id, ActionState.FAILED_UNCHANGED),
        (batch.actions[1].action_id, ActionState.FAILED_UNCHANGED),
    )
    assert result.batch_state is BatchState.COMPLETED
    reopened = CleanupJournal(journal.path)
    assert reopened.unresolved_actions() == ()
    assert reopened.event_states(batch.actions[1].action_id) == (
        "INTENT_RECORDED",
        "FAILED_UNCHANGED",
    )


def test_ambiguous_mover_failure_is_unknown_and_never_auto_retried(
    candidate_fixture: tuple[_FileView, TriageItem, cleanup.ScanCleanupCandidate],
    tmp_path: Path,
) -> None:
    view, _item, candidate = candidate_fixture
    batch = cleanup.prepare_cleanup_batch((candidate,))
    challenge = cleanup.issue_cleanup_confirmation(batch)
    approval = cleanup.confirm_cleanup_batch(batch, challenge, challenge.phrase)
    calls = 0

    def ambiguous(
        source: Path, _destination: Path, _snapshot: object, _boundary: object
    ) -> ExactMutationResult:
        nonlocal calls
        calls += 1
        view.values.pop(view.key(source))
        source.unlink()
        raise RuntimeError("crash after object disappeared")

    journal = CleanupJournal(tmp_path / "state" / "unknown.db")
    result = cleanup.execute_approved_batch(
        batch, approval, journal=journal, mover=ambiguous
    )
    assert calls == 1
    assert result.action_states[0][1] is ActionState.UNKNOWN
    cleanup.reconcile_unfinished_actions(journal)
    assert calls == 1
    assert journal.action(batch.actions[0].action_id).state is ActionState.UNKNOWN


def test_preflight_change_refuses_before_durable_intent(
    candidate_fixture: tuple[_FileView, TriageItem, cleanup.ScanCleanupCandidate],
    tmp_path: Path,
) -> None:
    view, _item_value, candidate = candidate_fixture
    batch = cleanup.prepare_cleanup_batch((candidate,))
    challenge = cleanup.issue_cleanup_confirmation(batch)
    approval = cleanup.confirm_cleanup_batch(batch, challenge, challenge.phrase)
    view.values[view.key(candidate.path)] = _metadata(size=8)
    journal = CleanupJournal(tmp_path / "state" / "journal.db")
    with pytest.raises(CleanupRefusal, match="changed"):
        cleanup.execute_approved_batch(batch, approval, journal=journal, mover=view.move)
    with pytest.raises(CleanupJournalError):
        journal.batch(batch.batch_id)


def test_restore_verified_quarantine_without_replacement(
    candidate_fixture: tuple[_FileView, TriageItem, cleanup.ScanCleanupCandidate],
    tmp_path: Path,
) -> None:
    view, _item_value, candidate = candidate_fixture
    batch = cleanup.prepare_cleanup_batch((candidate,))
    challenge = cleanup.issue_cleanup_confirmation(batch)
    approval = cleanup.confirm_cleanup_batch(batch, challenge, challenge.phrase)
    journal = CleanupJournal(tmp_path / "state" / "journal.db")
    cleanup.execute_approved_batch(
        batch,
        approval,
        journal=journal,
        recycle_after_quarantine=False,
        mover=view.move,
    )

    def restore(
        source: Path, destination: Path, _snapshot: object, _boundary: object
    ) -> ExactMutationResult:
        metadata = view.values.pop(view.key(source))
        source.rename(destination)
        view.values[view.key(destination)] = metadata
        return ExactMutationResult(str(source), str(destination), True, False, True)

    state = cleanup.restore_quarantined_action(
        journal, batch.actions[0].action_id, restorer=restore
    )
    assert state is ActionState.RESTORED
    assert candidate.path.exists()


def test_restore_result_loss_is_reconciled_as_restored(
    candidate_fixture: tuple[_FileView, TriageItem, cleanup.ScanCleanupCandidate],
    tmp_path: Path,
) -> None:
    view, _item_value, candidate = candidate_fixture
    batch = cleanup.prepare_cleanup_batch((candidate,))
    challenge = cleanup.issue_cleanup_confirmation(batch)
    approval = cleanup.confirm_cleanup_batch(batch, challenge, challenge.phrase)
    journal = CleanupJournal(tmp_path / "state" / "restore-result-lost.db")
    cleanup.execute_approved_batch(batch, approval, journal=journal, mover=view.move)

    def restored_then_lost(
        source: Path, destination: Path, _snapshot: object, _boundary: object
    ) -> ExactMutationResult:
        metadata = view.values.pop(view.key(source))
        source.rename(destination)
        view.values[view.key(destination)] = metadata
        raise OSError("restore result channel lost")

    action_id = batch.actions[0].action_id
    state = cleanup.restore_quarantined_action(
        journal,
        action_id,
        restorer=restored_then_lost,
    )
    assert state is ActionState.RESTORED
    assert journal.event_states(action_id)[-3:] == (
        "RESTORE_INTENT",
        "RESTORING",
        "RESTORED",
    )


def test_startup_reconciliation_distinguishes_completed_restore(
    candidate_fixture: tuple[_FileView, TriageItem, cleanup.ScanCleanupCandidate],
    tmp_path: Path,
) -> None:
    view, _item_value, candidate = candidate_fixture
    batch = cleanup.prepare_cleanup_batch((candidate,))
    challenge = cleanup.issue_cleanup_confirmation(batch)
    approval = cleanup.confirm_cleanup_batch(batch, challenge, challenge.phrase)
    journal = CleanupJournal(tmp_path / "state" / "restore-crash.db")
    cleanup.execute_approved_batch(batch, approval, journal=journal, mover=view.move)
    action_id = batch.actions[0].action_id
    action = journal.action(action_id)
    journal.transition(
        action_id,
        expected=(ActionState.QUARANTINED,),
        new_state=ActionState.RESTORE_INTENT,
    )
    journal.transition(
        action_id,
        expected=(ActionState.RESTORE_INTENT,),
        new_state=ActionState.RESTORING,
    )
    quarantine = Path(action.quarantine_path or "")
    metadata = view.values.pop(view.key(quarantine))
    quarantine.rename(candidate.path)
    view.values[view.key(candidate.path)] = metadata

    assert cleanup.reconcile_unfinished_actions(journal) == (
        (action_id, ActionState.RESTORED),
    )


def test_scan_and_triage_modules_do_not_import_executor() -> None:
    root = Path(__file__).parents[1] / "src" / "devclean"
    for target in (root / "scanner", root / "core" / "triage.py"):
        paths = target.glob("*.py") if target.is_dir() else (target,)
        for path in paths:
            text = path.read_text(encoding="utf-8")
            assert "postscan_cleanup" not in text
            assert "exact_cleanup" not in text
