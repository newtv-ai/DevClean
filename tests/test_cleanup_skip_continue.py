"""One entry that cannot be deleted is skipped; everything else still goes.

User decision 2026-11-11: whatever item or file cannot be deleted is skipped and
the cleanup carries on with the rest; afterwards the report lists what stayed
and why.

The Windows walk (``exact_cleanup._empty_temp_folder`` and the purges around
it) runs here on a real folder inside ``tmp_path``.  Only the Win32 calls are
replaced, by stand-ins that delete through ``os`` and raise the error shapes
Windows produces:

* ``winerror``: what ``os`` functions raise on Windows (PermissionError, errno
  13, winerror 32 for a sharing violation or 5 for access denied);
* ``errno``: what ``_raise_windows_error`` builds, ``OSError(code, ...)`` with
  the Windows code in errno and no winerror (32 arrives as BrokenPipeError).
"""

# Expected UI strings are Chinese prose with fullwidth punctuation.
# ruff: noqa: RUF001

from __future__ import annotations

import errno
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import devclean.platform.windows.exact_cleanup as exact_cleanup
from devclean.core.postscan_cleanup import CleanupExecutionResult, CleanupRefusal
from devclean.platform.windows.exact_cleanup import (
    DirectoryPurgeResult,
    ExactCleanupError,
    ExactDirectorySnapshot,
    ExactRootBoundary,
    TempFolderPurgeResult,
    purge_directory_tree_skipping_in_use,
)
from devclean.platform.windows.filesystem import read_file_metadata
from devclean.ui import app

_IN_USE = 32
_DENIED = 5
_GONE = 2


def _winerror_shape(code: int, path: str) -> OSError:
    """How ``os`` functions report a Windows error: errno mapped, winerror kept."""

    if code == _GONE:
        error: OSError = FileNotFoundError(errno.ENOENT, "The system cannot find the file", path)
    else:
        error = PermissionError(errno.EACCES, "The process cannot access the file", path)
    error.winerror = code  # set by Windows itself; written by hand on this host
    return error


def _errno_shape(code: int, path: str) -> OSError:
    """How ``_raise_windows_error`` reports it: Windows code in errno, no winerror."""

    error = OSError(code, "Windows error", path)
    assert getattr(error, "winerror", None) is None
    return error


_SHAPES = {"winerror": _winerror_shape, "errno": _errno_shape}


@dataclass
class _Handle:
    path: str
    deletable: bool
    closed: bool = False


class _FakeWindows:
    """The Win32 calls of the walk, performed on *root* through ``os``."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        shape: Callable[[int, str], OSError],
    ) -> None:
        self.shape = shape
        # path -> Windows code raised when the entry is opened for deletion
        self.held: dict[str, int] = {}
        # path -> Windows code raised when the folder is listed
        self.unlistable: dict[str, int] = {}
        # entries whose opened handle resolves outside the folder
        self.escaped: set[str] = set()
        # entries another program removes just before DevClean gets to them
        self.vanishing: set[str] = set()
        self.handles: list[_Handle] = []
        self.readonly_flags: list[bool] = []
        for name, replacement in {
            "_open_boundary": self._open_boundary,
            "_open_exact_directory_for_mutation": self._open_for_deletion,
            "_open_exact_directory": self._open_for_listing,
            "_close_handle": self._close,
            "read_file_metadata_handle": lambda _handle: None,
            "_require_directory_snapshot": lambda _metadata, _expected: None,
            "_require_handle_in_boundary": lambda *_args, **_kwargs: None,
            "_require_final_path": lambda _handle, _path: None,
            "_final_path": lambda handle: handle.path,
            "_require_purge_directory": self._require_inside,
            "_list_purge_entries": self._list,
            "_delete_leaf": self._delete_leaf,
            "_delete_empty_directory": self._delete_empty_directory,
            "_set_delete_disposition": self._delete_root,
        }.items():
            monkeypatch.setattr(exact_cleanup, name, replacement)

    @staticmethod
    def _plain(path: str) -> str:
        return path[4:] if path.startswith("\\\\?\\") else path

    def _open_boundary(self, boundary: ExactRootBoundary) -> tuple[_Handle, str]:
        handle = _Handle(str(boundary.path), deletable=False)
        self.handles.append(handle)
        return handle, str(boundary.path)

    def _open_for_deletion(self, path: str) -> _Handle:
        if path in self.held:
            raise self.shape(self.held[path], path)
        handle = _Handle(path, deletable=True)
        self.handles.append(handle)
        return handle

    def _open_for_listing(self, path: str, **_kwargs: object) -> _Handle:
        handle = _Handle(self._plain(path), deletable=False)
        self.handles.append(handle)
        return handle

    @staticmethod
    def _close(handle: _Handle) -> None:
        handle.closed = True

    def _require_inside(self, _handle: _Handle, path: str, _root: str) -> None:
        if path in self.escaped:
            raise ExactCleanupError("opened object's final path escaped the approved root")

    def _list(
        self, directory: str, _root: str, *, already_pinned: bool = False
    ) -> tuple[tuple[str, Any, int], ...]:
        assert already_pinned
        if directory in self.unlistable:
            raise self.shape(self.unlistable[directory], directory)
        entries = []
        with os.scandir(directory) as listing:
            for entry in listing:
                if entry.is_dir(follow_symlinks=False):
                    entries.append((entry.path, exact_cleanup._EntryKind.DIRECTORY, 0))
                else:
                    size = entry.stat(follow_symlinks=False).st_size
                    entries.append((entry.path, exact_cleanup._EntryKind.FILE, size))
        return tuple(sorted(entries))

    def _delete_leaf(self, path: str, _root: str, *, ignore_readonly: bool = False) -> None:
        self.readonly_flags.append(ignore_readonly)
        if path in self.escaped:
            raise ExactCleanupError("opened object's final path escaped the approved root")
        if path in self.vanishing:
            os.remove(path)
            raise self.shape(_GONE, path)
        if path in self.held:
            raise self.shape(self.held[path], path)
        os.remove(path)

    @staticmethod
    def _delete_empty_directory(
        path: str, _root: str, *, pinned_handle: _Handle | None = None
    ) -> None:
        assert pinned_handle is not None and pinned_handle.deletable, path
        os.rmdir(path)

    @staticmethod
    def _delete_root(handle: _Handle, source: str) -> None:
        assert handle.deletable, source
        os.rmdir(source)


def _write(path: Path, data: bytes = b"x") -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return str(path)


def _tree(tmp_path: Path) -> dict[str, str]:
    root = tmp_path / "Cache"
    return {
        "root": str(root),
        "a": _write(root / "a.bin", b"aa"),
        "b": _write(root / "b.bin", b"held"),
        "c": _write(root / "c.bin", b"ccc"),
        "sub": str(root / "sub"),
        "x": _write(root / "sub" / "x.bin", b"held"),
        "y": _write(root / "sub" / "y.bin", b"y"),
        "z": _write(root / "sub" / "deep" / "z.bin", b"zz"),
        "held_folder": str(root / "held"),
        "p": _write(root / "held" / "p.bin", b"p"),
        "q": _write(root / "held" / "q.bin", b"qq"),
    }


def _purge_tree(root: str) -> DirectoryPurgeResult:
    return exact_cleanup._purge_tree_skipping_in_use_windows(
        root,
        ExactDirectorySnapshot(1, "root", "file_id_128", 1),
        ExactRootBoundary(Path(os.path.dirname(root)), 1, "boundary", "file_id_128"),
        None,
        None,
    )


@pytest.mark.parametrize("shape", sorted(_SHAPES))
@pytest.mark.parametrize(("code", "counter"), [(_IN_USE, "in_use"), (_DENIED, "denied")])
def test_every_file_that_can_go_goes_around_those_that_cannot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
    code: int,
    counter: str,
) -> None:
    paths = _tree(tmp_path)
    windows = _FakeWindows(monkeypatch, _SHAPES[shape])
    # Two files another program holds, and a folder it has open itself.
    windows.held = {paths["b"]: code, paths["x"]: code, paths["held_folder"]: code}

    result = _purge_tree(paths["root"])

    # Nothing stopped at the first file left: everything else went.
    assert result.completed and not result.root_absent
    gone = ("a", "c", "y", "z", "p", "q")
    assert not any(os.path.exists(paths[name]) for name in gone)
    assert (result.files_removed, result.bytes_removed) == (6, 2 + 3 + 1 + 2 + 1 + 2)
    assert os.path.exists(paths["b"]) and os.path.exists(paths["x"])
    # The open folder could not be deleted, but its contents were not spared.
    assert os.path.isdir(paths["held_folder"]) and not os.listdir(paths["held_folder"])
    assert not os.path.exists(os.path.join(paths["sub"], "deep"))
    counts = {"in_use": result.in_use, "denied": result.denied}
    assert counts[counter] == 3 and sum(counts.values()) == 3 and result.unsafe == 0
    assert {path for path, _why in result.examples} == {
        paths["b"],
        paths["x"],
        paths["held_folder"],
    }
    # Read-only files inside an approved tree go as well (git object packs...).
    assert windows.readonly_flags and all(windows.readonly_flags)
    assert all(handle.closed for handle in windows.handles)


@pytest.mark.parametrize("shape", sorted(_SHAPES))
def test_a_selected_folder_another_program_has_open_is_still_emptied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    paths = _tree(tmp_path)
    windows = _FakeWindows(monkeypatch, _SHAPES[shape])
    windows.held = {paths["root"]: _IN_USE}

    result = _purge_tree(paths["root"])

    assert result.completed and not result.root_absent
    assert os.listdir(paths["root"]) == []
    assert (result.files_removed, result.in_use, result.denied) == (8, 1, 0)
    assert result.examples == ((paths["root"], "正被程序使用"),)


def test_a_selected_folder_that_cannot_even_be_listed_still_fails_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _tree(tmp_path)
    windows = _FakeWindows(monkeypatch, _errno_shape)
    windows.held = {paths["root"]: _IN_USE}

    def refuse(path: str, **_kwargs: object) -> _Handle:
        raise _errno_shape(_IN_USE, path)

    monkeypatch.setattr(exact_cleanup, "_open_exact_directory", refuse)

    with pytest.raises(OSError) as raised:
        _purge_tree(paths["root"])
    assert raised.value.errno == _IN_USE
    assert os.path.exists(paths["a"])


@pytest.mark.parametrize("shape", sorted(_SHAPES))
def test_a_folder_that_cannot_be_listed_stays_and_the_rest_goes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    paths = _tree(tmp_path)
    windows = _FakeWindows(monkeypatch, _SHAPES[shape])
    windows.unlistable = {paths["sub"]: _DENIED}

    result = _purge_tree(paths["root"])

    assert result.completed and not result.root_absent
    assert all(os.path.exists(paths[name]) for name in ("x", "y", "z"))
    assert not any(os.path.exists(paths[name]) for name in ("a", "b", "c", "p", "q"))
    assert not os.path.exists(paths["held_folder"])
    assert (result.files_removed, result.denied, result.in_use) == (5, 1, 0)


def test_a_safety_check_turns_away_only_the_entry_that_fails_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _tree(tmp_path)
    windows = _FakeWindows(monkeypatch, _errno_shape)
    # One file and one folder resolve outside the approved folder once opened.
    windows.escaped = {paths["c"], paths["sub"]}

    result = _purge_tree(paths["root"])

    assert result.completed and not result.root_absent
    assert result.unsafe == 2 and result.in_use == result.denied == 0
    assert os.path.exists(paths["c"]) and os.path.exists(paths["x"])
    assert not any(os.path.exists(paths[name]) for name in ("a", "b", "p", "q"))
    reasons = dict(result.examples)
    assert reasons[paths["c"]].startswith("安全检查未通过，只跳过这一项")
    assert "离开了要清理的目录" in reasons[paths["sub"]]


@pytest.mark.parametrize("shape", sorted(_SHAPES))
def test_an_entry_gone_on_its_own_does_not_keep_its_folders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    paths = _tree(tmp_path)
    windows = _FakeWindows(monkeypatch, _SHAPES[shape])
    windows.vanishing = {paths["y"]}

    result = _purge_tree(paths["root"])

    assert result.completed and result.root_absent
    assert result.in_use == result.denied == result.unsafe == 0


def test_an_unknown_error_still_stops_the_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _tree(tmp_path)
    windows = _FakeWindows(monkeypatch, _errno_shape)
    windows.held = {paths["b"]: 1117}  # ERROR_IO_DEVICE: not a reason to carry on

    with pytest.raises(OSError):
        _purge_tree(paths["root"])


@pytest.mark.parametrize("shape", sorted(_SHAPES))
def test_a_temp_folder_is_emptied_the_same_way_and_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    paths = _tree(tmp_path)
    windows = _FakeWindows(monkeypatch, _SHAPES[shape])
    windows.held = {paths["x"]: _IN_USE, paths["held_folder"]: _IN_USE}

    def keep(path: str, _is_directory: bool, _size: int, _written: int | None) -> str | None:
        return "你设为保留" if path == paths["a"] else None

    result = exact_cleanup._purge_temp_folder_contents_windows(
        paths["root"],
        ExactDirectorySnapshot(1, "root", "file_id_128", 1),
        ExactRootBoundary(tmp_path, 1, "boundary", "file_id_128"),
        keep,
        None,
        None,
    )

    assert result.completed and os.path.isdir(paths["root"])
    assert sorted(os.listdir(paths["root"])) == ["a.bin", "held", "sub"]
    assert os.listdir(paths["held_folder"]) == []
    assert (result.files_removed, result.in_use, result.kept) == (6, 2, 1)


# ---- the same rules on the POSIX implementation (Linux test fixtures) ----------


def _arguments(folder: Path, boundary: Path) -> tuple[ExactDirectorySnapshot, ExactRootBoundary]:
    target = read_file_metadata(folder)
    outer = read_file_metadata(boundary)
    assert target.volume_serial is not None and target.file_id is not None
    assert target.file_id_kind is not None and target.creation_time_ns is not None
    assert outer.volume_serial is not None and outer.file_id is not None
    assert outer.file_id_kind is not None
    return (
        ExactDirectorySnapshot(
            target.volume_serial, target.file_id, target.file_id_kind, target.creation_time_ns
        ),
        ExactRootBoundary(boundary, outer.volume_serial, outer.file_id, outer.file_id_kind),
    )


def test_a_folder_swapped_for_a_link_mid_cleanup_is_skipped_and_the_rest_goes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _tree(tmp_path)
    snapshot, boundary = _arguments(Path(paths["root"]), tmp_path)
    if os.name == "nt":
        real_check = exact_cleanup._require_purge_directory

        def check(handle: Any, path: str, root: str) -> None:
            if path == paths["sub"]:
                raise ExactCleanupError("selected tree directory became a reparse/cloud boundary")
            real_check(handle, path, root)

        monkeypatch.setattr(exact_cleanup, "_require_purge_directory", check)
    else:
        real_open = os.open

        def open_entry(name: Any, flags: int, *args: Any, **kwargs: Any) -> int:
            if name == "sub" and kwargs.get("dir_fd") is not None:
                raise OSError(errno.ELOOP, "Too many levels of symbolic links", name)
            return real_open(name, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", open_entry)

    result = purge_directory_tree_skipping_in_use(Path(paths["root"]), snapshot, boundary)

    assert result.completed and not result.root_absent and result.unsafe == 1
    assert all(os.path.exists(paths[name]) for name in ("x", "y", "z"))
    assert not any(os.path.exists(paths[name]) for name in ("a", "b", "c", "p", "q"))


def test_a_folder_that_cannot_be_listed_is_left_on_the_real_filesystem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _tree(tmp_path)
    snapshot, boundary = _arguments(Path(paths["root"]), tmp_path)
    locked = os.stat(paths["sub"]).st_ino
    suffix = os.path.normcase(os.path.join("Cache", "sub"))
    real_scandir = os.scandir

    def scandir(target: Any = ".") -> Any:
        if isinstance(target, int):
            hit = os.fstat(target).st_ino == locked
        else:
            hit = os.path.normcase(os.fspath(target)).endswith(suffix)
        if hit:
            denied = PermissionError(errno.EACCES, "Access is denied", paths["sub"])
            if os.name == "nt":
                denied.winerror = _DENIED
            raise denied
        return real_scandir(target)

    monkeypatch.setattr(os, "scandir", scandir)

    result = purge_directory_tree_skipping_in_use(Path(paths["root"]), snapshot, boundary)

    assert result.completed and not result.root_absent
    assert (result.denied, result.files_removed) == (1, 5)
    assert all(os.path.exists(paths[name]) for name in ("x", "y", "z"))


# ---- what the window shows after a cleanup ------------------------------------


class _Var:
    def __init__(self) -> None:
        self.value = ""

    def set(self, value: str) -> None:
        self.value = value

    def get(self) -> str:
        return self.value


def _left_behind(path: str, *, files: int, in_use: int) -> TempFolderPurgeResult:
    return TempFolderPurgeResult(
        root_path=path,
        files_removed=files,
        links_removed=0,
        directories_removed=0,
        bytes_removed=files * 1024,
        in_use=in_use,
        denied=0,
        kept=0,
        completed=True,
        examples=((path + r"\data_0", "正被程序使用"),),
    )


def test_the_status_and_the_report_say_what_went_what_stayed_and_why() -> None:
    from types import SimpleNamespace

    from devclean.core.application_cleanup import RUNNING_OWNER_REFUSAL, running_owner_reason
    from devclean.core.cleanup_journal import ActionState

    done, partly, gone = r"C:\c\Cache", r"C:\c\GPUCache", r"C:\c\vanished.bin"
    refused, rechecked, odd = r"C:\c\.tmp\plugins.sync.lock", r"C:\c\Code Cache", r"C:\c\odd"
    window: Any = app.DevCleanWindow.__new__(app.DevCleanWindow)
    paths = (done, partly, refused, rechecked, odd)
    window._deletable = [SimpleNamespace(path=path) for path in paths]
    window._unsure = []
    window._deletable_tree = window._unsure_tree = None
    window._checked = set(paths)
    window._cleanup_failures = {}
    window._status = _Var()
    window._fill = lambda *_args: None
    window._refresh_totals = lambda: None
    window._sync_buttons = lambda: None
    running = f"{RUNNING_OWNER_REFUSAL}: codex: codex-plugin-sync-lock"
    long_detail = "PermissionError: [WinError 5] Access is denied: '" + "x" * 200 + "'"
    odd_message = "an unexpected safety refusal " + "y" * 200
    result = CleanupExecutionResult(
        action_states=(
            ("a1", ActionState.PURGED),
            ("a2", ActionState.PURGED),
            ("a3", ActionState.FAILED_UNCHANGED),
            ("a4", ActionState.FAILED_UNCHANGED),
            ("a5", ActionState.FAILED_UNCHANGED),
        ),
        purged_logical_bytes=3 * 1024,
        completed_paths=(done,),
        failures=(
            (refused, CleanupRefusal(running)),
            (
                rechecked,
                CleanupRefusal(
                    "the selected folder could not be read again before deletion: " + long_detail
                ),
            ),
            (odd, CleanupRefusal(odd_message)),
        ),
        partial=((partly, _left_behind(partly, files=0, in_use=5)),),
        removed=((done, 3, 3 * 1024), (partly, 0, 0)),
    )

    window._report_deletion((result,), ((gone, "扫描后已自行消失"),))

    status = window._status.get()
    # A folder that kept everything is not counted as cleaned.
    assert status.startswith("已删除 1 项；1 项删了能删的部分，被占用等删不掉的留下；")
    assert "4 项未完成" in status
    assert "删了一部分的项共留下：正被程序使用 5 项" in status
    assert "见「清理结果」" in status
    name, why = running_owner_reason(running) or ("", "")
    assert f"{name} 正在运行，这一项要等它关闭后再清理：{why} 1 项" in status

    report = window._cleanup_report
    assert report.startswith("全部删除 1 项，删了一部分 1 项，未删除 4 项；共释放 3.0 KB。")
    assert f"• {partly}\n    已清理 0 个文件（0 B），留下：正被程序使用 5 项" in report
    assert f"      {partly}\\data_0 —— 正被程序使用" in report
    assert f"• {done} —— 3 个文件，3.0 KB" in report
    # Every reason in full: nothing cut off.
    assert f"    原因：删除前复检：这个目录本身无法再次读取（{long_detail}）" in report
    assert f"    原因：安全检查拒绝（{odd_message}）" in report
    assert f"• {gone}\n    原因：扫描后已自行消失" in report
    # Rows that stayed keep their tick and show what happened to them.
    assert window._checked == {partly, refused, rechecked, odd}
    assert window._cleanup_failures[partly].startswith("已清理 0 个文件（0 B），留下：")
    assert window._row_reason(odd, "") == f"上次清理未完成：安全检查拒绝（{odd_message}）"


def test_a_long_status_wraps_at_the_window_width_instead_of_being_cut_off() -> None:
    from types import SimpleNamespace

    configured: dict[str, int] = {}
    bound: list[Callable[[Any], None]] = []

    class Label:
        def configure(self, **options: int) -> None:
            configured.update(options)

    class Container:
        def bind(self, sequence: str, handler: Callable[[Any], None], add: str) -> None:
            assert (sequence, add) == ("<Configure>", "+")
            bound.append(handler)

    app.wrap_to_width(Label(), Container(), 24)  # type: ignore[arg-type]
    (handler,) = bound
    handler(SimpleNamespace(width=1000))
    assert configured == {"wraplength": 976}


def test_double_clicking_a_box_ticks_it_twice_instead_of_opening_explorer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    row = r"C:\Users\me\AppData\Local\Temp"

    class Tree:
        column = "#1"

        def identify_region(self, _x: int, _y: int) -> str:
            return "cell"

        def identify_column(self, _x: int) -> str:
            return self.column

        def identify_row(self, _y: int) -> str:
            return row

        def set(self, _row: str, _column: str, _value: str) -> None:
            return None

    tree = Tree()
    window: Any = app.DevCleanWindow.__new__(app.DevCleanWindow)
    window._deletable_tree = tree
    window._checked = {row}
    window._status = _Var()
    window._refresh_totals = lambda: None
    window._sync_buttons = lambda: None
    opened: list[str] = []

    def open_in_explorer(path: str) -> str:
        opened.append(path)
        return path

    monkeypatch.setattr(app, "_open_path_in_explorer", open_in_explorer)
    event = SimpleNamespace(widget=tree, x=5, y=5)

    # Tk sends a double click's first press as a click, its second only here.
    window._on_row_click(event)
    assert row not in window._checked
    window._on_row_double_click(event)
    assert row in window._checked and opened == []

    tree.column = "#4"  # the path: a double click there still opens Explorer
    window._on_row_double_click(event)
    assert opened == [row] and row in window._checked


def test_reason_texts_for_the_whole_tree_recheck_are_in_the_users_terms() -> None:
    cases = {
        "application whole-tree authority is no longer a deletable TOOL rule": (
            "删除前复检：这个目录已不再属于可以整目录删除的应用缓存"
        ),
        "fresh whole-tree policy scan has no reliable last-use timestamp": (
            "删除前复检：读不到这个目录的任何时间信息，无法确认它的使用情况"
        ),
    }
    for message, shown in cases.items():
        assert app._reason_of(CleanupRefusal(message)) == shown


def test_a_tree_left_only_for_a_safety_skip_is_partly_cleaned_not_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace
    from queue import Queue

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
    from devclean.platform.windows.filesystem import FileSystemMetadata
    from devclean.scanner import CancellationToken
    from devclean.ui import product_app

    def windows_identity(path: str | Path) -> FileSystemMetadata:
        return replace(
            read_file_metadata(path), file_id_kind="file_id_128", attributes=0x10, link_count=1
        )

    root = tmp_path / "cache"
    _write(root / "entry.bin", b"data")
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
    monkeypatch.setattr(product_app, "read_file_metadata", windows_identity)
    monkeypatch.setattr(postscan_cleanup, "read_file_metadata", windows_identity)
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
    swapped = str(root / "swapped")

    def purge(path: Path, *_args: object, **_kwargs: object) -> DirectoryPurgeResult:
        # Everything went except one entry a safety check turned away.
        return DirectoryPurgeResult(
            root_path=str(path),
            files_removed=3,
            links_removed=0,
            directories_removed=2,
            bytes_removed=12,
            root_absent=False,
            completed=True,
            examples=((swapped, "安全检查未通过，只跳过这一项（清理时它变成了链接或云端占位项）"),),
            unsafe=1,
        )

    result = postscan_cleanup.execute_cleanup_batch(
        postscan_cleanup.prepare_cleanup_batch((candidate,)),
        CleanupMode.PERMANENT,
        delete_config=rules.delete.classification,
        keep_config=rules.keep.classification,
        known_roots=(known,),
        journal=CleanupJournal(tmp_path / "state" / "journal.db"),
        directory_purger=purge,
    )

    assert result.failures == ()
    assert result.action_states[0][1] is ActionState.PURGED
    assert result.completed_paths == ()
    ((path, left),) = result.partial
    assert path == str(root) and left.unsafe == 1 and left.files_removed == 3
    assert result.removed == ((str(root), 3, 12),)
    assert "安全检查未通过、只跳过它们自己 1 项" in app._temp_folder_note(left)
