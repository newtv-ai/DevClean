from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from queue import Queue
from typing import Any, cast

import pytest

from devclean.core.cleanup_catalog import CleanupCategory, SourceDomain
from devclean.core.triage import (
    Actionability,
    CleanupTargetKind,
    DirectoryScope,
    EvidenceKind,
    ExecutionPolicy,
    RecoveryCapability,
    ReviewLane,
    RiskTier,
    TriageItem,
    TriageSession,
)
from devclean.core.user_rules import (
    DecisionRule,
    RuleMatch,
    UserRules,
    default_rules,
)
from devclean.scanner.filesystem import (
    CancellationToken,
    ScanOptions,
    ScanRecord,
    ScanRecordKind,
    scan_roots,
)
from devclean.ui import app


def _item(
    path: str,
    *,
    size: int,
    target: CleanupTargetKind = CleanupTargetKind.FILE,
) -> TriageItem:
    root = str(Path(path).parents[1])
    kind = (
        ScanRecordKind.DIRECTORY if target is CleanupTargetKind.DIRECTORY else ScanRecordKind.FILE
    )
    record = ScanRecord(
        root=root,
        path=path,
        kind=kind,
        depth=1,
        logical_size=0 if target is CleanupTargetKind.DIRECTORY else size,
        allocated_size=0 if target is CleanupTargetKind.DIRECTORY else size,
        raw_allocated_size=0 if target is CleanupTargetKind.DIRECTORY else size,
        volume_serial=1,
        file_id=f"{abs(hash(path)):032x}"[-32:],
        file_id_kind="file_id_128",
        link_count=1,
        attributes=0x10 if target is CleanupTargetKind.DIRECTORY else 0,
        creation_time_ns=1,
        last_write_time_ns=2,
    )
    return TriageItem(
        record=record,
        path=path,
        logical_size=0 if target is CleanupTargetKind.DIRECTORY else size,
        allocated_size=None if target is CleanupTargetKind.DIRECTORY else size,
        category=CleanupCategory.OTHER,
        source_domain=SourceDomain.GENERAL_STORAGE,
        lane=ReviewLane.DETERMINISTIC_CANDIDATE,
        risk_tier=RiskTier.LOW,
        evidence_kind=EvidenceKind.KNOWN_ROOT_HEURISTIC,
        actionability=Actionability.REVIEW_PLAN,
        execution_policy=ExecutionPolicy.USER_CHOICE_DELETE,
        recovery=RecoveryCapability.UNKNOWN,
        reason="test observation",
        tags=("whole_directory",) if target is CleanupTargetKind.DIRECTORY else ("known_root",),
        target_kind=target,
        directory_scope=(
            DirectoryScope.KNOWN_CACHE_ROOT if target is CleanupTargetKind.DIRECTORY else None
        ),
    )


def test_scanner_prunes_configured_directory_without_reading_its_children(
    tmp_path: Path,
) -> None:
    kept = tmp_path / "kept"
    skipped = tmp_path / ".git"
    kept.mkdir()
    skipped.mkdir()
    (kept / "visible.tmp").write_bytes(b"visible")
    (skipped / "hidden.tmp").write_bytes(b"hidden")

    records = tuple(
        scan_roots(
            (tmp_path,),
            ScanOptions(
                exact_file_identity=False,
                skip_directory_names=frozenset({".git"}),
            ),
        )
    )
    paths = {Path(record.path) for record in records}

    assert kept / "visible.tmp" in paths
    assert skipped in paths
    assert skipped / "hidden.tmp" not in paths


def test_keep_descendant_suppresses_whole_directory_candidate() -> None:
    directory = _item(
        r"G:\work\cache",
        size=0,
        target=CleanupTargetKind.DIRECTORY,
    )
    protected = _item(r"G:\work\cache\important.bin", size=4)
    disposable = _item(r"G:\work\cache\other.bin", size=7)
    base = default_rules()
    rules = UserRules(
        scan=base.scan,
        delete=base.delete,
        keep=replace(
            base.keep,
            rules=(
                DecisionRule(
                    rule_id="keep_important",
                    group="manual",
                    match=RuleMatch.EXACT_PATH,
                    value=protected.path,
                ),
            ),
        ),
    )
    session = TriageSession(review_sample_per_category=rules.scan.review_sample_per_category)
    for item in (directory, protected, disposable):
        session.observe_path(item.path, rules)
        session.add(item)

    deletable, unsure = app._partition_items(session, rules)

    assert directory not in deletable
    assert protected not in deletable
    assert disposable in deletable
    assert protected not in unsure


def test_live_preview_is_bounded_but_totals_cover_all_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app, "_LIVE_ROWS_DRAWN", 2)
    rules = default_rules()
    session = TriageSession(review_sample_per_category=rules.scan.review_sample_per_category)
    items = (
        _item(r"G:\work\cache\a.bin", size=1),
        _item(r"G:\work\cache\b.bin", size=10),
        _item(r"G:\work\cache\c.bin", size=5),
    )
    for item in items:
        session.observe_path(item.path, rules)
        session.add(item)

    (rows, count, total), _unsure = app._rows_of(session, rules)

    assert count == 3
    assert total == 16
    assert [row[1] for row in rows] == [10, 5]


def test_live_preview_directory_totals_do_not_depend_on_bucket_order() -> None:
    rules = default_rules()
    session = TriageSession(review_sample_per_category=rules.scan.review_sample_per_category)
    outside = replace(
        _item(r"G:\work\Temp\setup.log", size=4_096),
        category=CleanupCategory.SYSTEM_LOGS,
    )
    directory = replace(
        _item(
            r"G:\work\npm-cache",
            size=0,
            target=CleanupTargetKind.DIRECTORY,
        ),
        category=CleanupCategory.NPM_CACHE,
    )
    children = tuple(
        replace(
            _item(
                rf"G:\work\npm-cache\entry{index}.log",
                size=1_000_000,
            ),
            category=CleanupCategory.SYSTEM_LOGS,
        )
        for index in range(3)
    )
    # SYSTEM_LOGS is intentionally created before the directory's NPM_CACHE
    # bucket, reproducing the order that used to double-count descendants.
    for item in (outside, directory, *children):
        session.observe_path(item.path, rules)
        session.add(item)

    (rows, count, total), _unsure = app._rows_of(session, rules)

    assert count == 2
    assert total == 3_004_096
    assert {row[0] for row in rows} == {outside.path, directory.path}


def test_slow_live_preview_does_not_immediately_repeat(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    clock = [0.0]
    preview_calls = 0

    def fake_scan(*_args: object, **_kwargs: object) -> Iterator[ScanRecord]:
        for index in range(4):
            clock[0] += 2.0 if index == 0 else 0.1
            yield ScanRecord(
                root=str(tmp_path),
                path=str(tmp_path / f"error-{index}"),
                kind=ScanRecordKind.ERROR,
                depth=1,
                error="test",
            )

    def fake_rows(
        _session: TriageSession,
        _rules: UserRules,
    ) -> tuple[app.PartialBucket, app.PartialBucket]:
        nonlocal preview_calls
        preview_calls += 1
        # Model a full-candidate preview taking longer than its refresh interval.
        clock[0] += 2.0
        empty: app.PartialBucket = ((), 0, 0)
        return (empty, empty)

    monkeypatch.setattr(app, "scan_roots", fake_scan)
    monkeypatch.setattr(app, "_rows_of", fake_rows)
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])

    window = app.DevCleanWindow.__new__(app.DevCleanWindow)
    window._events = Queue()
    rules = default_rules()
    window._scan_worker(
        "scan",
        (tmp_path,),
        CancellationToken(),
        rules,
        (),
    )

    assert preview_calls == 1


def test_scan_snapshot_coalescing_preserves_next_state_event() -> None:
    window = app.DevCleanWindow.__new__(app.DevCleanWindow)
    window._events = Queue()
    window._pending_event = cast(tuple[str, Any] | None, None)
    window._events.put(("progress", ("scan", 2, 20)))
    window._events.put(("scan_done", ("scan", object(), False)))
    window._events.put(("progress", ("scan", 3, 30)))

    kind, payload, consumed = window._newest_scan_snapshot(
        "progress",
        ("scan", 1, 10),
        64,
    )

    assert kind == "progress"
    assert payload == ("scan", 2, 20)
    assert consumed == 1
    assert window._pending_event is not None
    assert window._pending_event[0] == "scan_done"
    assert window._events.get_nowait() == ("progress", ("scan", 3, 30))


def test_event_pump_reschedules_after_handler_exception() -> None:
    scheduled: list[tuple[int, object]] = []

    class Root:
        def after(self, delay: int, callback: object) -> None:
            scheduled.append((delay, callback))

    window = app.DevCleanWindow.__new__(app.DevCleanWindow)
    window._root = cast(Any, Root())
    window._events = Queue()
    window._pending_event = None
    window._scan_token = "scan"
    window._events.put(("scan_partial", ("scan", ())))

    with pytest.raises(ValueError):
        window._drain_events()

    assert len(scheduled) == 1
    assert scheduled[0][1] == window._drain_events


def test_review_sample_keeps_largest_items_per_category() -> None:
    rules = default_rules()
    session = TriageSession(review_sample_per_category=2)
    items = (
        _item(r"G:\work\cache\small.bin", size=1),
        _item(r"G:\work\cache\large.bin", size=30),
        _item(r"G:\work\cache\medium.bin", size=20),
    )
    for item in items:
        session.observe_path(item.path, rules)
        session.add(item)

    retained = {item.path for item in session.iter_items()}
    assert retained == {
        r"G:\work\cache\large.bin",
        r"G:\work\cache\medium.bin",
    }


def test_ai_grouping_merges_only_equivalent_generated_name_siblings() -> None:
    rules = default_rules()
    now = datetime(2026, 7, 27, tzinfo=UTC)
    recent_ns = int((now - timedelta(days=2)).timestamp() * 1_000_000_000)

    def observed(path: str, size: int) -> TriageItem:
        item = _item(path, size=size)
        return replace(
            item,
            record=replace(item.record, last_write_time_ns=recent_ns),
        )

    first = observed(
        r"G:\cache\urlsoceng.store.4_13429459072546848",
        10_000,
    )
    second = observed(
        r"G:\cache\urlsoceng.store.4_13429460000000000",
        20_000,
    )
    groups = app._group_ai_candidates((first, second), rules, now=now)

    assert len(groups) == 1
    assert groups[0].members == (first, second)
    summary = groups[0].similarity_summary()
    assert summary is not None
    assert summary.filename_pattern == "urlsoceng.store.4_*"

    semantic = (
        observed(r"G:\cache\config.json", 10_000),
        observed(r"G:\cache\database.json", 20_000),
    )
    different_parent = (
        first,
        observed(
            r"G:\other\urlsoceng.store.4_13429460000000000",
            20_000,
        ),
    )
    different_reason = (first, replace(second, reason="different evidence"))
    different_size_band = (
        first,
        observed(
            r"G:\cache\urlsoceng.store.4_13429470000000000",
            2 * 1024 * 1024,
        ),
    )
    old = observed(
        r"G:\cache\urlsoceng.store.4_13429480000000000",
        20_000,
    )
    old = replace(
        old,
        record=replace(
            old.record,
            last_write_time_ns=int((now - timedelta(days=45)).timestamp() * 1_000_000_000),
        ),
    )

    for candidates in (
        semantic,
        different_parent,
        different_reason,
        different_size_band,
        (first, old),
    ):
        assert len(app._group_ai_candidates(candidates, rules, now=now)) == 2

    with pytest.raises(ValueError, match="files only"):
        app._group_ai_candidates(
            (
                _item(
                    r"G:\work\cache",
                    size=0,
                    target=CleanupTargetKind.DIRECTORY,
                ),
            ),
            rules,
            now=now,
        )


def test_open_path_in_explorer_opens_directory_selects_file_and_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "folder"
    directory.mkdir()
    file_path = directory / "locked.bin"
    file_path.write_bytes(b"x")
    opened: list[Path] = []
    selected: list[list[str]] = []
    monkeypatch.setattr(os, "startfile", lambda path: opened.append(Path(path)))
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda command: selected.append(command),
    )

    assert app._open_path_in_explorer(str(directory)) == directory
    assert app._open_path_in_explorer(str(file_path)) == file_path
    missing = directory / "already-gone.bin"
    assert app._open_path_in_explorer(str(missing)) == directory

    assert opened == [directory, directory]
    assert selected == [["explorer.exe", f"/select,{file_path}"]]


def test_confidence_lanes_do_not_spend_ai_on_user_review() -> None:
    direct = _item(r"G:\work\safe-cache\payload.bin", size=10)
    user_review = replace(
        direct,
        path=r"G:\work\maybe-cache\payload.bin",
        record=replace(direct.record, path=r"G:\work\maybe-cache\payload.bin"),
        lane=ReviewLane.USER_REVIEW,
        risk_tier=RiskTier.MEDIUM,
        actionability=Actionability.USER_REVIEW,
        tags=("user_review",),
    )
    ai_review = replace(
        direct,
        path=r"G:\work\unknown\payload.bin",
        record=replace(direct.record, path=r"G:\work\unknown\payload.bin"),
        lane=ReviewLane.AI_REVIEW,
        risk_tier=RiskTier.HIGH,
        actionability=Actionability.AI_REVIEW,
        tags=("ai_review_required",),
    )

    assert app.is_direct_cleanup_eligible(direct)
    assert not app.is_ai_review_eligible(direct)
    assert app.is_user_review_eligible(user_review)
    assert not app.is_direct_cleanup_eligible(user_review)
    assert not app.is_ai_review_eligible(user_review)
    assert app.is_ai_review_eligible(ai_review)
    assert not app.is_user_review_eligible(ai_review)


def test_partition_keeps_user_review_out_of_direct_cleanup() -> None:
    rules = default_rules()
    direct = _item(r"G:\work\safe-cache\payload.bin", size=10)
    user_review = replace(
        _item(r"G:\work\maybe-cache\payload.bin", size=20),
        lane=ReviewLane.USER_REVIEW,
        risk_tier=RiskTier.MEDIUM,
        actionability=Actionability.USER_REVIEW,
        tags=("user_review",),
    )
    ai_review = replace(
        _item(r"G:\work\unknown\payload.bin", size=30),
        lane=ReviewLane.AI_REVIEW,
        risk_tier=RiskTier.HIGH,
        actionability=Actionability.AI_REVIEW,
        tags=("ai_review_required",),
    )
    session = TriageSession(review_sample_per_category=rules.scan.review_sample_per_category)
    for item in (direct, user_review, ai_review):
        session.observe_path(item.path, rules)
        session.add(item)

    deletable, needs_review = app._partition_items(session, rules)

    assert deletable == (direct,)
    assert set(needs_review) == {user_review, ai_review}
