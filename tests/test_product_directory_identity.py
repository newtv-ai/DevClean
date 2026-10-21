from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from queue import Queue
from typing import Any

import pytest

from devclean.core import cleanup_journal, postscan_cleanup
from devclean.core.application_cleanup import (
    ApplicationCleanupRule,
    DecisionOwner,
    LastUseStrategy,
    MatchKind,
    RebuildCost,
)
from devclean.core.cleanup_catalog import CleanupCategory, CleanupPolicy, KnownCleanupRoot
from devclean.core.cleanup_journal import ActionState, CleanupJournal, CleanupMode
from devclean.core.triage import TriageSession
from devclean.core.user_rules import default_rules
from devclean.platform.windows.exact_cleanup import (
    DirectoryPurgeProgress,
    DirectoryPurgeResult,
    ExactDirectorySnapshot,
    ExactRootBoundary,
)
from devclean.platform.windows.filesystem import FileSystemMetadata, read_file_metadata
from devclean.scanner import CancellationToken
from devclean.scanner.tree_summary import TreeSummaryIncomplete
from devclean.ui import product_app


def _windows_identity(path: str | Path) -> FileSystemMetadata:
    return replace(
        read_file_metadata(path),
        file_id_kind="file_id_128",
        attributes=0x10,
        link_count=1,
    )


def test_summary_scan_can_prepare_and_execute_its_directory_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "cache"
    root.mkdir()
    payload = root / "entry.bin"
    payload.write_bytes(b"data")
    rule = ApplicationCleanupRule(
        rule_id="test-cache",
        app_id="test",
        root_key="TEST",
        relative_pattern="",
        match_kind=MatchKind.PREFIX,
        owner=DecisionOwner.TOOL,
        last_use=LastUseStrategy.FILE_MTIME,
        rebuild_cost=RebuildCost.LOW,
        allow_whole_tree=True,
        label="Audited cache",
    )
    known = KnownCleanupRoot(
        root,
        CleanupCategory.IDE_CACHE,
        CleanupPolicy.VENDOR_MANAGED,
        "cache",
        delete_root_itself=True,
        application_rule=rule,
    )
    monkeypatch.setattr(product_app, "read_file_metadata", _windows_identity)
    monkeypatch.setattr(postscan_cleanup, "read_file_metadata", _windows_identity)
    monkeypatch.setattr(postscan_cleanup, "is_local_fixed_path", lambda _path: True)
    for name in ("secure_private_directory", "secure_private_file"):
        monkeypatch.setattr(cleanup_journal, name, lambda _path: None)
    monkeypatch.setattr(cleanup_journal, "is_local_fixed_path", lambda _path: True)
    window = object.__new__(product_app.ProductDevCleanWindow)
    window._events = Queue()
    rules = default_rules()
    window._scan_worker("test", (root,), CancellationToken(), rules, (known,))
    session: TriageSession | None = None
    while not window._events.empty():
        event, data = window._events.get_nowait()
        if event == "scan_done":
            session = data[1]
    assert session is not None
    item = next(session.iter_items())
    candidate = postscan_cleanup.candidate_from_directory_item(
        item,
        session.subtree_totals(item.path),
        known_roots=(known,),
        delete_config=rules.delete.classification,
        keep_config=rules.keep.classification,
    )
    assert candidate.snapshot.file_id == _windows_identity(root).file_id

    def purge(
        path: Path,
        expected: ExactDirectorySnapshot,
        boundary: ExactRootBoundary,
        *,
        on_progress: Callable[[DirectoryPurgeProgress], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> DirectoryPurgeResult:
        assert path == root
        assert expected.file_id == candidate.snapshot.file_id
        assert boundary.path == tmp_path
        payload.unlink()
        root.rmdir()
        return DirectoryPurgeResult(str(root), 1, 0, 1, 4, True, True)

    result = postscan_cleanup.execute_cleanup_batch(
        postscan_cleanup.prepare_cleanup_batch((candidate,)),
        CleanupMode.PERMANENT,
        delete_config=rules.delete.classification,
        keep_config=rules.keep.classification,
        known_roots=(known,),
        journal=CleanupJournal(tmp_path / "state" / "journal.db"),
        directory_purger=purge,
    )
    assert result.action_states[0][1] is ActionState.PURGED
    assert result.purged_logical_bytes == 4


@pytest.mark.parametrize(
    "changes",
    [
        {"file_id": "replacement"},
        {"creation_time_ns": 1},
        {"is_reparse_point": True},
        {"is_cloud_placeholder": True},
    ],
)
def test_root_replacement_during_summary_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changes: dict[str, Any],
) -> None:
    before = _windows_identity(tmp_path)
    snapshots = iter((before, replace(before, **changes)))
    monkeypatch.setattr(product_app, "read_file_metadata", lambda _path: next(snapshots))
    with pytest.raises(TreeSummaryIncomplete):
        product_app._identified_tree_summary(tmp_path, CancellationToken())
