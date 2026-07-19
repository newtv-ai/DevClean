"""Live-session incremental inventory coordination for GUI callers.

The coordinator treats ``ReadDirectoryChangesW`` notifications as bounded
invalidation hints.  A monitor is started before the initial traversal, hints
that arrive while a generation is being built are reconciled before publish,
and monitor uncertainty always falls back to an ordinary full traversal.

Only an in-memory, atomically replaced tuple is published.  A cancelled or
failed attempt therefore cannot expose a half-updated inventory.  This module
is observational: it contains no cleanup, elevation, or execution capability.
"""

from __future__ import annotations

import errno
import os
from collections.abc import Callable, Iterable, Iterator
from contextlib import suppress
from dataclasses import dataclass, replace
from enum import StrEnum
from threading import Lock
from typing import Protocol, TypeAlias

from devclean.core.incremental import FallbackReason

from .change_monitor import ChangeBatch, ChangeHint, DirectoryChangeMonitor
from .filesystem import (
    CancellationToken,
    ProgressCallback,
    ScanOptions,
    ScanRecord,
    ScanRecordKind,
    ScanStats,
    scan_roots,
)

PathLike: TypeAlias = str | os.PathLike[str]

_MAX_ROOTS = 256
_MAX_INVALIDATED_SUBTREES = 256
_MAX_RECONCILIATION_ROUNDS = 16
_MAX_ERROR_TEXT = 2048


class SessionScanMode(StrEnum):
    """How the published result was produced."""

    FULL = "FULL"
    INCREMENTAL = "INCREMENTAL"
    FALLBACK = "FALLBACK"


class SessionScanStatus(StrEnum):
    """Whether an attempted generation became the visible snapshot."""

    COMMITTED = "COMMITTED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class CoordinatorFallbackReason(StrEnum):
    """Coordinator failures that are not emitted by the monitor itself."""

    NO_BASELINE = "NO_BASELINE"
    NO_LIVE_MONITOR = "NO_LIVE_MONITOR"
    MONITOR_START_FAILED = "MONITOR_START_FAILED"
    INVALIDATION_CAPACITY_EXCEEDED = "INVALIDATION_CAPACITY_EXCEEDED"
    RECONCILIATION_ROUND_LIMIT = "RECONCILIATION_ROUND_LIMIT"
    ACKNOWLEDGEMENT_FAILED = "ACKNOWLEDGEMENT_FAILED"
    SESSION_CLOSED = "SESSION_CLOSED"


@dataclass(frozen=True, slots=True)
class FallbackReport:
    """One bounded reason why a full refresh was or will be required."""

    code: str
    root: str | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or not self.code or len(self.code) > 128:
            raise ValueError("fallback code must be bounded text")
        if self.root is not None and (not self.root or len(self.root) > 32_767):
            raise ValueError("fallback root must be a bounded path")
        if self.detail is not None and len(self.detail) > _MAX_ERROR_TEXT:
            raise ValueError("fallback detail is too long")


@dataclass(frozen=True, slots=True)
class SessionScanStats:
    """GUI-oriented counters for a committed or rejected generation."""

    generation: int
    roots: int
    records: int
    files: int
    directories: int
    boundaries: int
    errors: int
    events_observed: int
    invalidated_subtrees: int
    reconciliation_rounds: int
    records_reobserved: int
    records_reused: int
    incremental_ready: bool


@dataclass(frozen=True, slots=True)
class SessionScanResult:
    """A complete response suitable for handing to a GUI worker boundary."""

    mode: SessionScanMode
    status: SessionScanStatus
    published: bool
    records: tuple[ScanRecord, ...]
    stats: SessionScanStats
    invalidated_paths: tuple[str, ...] = ()
    fallbacks: tuple[FallbackReport, ...] = ()
    error: str | None = None

    def __post_init__(self) -> None:
        if self.published is not (self.status is SessionScanStatus.COMMITTED):
            raise ValueError("only committed session results may be published")
        if self.error is not None and len(self.error) > _MAX_ERROR_TEXT:
            raise ValueError("session error is too long")


class _MonitorLike(Protocol):
    root: str
    session_token: str

    def start(self) -> _MonitorLike: ...

    def wait_until_ready(self, timeout: float | None = None) -> bool: ...

    def read_changes(self, *, session_token: str, after_sequence: int) -> ChangeBatch: ...

    def acknowledge(
        self, *, session_token: str, through_sequence: int
    ) -> FallbackReason | None: ...

    def stop(self, timeout: float = 2.0) -> None: ...


class _MonitorFactory(Protocol):
    def __call__(self, root: str) -> _MonitorLike: ...


class _Scanner(Protocol):
    def __call__(
        self,
        roots: Iterable[PathLike],
        options: ScanOptions | None = None,
        cancel: CancellationToken | None = None,
        progress: ProgressCallback | None = None,
    ) -> Iterator[ScanRecord]: ...


@dataclass(frozen=True, slots=True)
class _ObservedBatch:
    cursors: dict[str, int]
    hints: tuple[tuple[str, ChangeHint], ...]
    fallbacks: tuple[FallbackReport, ...]


@dataclass(frozen=True, slots=True)
class _Reconciliation:
    records: dict[tuple[str, str], ScanRecord]
    cursors: dict[str, int]
    invalidated_paths: tuple[str, ...]
    events_observed: int
    rounds: int
    records_reobserved: int
    fallbacks: tuple[FallbackReport, ...]

    @property
    def requires_full_scan(self) -> bool:
        return bool(self.fallbacks)


class _ScanCancelled(RuntimeError):
    pass


class _InvalidationCapacity(RuntimeError):
    pass


class IncrementalScanSession:
    """Maintain one fail-closed, same-process incremental inventory session.

    Instances are intentionally not persistent.  Closing the process, losing a
    monitor handle, changing the root identity, overflowing a notification
    buffer, or violating a token/sequence invariant makes the next refresh a
    full traversal.  Calls are serialized; GUI code should invoke them from a
    worker thread.
    """

    def __init__(
        self,
        roots: Iterable[PathLike],
        options: ScanOptions | None = None,
        *,
        monitor_ready_timeout: float = 5.0,
        max_invalidated_subtrees: int = _MAX_INVALIDATED_SUBTREES,
        max_reconciliation_rounds: int = _MAX_RECONCILIATION_ROUNDS,
        monitor_factory: _MonitorFactory | None = None,
        scanner: _Scanner = scan_roots,
    ) -> None:
        self.roots = _normalize_roots(roots)
        self.options = options or ScanOptions()
        if monitor_ready_timeout < 0:
            raise ValueError("monitor_ready_timeout cannot be negative")
        if not 1 <= max_invalidated_subtrees <= _MAX_INVALIDATED_SUBTREES:
            raise ValueError("max_invalidated_subtrees is outside its safety bound")
        if not 1 <= max_reconciliation_rounds <= _MAX_RECONCILIATION_ROUNDS:
            raise ValueError("max_reconciliation_rounds is outside its safety bound")
        self.monitor_ready_timeout = monitor_ready_timeout
        self.max_invalidated_subtrees = max_invalidated_subtrees
        self.max_reconciliation_rounds = max_reconciliation_rounds
        self._monitor_factory = monitor_factory or DirectoryChangeMonitor
        self._scanner = scanner

        self._operation_lock = Lock()
        self._monitors: dict[str, _MonitorLike] = {}
        self._cursors: dict[str, int] = {}
        self._records: tuple[ScanRecord, ...] = ()
        self._generation = 0
        self._has_baseline = False
        self._incremental_ready = False
        self._closed = False

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def has_baseline(self) -> bool:
        return self._has_baseline

    @property
    def incremental_ready(self) -> bool:
        return self._incremental_ready

    @property
    def current_records(self) -> tuple[ScanRecord, ...]:
        """Return the last atomically published immutable snapshot."""

        return self._records

    def baseline(
        self,
        *,
        cancel: CancellationToken | None = None,
        progress: ProgressCallback | None = None,
    ) -> SessionScanResult:
        """Start monitors first, then build and publish a complete baseline."""

        return self._serialized_scan(
            lambda: self._run_full(
                mode=SessionScanMode.FULL,
                cancel=cancel or CancellationToken(),
                progress=progress,
                initial_fallbacks=(),
            ),
            attempted_mode=SessionScanMode.FULL,
        )

    def refresh(
        self,
        *,
        cancel: CancellationToken | None = None,
        progress: ProgressCallback | None = None,
    ) -> SessionScanResult:
        """Reobserve only invalidated subtrees, or explicitly fall back full."""

        token = cancel or CancellationToken()

        def run() -> SessionScanResult:
            if not self._has_baseline:
                report = _coordinator_fallback(CoordinatorFallbackReason.NO_BASELINE)
                return self._run_full(
                    mode=SessionScanMode.FALLBACK,
                    cancel=token,
                    progress=progress,
                    initial_fallbacks=(report,),
                )
            if not self._incremental_ready:
                report = _coordinator_fallback(
                    CoordinatorFallbackReason.NO_LIVE_MONITOR,
                    detail="no trustworthy live-session cursor covers the published baseline",
                )
                return self._run_full(
                    mode=SessionScanMode.FALLBACK,
                    cancel=token,
                    progress=progress,
                    initial_fallbacks=(report,),
                )
            return self._run_incremental(cancel=token, progress=progress)

        return self._serialized_scan(run, attempted_mode=SessionScanMode.INCREMENTAL)

    def close(self) -> None:
        """Stop all live handles; a closed session cannot be restarted."""

        with self._operation_lock:
            if self._closed:
                return
            self._closed = True
            self._stop_monitors()

    def __enter__(self) -> IncrementalScanSession:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _serialized_scan(
        self,
        operation: Callable[[], SessionScanResult],
        *,
        attempted_mode: SessionScanMode,
    ) -> SessionScanResult:
        with self._operation_lock:
            if self._closed:
                fallback = _coordinator_fallback(CoordinatorFallbackReason.SESSION_CLOSED)
                return self._rejected_result(
                    attempted_mode,
                    SessionScanStatus.FAILED,
                    fallbacks=(fallback,),
                    error="incremental scan session is closed",
                )
            try:
                return operation()
            except _ScanCancelled:
                return self._rejected_result(attempted_mode, SessionScanStatus.CANCELLED)
            except BaseException as error:
                return self._rejected_result(
                    attempted_mode,
                    SessionScanStatus.FAILED,
                    error=_bounded_error(error),
                )

    def _run_full(
        self,
        *,
        mode: SessionScanMode,
        cancel: CancellationToken,
        progress: ProgressCallback | None,
        initial_fallbacks: tuple[FallbackReport, ...],
    ) -> SessionScanResult:
        monitor_fallbacks = self._replace_monitors()
        fallbacks = (*initial_fallbacks, *monitor_fallbacks)
        try:
            return self._finish_full_scan(
                mode=mode,
                cancel=cancel,
                progress=progress,
                fallbacks=fallbacks,
            )
        except BaseException:
            # A replacement monitor starts with cursor zero and cannot be
            # attached to an older published snapshot.  If this full attempt
            # does not commit, discard those handles as well as its staged map.
            self._stop_monitors()
            raise

    def _finish_full_scan(
        self,
        *,
        mode: SessionScanMode,
        cancel: CancellationToken,
        progress: ProgressCallback | None,
        fallbacks: tuple[FallbackReport, ...],
    ) -> SessionScanResult:
        staged, observed = self._scan_complete_roots(cancel=cancel, progress=progress)
        cursors = {root: 0 for root in self._monitors}
        invalidated: tuple[str, ...] = ()
        events = 0
        rounds = 0
        reobserved = observed

        if self._incremental_ready:
            reconciled = self._reconcile(
                staged,
                cursors,
                cancel=cancel,
                progress=progress,
            )
            if reconciled.requires_full_scan:
                # The change stream cannot prove a safe cut.  Throw away the
                # partially reconciled map and publish only a fresh ordinary
                # full traversal.  No cursor from the failed stream is reused.
                fallbacks = (*fallbacks, *reconciled.fallbacks)
                self._stop_monitors()
                staged, observed = self._scan_complete_roots(cancel=cancel, progress=progress)
                reobserved += observed
                cursors = {}
                invalidated = reconciled.invalidated_paths
                events = reconciled.events_observed
                rounds = reconciled.rounds
            else:
                staged = reconciled.records
                cursors = reconciled.cursors
                invalidated = reconciled.invalidated_paths
                events = reconciled.events_observed
                rounds = reconciled.rounds
                reobserved += reconciled.records_reobserved

        prior = self._records
        records = self._finalize_records(staged)
        self._publish(records)
        acknowledgement_fallbacks = self._acknowledge(cursors)
        fallbacks = (*fallbacks, *acknowledgement_fallbacks)
        return self._committed_result(
            mode,
            records,
            prior=prior,
            invalidated=invalidated,
            fallbacks=fallbacks,
            events=events,
            rounds=rounds,
            reobserved=reobserved,
        )

    def _run_incremental(
        self,
        *,
        cancel: CancellationToken,
        progress: ProgressCallback | None,
    ) -> SessionScanResult:
        staged = {_record_key(record): record for record in self._records}
        reconciled = self._reconcile(
            staged,
            dict(self._cursors),
            cancel=cancel,
            progress=progress,
        )
        if reconciled.requires_full_scan:
            return self._run_full(
                mode=SessionScanMode.FALLBACK,
                cancel=cancel,
                progress=progress,
                initial_fallbacks=reconciled.fallbacks,
            )

        prior = self._records
        records = self._finalize_records(reconciled.records)
        self._publish(records)
        acknowledgement_fallbacks = self._acknowledge(reconciled.cursors)
        return self._committed_result(
            SessionScanMode.INCREMENTAL,
            records,
            prior=prior,
            invalidated=reconciled.invalidated_paths,
            fallbacks=acknowledgement_fallbacks,
            events=reconciled.events_observed,
            rounds=reconciled.rounds,
            reobserved=reconciled.records_reobserved,
        )

    def _replace_monitors(self) -> tuple[FallbackReport, ...]:
        self._stop_monitors()
        monitors: dict[str, _MonitorLike] = {}
        fallbacks: list[FallbackReport] = []
        for root in self.roots:
            try:
                monitor = self._monitor_factory(root).start()
                monitors[root] = monitor
            except BaseException as error:
                fallbacks.append(
                    _coordinator_fallback(
                        CoordinatorFallbackReason.MONITOR_START_FAILED,
                        root=root,
                        detail=_bounded_error(error),
                    )
                )

        for root, monitor in monitors.items():
            try:
                ready = monitor.wait_until_ready(self.monitor_ready_timeout)
                batch = monitor.read_changes(
                    session_token=monitor.session_token,
                    after_sequence=0,
                )
            except BaseException as error:
                fallbacks.append(
                    _coordinator_fallback(
                        CoordinatorFallbackReason.MONITOR_START_FAILED,
                        root=root,
                        detail=_bounded_error(error),
                    )
                )
                continue
            identity_fallback = _validate_batch_identity(
                root,
                monitor.session_token,
                batch,
                expected_after=0,
            )
            if identity_fallback is not None:
                fallbacks.append(identity_fallback)
            elif not ready or batch.requires_full_scan:
                code = (
                    batch.fallback_reason.value
                    if batch.fallback_reason is not None
                    else FallbackReason.MONITOR_NOT_READY.value
                )
                fallbacks.append(FallbackReport(code, root, batch.detail))

        if fallbacks or len(monitors) != len(self.roots):
            for monitor in monitors.values():
                _best_effort_stop(monitor)
            self._monitors = {}
            self._cursors = {}
            self._incremental_ready = False
            return tuple(fallbacks)

        self._monitors = monitors
        self._cursors = {root: 0 for root in monitors}
        self._incremental_ready = True
        return ()

    def _stop_monitors(self) -> None:
        for monitor in self._monitors.values():
            _best_effort_stop(monitor)
        self._monitors = {}
        self._cursors = {}
        self._incremental_ready = False

    def _scan_complete_roots(
        self,
        *,
        cancel: CancellationToken,
        progress: ProgressCallback | None,
    ) -> tuple[dict[tuple[str, str], ScanRecord], int]:
        raw_options = replace(self.options, deduplicate_hardlinks=False)
        records = self._collect_scan(
            self.roots,
            options=raw_options,
            cancel=cancel,
            progress=progress,
        )
        mapped: dict[tuple[str, str], ScanRecord] = {}
        for record in records:
            rebased = self._rebase_record(record, configured_root=record.root)
            mapped[_record_key(rebased)] = rebased
        return mapped, len(records)

    def _collect_scan(
        self,
        roots: Iterable[PathLike],
        *,
        options: ScanOptions,
        cancel: CancellationToken,
        progress: ProgressCallback | None,
    ) -> list[ScanRecord]:
        final_stats: ScanStats | None = None

        def capture(stats: ScanStats) -> None:
            nonlocal final_stats
            final_stats = stats
            if progress is not None:
                progress(stats)

        records = list(
            self._scanner(
                roots,
                options=options,
                cancel=cancel,
                progress=capture,
            )
        )
        if final_stats is None:
            raise RuntimeError("scanner omitted its terminal progress snapshot")
        if cancel.is_cancelled() or final_stats.cancelled:
            raise _ScanCancelled
        if not final_stats.completed:
            raise RuntimeError("scanner ended without a complete terminal progress snapshot")
        return records

    def _reconcile(
        self,
        staged: dict[tuple[str, str], ScanRecord],
        cursors: dict[str, int],
        *,
        cancel: CancellationToken,
        progress: ProgressCallback | None,
    ) -> _Reconciliation:
        invalidated: set[str] = set()
        events = 0
        rounds = 0
        reobserved = 0
        working = dict(staged)

        while True:
            if cancel.is_cancelled():
                raise _ScanCancelled
            observed = self._read_batches(cursors)
            if observed.fallbacks:
                return _Reconciliation(
                    working,
                    cursors,
                    tuple(sorted(invalidated, key=_path_sort_key)),
                    events,
                    rounds,
                    reobserved,
                    observed.fallbacks,
                )
            cursors = observed.cursors
            if not observed.hints:
                return _Reconciliation(
                    working,
                    cursors,
                    tuple(sorted(invalidated, key=_path_sort_key)),
                    events,
                    rounds,
                    reobserved,
                    (),
                )
            events += len(observed.hints)
            rounds += 1
            if rounds > self.max_reconciliation_rounds:
                fallback = _coordinator_fallback(
                    CoordinatorFallbackReason.RECONCILIATION_ROUND_LIMIT,
                    detail="change stream did not quiesce within the bounded round count",
                )
                return _Reconciliation(
                    working,
                    cursors,
                    tuple(sorted(invalidated, key=_path_sort_key)),
                    events,
                    rounds,
                    reobserved,
                    (fallback,),
                )
            try:
                subtrees = self._invalidation_subtrees(observed.hints)
            except _InvalidationCapacity:
                fallback = _coordinator_fallback(
                    CoordinatorFallbackReason.INVALIDATION_CAPACITY_EXCEEDED,
                    detail="distinct invalidated subtree count exceeded its hard bound",
                )
                return _Reconciliation(
                    working,
                    cursors,
                    tuple(sorted(invalidated, key=_path_sort_key)),
                    events,
                    rounds,
                    reobserved,
                    (fallback,),
                )
            invalidated.update(path for _, path in subtrees)
            observed_count = self._replace_subtrees(
                working,
                subtrees,
                cancel=cancel,
                progress=progress,
            )
            reobserved += observed_count

    def _read_batches(self, cursors: dict[str, int]) -> _ObservedBatch:
        next_cursors = dict(cursors)
        hints: list[tuple[str, ChangeHint]] = []
        fallbacks: list[FallbackReport] = []
        for root, monitor in self._monitors.items():
            after = cursors.get(root)
            if after is None:
                fallbacks.append(
                    FallbackReport(
                        FallbackReason.SESSION_SEQUENCE_GAP.value,
                        root,
                        "monitor cursor is missing for an approved root",
                    )
                )
                continue
            try:
                batch = monitor.read_changes(
                    session_token=monitor.session_token,
                    after_sequence=after,
                )
            except BaseException as error:
                fallbacks.append(
                    FallbackReport(
                        FallbackReason.MONITOR_IO_ERROR.value,
                        root,
                        _bounded_error(error),
                    )
                )
                continue
            identity_fallback = _validate_batch_identity(
                root,
                monitor.session_token,
                batch,
                expected_after=after,
            )
            if identity_fallback is not None:
                fallbacks.append(identity_fallback)
                continue
            if batch.requires_full_scan:
                code = (
                    batch.fallback_reason.value
                    if batch.fallback_reason is not None
                    else FallbackReason.MONITOR_IO_ERROR.value
                )
                fallbacks.append(FallbackReport(code, root, batch.detail))
                continue
            if batch.from_sequence != after or batch.through_sequence < after:
                fallbacks.append(
                    FallbackReport(
                        FallbackReason.SESSION_SEQUENCE_GAP.value,
                        root,
                        "monitor returned a non-contiguous sequence range",
                    )
                )
                continue
            next_cursors[root] = batch.through_sequence
            hints.extend((root, hint) for hint in batch.hints)
        return _ObservedBatch(next_cursors, tuple(hints), tuple(fallbacks))

    def _invalidation_subtrees(
        self, hints: tuple[tuple[str, ChangeHint], ...]
    ) -> tuple[tuple[str, str], ...]:
        candidates: list[tuple[str, str]] = []
        for root, hint in hints:
            relative_parts = hint.relative_path.split("\\")
            changed = os.path.abspath(os.path.join(root, *relative_parts))
            if not _is_within(changed, root) or _same_path(changed, root):
                parent = root
            else:
                parent = os.path.dirname(changed)
                while not _same_path(parent, root) and not os.path.lexists(parent):
                    parent = os.path.dirname(parent)
                if not _is_within(parent, root):
                    parent = root
            candidates.append((root, parent))

        # Parent subtrees are conservative: rescanning one refreshes the
        # directory record, membership, and all descendants.  Keep only the
        # shallowest ancestor when hints overlap.
        result: list[tuple[str, str]] = []
        for root, path in sorted(
            set(candidates),
            key=lambda pair: (
                _root_rank(pair[0], self.roots),
                *_path_sort_key(pair[1]),
            ),
        ):
            if any(
                existing_root == root and _is_within(path, existing)
                for existing_root, existing in result
            ):
                continue
            result.append((root, path))
            if len(result) > self.max_invalidated_subtrees:
                raise _InvalidationCapacity
        return tuple(result)

    def _replace_subtrees(
        self,
        working: dict[tuple[str, str], ScanRecord],
        subtrees: tuple[tuple[str, str], ...],
        *,
        cancel: CancellationToken,
        progress: ProgressCallback | None,
    ) -> int:
        raw_options = replace(self.options, deduplicate_hardlinks=False)
        observed = 0
        for configured_root, subtree in subtrees:
            if cancel.is_cancelled():
                raise _ScanCancelled
            for key, record in tuple(working.items()):
                if _is_within(record.path, subtree):
                    del working[key]
            records = self._collect_scan(
                (subtree,),
                options=raw_options,
                cancel=cancel,
                progress=progress,
            )
            observed += len(records)
            for record in records:
                # A vanished non-root subtree is ordinary removal, not a new
                # ERROR row that an independent traversal would have emitted.
                if (
                    not _same_path(subtree, configured_root)
                    and record.kind is ScanRecordKind.ERROR
                    and _same_path(record.path, subtree)
                    and record.error_code in {errno.ENOENT, 2, 3}
                ):
                    continue
                rebased = self._rebase_record(record, configured_root=configured_root)
                working[_record_key(rebased)] = rebased
        return observed

    def _rebase_record(self, record: ScanRecord, *, configured_root: str) -> ScanRecord:
        if not _is_within(record.path, configured_root):
            raise RuntimeError("scanner returned a path outside its configured root")
        relative = os.path.relpath(record.path, configured_root)
        depth = 0 if relative == os.curdir else len(relative.split(os.sep))
        return replace(record, root=configured_root, depth=depth)

    def _finalize_records(
        self, records: dict[tuple[str, str], ScanRecord]
    ) -> tuple[ScanRecord, ...]:
        ordered = sorted(
            records.values(),
            key=lambda record: (
                _root_rank(record.root, self.roots),
                *_path_sort_key(record.path),
                _kind_rank(record.kind),
            ),
        )
        if not self.options.deduplicate_hardlinks:
            return tuple(
                replace(
                    record,
                    allocated_size=record.raw_allocated_size,
                    hardlink_duplicate=False,
                    allocation_uncertain=False,
                )
                if record.kind is ScanRecordKind.FILE
                else record
                for record in ordered
            )

        seen: set[tuple[int, str]] = set()
        finalized: list[ScanRecord] = []
        for record in ordered:
            if record.kind is not ScanRecordKind.FILE or record.raw_allocated_size is None:
                finalized.append(record)
                continue
            if record.link_count is None or record.link_count <= 1:
                finalized.append(
                    replace(
                        record,
                        allocated_size=record.raw_allocated_size,
                        hardlink_duplicate=False,
                        allocation_uncertain=False,
                    )
                )
                continue
            if record.volume_serial is None or record.file_id is None:
                finalized.append(
                    replace(
                        record,
                        allocated_size=None,
                        hardlink_duplicate=False,
                        allocation_uncertain=True,
                    )
                )
                continue
            identity = (record.volume_serial, record.file_id)
            if identity in seen:
                finalized.append(
                    replace(
                        record,
                        allocated_size=0,
                        hardlink_duplicate=True,
                        allocation_uncertain=False,
                    )
                )
            elif len(seen) >= self.options.max_hardlink_identities:
                finalized.append(
                    replace(
                        record,
                        allocated_size=None,
                        hardlink_duplicate=False,
                        allocation_uncertain=True,
                    )
                )
            else:
                seen.add(identity)
                finalized.append(
                    replace(
                        record,
                        allocated_size=record.raw_allocated_size,
                        hardlink_duplicate=False,
                        allocation_uncertain=False,
                    )
                )
        return tuple(finalized)

    def _publish(self, records: tuple[ScanRecord, ...]) -> None:
        # One immutable reference replacement is the only visibility point.
        self._records = records
        self._generation += 1
        self._has_baseline = True

    def _acknowledge(self, cursors: dict[str, int]) -> tuple[FallbackReport, ...]:
        if not self._incremental_ready:
            return ()
        fallbacks: list[FallbackReport] = []
        for root, monitor in self._monitors.items():
            through = cursors.get(root)
            if through is None:
                fallbacks.append(
                    _coordinator_fallback(
                        CoordinatorFallbackReason.ACKNOWLEDGEMENT_FAILED,
                        root=root,
                        detail="published generation has no cursor for this root",
                    )
                )
                continue
            try:
                reason = monitor.acknowledge(
                    session_token=monitor.session_token,
                    through_sequence=through,
                )
            except BaseException as error:
                fallbacks.append(
                    _coordinator_fallback(
                        CoordinatorFallbackReason.ACKNOWLEDGEMENT_FAILED,
                        root=root,
                        detail=_bounded_error(error),
                    )
                )
                continue
            if reason is not None:
                fallbacks.append(FallbackReport(reason.value, root, "monitor rejected cursor"))
        if fallbacks:
            self._stop_monitors()
        else:
            self._cursors = dict(cursors)
        return tuple(fallbacks)

    def _committed_result(
        self,
        mode: SessionScanMode,
        records: tuple[ScanRecord, ...],
        *,
        prior: tuple[ScanRecord, ...],
        invalidated: tuple[str, ...],
        fallbacks: tuple[FallbackReport, ...],
        events: int,
        rounds: int,
        reobserved: int,
    ) -> SessionScanResult:
        prior_map = {_record_key(record): record for record in prior}
        reused = sum(prior_map.get(_record_key(record)) == record for record in records)
        stats = _stats_for(
            records,
            generation=self._generation,
            roots=len(self.roots),
            events=events,
            invalidated=len(invalidated),
            rounds=rounds,
            reobserved=reobserved,
            reused=reused,
            incremental_ready=self._incremental_ready,
        )
        return SessionScanResult(
            mode,
            SessionScanStatus.COMMITTED,
            True,
            records,
            stats,
            invalidated,
            fallbacks,
        )

    def _rejected_result(
        self,
        mode: SessionScanMode,
        status: SessionScanStatus,
        *,
        fallbacks: tuple[FallbackReport, ...] = (),
        error: str | None = None,
    ) -> SessionScanResult:
        stats = _stats_for(
            self._records,
            generation=self._generation,
            roots=len(self.roots),
            incremental_ready=self._incremental_ready,
        )
        return SessionScanResult(
            mode,
            status,
            False,
            self._records,
            stats,
            fallbacks=fallbacks,
            error=error,
        )


def _normalize_roots(roots: Iterable[PathLike]) -> tuple[str, ...]:
    candidates: dict[str, str] = {}
    for raw in roots:
        root = os.path.abspath(os.fspath(raw))
        if not root or len(root) > 32_767:
            raise ValueError("scan root must be a bounded absolute path")
        candidates.setdefault(os.path.normcase(root), root)
        if len(candidates) > _MAX_ROOTS:
            raise ValueError("scan root count exceeds its hard bound")
    if not candidates:
        raise ValueError("at least one scan root is required")
    ordered = sorted(
        candidates.values(),
        key=lambda path: (len(_path_parts(path)), *_path_sort_key(path)),
    )
    result: list[str] = []
    for candidate in ordered:
        if any(_is_within(candidate, root) for root in result):
            continue
        result.append(candidate)
    return tuple(result)


def _record_key(record: ScanRecord) -> tuple[str, str]:
    return (os.path.normcase(os.path.abspath(record.path)), record.kind.value)


def _is_within(path: str, root: str) -> bool:
    try:
        return os.path.normcase(os.path.commonpath((path, root))) == os.path.normcase(
            os.path.abspath(root)
        )
    except ValueError:
        return False


def _same_path(left: str, right: str) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def _path_parts(path: str) -> tuple[str, ...]:
    drive, tail = os.path.splitdrive(os.path.abspath(path))
    return (drive, *tuple(part for part in tail.replace("\\", "/").split("/") if part))


def _path_sort_key(path: str) -> tuple[int, str]:
    absolute = os.path.abspath(path)
    return (len(_path_parts(absolute)), os.path.normcase(absolute))


def _root_rank(root: str, roots: tuple[str, ...]) -> int:
    normalized = os.path.normcase(os.path.abspath(root))
    for index, candidate in enumerate(roots):
        if os.path.normcase(candidate) == normalized:
            return index
    return len(roots)


def _kind_rank(kind: ScanRecordKind) -> int:
    return {
        ScanRecordKind.DIRECTORY: 0,
        ScanRecordKind.FILE: 1,
        ScanRecordKind.BOUNDARY: 2,
        ScanRecordKind.ERROR: 3,
    }[kind]


def _stats_for(
    records: tuple[ScanRecord, ...],
    *,
    generation: int,
    roots: int,
    events: int = 0,
    invalidated: int = 0,
    rounds: int = 0,
    reobserved: int = 0,
    reused: int = 0,
    incremental_ready: bool,
) -> SessionScanStats:
    return SessionScanStats(
        generation=generation,
        roots=roots,
        records=len(records),
        files=sum(record.kind is ScanRecordKind.FILE for record in records),
        directories=sum(record.kind is ScanRecordKind.DIRECTORY for record in records),
        boundaries=sum(record.kind is ScanRecordKind.BOUNDARY for record in records),
        errors=sum(record.kind is ScanRecordKind.ERROR for record in records),
        events_observed=events,
        invalidated_subtrees=invalidated,
        reconciliation_rounds=rounds,
        records_reobserved=reobserved,
        records_reused=reused,
        incremental_ready=incremental_ready,
    )


def _coordinator_fallback(
    reason: CoordinatorFallbackReason,
    *,
    root: str | None = None,
    detail: str | None = None,
) -> FallbackReport:
    return FallbackReport(reason.value, root, detail)


def _validate_batch_identity(
    root: str,
    session_token: str,
    batch: ChangeBatch,
    *,
    expected_after: int,
) -> FallbackReport | None:
    if batch.session_token != session_token:
        return FallbackReport(
            FallbackReason.SESSION_TOKEN_MISMATCH.value,
            root,
            "monitor response token differs from its live session",
        )
    if not _same_path(batch.root, root):
        return FallbackReport(
            FallbackReason.ROOT_CHANGED.value,
            root,
            "monitor response root differs from the approved session root",
        )
    if batch.from_sequence != expected_after or batch.through_sequence < expected_after:
        return FallbackReport(
            FallbackReason.SESSION_SEQUENCE_GAP.value,
            root,
            "monitor returned a non-contiguous sequence range",
        )
    return None


def _bounded_error(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"[:_MAX_ERROR_TEXT]


def _best_effort_stop(monitor: _MonitorLike) -> None:
    with suppress(BaseException):
        monitor.stop()


__all__ = [
    "CoordinatorFallbackReason",
    "FallbackReport",
    "IncrementalScanSession",
    "SessionScanMode",
    "SessionScanResult",
    "SessionScanStats",
    "SessionScanStatus",
]
