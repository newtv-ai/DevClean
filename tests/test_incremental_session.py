from __future__ import annotations

import os
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from devclean.core.incremental import FallbackReason
from devclean.scanner import (
    CancellationToken,
    ChangeAction,
    ChangeBatch,
    ChangeHint,
    IncrementalScanSession,
    MonitorState,
    ScanOptions,
    ScanRecord,
    SessionScanMode,
    SessionScanStatus,
    scan_roots,
)


class _FakeMonitor:
    def __init__(self, root: str, factory: _FakeMonitorFactory) -> None:
        self.root = os.path.abspath(root)
        self.session_token = f"session_{uuid4().hex}"
        self.factory = factory
        self.state = MonitorState.NEW
        self.sequence = 0
        self.acknowledged = 0
        self.hints: list[ChangeHint] = []
        self.failure: FallbackReason | None = None
        self.ack_failure: FallbackReason | None = None
        self.started_before_scan = False

    def start(self) -> _FakeMonitor:
        self.state = MonitorState.ACTIVE
        self.started_before_scan = self.factory.scan_calls == 0
        return self

    def wait_until_ready(self, timeout: float | None = None) -> bool:
        del timeout
        return self.state is MonitorState.ACTIVE

    def emit(self, action: ChangeAction, relative_path: str) -> None:
        self.sequence += 1
        self.hints.append(ChangeHint(self.sequence, action, relative_path))

    def fail(self, reason: FallbackReason) -> None:
        self.failure = reason
        if reason not in {
            FallbackReason.SESSION_TOKEN_MISMATCH,
            FallbackReason.SESSION_SEQUENCE_GAP,
            FallbackReason.SESSION_SEQUENCE_REGRESSION,
        }:
            self.state = MonitorState.FAILED

    def read_changes(self, *, session_token: str, after_sequence: int) -> ChangeBatch:
        if session_token != self.session_token:
            return ChangeBatch(
                self.session_token,
                self.root,
                MonitorState.ACTIVE,
                self.sequence,
                self.sequence,
                self.acknowledged,
                (),
                FallbackReason.SESSION_TOKEN_MISMATCH,
                "fake token mismatch",
            )
        if self.failure is not None:
            return ChangeBatch(
                self.session_token,
                self.root,
                self.state,
                min(after_sequence, self.sequence),
                self.sequence,
                self.acknowledged,
                tuple(hint for hint in self.hints if hint.sequence > after_sequence),
                self.failure,
                "injected monitor failure",
            )
        return ChangeBatch(
            self.session_token,
            self.root,
            self.state,
            after_sequence,
            self.sequence,
            self.acknowledged,
            tuple(hint for hint in self.hints if hint.sequence > after_sequence),
        )

    def acknowledge(
        self, *, session_token: str, through_sequence: int
    ) -> FallbackReason | None:
        if session_token != self.session_token:
            return FallbackReason.SESSION_TOKEN_MISMATCH
        if self.ack_failure is not None:
            return self.ack_failure
        self.acknowledged = through_sequence
        self.hints = [hint for hint in self.hints if hint.sequence > through_sequence]
        return None

    def stop(self, timeout: float = 2.0) -> None:
        del timeout
        self.state = MonitorState.STOPPED


class _FakeMonitorFactory:
    def __init__(self) -> None:
        self.monitors: list[_FakeMonitor] = []
        self.scan_calls = 0

    def __call__(self, root: str) -> _FakeMonitor:
        monitor = _FakeMonitor(root, self)
        self.monitors.append(monitor)
        return monitor

    def latest(self, root: Path | str | None = None) -> _FakeMonitor:
        if root is None:
            return self.monitors[-1]
        normalized = os.path.normcase(os.path.abspath(root))
        return next(
            monitor
            for monitor in reversed(self.monitors)
            if os.path.normcase(monitor.root) == normalized
        )


class _CountingScanner:
    def __init__(self, factory: _FakeMonitorFactory) -> None:
        self.factory = factory
        self.after_record: Any = None

    def __call__(
        self,
        roots: Iterable[str | os.PathLike[str]],
        options: ScanOptions | None = None,
        cancel: CancellationToken | None = None,
        progress: Any = None,
    ) -> Iterator[ScanRecord]:
        self.factory.scan_calls += 1
        call = self.factory.scan_calls
        triggered = False
        for record in scan_roots(roots, options, cancel, progress):
            yield record
            if not triggered and self.after_record is not None:
                triggered = bool(self.after_record(call, record))


def _record_map(records: Iterable[ScanRecord]) -> dict[tuple[str, str], ScanRecord]:
    return {
        (os.path.normcase(os.path.abspath(record.path)), record.kind.value): record
        for record in records
    }


def _file(records: Iterable[ScanRecord], name: str) -> ScanRecord:
    return next(record for record in records if Path(record.path).name == name)


def test_baseline_starts_monitors_before_scan_and_reconciles_inflight_change(
    tmp_path: Path,
) -> None:
    sample = tmp_path / "sample.bin"
    sample.write_bytes(b"old")
    factory = _FakeMonitorFactory()
    scanner = _CountingScanner(factory)

    def mutate_after_stale_file(call: int, record: ScanRecord) -> bool:
        if call != 1 or Path(record.path) != sample:
            return False
        sample.write_bytes(b"new-content")
        factory.latest(tmp_path).emit(ChangeAction.MODIFIED, sample.name)
        return True

    scanner.after_record = mutate_after_stale_file
    session = IncrementalScanSession(
        [tmp_path],
        monitor_factory=factory,
        scanner=scanner,
    )

    result = session.baseline()

    assert result.status is SessionScanStatus.COMMITTED
    assert result.mode is SessionScanMode.FULL
    assert result.published is True
    assert _file(result.records, sample.name).logical_size == len(b"new-content")
    assert result.stats.events_observed >= 1
    assert result.stats.reconciliation_rounds >= 1
    assert result.stats.incremental_ready is True
    assert scanner.factory.scan_calls >= 2
    assert all(monitor.started_before_scan for monitor in factory.monitors)


def test_incremental_refresh_matches_independent_full_after_add_modify_rename_delete(
    tmp_path: Path,
) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    original = nested / "before.bin"
    original.write_bytes(b"before")
    factory = _FakeMonitorFactory()
    session = IncrementalScanSession([tmp_path], monitor_factory=factory)
    assert session.baseline().status is SessionScanStatus.COMMITTED
    monitor = factory.latest(tmp_path)

    added = nested / "temporary.bin"
    added.write_bytes(b"temporary")
    monitor.emit(ChangeAction.ADDED, r"nested\temporary.bin")
    original.write_bytes(b"modified payload")
    monitor.emit(ChangeAction.MODIFIED, r"nested\before.bin")
    renamed = nested / "after.bin"
    original.rename(renamed)
    monitor.emit(ChangeAction.RENAMED_OLD_NAME, r"nested\before.bin")
    monitor.emit(ChangeAction.RENAMED_NEW_NAME, r"nested\after.bin")
    added.unlink()
    monitor.emit(ChangeAction.REMOVED, r"nested\temporary.bin")

    result = session.refresh()
    independent = tuple(scan_roots([tmp_path]))

    assert result.mode is SessionScanMode.INCREMENTAL
    assert result.status is SessionScanStatus.COMMITTED
    assert _record_map(result.records) == _record_map(independent)
    assert result.stats.events_observed == 5
    assert result.stats.invalidated_subtrees == 1
    assert result.stats.records_reused >= 1
    assert result.invalidated_paths == (os.path.abspath(nested),)


def test_multiple_and_overlapping_roots_are_deduplicated(tmp_path: Path) -> None:
    first = tmp_path / "first"
    nested = first / "nested"
    second = tmp_path / "second"
    nested.mkdir(parents=True)
    second.mkdir()
    (nested / "one.bin").write_bytes(b"one")
    (second / "two.bin").write_bytes(b"two")
    factory = _FakeMonitorFactory()

    session = IncrementalScanSession(
        [nested, first, first, second],
        monitor_factory=factory,
    )
    result = session.baseline()

    assert session.roots == (os.path.abspath(first), os.path.abspath(second))
    assert result.stats.roots == 2
    assert len(factory.monitors) == 2
    assert len(_record_map(result.records)) == len(result.records)
    assert {Path(record.path).name for record in result.records} >= {
        "first",
        "nested",
        "one.bin",
        "second",
        "two.bin",
    }


@pytest.mark.parametrize(
    "reason",
    [
        FallbackReason.MONITOR_BUFFER_OVERFLOW,
        FallbackReason.MONITOR_HANDLE_LOST,
        FallbackReason.MONITOR_CAPACITY_EXCEEDED,
        FallbackReason.SESSION_TOKEN_MISMATCH,
        FallbackReason.SESSION_SEQUENCE_GAP,
        FallbackReason.SESSION_SEQUENCE_REGRESSION,
        FallbackReason.ROOT_CHANGED,
    ],
)
def test_monitor_uncertainty_discards_incremental_stage_and_full_falls_back(
    tmp_path: Path,
    reason: FallbackReason,
) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    sample = nested / "payload.bin"
    sample.write_bytes(b"old")
    factory = _FakeMonitorFactory()
    session = IncrementalScanSession([tmp_path], monitor_factory=factory)
    session.baseline()
    old_monitor = factory.latest(tmp_path)
    sample.write_bytes(b"replacement payload")
    old_monitor.fail(reason)

    result = session.refresh()

    assert result.mode is SessionScanMode.FALLBACK
    assert result.status is SessionScanStatus.COMMITTED
    assert _record_map(result.records) == _record_map(scan_roots([tmp_path]))
    assert reason.value in {fallback.code for fallback in result.fallbacks}
    assert factory.latest(tmp_path) is not old_monitor
    assert result.stats.incremental_ready is True


def test_distinct_invalidation_capacity_forces_full_fallback(tmp_path: Path) -> None:
    one = tmp_path / "one"
    two = tmp_path / "two"
    one.mkdir()
    two.mkdir()
    (one / "a.bin").write_bytes(b"a")
    (two / "b.bin").write_bytes(b"b")
    factory = _FakeMonitorFactory()
    session = IncrementalScanSession(
        [tmp_path],
        max_invalidated_subtrees=1,
        monitor_factory=factory,
    )
    session.baseline()
    monitor = factory.latest(tmp_path)
    monitor.emit(ChangeAction.MODIFIED, r"one\a.bin")
    monitor.emit(ChangeAction.MODIFIED, r"two\b.bin")

    result = session.refresh()

    assert result.mode is SessionScanMode.FALLBACK
    assert "INVALIDATION_CAPACITY_EXCEEDED" in {
        fallback.code for fallback in result.fallbacks
    }


def test_reconciliation_round_limit_forces_full_fallback(tmp_path: Path) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    sample = nested / "payload.bin"
    sample.write_bytes(b"zero")
    factory = _FakeMonitorFactory()
    scanner = _CountingScanner(factory)
    session = IncrementalScanSession(
        [tmp_path],
        max_reconciliation_rounds=1,
        monitor_factory=factory,
        scanner=scanner,
    )
    session.baseline()
    first_monitor = factory.latest(tmp_path)
    sample.write_bytes(b"one")
    first_monitor.emit(ChangeAction.MODIFIED, r"nested\payload.bin")

    def keep_first_monitor_busy(call: int, record: ScanRecord) -> bool:
        if call != 2 or Path(record.path) != sample:
            return False
        sample.write_bytes(b"two")
        first_monitor.emit(ChangeAction.MODIFIED, r"nested\payload.bin")
        return True

    scanner.after_record = keep_first_monitor_busy
    result = session.refresh()

    assert result.mode is SessionScanMode.FALLBACK
    assert "RECONCILIATION_ROUND_LIMIT" in {
        fallback.code for fallback in result.fallbacks
    }
    assert _file(result.records, sample.name).logical_size == 3


def test_cancelled_incremental_attempt_does_not_publish_or_ack_and_can_retry(
    tmp_path: Path,
) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    sample = nested / "payload.bin"
    sample.write_bytes(b"old")
    factory = _FakeMonitorFactory()
    session = IncrementalScanSession([tmp_path], monitor_factory=factory)
    baseline = session.baseline()
    generation = session.generation
    old_records = session.current_records
    monitor = factory.latest(tmp_path)
    sample.write_bytes(b"new payload")
    monitor.emit(ChangeAction.MODIFIED, r"nested\payload.bin")
    token = CancellationToken()
    token.cancel()

    cancelled = session.refresh(cancel=token)

    assert cancelled.status is SessionScanStatus.CANCELLED
    assert cancelled.published is False
    assert session.generation == generation
    assert session.current_records is old_records
    assert monitor.acknowledged == 0
    assert _file(cancelled.records, sample.name) == _file(baseline.records, sample.name)

    retried = session.refresh()
    assert retried.status is SessionScanStatus.COMMITTED
    assert _file(retried.records, sample.name).logical_size == len(b"new payload")
    assert monitor.acknowledged == 1


def test_cancelled_full_attempt_publishes_nothing_and_discards_new_monitor(
    tmp_path: Path,
) -> None:
    (tmp_path / "payload.bin").write_bytes(b"payload")
    factory = _FakeMonitorFactory()
    session = IncrementalScanSession([tmp_path], monitor_factory=factory)
    token = CancellationToken()
    token.cancel()

    result = session.baseline(cancel=token)

    assert result.status is SessionScanStatus.CANCELLED
    assert result.records == ()
    assert session.has_baseline is False
    assert session.incremental_ready is False
    assert factory.monitors[-1].state is MonitorState.STOPPED


def test_scanner_failure_does_not_replace_last_committed_snapshot(tmp_path: Path) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    sample = nested / "payload.bin"
    sample.write_bytes(b"old")
    factory = _FakeMonitorFactory()
    scanner = _CountingScanner(factory)
    session = IncrementalScanSession(
        [tmp_path],
        monitor_factory=factory,
        scanner=scanner,
    )
    session.baseline()
    generation = session.generation
    old_records = session.current_records
    monitor = factory.latest(tmp_path)
    sample.write_bytes(b"new")
    monitor.emit(ChangeAction.MODIFIED, r"nested\payload.bin")

    def fail_targeted_scan(call: int, record: ScanRecord) -> bool:
        if call == 2:
            raise RuntimeError("injected scan failure")
        return False

    scanner.after_record = fail_targeted_scan
    failed = session.refresh()

    assert failed.status is SessionScanStatus.FAILED
    assert failed.published is False
    assert "injected scan failure" in (failed.error or "")
    assert session.generation == generation
    assert session.current_records is old_records
    assert monitor.acknowledged == 0


def test_refresh_without_baseline_is_explicit_full_fallback(tmp_path: Path) -> None:
    (tmp_path / "payload.bin").write_bytes(b"payload")
    factory = _FakeMonitorFactory()
    session = IncrementalScanSession([tmp_path], monitor_factory=factory)

    result = session.refresh()

    assert result.mode is SessionScanMode.FALLBACK
    assert result.status is SessionScanStatus.COMMITTED
    assert "NO_BASELINE" in {fallback.code for fallback in result.fallbacks}


def test_acknowledgement_failure_keeps_snapshot_but_invalidates_next_refresh(
    tmp_path: Path,
) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    sample = nested / "payload.bin"
    sample.write_bytes(b"old")
    factory = _FakeMonitorFactory()
    session = IncrementalScanSession([tmp_path], monitor_factory=factory)
    session.baseline()
    monitor = factory.latest(tmp_path)
    sample.write_bytes(b"new")
    monitor.emit(ChangeAction.MODIFIED, r"nested\payload.bin")
    monitor.ack_failure = FallbackReason.SESSION_SEQUENCE_GAP

    committed = session.refresh()

    assert committed.status is SessionScanStatus.COMMITTED
    assert committed.stats.incremental_ready is False
    assert session.incremental_ready is False
    assert FallbackReason.SESSION_SEQUENCE_GAP.value in {
        fallback.code for fallback in committed.fallbacks
    }
    next_refresh = session.refresh()
    assert next_refresh.mode is SessionScanMode.FALLBACK
    assert "NO_LIVE_MONITOR" in {fallback.code for fallback in next_refresh.fallbacks}


def test_closed_session_rejects_future_scans_without_losing_snapshot(tmp_path: Path) -> None:
    (tmp_path / "payload.bin").write_bytes(b"payload")
    factory = _FakeMonitorFactory()
    session = IncrementalScanSession([tmp_path], monitor_factory=factory)
    baseline = session.baseline()
    session.close()

    result = session.refresh()

    assert result.status is SessionScanStatus.FAILED
    assert result.records == baseline.records
    assert "SESSION_CLOSED" in {fallback.code for fallback in result.fallbacks}
