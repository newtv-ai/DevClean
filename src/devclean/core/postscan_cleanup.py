"""Post-scan cleanup orchestration for the mode explicitly chosen in the UI.

There is intentionally no entry point from the scanner or classifier into this
module.  A caller must retain exact ``TriageItem`` objects from a completed
scan, prepare a bounded opaque batch, and pass the recycle or permanent mode
selected by the user.  AI is optional classification advice only and cannot
invoke this layer.
"""

# User-facing cleanup text is Chinese prose, so it uses fullwidth punctuation.
# Matches ``core/triage.py``, which carries this for the same reason.

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from devclean.core.application_cleanup import DecisionOwner, process_guard_detail
from devclean.core.cleanup_catalog import (
    CleanupCategory,
    CleanupPolicy,
    KnownCleanupRoot,
    known_root_for_path,
)
from devclean.core.cleanup_journal import (
    ActionState,
    CleanupIntent,
    CleanupJournal,
    CleanupMode,
    JournalTargetKind,
)
from devclean.core.live_owners import (
    HIDDEN_RELATED_REFUSAL,
    LIVE_CHECK_UNAVAILABLE_REFUSAL,
    LIVE_OWNER_REFUSAL,
    live_owner_refusal,
)
from devclean.core.paths import data_dir
from devclean.core.system_anchor import system_anchor_refusal
from devclean.core.temp_folders import temp_entry_guard
from devclean.core.triage import (
    Actionability,
    CleanupTargetKind,
    DirectoryScope,
    DirectorySubtreeTotals,
    ExecutionPolicy,
    ReviewLane,
    TriageItem,
    directory_cleanup_scope,
    installer_ownership,
)
from devclean.core.user_rules import DeleteClassification, KeepClassification, UserRules
from devclean.core.whole_tree_policy import (
    WholeTreePolicyEvidence,
    WholeTreePolicyRefusal,
    require_application_whole_tree_policy,
)
from devclean.platform.windows.exact_cleanup import (
    DirectoryPurgeProgress,
    DirectoryPurgeResult,
    ExactDirectorySnapshot,
    ExactFileSnapshot,
    ExactMutationResult,
    ExactRootBoundary,
    TempFolderPurgeResult,
    directory_metadata_matches_snapshot,
    metadata_matches_snapshot,
    purge_directory_tree_skipping_in_use,
    purge_exact_file,
    purge_temp_folder_contents,
    recycle_exact_object,
)
from devclean.platform.windows.filesystem import (
    FILE_ATTRIBUTE_REPARSE_POINT,
    FileSystemMetadata,
    read_file_metadata,
)
from devclean.platform.windows.volumes import is_local_fixed_path
from devclean.scanner.filesystem import ScanRecordKind

MAX_CLEANUP_BATCH_FILES = 32
REVIEW_NOT_APPROVED_REFUSAL = "this item awaits an AI or user decision and was not approved"
INSTALLED_PROGRAM_REFUSAL = "the target now belongs to an installed program or installer cache"
# Windows always reports a 128-bit file id. Other hosts (Linux test fixtures)
# only offer the inode, which pins one object on that filesystem just the same.
_ACCEPTED_FILE_ID_KINDS = (
    frozenset({"file_id_128"}) if os.name == "nt" else frozenset({"file_id_128", "stat_ino"})
)
_MAX_PATH_LENGTH = 32_767
_SEAL = object()
_CAPABILITY_KEY = secrets.token_bytes(32)
# Machine-wide cleanup exceptions are carried by the discovered scan roots whose
# JSON entry explicitly sets ``allow_inside_system_anchor``.
type Recycler = Callable[[Path, ExactFileSnapshot, ExactRootBoundary], ExactMutationResult]
type PermanentPurger = Callable[[Path, ExactFileSnapshot, ExactRootBoundary], ExactMutationResult]
type DirectoryTreePurger = Callable[..., DirectoryPurgeResult]
type TempFolderPurger = Callable[..., TempFolderPurgeResult]


class CleanupRefusal(ValueError):
    """A scan candidate, prepared batch, or execution boundary was refused."""


@dataclass(frozen=True, slots=True)
class ScanCleanupCandidate:
    """Opaque exact-file capability derived from one completed scan item."""

    candidate_id: str
    path: Path
    scan_root: Path
    scan_root_snapshot: ExactFileSnapshot
    snapshot: ExactFileSnapshot
    category: CleanupCategory
    target_kind: CleanupTargetKind = CleanupTargetKind.FILE
    directory_scope: DirectoryScope | None = None
    subtree_files: int = 0
    subtree_bytes: int = 0
    # Deletable by a scan rule, not by an AI or user decision on an uncertain item.
    rule_proven: bool = False
    _integrity: str = field(repr=False, compare=False, default="")
    _seal: object = field(repr=False, compare=False, default=None)

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
            self.target_kind,
            self.directory_scope,
            self.subtree_files,
            self.subtree_bytes,
            self.rule_proven,
        )
        if not hmac.compare_digest(self._integrity, expected):
            raise CleanupRefusal("opaque cleanup candidate was altered")

    @property
    def selected_logical_bytes(self) -> int:
        """Bytes this one candidate stands for, whether a file or a whole tree."""

        if self.target_kind is CleanupTargetKind.DIRECTORY:
            return self.subtree_bytes
        return self.snapshot.logical_size


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
    """A bounded opaque selection prepared after the user's scan."""

    batch_id: str
    actions: tuple[CleanupAction, ...]
    digest: str
    _seal: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._seal is not _SEAL:
            raise CleanupRefusal("cleanup batches must be prepared by DevClean")


@dataclass(frozen=True, slots=True)
class PreparedCleanupPlan:
    """One user-visible manifest split into independently journaled batches."""

    batches: tuple[PreparedCleanupBatch, ...]
    _seal: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._seal is not _SEAL:
            raise CleanupRefusal("cleanup plans must be prepared by DevClean")

    @property
    def actions(self) -> tuple[CleanupAction, ...]:
        return tuple(action for batch in self.batches for action in batch.actions)


@dataclass(frozen=True, slots=True)
class CleanupExecutionResult:
    action_states: tuple[tuple[str, ActionState], ...]
    purged_logical_bytes: int
    completed_paths: tuple[str, ...]
    unverified_recycle_paths: tuple[str, ...] = ()
    # Why each failed action did not complete, for display next to its row.
    failures: tuple[tuple[str, BaseException], ...] = ()
    # Folders emptied except for entries that had to stay (in use, no
    # permission, claimed by another rule); they are not in completed_paths.
    partial: tuple[tuple[str, TempFolderPurgeResult], ...] = ()
    # What each finished or partly finished action removed: (path, files, bytes).
    removed: tuple[tuple[str, int, int], ...] = ()


@dataclass(frozen=True, slots=True)
class CleanupExecutionProgress:
    """One non-authoritative UI update from an executing cleanup batch."""

    action_index: int
    action_count: int
    path: str
    target_kind: CleanupTargetKind
    files_processed: int
    files_total: int
    completed: bool


type CleanupProgressReporter = Callable[[CleanupExecutionProgress], None]
type ActionDoneReporter = Callable[[CleanupExecutionResult], None]


def candidate_from_triage_item(
    item: TriageItem,
    *,
    delete_config: DeleteClassification,
    keep_config: KeepClassification,
    known_roots: tuple[KnownCleanupRoot, ...] = (),
    review_approved: bool = False,
) -> ScanCleanupCandidate:
    """Bind an exact scan observation to its original, user-chosen scan root.

    Presentation-only and protected items are never candidates. Rule-deletable
    items need nothing more than the user's cleanup click. AI/manual-review
    items need *review_approved*: an AI or user DELETE decision for this item.
    """

    record = item.record
    if record.kind is not ScanRecordKind.FILE or item.path != record.path:
        raise CleanupRefusal("candidate must be an exact file item from the completed scan")
    if item.lane is ReviewLane.REPORT_ONLY or item.actionability is Actionability.REPORT_ONLY:
        raise CleanupRefusal("protected and report-only items cannot become cleanup actions")
    _require_review_approval(item, review_approved)
    if item.execution_policy is not ExecutionPolicy.USER_CHOICE_DELETE:
        raise CleanupRefusal("this item requires an implemented vendor action or is not executable")
    path = _absolute_local_path(Path(record.path), "candidate")
    scan_root = _absolute_local_path(Path(record.root), "scan root")
    _require_strict_descendant(path, scan_root)
    _reject_protected_path(path, known_roots, keep_config)
    if not is_local_fixed_path(path) or not is_local_fixed_path(scan_root):
        raise CleanupRefusal("candidate and original scan root must be on a local fixed volume")
    snapshot = _snapshot_from_record(item)
    root_snapshot = _directory_snapshot(scan_root, "original scan root")
    candidate_id = "candidate_" + secrets.token_hex(16)
    rule_proven = item.lane is ReviewLane.DETERMINISTIC_CANDIDATE
    integrity = _candidate_integrity(
        candidate_id,
        path,
        scan_root,
        root_snapshot,
        snapshot,
        item.category,
        rule_proven=rule_proven,
    )
    return ScanCleanupCandidate(
        candidate_id=candidate_id,
        path=path,
        scan_root=scan_root,
        scan_root_snapshot=root_snapshot,
        snapshot=snapshot,
        category=item.category,
        rule_proven=rule_proven,
        _integrity=integrity,
        _seal=_SEAL,
    )


def candidate_from_directory_item(
    item: TriageItem,
    totals: DirectorySubtreeTotals,
    *,
    delete_config: DeleteClassification,
    keep_config: KeepClassification,
    known_roots: tuple[KnownCleanupRoot, ...] = (),
    review_approved: bool = False,
) -> ScanCleanupCandidate:
    """Bind one whole-directory observation to its original scan root.

    Eligibility is re-derived here rather than trusted from the classification
    that produced *item*, and it is re-derived once more at execution preflight.
    Unlike a file candidate, whose reach is the one object the user pointed at,
    a directory candidate authorises everything beneath it, so the reason it is
    allowed has to still hold at the moment of mutation.
    """

    record = item.record
    if item.target_kind is not CleanupTargetKind.DIRECTORY:
        raise CleanupRefusal("directory candidates require a whole-directory scan item")
    _require_review_approval(item, review_approved)
    if record.kind is not ScanRecordKind.DIRECTORY or item.path != record.path:
        raise CleanupRefusal("candidate must be an exact directory item from the scan")
    path = _absolute_local_path(Path(record.path), "candidate directory")
    scope = _require_directory_scope(path, known_roots, delete_config, keep_config)
    original_root = _absolute_local_path(Path(record.root), "scan root")
    if scope is DirectoryScope.TEMP_FOLDER:
        # Only the contents go; a temp folder that is itself a scan root (the
        # Windows Temp folder) is its own boundary.
        scan_root = original_root if _normalized(path) != _normalized(original_root) else path
        _require_descendant_or_self(path, scan_root)
    else:
        scan_root = _directory_execution_boundary(path, original_root, known_roots)
        _require_strict_descendant(path, scan_root)
    _reject_protected_path(path, known_roots, keep_config)
    if not is_local_fixed_path(path) or not is_local_fixed_path(scan_root):
        raise CleanupRefusal("candidate and original scan root must be on a local fixed volume")
    if totals.files < 0 or totals.logical_bytes < 0:
        raise CleanupRefusal("directory subtree totals must be non-negative")
    policy_evidence = (
        None
        if scope is DirectoryScope.TEMP_FOLDER
        else _application_whole_tree_policy(path, known_roots)
    )
    subtree_files = totals.files
    subtree_bytes = totals.logical_bytes
    if policy_evidence is not None:
        subtree_files = policy_evidence.files
        subtree_bytes = policy_evidence.logical_bytes
    snapshot = _snapshot_selected_directory(item)
    root_snapshot = _directory_snapshot(scan_root, "original scan root")
    candidate_id = "candidate_" + secrets.token_hex(16)
    rule_proven = item.lane is ReviewLane.DETERMINISTIC_CANDIDATE
    integrity = _candidate_integrity(
        candidate_id,
        path,
        scan_root,
        root_snapshot,
        snapshot,
        item.category,
        CleanupTargetKind.DIRECTORY,
        scope,
        subtree_files,
        subtree_bytes,
        rule_proven,
    )
    return ScanCleanupCandidate(
        candidate_id=candidate_id,
        path=path,
        scan_root=scan_root,
        scan_root_snapshot=root_snapshot,
        snapshot=snapshot,
        category=item.category,
        target_kind=CleanupTargetKind.DIRECTORY,
        directory_scope=scope,
        subtree_files=subtree_files,
        subtree_bytes=subtree_bytes,
        rule_proven=rule_proven,
        _integrity=integrity,
        _seal=_SEAL,
    )


def _require_review_approval(item: TriageItem, review_approved: bool) -> None:
    if item.lane in {ReviewLane.AI_REVIEW, ReviewLane.USER_REVIEW} and not review_approved:
        raise CleanupRefusal(REVIEW_NOT_APPROVED_REFUSAL)


def _reject_overlapping_candidates(candidates: Sequence[ScanCleanupCandidate]) -> None:
    """Refuse a selection where one target already contains another.

    Once whole directories can be selected, a plan can hold both a tree and
    something inside it. Executing that would delete the tree and then fail
    on the now-missing child, spending the user's deletion choice to reach a
    redundant second action. Refusing it while the plan is prepared keeps the
    execution list and reclaimed-byte total unambiguous.
    """

    # Emptying a temp folder leaves what other rules claim, so selecting a row
    # inside it as well is not redundant.
    directories = sorted(
        (
            _normalized(candidate.path)
            for candidate in candidates
            if candidate.target_kind is CleanupTargetKind.DIRECTORY
            and candidate.directory_scope is not DirectoryScope.TEMP_FOLDER
        ),
        key=len,
    )
    if not directories:
        return
    for candidate in candidates:
        target = _normalized(candidate.path)
        for directory in directories:
            if target != directory and target.startswith(directory + os.sep):
                raise CleanupRefusal(
                    "a selected directory already contains another selected target; "
                    "remove the inner item or the directory"
                )


def _require_directory_scope(
    path: Path,
    known_roots: tuple[KnownCleanupRoot, ...],
    delete_config: DeleteClassification,
    keep_config: KeepClassification,
) -> DirectoryScope:
    scope = directory_cleanup_scope(path, known_roots, delete_config, keep_config)
    if scope is DirectoryScope.NOT_ELIGIBLE:
        raise CleanupRefusal(
            "whole-directory cleanup is limited to recognised cache roots and "
            "deterministically regenerable tool directories"
        )
    if scope is DirectoryScope.KNOWN_CACHE_ROOT:
        known = known_root_for_path(path, known_roots)
        rule = known.application_rule if known is not None else None
        if not (
            known is not None
            and known.policy is CleanupPolicy.VENDOR_MANAGED
            and rule is not None
            and rule.owner is DecisionOwner.TOOL
            and rule.allow_whole_tree
        ):
            raise CleanupRefusal(
                "whole-directory vendor cleanup requires an attached audited application TOOL rule"
            )
    return scope


def _application_whole_tree_policy(
    path: Path,
    known_roots: tuple[KnownCleanupRoot, ...],
) -> WholeTreePolicyEvidence | None:
    try:
        return require_application_whole_tree_policy(path, known_roots)
    except WholeTreePolicyRefusal as error:
        raise CleanupRefusal(str(error)) from error


def prepare_cleanup_batch(
    candidates: Sequence[ScanCleanupCandidate],
) -> PreparedCleanupBatch:
    """Prepare one bounded batch after scanning has ended."""

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
    _reject_overlapping_candidates(candidates)
    actions = tuple(
        CleanupAction(
            action_id="action_" + secrets.token_hex(16),
            candidate=candidate,
            _seal=_SEAL,
        )
        for candidate in candidates
    )
    batch_id = "batch_" + secrets.token_hex(16)
    digest = _batch_digest(batch_id, actions)
    return PreparedCleanupBatch(batch_id, actions, digest, _SEAL)


def prepare_cleanup_plan(
    candidates: Sequence[ScanCleanupCandidate],
) -> PreparedCleanupPlan:
    """Prepare one bounded manifest while retaining 32-action journal batches."""

    selected = tuple(candidates)
    if not selected:
        raise CleanupRefusal("select at least one completed-scan candidate")
    if len({candidate.candidate_id for candidate in selected}) != len(selected):
        raise CleanupRefusal("candidate IDs must be unique across the cleanup plan")
    if len({_normalized(candidate.path) for candidate in selected}) != len(selected):
        raise CleanupRefusal("selected candidates must not share a source path")
    _reject_overlapping_candidates(selected)
    batches = tuple(
        prepare_cleanup_batch(selected[offset : offset + MAX_CLEANUP_BATCH_FILES])
        for offset in range(0, len(selected), MAX_CLEANUP_BATCH_FILES)
    )
    return PreparedCleanupPlan(batches, _SEAL)


def execute_cleanup_batch(
    batch: PreparedCleanupBatch,
    mode: CleanupMode,
    *,
    delete_config: DeleteClassification,
    keep_config: KeepClassification,
    journal: CleanupJournal | None = None,
    recycler: Recycler = recycle_exact_object,
    purger: PermanentPurger = purge_exact_file,
    directory_purger: DirectoryTreePurger = purge_directory_tree_skipping_in_use,
    temp_folder_purger: TempFolderPurger = purge_temp_folder_contents,
    on_progress: CleanupProgressReporter | None = None,
    known_roots: tuple[KnownCleanupRoot, ...] = (),
    user_rules: UserRules | None = None,
    on_action_done: ActionDoneReporter | None = None,
) -> CleanupExecutionResult:
    """Execute a durable, post-scan batch once; there is no replay path.

    *on_action_done* hears about each action as soon as it is over -- removed,
    partly removed or refused -- as a one-action result, so a window can show
    every row's outcome while the rest still runs.

    Two outcomes, matching what the user chose: ``RECYCLE`` sends the object to
    the Windows Recycle Bin, where they already know how to get it back, and the
    permanent mode deletes the verified handle outright.  Nothing is staged
    anywhere in between -- a private holding area would not free the space the
    user is trying to reclaim.
    """

    _require_batch(batch)
    active_journal = journal or CleanupJournal()
    intents = tuple(_intent_for(action, batch.batch_id) for action in batch.actions)
    active_journal.record_batch(batch.batch_id, mode, intents)

    purged_directory_bytes: dict[str, int] = {}
    unverified_recycle_paths: list[str] = []
    failures: list[tuple[str, BaseException]] = []
    partial: list[tuple[str, TempFolderPurgeResult]] = []
    removed_files: dict[str, int] = {}
    for action_index, action in enumerate(batch.actions):
        candidate = action.candidate
        action_failure: BaseException | None = None
        _report_cleanup_progress(
            on_progress,
            action_index=action_index,
            action_count=len(batch.actions),
            candidate=candidate,
            files_processed=0,
            completed=False,
        )
        try:
            active_journal.transition(
                action.action_id,
                expected=(ActionState.INTENT_RECORDED,),
                new_state=ActionState.EXECUTING,
                detail="exact-object mutation beginning before per-item preflight",
            )
            if _optional_metadata(candidate.path) is None:
                if mode is not CleanupMode.RECYCLE:
                    purged_directory_bytes[action.action_id] = 0
                active_journal.transition(
                    action.action_id,
                    expected=(ActionState.EXECUTING,),
                    new_state=(
                        ActionState.RECYCLED if mode is CleanupMode.RECYCLE else ActionState.PURGED
                    ),
                    detail="object was already absent; nothing to remove",
                )
                continue
            _preflight_candidate(
                candidate,
                known_roots=known_roots,
                delete_config=delete_config,
                keep_config=keep_config,
            )
            boundary = _execution_boundary(candidate)
            is_directory = candidate.target_kind is CleanupTargetKind.DIRECTORY
            is_temp_folder = candidate.directory_scope is DirectoryScope.TEMP_FOLDER
            if is_temp_folder and mode is CleanupMode.RECYCLE:
                raise CleanupRefusal(
                    "a temp folder is emptied permanently; the Recycle Bin would free no space"
                )
            if mode is CleanupMode.RECYCLE:
                outcome = recycler(candidate.path, candidate.snapshot, boundary)
                if not outcome.recycled:
                    unverified_recycle_paths.append(str(candidate.path))
                    active_journal.transition(
                        action.action_id,
                        expected=(ActionState.EXECUTING,),
                        new_state=ActionState.RECYCLED,
                        detail=(
                            "Windows removed the object after a recycle request, "
                            "but Recycle Bin placement could not be verified"
                        ),
                    )
                    continue
                active_journal.transition(
                    action.action_id,
                    expected=(ActionState.EXECUTING,),
                    new_state=ActionState.RECYCLED,
                    detail="exact object accepted by the Windows Recycle Bin",
                )
            else:
                active_journal.transition(
                    action.action_id,
                    expected=(ActionState.EXECUTING,),
                    new_state=ActionState.PURGE_PENDING,
                    detail="durable irreversible intent recorded before deletion",
                )
                if is_temp_folder:
                    emptied = temp_folder_purger(
                        candidate.path,
                        _directory_identity(candidate.snapshot),
                        boundary,
                        keep=temp_entry_guard(
                            candidate.path,
                            known_roots,
                            user_rules,
                            in_use=_entry_in_use,
                        ),
                        on_progress=_temp_folder_reporter(
                            on_progress, action_index, len(batch.actions), candidate
                        ),
                    )
                    if not emptied.completed:
                        raise CleanupRefusal("temp folder cleanup stopped before it finished")
                    purged_directory_bytes[action.action_id] = emptied.bytes_removed
                    removed_files[action.action_id] = emptied.files_removed
                    if emptied.skipped:
                        partial.append((str(candidate.path), emptied))
                elif is_directory:
                    identity = _directory_identity(candidate.snapshot)

                    def report_directory(
                        progress: DirectoryPurgeProgress,
                        *,
                        current_index: int = action_index,
                        current_candidate: ScanCleanupCandidate = candidate,
                    ) -> None:
                        _report_cleanup_progress(
                            on_progress,
                            action_index=current_index,
                            action_count=len(batch.actions),
                            candidate=current_candidate,
                            files_processed=(progress.files_removed + progress.links_removed),
                            completed=False,
                        )

                    tree = directory_purger(
                        candidate.path,
                        identity,
                        boundary,
                        on_progress=report_directory,
                    )
                    left_in_use = tree.in_use + tree.denied + tree.unsafe
                    if not tree.completed or (not tree.root_absent and not left_in_use):
                        raise CleanupRefusal(
                            "tree deletion stopped before the whole tree was removed"
                        )
                    purged_directory_bytes[action.action_id] = tree.bytes_removed
                    removed_files[action.action_id] = tree.files_removed
                    if not tree.root_absent:
                        # What a running program held open stays; the rest went.
                        partial.append(
                            (
                                str(candidate.path),
                                TempFolderPurgeResult(
                                    root_path=tree.root_path,
                                    files_removed=tree.files_removed,
                                    links_removed=tree.links_removed,
                                    directories_removed=tree.directories_removed,
                                    bytes_removed=tree.bytes_removed,
                                    in_use=tree.in_use,
                                    denied=tree.denied,
                                    kept=0,
                                    completed=tree.completed,
                                    examples=tree.examples,
                                    unsafe=tree.unsafe,
                                ),
                            )
                        )
                else:
                    purger(candidate.path, candidate.snapshot, boundary)
                    if metadata_matches_snapshot(
                        _optional_metadata(candidate.path), candidate.snapshot
                    ):
                        raise CleanupRefusal(
                            "purger returned while the verified object still exists"
                        )
                active_journal.transition(
                    action.action_id,
                    expected=(ActionState.PURGE_PENDING,),
                    new_state=ActionState.PURGED,
                    detail="handle-bound deletion verified",
                )
        except Exception as error:
            _record_failure(active_journal, action.action_id, error)
            failures.append((str(candidate.path), error))
            action_failure = error
        finally:
            _report_cleanup_progress(
                on_progress,
                action_index=action_index,
                action_count=len(batch.actions),
                candidate=candidate,
                files_processed=max(1, candidate.subtree_files),
                completed=True,
            )
            _report_action_done(
                on_action_done,
                active_journal,
                action,
                failure=action_failure,
                left=next(
                    (outcome for path, outcome in partial if path == str(candidate.path)), None
                ),
                purged_bytes=purged_directory_bytes.get(
                    action.action_id, candidate.selected_logical_bytes
                ),
                files=removed_files.get(action.action_id),
                unverified=str(candidate.path) in unverified_recycle_paths,
            )

    active_journal.finalize_batch(batch.batch_id)
    actions = active_journal.actions_for_batch(batch.batch_id)
    sizes = {
        action.action_id: purged_directory_bytes.get(
            action.action_id, action.candidate.selected_logical_bytes
        )
        for action in batch.actions
    }
    purged_bytes = sum(
        sizes[action.action_id] for action in actions if action.state is ActionState.PURGED
    )
    action_states = tuple((action.action_id, action.state) for action in actions)
    completed_ids = {
        action_id
        for action_id, state in action_states
        if state in {ActionState.PURGED, ActionState.RECYCLED}
    }
    left_behind = {path for path, _outcome in partial}
    removed = tuple(
        (
            str(action.candidate.path),
            removed_files.get(
                action.action_id,
                max(1, action.candidate.subtree_files)
                if action.candidate.target_kind is CleanupTargetKind.DIRECTORY
                else 1,
            ),
            sizes[action.action_id],
        )
        for action in batch.actions
        if action.action_id in completed_ids
    )
    return CleanupExecutionResult(
        action_states=action_states,
        purged_logical_bytes=purged_bytes,
        completed_paths=tuple(
            str(action.candidate.path)
            for action in batch.actions
            if action.action_id in completed_ids and str(action.candidate.path) not in left_behind
        ),
        unverified_recycle_paths=tuple(unverified_recycle_paths),
        failures=tuple(failures),
        partial=tuple(partial),
        removed=removed,
    )



def _report_action_done(
    reporter: ActionDoneReporter | None,
    journal: CleanupJournal,
    action: CleanupAction,
    *,
    failure: BaseException | None,
    left: TempFolderPurgeResult | None,
    purged_bytes: int,
    files: int | None,
    unverified: bool,
) -> None:
    """Tell *reporter* how one action ended; a reporting problem never stops deletion."""

    if reporter is None:
        return
    try:
        candidate = action.candidate
        path = str(candidate.path)
        state = journal.action(action.action_id).state
        finished = state in {ActionState.PURGED, ActionState.RECYCLED}
        if files is None:
            files = (
                max(1, candidate.subtree_files)
                if candidate.target_kind is CleanupTargetKind.DIRECTORY
                else 1
            )
        reporter(
            CleanupExecutionResult(
                action_states=((action.action_id, state),),
                purged_logical_bytes=purged_bytes if state is ActionState.PURGED else 0,
                completed_paths=(path,) if finished and left is None else (),
                unverified_recycle_paths=(path,) if unverified else (),
                failures=((path, failure),) if failure is not None else (),
                partial=((path, left),) if left is not None else (),
                removed=((path, files, purged_bytes),) if finished else (),
            )
        )
    except Exception:
        return


def _entry_in_use(path: str, is_directory: bool) -> str | None:
    """Why a running program or service may use an entry inside a temp folder."""

    refusal = live_owner_refusal(Path(path), directory=is_directory, rule_proven=True)
    if refusal is None:
        return None
    if refusal.startswith(LIVE_OWNER_REFUSAL):
        return f"{refusal[len(LIVE_OWNER_REFUSAL) :].lstrip(': ')} 在使用"
    if refusal.startswith(HIDDEN_RELATED_REFUSAL):
        name = refusal[len(HIDDEN_RELATED_REFUSAL) :].lstrip(": ").partition(": ")[0]
        return f"可能正被运行中的 {name} 使用"
    if refusal.startswith(LIVE_CHECK_UNAVAILABLE_REFUSAL):
        return "无法确认运行中的程序是否在使用"
    return "可能正被运行中的程序使用"


def _temp_folder_reporter(
    reporter: CleanupProgressReporter | None,
    action_index: int,
    action_count: int,
    candidate: ScanCleanupCandidate,
) -> Callable[[DirectoryPurgeProgress], None]:
    def report(progress: DirectoryPurgeProgress) -> None:
        _report_cleanup_progress(
            reporter,
            action_index=action_index,
            action_count=action_count,
            candidate=candidate,
            files_processed=progress.files_removed + progress.links_removed,
            completed=False,
        )

    return report


def _report_cleanup_progress(
    reporter: CleanupProgressReporter | None,
    *,
    action_index: int,
    action_count: int,
    candidate: ScanCleanupCandidate,
    files_processed: int,
    completed: bool,
) -> None:
    """Report progress without allowing presentation failures to stop deletion."""

    if reporter is None:
        return
    try:
        reporter(
            CleanupExecutionProgress(
                action_index=action_index,
                action_count=action_count,
                path=str(candidate.path),
                target_kind=candidate.target_kind,
                files_processed=max(0, files_processed),
                files_total=max(1, candidate.subtree_files),
                completed=completed,
            )
        )
    except Exception:
        return


def _snapshot_from_record(item: TriageItem) -> ExactFileSnapshot:
    record = item.record
    if record.volume_serial is None and record.file_id is None:
        return _snapshot_from_live_read(item)
    if (
        record.volume_serial is None
        or record.file_id is None
        or record.file_id_kind is None
        or record.link_count is None
        or record.creation_time_ns is None
        or record.last_write_time_ns is None
    ):
        raise CleanupRefusal("scan item lacks stable file identity or timestamps")
    if record.file_id_kind not in _ACCEPTED_FILE_ID_KINDS:
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


def _snapshot_selected_directory(item: TriageItem) -> ExactFileSnapshot:
    """Pin the same directory object that produced the visible scan row."""

    record = item.record
    if (
        record.volume_serial is None
        or record.file_id is None
        or record.file_id_kind is None
        or record.creation_time_ns is None
    ):
        raise CleanupRefusal("scan item lacks a stable directory identity")
    if record.file_id_kind not in _ACCEPTED_FILE_ID_KINDS:
        raise CleanupRefusal("cleanup requires a Windows 128-bit directory identity")
    if (record.attributes or 0) & FILE_ATTRIBUTE_REPARSE_POINT or record.reparse_tag is not None:
        raise CleanupRefusal("reparse-point records cannot become cleanup actions")
    expected = ExactDirectorySnapshot(
        volume_serial=int(record.volume_serial),
        file_id=str(record.file_id),
        file_id_kind=str(record.file_id_kind),
        creation_time_ns=int(record.creation_time_ns),
    )
    metadata = read_file_metadata(item.path)
    if not directory_metadata_matches_snapshot(metadata, expected):
        raise CleanupRefusal("selected directory was replaced since the completed scan")
    return _snapshot_from_directory_metadata(metadata, "candidate directory")


def _snapshot_from_live_read(item: TriageItem) -> ExactFileSnapshot:
    """Pin the exact identity of a file the scan observed without opening it."""

    record = item.record
    metadata = read_file_metadata(Path(record.path))
    if metadata.is_reparse_point:
        raise CleanupRefusal("selected file was replaced by a link since the completed scan")
    if (
        metadata.is_directory
        or metadata.is_cloud_placeholder
        or metadata.volume_serial is None
        or metadata.file_id is None
        or metadata.file_id_kind is None
        or metadata.link_count is None
        or metadata.creation_time_ns is None
        or metadata.last_write_time_ns is None
    ):
        raise CleanupRefusal("selected file has no stable ordinary-file identity")
    if metadata.file_id_kind not in _ACCEPTED_FILE_ID_KINDS:
        raise CleanupRefusal("cleanup requires a Windows 128-bit file identity")
    if metadata.link_count != 1:
        raise CleanupRefusal("hard-linked files cannot become cleanup actions")
    if (metadata.attributes or 0) & FILE_ATTRIBUTE_REPARSE_POINT:
        raise CleanupRefusal("reparse-point records cannot become cleanup actions")
    if metadata.logical_size != record.logical_size:
        raise CleanupRefusal("selected file's size changed since the completed scan")
    if (
        record.last_write_time_ns is not None
        and metadata.last_write_time_ns != record.last_write_time_ns
    ):
        raise CleanupRefusal("selected file was modified since the completed scan")
    if record.creation_time_ns is not None and metadata.creation_time_ns != record.creation_time_ns:
        raise CleanupRefusal("selected file was replaced since the completed scan")
    return ExactFileSnapshot(
        logical_size=metadata.logical_size,
        volume_serial=int(metadata.volume_serial),
        file_id=str(metadata.file_id),
        file_id_kind=str(metadata.file_id_kind),
        link_count=int(metadata.link_count),
        attributes=metadata.attributes,
        reparse_tag=metadata.reparse_tag,
        creation_time_ns=int(metadata.creation_time_ns),
        last_write_time_ns=int(metadata.last_write_time_ns),
    )


def _preflight_candidate(
    candidate: ScanCleanupCandidate,
    *,
    delete_config: DeleteClassification,
    keep_config: KeepClassification,
    known_roots: tuple[KnownCleanupRoot, ...] = (),
) -> None:
    temp_folder = candidate.directory_scope is DirectoryScope.TEMP_FOLDER
    refusal = process_guard_detail(candidate.path)
    if refusal is not None:
        raise CleanupRefusal(refusal)
    refusal = live_owner_refusal(
        candidate.path,
        # A temp folder is checked entry by entry while it is emptied; what a
        # running program uses inside it stays, the rest still goes.
        directory=candidate.target_kind is CleanupTargetKind.DIRECTORY and not temp_folder,
        rule_proven=candidate.rule_proven,
    )
    if refusal is not None:
        raise CleanupRefusal(refusal)
    _reject_protected_path(candidate.path, known_roots, keep_config)
    if temp_folder:
        _require_descendant_or_self(candidate.path, candidate.scan_root)
    else:
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
    metadata = read_file_metadata(candidate.path)
    if candidate.target_kind is CleanupTargetKind.DIRECTORY:
        _require_directory_scope(candidate.path, known_roots, delete_config, keep_config)
        if not directory_metadata_matches_snapshot(
            metadata, _directory_identity(candidate.snapshot)
        ):
            raise CleanupRefusal("candidate directory changed since the completed scan")
        if not temp_folder:
            _application_whole_tree_policy(candidate.path, known_roots)
        return
    if not metadata_matches_snapshot(metadata, candidate.snapshot):
        raise CleanupRefusal("candidate changed since the completed scan")
    if installer_ownership(candidate.path) is not None:
        raise CleanupRefusal(INSTALLED_PROGRAM_REFUSAL)


def _intent_for(action: CleanupAction, batch_id: str) -> CleanupIntent:
    candidate = action.candidate
    return CleanupIntent(
        action_id=action.action_id,
        candidate_id=candidate.candidate_id,
        source_path=str(candidate.path),
        scan_root=str(candidate.scan_root),
        scan_root_snapshot=candidate.scan_root_snapshot,
        category=candidate.category.value,
        snapshot=candidate.snapshot,
        target_kind=(
            JournalTargetKind.DIRECTORY
            if candidate.target_kind is CleanupTargetKind.DIRECTORY
            else JournalTargetKind.FILE
        ),
        subtree_files=candidate.subtree_files,
        subtree_bytes=candidate.subtree_bytes,
    )


def _directory_identity(snapshot: ExactFileSnapshot) -> ExactDirectorySnapshot:
    """Project a directory's captured metadata onto its stable identity only."""

    return ExactDirectorySnapshot(
        volume_serial=snapshot.volume_serial,
        file_id=snapshot.file_id,
        file_id_kind=snapshot.file_id_kind,
        creation_time_ns=snapshot.creation_time_ns,
    )


def _execution_boundary(candidate: ScanCleanupCandidate) -> ExactRootBoundary:
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
    try:
        source = _optional_metadata(Path(action.source_path))
    except OSError:
        source = None
        observation_failed = True
    else:
        observation_failed = False
    if action.state is ActionState.PURGE_PENDING or observation_failed:
        state = ActionState.UNKNOWN
    elif metadata_matches_snapshot(source, action.snapshot):
        state = ActionState.FAILED_UNCHANGED
    else:
        state = ActionState.UNKNOWN
    journal.transition(
        action_id,
        expected=(action.state,),
        new_state=state,
        detail="mutation failed; state classified by exact-identity reconciliation",
        error=f"{type(error).__name__}: {error}",
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


def _require_batch(batch: PreparedCleanupBatch) -> None:
    if not isinstance(batch, PreparedCleanupBatch) or batch._seal is not _SEAL:
        raise CleanupRefusal("expected an opaque prepared cleanup batch")
    if not hmac.compare_digest(batch.digest, _batch_digest(batch.batch_id, batch.actions)):
        raise CleanupRefusal("prepared cleanup batch was altered")


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
    target_kind: CleanupTargetKind = CleanupTargetKind.FILE,
    directory_scope: DirectoryScope | None = None,
    subtree_files: int = 0,
    subtree_bytes: int = 0,
    rule_proven: bool = False,
) -> str:
    values = (
        candidate_id,
        _normalized(path),
        _normalized(scan_root),
        repr(scan_root_snapshot),
        repr(snapshot),
        category.value,
        target_kind.value,
        directory_scope.value if directory_scope else "",
        str(subtree_files),
        str(subtree_bytes),
        "rule" if rule_proven else "review",
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


def _require_descendant_or_self(path: Path, root: Path) -> None:
    normalized_path = _normalized(path)
    normalized_root = _normalized(root)
    try:
        common = os.path.commonpath((normalized_path, normalized_root))
    except ValueError as error:
        raise CleanupRefusal("candidate is on a different volume from its scan root") from error
    if common != normalized_root:
        raise CleanupRefusal("candidate must stay inside its original scan root")


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


def _reject_protected_path(
    path: Path,
    known_roots: tuple[KnownCleanupRoot, ...],
    keep_config: KeepClassification,
) -> None:
    _reject_system_anchor(path, known_roots, keep_config)
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
    return _snapshot_from_directory_metadata(metadata, label)


def _snapshot_from_directory_metadata(
    metadata: FileSystemMetadata,
    label: str,
) -> ExactFileSnapshot:
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
        # Windows always reports attribute bits; other hosts have none to report.
        or (metadata.attributes is None and os.name == "nt")
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


def _reject_system_anchor(
    path: Path,
    known_roots: tuple[KnownCleanupRoot, ...],
    keep_config: KeepClassification,
) -> None:
    refusal = system_anchor_refusal(path, known_roots, keep_config)
    if refusal is not None:
        raise CleanupRefusal(refusal)


def _directory_execution_boundary(
    path: Path,
    scan_root: Path,
    known_roots: tuple[KnownCleanupRoot, ...],
) -> Path:
    """Use the parent only for a configured cleanup root that may itself go."""

    if _normalized(path) != _normalized(scan_root):
        return scan_root
    known = known_root_for_path(path, known_roots)
    if (
        known is None
        or _normalized(known.path) != _normalized(path)
        or not known.delete_root_itself
    ):
        raise CleanupRefusal("the scan boundary itself is not a cleanup candidate")
    parent = path.parent
    if parent == path:
        raise CleanupRefusal("a volume root can never become a cleanup candidate")
    return _absolute_local_path(parent, "cleanup-root parent boundary")


__all__ = [
    "INSTALLED_PROGRAM_REFUSAL",
    "MAX_CLEANUP_BATCH_FILES",
    "REVIEW_NOT_APPROVED_REFUSAL",
    "CleanupAction",
    "CleanupExecutionProgress",
    "CleanupExecutionResult",
    "CleanupRefusal",
    "PreparedCleanupBatch",
    "PreparedCleanupPlan",
    "ScanCleanupCandidate",
    "candidate_from_directory_item",
    "candidate_from_triage_item",
    "execute_cleanup_batch",
    "prepare_cleanup_batch",
    "prepare_cleanup_plan",
]
