"""Post-scan, explicitly confirmed cleanup orchestration.

There is intentionally no entry point from the scanner or classifier into this
module.  A caller must retain exact ``TriageItem`` objects from a completed
scan, prepare a bounded opaque batch, show the generated confirmation phrase,
and return a matching opaque approval.  AI is optional advice only.  The user
may choose recoverable private quarantine or independently strong-confirm an
exact permanent purge; neither path is reachable from scanning or AI import.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypeAlias

from devclean.core.cleanup_catalog import CleanupCategory, KnownCleanupRoot
from devclean.core.cleanup_journal import (
    ActionState,
    BatchState,
    CleanupIntent,
    CleanupJournal,
    CleanupMode,
)
from devclean.core.paths import data_dir
from devclean.core.triage import (
    Actionability,
    EvidenceKind,
    ExecutionPolicy,
    ReviewLane,
    RiskTier,
    TriageItem,
)
from devclean.platform.windows.exact_cleanup import (
    QUARANTINE_DIRECTORY_NAME,
    ExactFileSnapshot,
    ExactMutationResult,
    ExactRootBoundary,
    metadata_matches_snapshot,
    prepare_private_quarantine_directory,
    purge_exact_file,
    quarantine_exact_file,
    restore_exact_file,
    verify_private_quarantine_directory,
)
from devclean.platform.windows.filesystem import (
    FILE_ATTRIBUTE_REPARSE_POINT,
    FileSystemMetadata,
    read_file_metadata,
)
from devclean.platform.windows.known_folders import (
    CanonicalCleanupKind,
    canonical_permanent_cleanup_roots,
)
from devclean.platform.windows.volumes import is_local_fixed_path
from devclean.scanner.filesystem import ScanRecordKind

MAX_CLEANUP_BATCH_FILES = 32
# AI model and container-cache artifacts commonly exceed 8 GiB.  The safety
# boundary is exact-file identity + a 32-file cap + a typed count/byte-bound
# confirmation, so allow a useful but still bounded 256 GiB batch.
MAX_CLEANUP_BATCH_BYTES = 256 * 1024 * 1024 * 1024
MAX_CLEANUP_PLAN_FILES = 256
MAX_CLEANUP_PLAN_BYTES = 1024 * 1024 * 1024 * 1024
CONFIRMATION_TTL = timedelta(minutes=10)
_MAX_PATH_LENGTH = 32_767
_SEAL = object()
_CAPABILITY_KEY = secrets.token_bytes(32)
_PROTECTED_SEGMENTS = frozenset(
    {
        "$recycle.bin",
        ".aws",
        ".azure",
        ".claude",
        ".codex",
        ".git",
        ".gnupg",
        ".hg",
        ".kube",
        ".ssh",
        ".svn",
        ".vscode",
        "desktop",
        "documents",
        "globalstorage",
        "local history",
        "music",
        "onedrive",
        "pictures",
        "system volume information",
        "videos",
        "workspacestorage",
        QUARANTINE_DIRECTORY_NAME.casefold(),
    }
)
_PROTECTED_SUFFIXES = frozenset(
    {".cer", ".crt", ".der", ".key", ".kdbx", ".p12", ".pem", ".pfx", ".ppk"}
)
_PROTECTED_NAMES = frozenset(
    {
        "credentials",
        "credentials.json",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
        "secrets.json",
    }
)
_PERMANENT_CATEGORIES = frozenset(
    {CleanupCategory.USER_TEMP, CleanupCategory.CRASH_DUMPS}
)

QuarantineMover: TypeAlias = Callable[
    [Path, Path, ExactFileSnapshot, ExactRootBoundary], ExactMutationResult
]
PermanentPurger: TypeAlias = Callable[
    [Path, ExactFileSnapshot, ExactRootBoundary], ExactMutationResult
]
Restorer: TypeAlias = Callable[
    [Path, Path, ExactFileSnapshot, ExactRootBoundary], ExactMutationResult
]


class CleanupRefusal(ValueError):
    """A scan candidate, approval, or execution boundary failed closed."""


@dataclass(frozen=True, slots=True)
class ScanCleanupCandidate:
    """Opaque exact-file capability derived from one completed scan item."""

    candidate_id: str
    path: Path
    scan_root: Path
    scan_root_snapshot: ExactFileSnapshot
    snapshot: ExactFileSnapshot
    category: CleanupCategory
    permanent_eligible: bool
    permanent_root: Path | None
    permanent_root_snapshot: ExactFileSnapshot | None
    _integrity: str = field(repr=False, compare=False)
    _seal: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._seal is not _SEAL:
            raise CleanupRefusal("cleanup candidates must come from a completed scan item")
        expected = _candidate_integrity(
            self.candidate_id,
            self.path,
            self.scan_root,
            self.scan_root_snapshot,
            self.snapshot,
            self.category,
            self.permanent_eligible,
            self.permanent_root,
            self.permanent_root_snapshot,
        )
        if not hmac.compare_digest(self._integrity, expected):
            raise CleanupRefusal("opaque cleanup candidate was altered")


@dataclass(frozen=True, slots=True)
class CleanupAction:
    """Opaque action with a fixed source, original scan root, and snapshot."""

    action_id: str
    candidate: ScanCleanupCandidate
    _seal: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._seal is not _SEAL or self.candidate._seal is not _SEAL:
            raise CleanupRefusal("cleanup actions must be prepared from scan candidates")


@dataclass(frozen=True, slots=True)
class PreparedCleanupBatch:
    """A bounded selection; it is not executable without final approval."""

    batch_id: str
    actions: tuple[CleanupAction, ...]
    digest: str
    created_at: datetime
    _seal: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._seal is not _SEAL:
            raise CleanupRefusal("cleanup batches must be prepared by DevClean")


@dataclass(frozen=True, slots=True)
class PreparedCleanupPlan:
    """One user-visible manifest split into independently journaled batches."""

    plan_id: str
    batches: tuple[PreparedCleanupBatch, ...]
    digest: str
    created_at: datetime
    _seal: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._seal is not _SEAL:
            raise CleanupRefusal("cleanup plans must be prepared by DevClean")

    @property
    def actions(self) -> tuple[CleanupAction, ...]:
        return tuple(action for batch in self.batches for action in batch.actions)


@dataclass(frozen=True, slots=True)
class CleanupConfirmationChallenge:
    batch_digest: str
    mode: CleanupMode
    phrase: str
    expires_at: datetime
    _seal: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._seal is not _SEAL:
            raise CleanupRefusal("confirmation challenge was not issued by DevClean")


@dataclass(frozen=True, slots=True)
class CleanupExecutionApproval:
    batch_digest: str
    mode: CleanupMode
    expires_at: datetime
    approval_nonce: str
    _seal: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._seal is not _SEAL:
            raise CleanupRefusal("cleanup approval was not confirmed by DevClean")


@dataclass(frozen=True, slots=True)
class CleanupExecutionResult:
    batch_id: str
    mode: CleanupMode
    batch_state: BatchState
    action_states: tuple[tuple[str, ActionState], ...]
    selected_logical_bytes: int
    purged_logical_bytes: int
    immediate_reclaim_upper_bound: int


def candidate_from_triage_item(
    item: TriageItem,
    *,
    known_roots: tuple[KnownCleanupRoot, ...] = (),
) -> ScanCleanupCandidate:
    """Bind an exact scan observation to its original, user-chosen scan root.

    Presentation-only and protected items are never candidates.  Ordinary
    AI/manual-review items can enter the recoverable workflow, while permanent
    eligibility is restricted to deterministic low-risk aged Temp/CrashDumps.
    """

    record = item.record
    if record.kind is not ScanRecordKind.FILE or item.path != record.path:
        raise CleanupRefusal("candidate must be an exact file item from the completed scan")
    if item.lane in {ReviewLane.PROTECTED, ReviewLane.REPORT_ONLY} or item.actionability in {
        Actionability.PROTECTED,
        Actionability.REPORT_ONLY,
    }:
        raise CleanupRefusal("protected and report-only items cannot become cleanup actions")
    if item.execution_policy not in {
        ExecutionPolicy.RECYCLE_ONLY,
        ExecutionPolicy.PERMANENT_APPROVED_CACHE,
    }:
        raise CleanupRefusal(
            "this item requires an implemented vendor action or is not executable"
        )
    path = _absolute_local_path(Path(record.path), "candidate")
    scan_root = _absolute_local_path(Path(record.root), "scan root")
    _require_strict_descendant(path, scan_root)
    _reject_protected_path(path)
    if not is_local_fixed_path(path) or not is_local_fixed_path(scan_root):
        raise CleanupRefusal("candidate and original scan root must be on a local fixed volume")
    snapshot = _snapshot_from_record(item)
    root_snapshot = _directory_snapshot(scan_root, "original scan root")
    # ``known_roots`` remains classification context only.  It is deliberately
    # not deletion authority because its environment-derived entries can be
    # redirected.  Permanent provenance comes only from Windows APIs below.
    del known_roots
    canonical_kind = {
        CleanupCategory.USER_TEMP: CanonicalCleanupKind.USER_TEMP,
        CleanupCategory.CRASH_DUMPS: CanonicalCleanupKind.CRASH_DUMPS,
    }.get(item.category)
    canonical = next(
        (
            root
            for root in canonical_permanent_cleanup_roots()
            if root.kind is canonical_kind and _is_descendant(path, root.path)
        ),
        None,
    )
    permanent_eligible = bool(
        canonical is not None
        and item.category in _PERMANENT_CATEGORIES
        and item.lane is ReviewLane.DETERMINISTIC_CANDIDATE
        and item.risk_tier is RiskTier.LOW
        and item.evidence_kind is EvidenceKind.AGE_AND_APPROVED_ROOT
        and item.actionability is Actionability.REVIEW_PLAN
        and item.execution_policy is ExecutionPolicy.PERMANENT_APPROVED_CACHE
    )
    permanent_root = canonical.path if permanent_eligible and canonical is not None else None
    permanent_root_snapshot = None
    if permanent_root is not None:
        permanent_root = _absolute_local_path(permanent_root, "approved permanent root")
        _require_strict_descendant(path, permanent_root)
        permanent_root_snapshot = _directory_snapshot(
            permanent_root, "approved permanent root"
        )
    candidate_id = "candidate_" + secrets.token_hex(16)
    integrity = _candidate_integrity(
        candidate_id,
        path,
        scan_root,
        root_snapshot,
        snapshot,
        item.category,
        permanent_eligible,
        permanent_root,
        permanent_root_snapshot,
    )
    return ScanCleanupCandidate(
        candidate_id=candidate_id,
        path=path,
        scan_root=scan_root,
        scan_root_snapshot=root_snapshot,
        snapshot=snapshot,
        category=item.category,
        permanent_eligible=permanent_eligible,
        permanent_root=permanent_root,
        permanent_root_snapshot=permanent_root_snapshot,
        _integrity=integrity,
        _seal=_SEAL,
    )


def prepare_cleanup_batch(
    candidates: Sequence[ScanCleanupCandidate],
) -> PreparedCleanupBatch:
    """Prepare a bounded, default-recoverable batch after scanning has ended."""

    if not candidates or len(candidates) > MAX_CLEANUP_BATCH_FILES:
        raise CleanupRefusal(
            f"select between 1 and {MAX_CLEANUP_BATCH_FILES} completed-scan candidates"
        )
    for candidate in candidates:
        if not isinstance(candidate, ScanCleanupCandidate) or candidate._seal is not _SEAL:
            raise CleanupRefusal("batch accepts opaque scan candidates only")
    if len({item.candidate_id for item in candidates}) != len(candidates):
        raise CleanupRefusal("candidate IDs must be unique")
    normalized_paths = {_normalized(candidate.path) for candidate in candidates}
    if len(normalized_paths) != len(candidates):
        raise CleanupRefusal("selected candidates must not share a source path")
    selected_bytes = sum(candidate.snapshot.logical_size for candidate in candidates)
    if selected_bytes > MAX_CLEANUP_BATCH_BYTES:
        raise CleanupRefusal("selected logical bytes exceed the per-batch safety limit")
    actions = tuple(
        CleanupAction(
            action_id="action_" + secrets.token_hex(16),
            candidate=candidate,
            _seal=_SEAL,
        )
        for candidate in candidates
    )
    batch_id = "batch_" + secrets.token_hex(16)
    created_at = datetime.now(UTC)
    digest = _batch_digest(batch_id, actions)
    return PreparedCleanupBatch(batch_id, actions, digest, created_at, _SEAL)


def prepare_cleanup_plan(
    candidates: Sequence[ScanCleanupCandidate],
) -> PreparedCleanupPlan:
    """Prepare one bounded manifest while retaining 32-action journal batches."""

    selected = tuple(candidates)
    if not selected or len(selected) > MAX_CLEANUP_PLAN_FILES:
        raise CleanupRefusal(
            f"select between 1 and {MAX_CLEANUP_PLAN_FILES} completed-scan candidates"
        )
    if len({candidate.candidate_id for candidate in selected}) != len(selected):
        raise CleanupRefusal("candidate IDs must be unique across the cleanup plan")
    if len({_normalized(candidate.path) for candidate in selected}) != len(selected):
        raise CleanupRefusal("selected candidates must not share a source path")
    if sum(candidate.snapshot.logical_size for candidate in selected) > MAX_CLEANUP_PLAN_BYTES:
        raise CleanupRefusal("selected logical bytes exceed the per-plan safety limit")
    batches = tuple(
        prepare_cleanup_batch(selected[offset : offset + MAX_CLEANUP_BATCH_FILES])
        for offset in range(0, len(selected), MAX_CLEANUP_BATCH_FILES)
    )
    plan_id = "plan_" + secrets.token_hex(16)
    created_at = datetime.now(UTC)
    return PreparedCleanupPlan(
        plan_id,
        batches,
        _plan_digest(plan_id, batches),
        created_at,
        _SEAL,
    )


def issue_cleanup_confirmation(
    batch: PreparedCleanupBatch,
    *,
    mode: CleanupMode = CleanupMode.RECYCLE,
) -> CleanupConfirmationChallenge:
    """Issue final-page text bound to this batch, mode, files, and scan roots."""

    _require_batch(batch)
    if mode is CleanupMode.PERMANENT and not all(
        action.candidate.permanent_eligible for action in batch.actions
    ):
        raise CleanupRefusal(
            "permanent purge is limited to deterministic low-risk aged Temp/CrashDumps"
        )
    code = secrets.token_hex(3).upper()
    selected_bytes = sum(
        action.candidate.snapshot.logical_size for action in batch.actions
    )
    if mode is CleanupMode.PERMANENT:
        phrase = f"永久删除 {len(batch.actions)} 个文件 {code}"
    elif mode is CleanupMode.CONFIRMED_PURGE:
        phrase = (
            f"不可恢复清除 {len(batch.actions)} 个文件并释放空间 "
            f"{selected_bytes} 字节 {code}"
        )
    else:
        phrase = f"移入安全隔离区 {len(batch.actions)} 个文件 {code}"
    return CleanupConfirmationChallenge(
        batch_digest=batch.digest,
        mode=mode,
        phrase=phrase,
        expires_at=datetime.now(UTC) + CONFIRMATION_TTL,
        _seal=_SEAL,
    )


def issue_cleanup_plan_confirmation(
    plan: PreparedCleanupPlan,
    *,
    mode: CleanupMode = CleanupMode.RECYCLE,
) -> CleanupConfirmationChallenge:
    """Issue one exact typed challenge for every file in a split plan."""

    _require_plan(plan)
    actions = plan.actions
    if mode is CleanupMode.PERMANENT and not all(
        action.candidate.permanent_eligible for action in actions
    ):
        raise CleanupRefusal(
            "permanent purge is limited to deterministic low-risk aged Temp/CrashDumps"
        )
    code = secrets.token_hex(3).upper()
    selected_bytes = sum(action.candidate.snapshot.logical_size for action in actions)
    if mode is CleanupMode.PERMANENT:
        phrase = f"永久删除 {len(actions)} 个文件 {code}"
    elif mode is CleanupMode.CONFIRMED_PURGE:
        phrase = (
            f"不可恢复清除 {len(actions)} 个文件并释放空间 "
            f"{selected_bytes} 字节 {code}"
        )
    else:
        phrase = f"移入安全隔离区 {len(actions)} 个文件 {code}"
    return CleanupConfirmationChallenge(
        batch_digest=plan.digest,
        mode=mode,
        phrase=phrase,
        expires_at=datetime.now(UTC) + CONFIRMATION_TTL,
        _seal=_SEAL,
    )


def confirm_cleanup_batch(
    batch: PreparedCleanupBatch,
    challenge: CleanupConfirmationChallenge,
    typed_phrase: str,
) -> CleanupExecutionApproval:
    """Return an opaque execution approval only for an exact typed phrase."""

    _require_batch(batch)
    if challenge._seal is not _SEAL or not hmac.compare_digest(
        challenge.batch_digest, batch.digest
    ):
        raise CleanupRefusal("confirmation challenge does not belong to this batch")
    if datetime.now(UTC) > challenge.expires_at:
        raise CleanupRefusal("confirmation challenge expired; review the batch again")
    if not hmac.compare_digest(
        typed_phrase.encode("utf-8"), challenge.phrase.encode("utf-8")
    ):
        raise CleanupRefusal("typed confirmation does not exactly match the final-page phrase")
    if challenge.mode is CleanupMode.PERMANENT and not all(
        action.candidate.permanent_eligible for action in batch.actions
    ):
        raise CleanupRefusal("batch is not eligible for permanent purge")
    return CleanupExecutionApproval(
        batch_digest=batch.digest,
        mode=challenge.mode,
        expires_at=challenge.expires_at,
        approval_nonce=secrets.token_hex(16),
        _seal=_SEAL,
    )


def confirm_cleanup_plan(
    plan: PreparedCleanupPlan,
    challenge: CleanupConfirmationChallenge,
    typed_phrase: str,
) -> tuple[CleanupExecutionApproval, ...]:
    """Convert one exact plan confirmation into sealed per-batch approvals."""

    _require_plan(plan)
    if challenge._seal is not _SEAL or not hmac.compare_digest(
        challenge.batch_digest, plan.digest
    ):
        raise CleanupRefusal("confirmation challenge does not belong to this plan")
    if datetime.now(UTC) > challenge.expires_at:
        raise CleanupRefusal("confirmation challenge expired; review the plan again")
    if not hmac.compare_digest(
        typed_phrase.encode("utf-8"), challenge.phrase.encode("utf-8")
    ):
        raise CleanupRefusal("typed confirmation does not exactly match the final-page phrase")
    if challenge.mode is CleanupMode.PERMANENT and not all(
        action.candidate.permanent_eligible for action in plan.actions
    ):
        raise CleanupRefusal("plan is not eligible for permanent purge")
    return tuple(
        CleanupExecutionApproval(
            batch_digest=batch.digest,
            mode=challenge.mode,
            expires_at=challenge.expires_at,
            approval_nonce=secrets.token_hex(16),
            _seal=_SEAL,
        )
        for batch in plan.batches
    )


def execute_approved_batch(
    batch: PreparedCleanupBatch,
    approval: CleanupExecutionApproval,
    *,
    journal: CleanupJournal | None = None,
    recycle_after_quarantine: bool = False,
    mover: QuarantineMover = quarantine_exact_file,
    purger: PermanentPurger = purge_exact_file,
) -> CleanupExecutionResult:
    """Execute a durable, post-scan batch once; there is no replay path."""

    _require_approval(batch, approval)
    if recycle_after_quarantine:
        raise CleanupRefusal(
            "automatic Recycle Bin forwarding is disabled until a forced-recycle "
            "implementation can prove recoverability; the private quarantine is retained"
        )
    active_journal = journal or CleanupJournal()
    _preflight_batch(batch, approval.mode)
    intents = tuple(_intent_for(action, batch.batch_id, approval.mode) for action in batch.actions)
    active_journal.record_batch(batch.batch_id, approval.mode, intents)

    prepared_directories: set[str] = set()
    for action_index, action in enumerate(batch.actions):
        candidate = action.candidate
        try:
            _preflight_candidate(candidate, approval.mode)
            active_journal.transition(
                action.action_id,
                expected=(ActionState.INTENT_RECORDED,),
                new_state=ActionState.EXECUTING,
                detail="exact-object mutation beginning after full-batch preflight",
            )
            boundary = _execution_boundary(candidate, approval.mode)
            destination = _quarantine_path(action, batch.batch_id, approval.mode)
            directory_key = _normalized(destination.parent)
            if directory_key in prepared_directories:
                verify_private_quarantine_directory(destination.parent, boundary)
            else:
                prepare_private_quarantine_directory(destination.parent, boundary)
                prepared_directories.add(directory_key)
            mover(
                candidate.path,
                destination,
                candidate.snapshot,
                boundary,
            )
            if not metadata_matches_snapshot(
                _optional_metadata(destination), candidate.snapshot
            ) or metadata_matches_snapshot(
                _optional_metadata(candidate.path), candidate.snapshot
            ):
                raise CleanupRefusal("quarantine mover did not establish exact postconditions")
            # The private parent blocks namespace traversal.  Do not rewrite
            # the file DACL: a later restore must retain the source ACL exactly.
            active_journal.transition(
                action.action_id,
                expected=(ActionState.EXECUTING,),
                new_state=ActionState.QUARANTINED,
                detail="exact object moved to same-volume private quarantine",
            )
            if approval.mode in {CleanupMode.PERMANENT, CleanupMode.CONFIRMED_PURGE}:
                active_journal.transition(
                    action.action_id,
                    expected=(ActionState.QUARANTINED,),
                    new_state=ActionState.PURGE_PENDING,
                    detail=(
                        "second durable irreversible intent recorded after exact quarantine"
                    ),
                )
                purger(
                    destination,
                    candidate.snapshot,
                    _execution_boundary(candidate, approval.mode),
                )
                if metadata_matches_snapshot(
                    _optional_metadata(destination), candidate.snapshot
                ):
                    raise CleanupRefusal(
                        "confirmed purger returned while quarantined object still exists"
                    )
                active_journal.transition(
                    action.action_id,
                    expected=(ActionState.PURGE_PENDING,),
                    new_state=ActionState.PURGED,
                    detail="handle-bound quarantine purge verified",
                )
        except Exception as error:
            _record_failure(active_journal, action.action_id, error)
            for unattempted in batch.actions[action_index + 1 :]:
                _record_unattempted(active_journal, unattempted.action_id)
            break

    state = active_journal.finalize_batch(batch.batch_id)
    actions = active_journal.actions_for_batch(batch.batch_id)
    selected_bytes = sum(action.candidate.snapshot.logical_size for action in batch.actions)
    sizes = {
        action.action_id: action.candidate.snapshot.logical_size for action in batch.actions
    }
    purged_bytes = sum(
        sizes[action.action_id] for action in actions if action.state is ActionState.PURGED
    )
    return CleanupExecutionResult(
        batch_id=batch.batch_id,
        mode=approval.mode,
        batch_state=state,
        action_states=tuple((action.action_id, action.state) for action in actions),
        selected_logical_bytes=selected_bytes,
        purged_logical_bytes=purged_bytes,
        # Compatibility name: this is an exact logical-byte sum of journaled
        # PURGED actions, never a claim about physical allocation or free space.
        immediate_reclaim_upper_bound=purged_bytes,
    )


def reconcile_unfinished_actions(journal: CleanupJournal) -> tuple[tuple[str, ActionState], ...]:
    """Reconcile durable unknowns by observation only; never replay mutation."""

    results: list[tuple[str, ActionState]] = []
    for action in journal.unresolved_actions():
        observation_failed = False
        try:
            source = _optional_metadata(Path(action.source_path))
            quarantine = (
                _optional_metadata(Path(action.quarantine_path))
                if action.quarantine_path is not None
                else None
            )
        except OSError:
            source = None
            quarantine = None
            observation_failed = True
        purge_may_have_started = (
            ActionState.PURGE_PENDING.value in journal.event_states(action.action_id)
        )
        if observation_failed:
            new_state = ActionState.UNKNOWN
            detail = "reconciliation could not observe source and quarantine safely"
        elif action.state in {ActionState.PURGE_PENDING, ActionState.UNKNOWN} and (
            purge_may_have_started
        ):
            new_state = ActionState.UNKNOWN
            detail = (
                "purge disposition may have started; observation cannot prove recovery"
            )
        elif metadata_matches_snapshot(quarantine, action.snapshot):
            new_state = ActionState.QUARANTINED
            detail = "reconciliation found the exact object in private quarantine"
        elif metadata_matches_snapshot(source, action.snapshot) and quarantine is None:
            if action.state in {ActionState.RESTORE_INTENT, ActionState.RESTORING}:
                new_state = ActionState.RESTORED
                detail = "reconciliation proved the exact object was restored"
            else:
                new_state = ActionState.FAILED_UNCHANGED
                detail = "reconciliation found the exact object unchanged at its source"
        else:
            new_state = ActionState.UNKNOWN
            detail = "reconciliation cannot prove the exact object's recoverable state"
        if action.state is not new_state:
            journal.transition(
                action.action_id,
                expected=(action.state,),
                new_state=new_state,
                detail=detail,
            )
        results.append((action.action_id, new_state))
        journal.finalize_batch(action.batch_id)
    return tuple(results)


def restore_quarantined_action(
    journal: CleanupJournal,
    action_id: str,
    *,
    restorer: Restorer = restore_exact_file,
) -> ActionState:
    """Restore a private-quarantine item without replacing an occupied source."""

    action = journal.action(action_id)
    if action.state is not ActionState.QUARANTINED or action.quarantine_path is None:
        raise CleanupRefusal("only a verified private-quarantine action can be restored")
    _reject_protected_path(Path(action.source_path))
    approved_root = Path(action.approved_root)
    quarantine_path = Path(action.quarantine_path)
    _require_strict_descendant(Path(action.source_path), approved_root)
    _require_strict_descendant(quarantine_path, approved_root)
    if not any(
        part.casefold().startswith(f"{QUARANTINE_DIRECTORY_NAME.casefold()}-batch_")
        for part in quarantine_path.parts
    ):
        raise CleanupRefusal("journal quarantine path is outside the private namespace")
    root_metadata = read_file_metadata(approved_root)
    if root_metadata.identity != (
        action.approved_root_snapshot.volume_serial,
        action.approved_root_snapshot.file_id,
    ):
        raise CleanupRefusal("approved restore root identity changed")
    if not metadata_matches_snapshot(
        _optional_metadata(quarantine_path), action.snapshot
    ):
        raise CleanupRefusal("quarantined object no longer matches the durable snapshot")
    boundary = _boundary(approved_root, action.approved_root_snapshot)
    journal.transition(
        action_id,
        expected=(ActionState.QUARANTINED,),
        new_state=ActionState.RESTORE_INTENT,
        detail="durable restore intent recorded",
    )
    try:
        journal.transition(
            action_id,
            expected=(ActionState.RESTORE_INTENT,),
            new_state=ActionState.RESTORING,
            detail="handle-bound restore beginning",
        )
        restorer(
            quarantine_path,
            Path(action.source_path),
            action.snapshot,
            boundary,
        )
        journal.transition(
            action_id,
            expected=(ActionState.RESTORING,),
            new_state=ActionState.RESTORED,
            detail="exact quarantined object restored without replacement",
        )
        journal.finalize_batch(action.batch_id)
        return ActionState.RESTORED
    except Exception as error:
        _record_failure(journal, action_id, error)
        journal.finalize_batch(action.batch_id)
        return journal.action(action_id).state


def _snapshot_from_record(item: TriageItem) -> ExactFileSnapshot:
    record = item.record
    if (
        record.volume_serial is None
        or record.file_id is None
        or record.file_id_kind is None
        or record.link_count is None
        or record.creation_time_ns is None
        or record.last_write_time_ns is None
    ):
        raise CleanupRefusal("scan item lacks stable file identity or timestamps")
    if record.file_id_kind != "file_id_128":
        raise CleanupRefusal("cleanup requires a Windows 128-bit file identity")
    if record.link_count != 1:
        raise CleanupRefusal("hard-linked files cannot become cleanup actions")
    if (record.attributes or 0) & FILE_ATTRIBUTE_REPARSE_POINT or record.reparse_tag is not None:
        raise CleanupRefusal("reparse-point records cannot become cleanup actions")
    return ExactFileSnapshot(
        logical_size=record.logical_size,
        volume_serial=int(record.volume_serial),
        file_id=str(record.file_id),
        file_id_kind=str(record.file_id_kind),
        link_count=int(record.link_count),
        attributes=record.attributes,
        reparse_tag=record.reparse_tag,
        creation_time_ns=int(record.creation_time_ns),
        last_write_time_ns=int(record.last_write_time_ns),
    )


def _preflight_batch(batch: PreparedCleanupBatch, mode: CleanupMode) -> None:
    for action in batch.actions:
        _preflight_candidate(action.candidate, mode)


def _preflight_candidate(candidate: ScanCleanupCandidate, mode: CleanupMode) -> None:
    _reject_protected_path(candidate.path)
    _require_strict_descendant(candidate.path, candidate.scan_root)
    if not is_local_fixed_path(candidate.path) or not is_local_fixed_path(candidate.scan_root):
        raise CleanupRefusal("execution target escaped the local fixed-volume boundary")
    root = read_file_metadata(candidate.scan_root)
    if (
        not root.is_directory
        or root.is_reparse_point
        or root.is_cloud_placeholder
        or root.identity
        != (
            candidate.scan_root_snapshot.volume_serial,
            candidate.scan_root_snapshot.file_id,
        )
    ):
        raise CleanupRefusal("original approved scan root identity changed")
    if mode is CleanupMode.PERMANENT:
        permanent_root = candidate.permanent_root
        permanent_snapshot = candidate.permanent_root_snapshot
        if (
            not candidate.permanent_eligible
            or permanent_root is None
            or permanent_snapshot is None
        ):
            raise CleanupRefusal("candidate has no narrow approved permanent root")
        _require_strict_descendant(candidate.path, permanent_root)
        root = read_file_metadata(permanent_root)
        if (
            not root.is_directory
            or root.is_reparse_point
            or root.is_cloud_placeholder
            or root.identity
            != (permanent_snapshot.volume_serial, permanent_snapshot.file_id)
        ):
            raise CleanupRefusal("approved Temp/CrashDumps root identity changed")
    metadata = read_file_metadata(candidate.path)
    if not metadata_matches_snapshot(metadata, candidate.snapshot):
        raise CleanupRefusal("candidate changed since the completed scan")


def _intent_for(
    action: CleanupAction, batch_id: str, mode: CleanupMode
) -> CleanupIntent:
    candidate = action.candidate
    if mode is CleanupMode.PERMANENT:
        if candidate.permanent_root is None or candidate.permanent_root_snapshot is None:
            raise CleanupRefusal("permanent action has no narrow approved root")
        approved_root = candidate.permanent_root
        approved_root_snapshot = candidate.permanent_root_snapshot
    else:
        approved_root = candidate.scan_root
        approved_root_snapshot = candidate.scan_root_snapshot
    destination = str(_quarantine_path(action, batch_id, mode))
    return CleanupIntent(
        action_id=action.action_id,
        candidate_id=candidate.candidate_id,
        source_path=str(candidate.path),
        scan_root=str(candidate.scan_root),
        approved_root=str(approved_root),
        approved_root_snapshot=approved_root_snapshot,
        quarantine_path=destination,
        category=candidate.category.value,
        snapshot=candidate.snapshot,
    )


def _quarantine_path(
    action: CleanupAction,
    batch_id: str,
    mode: CleanupMode,
) -> Path:
    # A unique batch directory is a direct child of the pinned approval root.
    # No shared, pre-existing staging namespace is ever adopted.
    candidate = action.candidate
    if mode is CleanupMode.PERMANENT:
        if candidate.permanent_root is None:
            raise CleanupRefusal("permanent action has no narrow approved root")
        approved_root = candidate.permanent_root
    else:
        approved_root = candidate.scan_root
    return (
        approved_root
        / f"{QUARANTINE_DIRECTORY_NAME}-{batch_id}"
        / action.action_id
    )


def _execution_boundary(
    candidate: ScanCleanupCandidate, mode: CleanupMode
) -> ExactRootBoundary:
    if mode is CleanupMode.PERMANENT:
        if candidate.permanent_root is None or candidate.permanent_root_snapshot is None:
            raise CleanupRefusal("permanent candidate lost its canonical approved root")
        return _boundary(candidate.permanent_root, candidate.permanent_root_snapshot)
    return _boundary(candidate.scan_root, candidate.scan_root_snapshot)


def _boundary(path: Path, snapshot: ExactFileSnapshot) -> ExactRootBoundary:
    return ExactRootBoundary(
        path=path,
        volume_serial=snapshot.volume_serial,
        file_id=snapshot.file_id,
        file_id_kind=snapshot.file_id_kind,
    )


def _record_failure(
    journal: CleanupJournal,
    action_id: str,
    error: Exception,
) -> None:
    action = journal.action(action_id)
    observation_failed = False
    try:
        source = _optional_metadata(Path(action.source_path))
        quarantine = (
            _optional_metadata(Path(action.quarantine_path))
            if action.quarantine_path is not None
            else None
        )
    except OSError:
        source = None
        quarantine = None
        observation_failed = True
    restoring = action.state in {ActionState.RESTORE_INTENT, ActionState.RESTORING}
    if action.state is ActionState.PURGE_PENDING or observation_failed:
        # A delete disposition can be accepted while another shared handle
        # keeps the name visible.  Never downgrade this phase to recoverable or
        # automatically replay it based on pathname observation.
        state = ActionState.UNKNOWN
    elif metadata_matches_snapshot(quarantine, action.snapshot):
        state = ActionState.QUARANTINED
    elif metadata_matches_snapshot(source, action.snapshot) and quarantine is None:
        state = ActionState.RESTORED if restoring else ActionState.FAILED_UNCHANGED
    else:
        state = ActionState.UNKNOWN
    journal.transition(
        action_id,
        expected=(action.state,),
        new_state=state,
        detail="mutation failed; state classified by exact-identity reconciliation",
        error=f"{type(error).__name__}: {error}",
    )


def _record_unattempted(journal: CleanupJournal, action_id: str) -> None:
    """Close a later durable intent after an earlier action stopped the batch."""

    action = journal.action(action_id)
    try:
        source = _optional_metadata(Path(action.source_path))
        quarantine = (
            _optional_metadata(Path(action.quarantine_path))
            if action.quarantine_path is not None
            else None
        )
    except OSError:
        source = None
        quarantine = None
    if metadata_matches_snapshot(source, action.snapshot) and quarantine is None:
        state = ActionState.FAILED_UNCHANGED
        detail = "not attempted because an earlier batch action failed; source unchanged"
    else:
        state = ActionState.UNKNOWN
        detail = "not attempted, but exact unchanged source state could not be proved"
    journal.transition(
        action_id,
        expected=(ActionState.INTENT_RECORDED,),
        new_state=state,
        detail=detail,
    )


def _optional_metadata(path: Path) -> FileSystemMetadata | None:
    try:
        return read_file_metadata(path)
    except FileNotFoundError:
        return None
    except OSError as error:
        if getattr(error, "winerror", None) in {2, 3} or error.errno == 2:
            return None
        raise


def _require_approval(
    batch: PreparedCleanupBatch, approval: CleanupExecutionApproval
) -> None:
    _require_batch(batch)
    if approval._seal is not _SEAL or not hmac.compare_digest(
        approval.batch_digest, batch.digest
    ):
        raise CleanupRefusal("execution approval does not belong to this exact batch")
    if datetime.now(UTC) > approval.expires_at:
        raise CleanupRefusal("execution approval expired; repeat final review")
    if approval.mode is CleanupMode.PERMANENT and not all(
        action.candidate.permanent_eligible for action in batch.actions
    ):
        raise CleanupRefusal("permanent approval does not cover this batch")


def _require_batch(batch: PreparedCleanupBatch) -> None:
    if not isinstance(batch, PreparedCleanupBatch) or batch._seal is not _SEAL:
        raise CleanupRefusal("expected an opaque prepared cleanup batch")
    if not hmac.compare_digest(batch.digest, _batch_digest(batch.batch_id, batch.actions)):
        raise CleanupRefusal("prepared cleanup batch was altered")


def _require_plan(plan: PreparedCleanupPlan) -> None:
    if not isinstance(plan, PreparedCleanupPlan) or plan._seal is not _SEAL:
        raise CleanupRefusal("expected an opaque prepared cleanup plan")
    if not plan.batches:
        raise CleanupRefusal("cleanup plan has no batches")
    for batch in plan.batches:
        _require_batch(batch)
    if not hmac.compare_digest(plan.digest, _plan_digest(plan.plan_id, plan.batches)):
        raise CleanupRefusal("prepared cleanup plan was altered")


def _plan_digest(plan_id: str, batches: Sequence[PreparedCleanupBatch]) -> str:
    digest = hashlib.sha256()
    digest.update(plan_id.encode("ascii"))
    for batch in batches:
        digest.update(b"\0")
        digest.update(batch.digest.encode("ascii"))
    return digest.hexdigest()


def _batch_digest(batch_id: str, actions: Sequence[CleanupAction]) -> str:
    digest = hashlib.sha256()
    digest.update(batch_id.encode("ascii"))
    for action in actions:
        candidate = action.candidate
        fields = (
            action.action_id,
            candidate.candidate_id,
            _normalized(candidate.path),
            _normalized(candidate.scan_root),
            str(candidate.snapshot.volume_serial),
            candidate.snapshot.file_id,
            str(candidate.snapshot.logical_size),
            str(candidate.snapshot.last_write_time_ns),
            _normalized(candidate.permanent_root) if candidate.permanent_root else "",
            "1" if candidate.permanent_eligible else "0",
        )
        for value in fields:
            digest.update(b"\0")
            digest.update(value.encode("utf-8", errors="strict"))
    return digest.hexdigest()


def _candidate_integrity(
    candidate_id: str,
    path: Path,
    scan_root: Path,
    scan_root_snapshot: ExactFileSnapshot,
    snapshot: ExactFileSnapshot,
    category: CleanupCategory,
    permanent_eligible: bool,
    permanent_root: Path | None,
    permanent_root_snapshot: ExactFileSnapshot | None,
) -> str:
    values = (
        candidate_id,
        _normalized(path),
        _normalized(scan_root),
        repr(scan_root_snapshot),
        repr(snapshot),
        category.value,
        "1" if permanent_eligible else "0",
        _normalized(permanent_root) if permanent_root else "",
        repr(permanent_root_snapshot),
    )
    digest = hmac.new(_CAPABILITY_KEY, digestmod=hashlib.sha256)
    for value in values:
        digest.update(b"\0")
        digest.update(value.encode("utf-8", errors="strict"))
    return digest.hexdigest()


def _absolute_local_path(path: Path, label: str) -> Path:
    text = os.path.abspath(os.fspath(path))
    if (
        not os.path.isabs(text)
        or len(text) > _MAX_PATH_LENGTH
        or text.startswith(("\\\\?\\", "\\\\.\\", "\\\\", "//"))
        or "\x00" in text
    ):
        raise CleanupRefusal(f"{label} must be an ordinary bounded absolute local path")
    return Path(text)


def _require_strict_descendant(path: Path, root: Path) -> None:
    normalized_path = _normalized(path)
    normalized_root = _normalized(root)
    try:
        common = os.path.commonpath((normalized_path, normalized_root))
    except ValueError as error:
        raise CleanupRefusal("candidate is on a different volume from its scan root") from error
    if common != normalized_root or normalized_path == normalized_root:
        raise CleanupRefusal("candidate must stay strictly below its original scan root")


def _is_descendant(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((_normalized(path), _normalized(root))) == _normalized(root)
    except ValueError:
        return False


def _reject_protected_path(path: Path) -> None:
    parts = {part.casefold() for part in path.parts}
    in_private_quarantine = any(
        part.startswith(f"{QUARANTINE_DIRECTORY_NAME.casefold()}-") for part in parts
    )
    if parts & _PROTECTED_SEGMENTS or in_private_quarantine:
        raise CleanupRefusal("hard-coded user-asset deny-list blocked the selected path")
    _reject_system_anchor(path)
    name = path.name.casefold()
    if (
        name == ".env"
        or name.startswith(".env.")
        or name in _PROTECTED_NAMES
        or path.suffix.casefold() in _PROTECTED_SUFFIXES
    ):
        raise CleanupRefusal("hard-coded credential deny-list blocked the selected path")
    state_root = _normalized(data_dir())
    try:
        state_common = os.path.commonpath((_normalized(path), state_root))
    except ValueError:
        state_common = ""
    if state_common == state_root:
        raise CleanupRefusal("DevClean state, journal, and evidence are protected")


def _normalized(path: Path) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def _directory_snapshot(path: Path, label: str) -> ExactFileSnapshot:
    metadata = read_file_metadata(path)
    if (
        not metadata.is_directory
        or metadata.is_reparse_point
        or metadata.is_cloud_placeholder
        or metadata.volume_serial is None
        or metadata.file_id is None
        or metadata.file_id_kind is None
        or metadata.creation_time_ns is None
        or metadata.last_write_time_ns is None
        or metadata.link_count is None
        or metadata.attributes is None
    ):
        raise CleanupRefusal(f"{label} has no stable ordinary-directory identity")
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


def _reject_system_anchor(path: Path) -> None:
    anchor = Path(path.anchor)
    protected_roots = (
        anchor / "Windows",
        anchor / "Program Files",
        anchor / "Program Files (x86)",
        anchor / "ProgramData",
        anchor / "Recovery",
        anchor / "$Recycle.Bin",
        anchor / "System Volume Information",
        anchor / "Windows.old",
        anchor / "$Windows.~BT",
    )
    normalized = _normalized(path)
    for protected in protected_roots:
        protected_normalized = _normalized(protected)
        try:
            common = os.path.commonpath((normalized, protected_normalized))
        except ValueError:
            continue
        if common == protected_normalized:
            raise CleanupRefusal("anchored Windows system-root deny-list blocked the path")
    if path.parent == anchor and path.name.casefold() in {
        "bootmgr",
        "hiberfil.sys",
        "pagefile.sys",
        "swapfile.sys",
    }:
        raise CleanupRefusal("anchored Windows system-file deny-list blocked the path")


__all__ = [
    "CONFIRMATION_TTL",
    "MAX_CLEANUP_BATCH_BYTES",
    "MAX_CLEANUP_BATCH_FILES",
    "MAX_CLEANUP_PLAN_BYTES",
    "MAX_CLEANUP_PLAN_FILES",
    "CleanupAction",
    "CleanupConfirmationChallenge",
    "CleanupExecutionApproval",
    "CleanupExecutionResult",
    "CleanupRefusal",
    "PreparedCleanupBatch",
    "PreparedCleanupPlan",
    "ScanCleanupCandidate",
    "candidate_from_triage_item",
    "confirm_cleanup_batch",
    "confirm_cleanup_plan",
    "execute_approved_batch",
    "issue_cleanup_confirmation",
    "issue_cleanup_plan_confirmation",
    "prepare_cleanup_batch",
    "prepare_cleanup_plan",
    "reconcile_unfinished_actions",
    "restore_quarantined_action",
]
