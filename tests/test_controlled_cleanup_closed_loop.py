from __future__ import annotations

import ast
import inspect
import json
import os
import textwrap
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import devclean.core.postscan_cleanup as cleanup
import devclean.scanner as scanner_package
import devclean.ui.app as app_module
from devclean.core.ai_review_contract import (
    AiRecommendation,
    AiReviewCandidateInput,
    build_ai_review_package,
    parse_ai_review_response,
    response_template,
)
from devclean.core.cleanup_catalog import (
    CleanupCategory,
    CleanupPolicy,
    KnownCleanupRoot,
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
    ReviewLane,
    TriageItem,
    triage_file,
)
from devclean.platform.windows.exact_cleanup import ExactMutationResult
from devclean.platform.windows.filesystem import FileSystemMetadata, read_file_metadata
from devclean.platform.windows.known_folders import (
    CanonicalCleanupKind,
    CanonicalCleanupRoot,
)
from devclean.scanner import ScanOptions, ScanRecord, ScanRecordKind, scan_roots
from devclean.ui.app import (
    DevCleanWindow,
    WorkbenchState,
    is_ai_review_eligible,
    is_direct_cleanup_eligible,
)

NOW = datetime(2026, 7, 16, 8, 0, tzinfo=UTC)


def _metadata(
    *,
    directory: bool,
    file_id: str,
    size: int = 0,
    volume: int = 42,
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
        link_count=1,
        attributes=16 if directory else 32,
        reparse_tag=None,
        is_reparse_point=False,
        is_cloud_placeholder=False,
        creation_time_ns=created,
        last_write_time_ns=modified,
    )


@dataclass
class _ExactFileView:
    root: Path
    values: dict[str, FileSystemMetadata] = field(default_factory=dict)
    calls: list[tuple[str, str]] = field(default_factory=list)
    _next_id: int = 1

    def __post_init__(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.add_directory(self.root)

    @staticmethod
    def key(path: str | Path) -> str:
        return str(Path(path).absolute()).casefold()

    def _id(self) -> str:
        value = f"{self._next_id:032x}"
        self._next_id += 1
        return value

    def add_directory(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        self.values[self.key(path)] = _metadata(directory=True, file_id=self._id())

    def add_file(self, relative: str, content: bytes = b"fixture") -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        self.values[self.key(path)] = _metadata(
            directory=False,
            file_id=self._id(),
            size=len(content),
        )
        return path

    def read(self, path: str | Path) -> FileSystemMetadata:
        try:
            return self.values[self.key(path)]
        except KeyError as error:
            raise FileNotFoundError(path) from error

    def record(self, path: Path) -> ScanRecord:
        metadata = self.read(path)
        return ScanRecord(
            root=str(self.root),
            path=str(path),
            kind=ScanRecordKind.FILE,
            depth=len(path.relative_to(self.root).parts),
            logical_size=metadata.logical_size,
            allocated_size=metadata.allocation_size,
            raw_allocated_size=metadata.allocation_size,
            volume_serial=metadata.volume_serial,
            file_id=metadata.file_id,
            file_id_kind=metadata.file_id_kind,
            link_count=metadata.link_count,
            attributes=metadata.attributes,
            reparse_tag=metadata.reparse_tag,
            creation_time_ns=metadata.creation_time_ns,
            last_write_time_ns=metadata.last_write_time_ns,
        )

    def move(
        self,
        source: Path,
        destination: Path,
        _snapshot: object,
        _boundary: object,
    ) -> ExactMutationResult:
        self.calls.append(("move", str(source)))
        metadata = self.values.pop(self.key(source))
        destination.parent.mkdir(parents=True, exist_ok=True)
        source.rename(destination)
        self.values[self.key(destination)] = metadata
        return ExactMutationResult(str(source), str(destination), True, False, True)

    def purge(
        self, source: Path, _snapshot: object, _boundary: object
    ) -> ExactMutationResult:
        self.calls.append(("purge", str(source)))
        self.values.pop(self.key(source))
        source.unlink()
        return ExactMutationResult(str(source), None, True, False, False)

    def recycle(self, destination: Path) -> None:
        self.calls.append(("recycle", str(destination)))
        self.values.pop(self.key(destination))
        destination.unlink()


@pytest.fixture
def exact_view(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> _ExactFileView:
    view = _ExactFileView(tmp_path / "scan-root")
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
        lambda: (
            CanonicalCleanupRoot(
                view.root / "Temp", CanonicalCleanupKind.USER_TEMP
            ),
        ),
    )
    return view


def _ai_item(view: _ExactFileView, name: str = "model.bin") -> TriageItem:
    path = view.add_file(f"huggingface/cache/{name}")
    return triage_file(
        view.record(path),
        now=NOW,
        temp_root=view.root / "unrelated-temp",
    )


def _permanent_item(
    view: _ExactFileView,
    name: str = "old.tmp",
) -> tuple[TriageItem, KnownCleanupRoot]:
    temp_root = view.root / "Temp"
    if view.key(temp_root) not in view.values:
        view.add_directory(temp_root)
    path = view.add_file(f"Temp/{name}")
    known = KnownCleanupRoot(
        path=temp_root,
        category=CleanupCategory.USER_TEMP,
        policy=CleanupPolicy.AGE_BASED_REVIEW,
        label="test Temp",
    )
    item = triage_file(
        view.record(path),
        now=NOW,
        temp_root=temp_root,
        known_roots=(known,),
    )
    return item, known


class _StatusSink:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def set(self, value: str) -> None:
        self.messages.append(value)


def _review_window(item: TriageItem) -> DevCleanWindow:
    """Construct only the stateful review seam; no Tk/display is required."""

    window = object.__new__(DevCleanWindow)
    window._state = WorkbenchState.REVIEW
    window._scan_complete = True
    window._displayed_items = {"row-1": item}
    window._marked_ids = set()
    window._ai_review_ids = {"row-1"}
    window._ai_import = None
    window._ai_recommendations = {}
    window._status = _StatusSink()
    window._apply_filters = lambda: None
    window._update_marked_card = lambda: None
    return window


def _recommend_recycle_response(package: Any) -> str:
    payload = response_template(package)
    recommendations = payload["recommendations"]
    assert isinstance(recommendations, list)
    recommendation = recommendations[0]
    assert isinstance(recommendation, dict)
    recommendation["recommendation"] = AiRecommendation.RECOMMEND_RECYCLE.value
    recommendation["reason"] = "Cache metadata indicates a recyclable generated artifact."
    return json.dumps(payload, ensure_ascii=False)


def test_scan_triage_and_ui_start_with_zero_cleanup_selection(
    exact_view: _ExactFileView,
) -> None:
    deterministic, _known = _permanent_item(exact_view)
    window = _review_window(deterministic)

    assert deterministic.lane is ReviewLane.DETERMINISTIC_CANDIDATE
    assert is_direct_cleanup_eligible(deterministic)
    assert window._marked_items() == ()
    assert window._marked_ids == set()
    with pytest.raises(CleanupRefusal, match="select between 1"):
        cleanup.prepare_cleanup_batch(())

    window._marked_ids.add("row-1")
    assert window._marked_items() == (deterministic,)


def test_ai_import_is_inert_until_explicit_adoption_then_recycle_executes(
    exact_view: _ExactFileView,
    tmp_path: Path,
) -> None:
    item = _ai_item(exact_view)
    source = Path(item.path)
    window = _review_window(item)
    package = build_ai_review_package(
        (AiReviewCandidateInput(item=item, hard_protected=False),),
        scan_session_id="scan_closed_loop",
        now=NOW,
    )

    imported = parse_ai_review_response(
        _recommend_recycle_response(package),
        package,
        now=NOW,
    )

    assert imported.execution_authority == "NONE"
    assert not hasattr(imported, "actions")
    assert not hasattr(imported, "execute")
    assert source.exists()
    assert exact_view.calls == []
    assert window._marked_items() == ()
    assert is_direct_cleanup_eligible(item)
    assert is_ai_review_eligible(item)

    recommendation = imported.recommendations[0]
    window._ai_import = imported
    window._ai_recommendations = {
        app_module._path_key(item.path): (
            recommendation.recommendation,
            recommendation.reason,
        )
    }
    window._adopt_ai_recommendations()
    assert window._marked_items() == (item,)
    assert source.exists()
    assert exact_view.calls == []

    candidate = cleanup.candidate_from_triage_item(window._marked_items()[0])
    batch = cleanup.prepare_cleanup_batch((candidate,))
    with pytest.raises(CleanupRefusal, match="Temp/CrashDumps"):
        cleanup.issue_cleanup_confirmation(batch, mode=CleanupMode.PERMANENT)
    challenge = cleanup.issue_cleanup_confirmation(batch)
    approval = cleanup.confirm_cleanup_batch(batch, challenge, challenge.phrase)
    journal = CleanupJournal(tmp_path / "state-ai" / "journal.db")
    action_id = batch.actions[0].action_id
    observed: list[str] = []

    def mover(
        source_path: Path,
        destination: Path,
        snapshot: object,
        boundary: object,
    ) -> ExactMutationResult:
        observed.extend(journal.event_states(action_id))
        assert journal.action(action_id).state is ActionState.EXECUTING
        return exact_view.move(source_path, destination, snapshot, boundary)

    def unexpected_purge(
        _source: Path, _snapshot: object, _boundary: object
    ) -> ExactMutationResult:
        raise AssertionError("RECYCLE mode must never call the permanent purger")

    result = cleanup.execute_approved_batch(
        batch,
        approval,
        journal=journal,
        mover=mover,
        purger=unexpected_purge,
    )

    assert observed == [ActionState.INTENT_RECORDED.value, ActionState.EXECUTING.value]
    assert result.mode is CleanupMode.RECYCLE
    assert result.batch_state is BatchState.NEEDS_REVIEW
    assert journal.action(action_id).state is ActionState.QUARANTINED
    assert [name for name, _path in exact_view.calls] == ["move"]
    assert not source.exists()


def test_permanent_mode_is_separately_confirmed_and_calls_only_purger(
    exact_view: _ExactFileView,
    tmp_path: Path,
) -> None:
    item, known = _permanent_item(exact_view)
    candidate = cleanup.candidate_from_triage_item(item, known_roots=(known,))
    batch = cleanup.prepare_cleanup_batch((candidate,))
    challenge = cleanup.issue_cleanup_confirmation(batch, mode=CleanupMode.PERMANENT)
    approval = cleanup.confirm_cleanup_batch(batch, challenge, challenge.phrase)
    journal = CleanupJournal(tmp_path / "state-permanent" / "journal.db")
    action_id = batch.actions[0].action_id

    def purger(
        source: Path, snapshot: object, boundary: object
    ) -> ExactMutationResult:
        assert journal.event_states(action_id) == (
            ActionState.INTENT_RECORDED.value,
            ActionState.EXECUTING.value,
            ActionState.QUARANTINED.value,
            ActionState.PURGE_PENDING.value,
        )
        assert journal.action(action_id).state is ActionState.PURGE_PENDING
        return exact_view.purge(source, snapshot, boundary)

    result = cleanup.execute_approved_batch(
        batch,
        approval,
        journal=journal,
        mover=exact_view.move,
        purger=purger,
    )

    assert result.mode is CleanupMode.PERMANENT
    assert result.immediate_reclaim_upper_bound == item.logical_size
    assert journal.action(action_id).state is ActionState.PURGED
    assert [name for name, _path in exact_view.calls] == ["move", "purge"]


def test_failure_stops_batch_and_reconciliation_never_replays_side_effects(
    exact_view: _ExactFileView,
    tmp_path: Path,
) -> None:
    first = _ai_item(exact_view, "first.bin")
    second = _ai_item(exact_view, "second.bin")
    candidates = tuple(
        cleanup.candidate_from_triage_item(item) for item in (first, second)
    )
    batch = cleanup.prepare_cleanup_batch(candidates)
    challenge = cleanup.issue_cleanup_confirmation(batch)
    approval = cleanup.confirm_cleanup_batch(batch, challenge, challenge.phrase)
    journal = CleanupJournal(tmp_path / "state-failure" / "journal.db")
    first_action, second_action = batch.actions
    side_effect_attempts: list[str] = []

    def failing_mover(
        source: Path,
        _destination: Path,
        _snapshot: object,
        _boundary: object,
    ) -> ExactMutationResult:
        side_effect_attempts.append(str(source))
        assert journal.action(first_action.action_id).state is ActionState.EXECUTING
        assert journal.action(second_action.action_id).state is ActionState.INTENT_RECORDED
        raise OSError("injected mover failure before mutation")

    result = cleanup.execute_approved_batch(
        batch,
        approval,
        journal=journal,
        mover=failing_mover,
    )

    assert result.batch_state is BatchState.COMPLETED
    assert side_effect_attempts == [str(Path(first.path))]
    assert journal.event_states(first_action.action_id) == (
        ActionState.INTENT_RECORDED.value,
        ActionState.EXECUTING.value,
        ActionState.FAILED_UNCHANGED.value,
    )
    assert journal.event_states(second_action.action_id) == (
        ActionState.INTENT_RECORDED.value,
        ActionState.FAILED_UNCHANGED.value,
    )
    assert journal.action(second_action.action_id).state is ActionState.FAILED_UNCHANGED
    assert Path(first.path).exists() and Path(second.path).exists()

    reconciled = cleanup.reconcile_unfinished_actions(journal)
    assert reconciled == ()
    assert side_effect_attempts == [str(Path(first.path))]
    assert cleanup.reconcile_unfinished_actions(journal) == ()

    with pytest.raises(CleanupJournalError):
        cleanup.execute_approved_batch(
            batch,
            approval,
            journal=journal,
            mover=failing_mover,
        )
    assert side_effect_attempts == [str(Path(first.path))]


def _ast_surface(source: str) -> tuple[set[str], set[str]]:
    tree = ast.parse(source)
    imports: set[str] = set()
    calls: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                calls.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                calls.add(node.func.attr)
    return imports, calls


def test_scan_worker_and_scanner_package_have_no_cleanup_execution_surface() -> None:
    forbidden_modules = {
        "devclean.core.cleanup_journal",
        "devclean.core.postscan_cleanup",
        "devclean.platform.windows.exact_cleanup",
        "devclean.platform.windows.recycle_bin",
    }
    forbidden_calls = {
        "DeleteFileW",
        "SetFileInformationByHandle",
        "SHFileOperationW",
        "candidate_from_triage_item",
        "confirm_cleanup_batch",
        "execute_approved_batch",
        "issue_cleanup_confirmation",
        "prepare_cleanup_batch",
        "purge_exact_file",
        "quarantine_exact_file",
        "recycle_staged_file",
        "remove",
        "rmtree",
        "unlink",
    }

    worker_source = textwrap.dedent(inspect.getsource(DevCleanWindow._scan_worker))
    worker_imports, worker_calls = _ast_surface(worker_source)
    assert worker_imports.isdisjoint(forbidden_modules)
    assert worker_calls.isdisjoint(forbidden_calls)

    scanner_root = Path(scanner_package.__file__).parent
    for path in scanner_root.glob("*.py"):
        imports, calls = _ast_surface(path.read_text(encoding="utf-8"))
        assert imports.isdisjoint(forbidden_modules), path
        assert calls.isdisjoint(forbidden_calls), path


def _canary_fingerprint(path: Path) -> tuple[bytes, tuple[int, str] | None, int | None, int]:
    metadata = read_file_metadata(path)
    return (
        path.read_bytes(),
        metadata.identity,
        metadata.last_write_time_ns,
        path.stat(follow_symlinks=False).st_mtime_ns,
    )


def test_2026_07_12_temp_and_crashdump_scan_canaries_remain_bit_exact(
    tmp_path: Path,
) -> None:
    """Regression for the withdrawn build that mutated Temp during scanning."""

    scope = tmp_path / "incident-regression"
    temp_root = scope / "Temp"
    crash_root = scope / "CrashDumps"
    temp_root.mkdir(parents=True)
    crash_root.mkdir(parents=True)
    temp_canary = temp_root / "scan-must-not-delete.tmp"
    crash_canary = crash_root / "scan-must-not-delete.dmp"
    temp_canary.write_bytes(b"TEMP-CANARY-2026-07-12\x00\xff")
    crash_canary.write_bytes(b"CRASHDUMP-CANARY-2026-07-12\x10\x80")
    old_ns = 1_577_836_800_000_000_000
    os.utime(temp_canary, ns=(old_ns, old_ns))
    os.utime(crash_canary, ns=(old_ns, old_ns))
    canaries = (temp_canary, crash_canary)
    before = {path: _canary_fingerprint(path) for path in canaries}
    known_roots = (
        KnownCleanupRoot(
            temp_root,
            CleanupCategory.USER_TEMP,
            CleanupPolicy.AGE_BASED_REVIEW,
            "incident Temp canary",
        ),
        KnownCleanupRoot(
            crash_root,
            CleanupCategory.CRASH_DUMPS,
            CleanupPolicy.AGE_BASED_REVIEW,
            "incident CrashDumps canary",
        ),
    )

    records = tuple(scan_roots((scope,), ScanOptions(include_directories=False)))
    items = tuple(
        triage_file(
            record,
            now=NOW,
            temp_root=temp_root,
            known_roots=known_roots,
        )
        for record in records
        if record.kind is ScanRecordKind.FILE
    )

    assert {Path(item.path) for item in items} == set(canaries)
    assert {item.category for item in items} == {
        CleanupCategory.USER_TEMP,
        CleanupCategory.CRASH_DUMPS,
    }
    assert all(item.lane is ReviewLane.DETERMINISTIC_CANDIDATE for item in items)
    assert {path: _canary_fingerprint(path) for path in canaries} == before
