"""A whole-tree cache summary skips only what it cannot read (user decision 2026-11-11).

Before, one unreadable entry made ``summarize_tree`` give up on the whole cache
root: the root was not listed and, because the generic walk skips it, none of
its files were offered either.  Now only that entry is left out and counted,
the rest of the tree is summed, listed and cleaned as before, and the user is
told what was left out.  The tree's own folder failing to read still keeps the
whole item out.

Everything runs on folders inside ``tmp_path``.
"""

# Expected UI strings are Chinese prose with fullwidth punctuation.
# ruff: noqa: RUF001

from __future__ import annotations

import errno
import os
import shutil
from collections.abc import Callable, Iterator
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
from devclean.core.triage import TriageItem, TriageSession
from devclean.core.user_rules import default_rules
from devclean.platform.windows import exact_cleanup
from devclean.platform.windows.filesystem import FileSystemMetadata, read_file_metadata
from devclean.scanner import CancellationToken
from devclean.scanner.tree_summary import SkipReason, TreeSummaryIncomplete, summarize_tree
from devclean.ui import app, product_app


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _cache(tmp_path: Path) -> Path:
    root = tmp_path / "cache"
    _write(root / "a.bin", b"a" * 17)
    _write(root / "open" / "b.bin", b"b" * 31)
    _write(root / "locked" / "x.bin", b"x" * 5)
    _write(root / "locked" / "deep" / "y.bin", b"y" * 7)
    return root


def _denied(path: Path, *, winerror: int = 5) -> OSError:
    """What listing a folder this account may not read raises."""

    error = PermissionError(errno.EACCES, "Access is denied", str(path))
    if os.name == "nt":
        error.winerror = winerror
    return error


def _refuse_listing(
    monkeypatch: pytest.MonkeyPatch, folder: Path, error: Callable[[], OSError]
) -> None:
    """Make *folder* fail to list, whether it is opened by path or by descriptor."""

    real_scandir = os.scandir
    target = os.path.normcase(os.path.abspath(folder))
    inode = os.stat(folder).st_ino

    def scandir(path: Any = ".") -> Any:
        if isinstance(path, int):
            refused = os.fstat(path).st_ino == inode
        else:
            refused = os.path.normcase(os.path.abspath(os.fspath(path))) == target
        if refused:
            raise error()
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", scandir)


# ---- the summary itself ---------------------------------------------------------


def test_an_unreadable_folder_is_skipped_and_the_rest_of_the_tree_is_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _cache(tmp_path)
    _refuse_listing(monkeypatch, root / "locked", lambda: _denied(root / "locked"))

    summary = summarize_tree(root)

    assert (summary.files, summary.logical_bytes) == (2, 17 + 31)
    assert summary.skipped == 1
    assert summary.skipped_by_reason == ((SkipReason.DENIED, 1),)
    assert summary.skipped_examples == ((str(root / "locked"), SkipReason.DENIED),)


def test_a_folder_another_program_holds_is_counted_as_in_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _cache(tmp_path)

    def in_use() -> OSError:
        error = OSError(errno.EBUSY, "in use", str(root / "locked"))
        if os.name == "nt":
            error = _denied(root / "locked", winerror=32)
        return error

    _refuse_listing(monkeypatch, root / "locked", in_use)

    summary = summarize_tree(root)

    assert (summary.files, summary.skipped) == (2, 1)
    assert summary.skipped_by_reason == ((SkipReason.IN_USE, 1),)


def test_a_file_whose_details_cannot_be_read_is_skipped_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _cache(tmp_path)
    bad = _write(root / "open" / "bad.bin", b"z" * 1000)
    real_scandir = os.scandir

    class Entry:
        def __init__(self, entry: os.DirEntry[str]) -> None:
            self._entry = entry
            self.path = entry.path
            self.name = entry.name

        def stat(self, *, follow_symlinks: bool = True) -> os.stat_result:
            if self.name == bad.name:
                raise _denied(bad)
            return self._entry.stat(follow_symlinks=follow_symlinks)

        def is_dir(self, *, follow_symlinks: bool = True) -> bool:
            return self._entry.is_dir(follow_symlinks=follow_symlinks)

        def is_file(self, *, follow_symlinks: bool = True) -> bool:
            return self._entry.is_file(follow_symlinks=follow_symlinks)

    class Listing:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def __enter__(self) -> Iterator[Entry]:
            return (Entry(entry) for entry in self._inner.__enter__())

        def __exit__(self, *details: object) -> None:
            self._inner.__exit__(*details)

    monkeypatch.setattr(os, "scandir", lambda path=".": Listing(real_scandir(path)))

    summary = summarize_tree(root)

    assert (summary.files, summary.logical_bytes) == (4, 17 + 31 + 5 + 7)
    assert summary.skipped_examples == ((str(bad), SkipReason.DENIED),)


def test_a_folder_that_vanished_on_its_own_is_not_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _cache(tmp_path)
    _refuse_listing(
        monkeypatch,
        root / "locked",
        lambda: FileNotFoundError(errno.ENOENT, "gone", str(root / "locked")),
    )

    summary = summarize_tree(root)

    assert (summary.files, summary.skipped) == (2, 0)


def test_the_tree_itself_unreadable_still_stops_the_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _cache(tmp_path)
    _refuse_listing(monkeypatch, root, lambda: _denied(root))

    with pytest.raises(TreeSummaryIncomplete):
        summarize_tree(root)
    with pytest.raises(TreeSummaryIncomplete):
        summarize_tree(tmp_path / "missing")


def _link_folder(link: Path, target: Path) -> None:
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))  # needs no privilege
    else:
        link.symlink_to(target, target_is_directory=True)


def test_a_folder_swapped_for_a_link_is_skipped_and_never_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _cache(tmp_path)
    outside = tmp_path / "outside"
    for index in range(3):
        _write(outside / f"o{index}.bin", b"o" * 1000)
    swapped = os.path.normcase(str(root / "locked"))
    real_scandir = os.scandir

    def scandir(path: Any = ".") -> Any:
        # Between the parent's listing and this folder's, the folder becomes a
        # link to somewhere outside the tree.
        if not isinstance(path, int) and os.path.normcase(os.fspath(path)) == swapped:
            (root / "locked").rename(tmp_path / "moved-away")
            _link_folder(root / "locked", outside)
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", scandir)

    summary = summarize_tree(root)

    assert (summary.files, summary.logical_bytes) == (2, 17 + 31)  # nothing from outside
    assert summary.skipped_examples == ((str(root / "locked"), SkipReason.CHANGED),)


# ---- the product scan: listed, labelled, cleaned ----------------------------------


def _windows_identity(path: str | Path) -> FileSystemMetadata:
    return replace(
        read_file_metadata(path), file_id_kind="file_id_128", attributes=0x10, link_count=1
    )


def _known_cache(root: Path) -> KnownCleanupRoot:
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
    return KnownCleanupRoot(
        root,
        CleanupCategory.IDE_CACHE,
        CleanupPolicy.VENDOR_MANAGED,
        "cache",
        delete_root_itself=True,
        application_rule=rule,
    )


def _product_scan(
    monkeypatch: pytest.MonkeyPatch, root: Path, known: KnownCleanupRoot
) -> Any:
    monkeypatch.setattr(product_app, "read_file_metadata", _windows_identity)
    monkeypatch.setattr(postscan_cleanup, "read_file_metadata", _windows_identity)
    monkeypatch.setattr(exact_cleanup, "read_file_metadata", _windows_identity)
    monkeypatch.setattr(postscan_cleanup, "is_local_fixed_path", lambda _path: True)
    for name in ("secure_private_directory", "secure_private_file"):
        monkeypatch.setattr(cleanup_journal, name, lambda _path: None)
    monkeypatch.setattr(cleanup_journal, "is_local_fixed_path", lambda _path: True)
    window: Any = object.__new__(product_app.ProductDevCleanWindow)
    window._events = Queue()
    window._scan_worker("test", (root,), CancellationToken(), default_rules(), (known,))
    session: TriageSession | None = None
    while not window._events.empty():
        event, data = window._events.get_nowait()
        assert event != "scan_error", data
        if event == "scan_done":
            session = data[1]
    assert session is not None
    return session


class _Var:
    def __init__(self, value: str = "") -> None:
        self.value = value

    def set(self, value: str) -> None:
        self.value = value

    def get(self) -> str:
        return self.value


def _finished_status(session: Any) -> str:
    window: Any = product_app.ProductDevCleanWindow.__new__(product_app.ProductDevCleanWindow)
    window._root = None
    window._scan_duration = _Var()
    window._status = _Var()
    window._session = session
    window._scan_clock_job = None
    window._scan_started_at = None
    window._scan_finished(False, app.ScanTotals(files=2, directories=3, logical_bytes=48))
    return str(window._status.get())


def test_a_cache_root_with_an_unreadable_folder_is_still_listed_and_cleaned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _cache(tmp_path)
    known = _known_cache(root)
    _refuse_listing(monkeypatch, root / "locked", lambda: _denied(root / "locked"))

    session = _product_scan(monkeypatch, root, known)

    # Listed, sized from what could be read, and saying what was left out.
    items: list[TriageItem] = list(session.iter_items())
    assert [item.path for item in items] == [str(root)]
    (item,) = items
    totals = session.subtree_totals(item.path)
    assert (totals.files, totals.logical_bytes) == (2, 17 + 31)
    assert app.is_direct_cleanup_eligible(item)
    assert "scan_skipped_entries" in item.tags
    note = session.summary_notes[str(root)]
    assert note.startswith("扫描时其中 1 项读不到，已跳过、未计入大小（没有读取权限 1 项）")
    assert str(root / "locked") in note
    assert item.reason.endswith(note)
    status = _finished_status(session)
    assert "其中 1 个应用缓存目录里有 1 项读不到，已跳过、未计入大小" in status
    assert str(root) in status

    # The deletion's own re-check skips the same folder and the rest goes.
    rules = default_rules()
    candidate = postscan_cleanup.candidate_from_directory_item(
        item,
        totals,
        known_roots=(known,),
        delete_config=rules.delete.classification,
        keep_config=rules.keep.classification,
    )
    result = postscan_cleanup.execute_cleanup_batch(
        postscan_cleanup.prepare_cleanup_batch((candidate,)),
        CleanupMode.PERMANENT,
        delete_config=rules.delete.classification,
        keep_config=rules.keep.classification,
        known_roots=(known,),
        journal=CleanupJournal(tmp_path / "state" / "journal.db"),
    )
    assert result.failures == ()
    assert result.action_states[0][1] is ActionState.PURGED
    ((path, left),) = result.partial
    assert path == str(root) and left.denied == 1
    assert not (root / "a.bin").exists() and not (root / "open").exists()
    assert (root / "locked" / "x.bin").exists()

    outcomes = app.cleanup_outcomes((result,), ())
    report = app.render_cleanup_report(outcomes, result.purged_logical_bytes, session.summary_notes)
    assert f"• {root}\n" in report
    assert f"    扫描时：{note}" in report


def test_a_cache_root_that_cannot_be_read_is_not_listed_but_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _cache(tmp_path)
    known = _known_cache(root)
    _refuse_listing(monkeypatch, root, lambda: _denied(root))

    session = _product_scan(monkeypatch, root, known)

    assert list(session.iter_items()) == []
    assert session.unlisted_roots == [str(root)]
    assert session.summary_notes == {}
    status = _finished_status(session)
    assert "1 个应用缓存目录本身无法读取或无法确认是同一个目录，整项未列出" in status
    assert str(root) in status


def test_a_fully_readable_cache_root_says_nothing_extra(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _cache(tmp_path)

    session = _product_scan(monkeypatch, root, _known_cache(root))

    (item,) = list(session.iter_items())
    assert "scan_skipped_entries" not in item.tags and "读不到" not in item.reason
    assert session.subtree_totals(item.path).files == 4
    assert session.summary_notes == {} and session.unlisted_roots == []
    assert "读不到" not in _finished_status(session)


def test_a_cache_root_gone_before_it_was_summed_is_not_reported_as_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _cache(tmp_path)
    known = _known_cache(root)

    def removed_first(path: Path, cancel: CancellationToken | None = None) -> Any:
        shutil.rmtree(root)  # the program removed its cache in the meantime
        return summarize_tree(path, cancel)

    monkeypatch.setattr(product_app, "summarize_tree", removed_first)

    session = _product_scan(monkeypatch, root, known)

    assert list(session.iter_items()) == [] and session.unlisted_roots == []
