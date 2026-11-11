"""A cleanup shows each row as it ends, never waits on a command, and stays fast.

User report 2026-11-11 on 9459930: "正在清理 128 项…" for a long time and nothing
removed.  The product cleanup told the window nothing until every row -- a
3.5 GB Temp folder first -- was over, and emptying a temp folder checked each
entry against every running program at a cost that grew with the process list.

Here: the official package-manager commands never wait for input; the faster
per-entry checks give exactly the answers the original ones gave; a file in
use is tried once; the window updates a row the moment it ends.  Everything
runs on folders inside ``tmp_path``; no real command is started.
"""

# Expected UI strings are Chinese prose with fullwidth punctuation.
# ruff: noqa: RUF001

from __future__ import annotations

import fnmatch
import os
import random
import subprocess
from pathlib import Path, PureWindowsPath
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from devclean.core import _application_cleanup_impl as impl
from devclean.core import (
    conda_maintenance,
    edge_cleanup,
    go_maintenance,
    live_owners,
    nuget_maintenance,
    pip_maintenance,
    pnpm_maintenance,
    uv_maintenance,
)
from devclean.core.cleanup_journal import ActionState
from devclean.core.installer_files import is_within
from devclean.core.postscan_cleanup import CleanupExecutionResult, CleanupRefusal
from devclean.platform.windows import exact_cleanup
from devclean.platform.windows.exact_cleanup import TempFolderPurgeResult
from devclean.ui import app
from tests.test_cleanup_skip_continue import _errno_shape, _FakeWindows, _purge_tree, _tree

# ---- official commands -------------------------------------------------------------


@pytest.mark.parametrize(
    ("module", "runner"),
    [
        (go_maintenance, "_run_go"),
        (nuget_maintenance, "_run_dotnet"),
        (pip_maintenance, "_run_pip"),
        (uv_maintenance, "_run_uv"),
        (pnpm_maintenance, "_run_pnpm"),
        (conda_maintenance, "_run_conda"),
    ],
)
def test_official_commands_never_wait_for_input_and_stop_at_their_timeout(
    monkeypatch: pytest.MonkeyPatch, module: ModuleType, runner: str
) -> None:
    seen: dict[str, Any] = {}

    def run(command: Any, **options: Any) -> subprocess.CompletedProcess[str]:
        seen.update(options)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(module.subprocess, "run", run)
    getattr(module, runner)(("tool", "clean"), {}, timeout=7)
    # A prompt has nobody to answer it: it fails at once instead of waiting.
    assert seen["stdin"] is subprocess.DEVNULL
    assert seen["timeout"] == 7

    def hang(command: Any, **options: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(command, options["timeout"])

    monkeypatch.setattr(module.subprocess, "run", hang)
    with pytest.raises(RuntimeError):  # one failed row, not a dead cleanup
        getattr(module, runner)(("tool", "clean"), {}, timeout=7)


# ---- the faster checks answer exactly as before ------------------------------------


def _linear_owner(folders: tuple[tuple[str, str], ...], target: str) -> str | None:
    """The original lookup: the first folder in order that *target* lies inside."""

    return next((name for folder, name in folders if is_within(target, folder)), None)


def test_the_owning_folder_lookup_finds_what_the_folder_scan_found() -> None:
    random.seed(20261111)
    sep = os.sep
    base = f"c:{sep}" if os.name == "nt" else sep
    names = ["a", "b", "ab", "a b", "temp", "x.y"]

    def folder(depth: int) -> str:
        return base + sep.join(random.choice(names) for _ in range(depth))

    for _round in range(300):
        folders: dict[str, str] = {}
        for index in range(random.randint(0, 12)):
            path = folder(random.randint(1, 4))
            if random.random() < 0.1:
                path += sep  # a trailing separator must not matter
            folders.setdefault(path, f"owner{index}")
        if random.random() < 0.05:
            folders.setdefault(base, "drive-wide")
        ordered = tuple(folders.items())
        rank: dict[str, tuple[int, str]] = {}
        for position, (path, owner) in enumerate(ordered):
            rank.setdefault(path.rstrip(sep), (position, owner))
        owners = live_owners._OwnerIndex({}, ordered, rank)
        for _target in range(40):
            target = folder(random.randint(1, 6))
            assert owners.owning_folder(target) == _linear_owner(ordered, target), (ordered, target)


def test_hidden_process_relations_are_the_same_when_worked_out_once_per_target(
    tmp_path: Path,
) -> None:
    hidden = [
        live_owners.HiddenProcess("tool.exe", None, None, "tool"),
        live_owners.HiddenProcess("helper.exe", None, None, None),
        live_owners.HiddenProcess("acmesync.exe", None, None, "acmesync"),
        live_owners.HiddenProcess(
            "agent.exe", str(tmp_path / "Agent" / "agent.exe"), str(tmp_path / "Agent"), None
        ),
        live_owners.HiddenProcess("msiexec.exe", None, None, None),
    ]
    targets = [
        tmp_path / "Temp" / "tool-cache" / "x.bin",
        tmp_path / "Temp" / "AcmeSyncData" / "y",
        tmp_path / "Temp" / "plain" / "z.msi",
        tmp_path / "Agent" / "agent.exe",
        tmp_path / "Temp" / "helper.exe",
        tmp_path / "Temp" / "unrelated" / "file.txt",
    ]
    for target in targets:
        normalized = os.path.normcase(str(target))
        written = str(target)
        shared = live_owners._NamedParts(
            live_owners._parts(normalized), live_owners._parts(written)
        )
        for process in hidden:
            for directory in (False, True):
                fresh = live_owners._relation(process, normalized, written, directory=directory)
                reused = live_owners._relation(
                    process, normalized, written, directory=directory, named=shared
                )
                assert reused == fresh, (process, target, directory)


def _original_matches(path: str, candidate: str, kind: impl.MatchKind) -> bool:
    candidate = candidate.casefold()
    if kind is impl.MatchKind.EXACT:
        return path == candidate
    if kind is impl.MatchKind.PREFIX:
        return path == candidate or path.startswith(candidate.rstrip("\\") + "\\")
    if fnmatch.fnmatchcase(path, candidate):
        return True
    if not any(char in candidate for char in "*?["):
        return path.startswith(candidate.rstrip("\\") + "\\")
    return False


def _original_could_hold(root: str, path: str) -> bool:
    return (
        path == root
        or path.startswith(root.rstrip("\\") + "\\")
        or any(char in root for char in "*?[")
    )


def test_cached_rule_matching_answers_as_the_original_did() -> None:
    roots = ["c:\\users\\me", "c:\\users\\me\\", "c:\\users\\me\\appdata\\local\\temp", "c:\\x*"]
    patterns = ["", "cache", "Cache", "logs\\*.log", "{a,b}", "msedge_installer.log", "*"]
    paths = [
        "c:\\users\\me",
        "c:\\users\\me\\cache",
        "c:\\users\\me\\cache\\f",
        "c:\\users\\me\\logs\\x.log",
        "c:\\users\\mex\\cache",
        "c:\\users\\me\\appdata\\local\\temp\\msedge_installer.log",
        "c:\\xyz\\q",
    ]
    for root in roots:
        for path in paths:
            assert impl._could_hold(root, path) == _original_could_hold(root, path)
        for pattern in patterns:
            candidate = root + ("\\" + pattern if pattern else "")
            for kind in impl.MatchKind:
                for path in paths:
                    expected = _original_matches(path, candidate, kind)
                    assert impl._matches(path, candidate, kind) == expected


def test_the_edge_profile_lookup_answers_as_before_without_walking_parents() -> None:
    data_root = PureWindowsPath(r"C:\Users\Me\AppData\Local\Microsoft\Edge\User Data")
    paths = [
        r"C:\Users\Me\AppData\Local\Microsoft\Edge\User Data\Default\Cache\x",
        r"c:\users\me\appdata\local\microsoft\edge\user data\Profile 2\Code Cache",
        r"C:\Users\Me\AppData\Local\Microsoft\Edge\User Data",
        r"C:\Users\Me\AppData\Local\Microsoft\Edge\User Data\Crashpad\x",
        r"C:\Users\Me\AppData\Local\Microsoft\Edge\User Data2\Default",
        r"C:\Users\Me\AppData\Local\Temp\msedge_installer.log",
    ]

    def original(path: str) -> PureWindowsPath | None:
        try:
            relative = PureWindowsPath(path).relative_to(data_root)
        except ValueError:
            return None
        if not relative.parts or not edge_cleanup._is_profile_dir_name(relative.parts[0]):
            return None
        return data_root / relative.parts[0]

    for path in paths:
        assert edge_cleanup._profile_root_for_path(path, data_root) == original(path), path


# ---- a file in use is tried once -------------------------------------------------


def test_a_file_in_use_is_tried_once_and_never_waited_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _tree(tmp_path)
    windows = _FakeWindows(monkeypatch, _errno_shape)
    windows.held = {paths["b"]: 32, paths["x"]: 32}
    attempts: dict[str, int] = {}
    delete = exact_cleanup._delete_leaf

    def counted(path: str, root: str, **options: Any) -> None:
        attempts[path] = attempts.get(path, 0) + 1
        delete(path, root, **options)

    monkeypatch.setattr(exact_cleanup, "_delete_leaf", counted)
    monkeypatch.setattr("time.sleep", _no_waiting)

    result = _purge_tree(paths["root"])

    assert result.in_use == 2
    assert attempts[paths["b"]] == attempts[paths["x"]] == 1


def _no_waiting(_seconds: float) -> None:
    raise AssertionError("cleanup must not wait for a file in use")


# ---- the window shows each row as it ends --------------------------------------------


class _Rows:
    def __init__(self, rows: list[str]) -> None:
        self.rows = {row: "" for row in rows}

    def exists(self, row: str) -> bool:
        return row in self.rows

    def delete(self, *rows: str) -> None:
        for row in rows:
            del self.rows[row]

    def set(self, row: str, column: str, value: str) -> None:
        assert column == "reason"
        self.rows[row] = value


class _Text:
    def __init__(self) -> None:
        self.value = ""

    def set(self, value: str) -> None:
        self.value = value


def test_each_row_is_shown_the_moment_it_ends() -> None:
    gone, partly, refused = r"C:\c\Cache", r"C:\c\Temp", r"C:\c\locked.bin"
    window: Any = app.DevCleanWindow.__new__(app.DevCleanWindow)
    window._deletable_tree = _Rows([gone, partly, refused])
    window._cleanup_failures = {}
    window._status = _Text()
    window._busy = "deleting"
    window._root = None
    progress: dict[str, Any] = {}
    window._progress = SimpleNamespace(configure=lambda **options: progress.update(options))
    synced: list[int] = []
    window._sync_buttons = lambda: synced.append(1)
    left = TempFolderPurgeResult(partly, 40, 0, 3, 4096, 2, 0, 0, True)

    window._item_done(
        1,
        3,
        CleanupExecutionResult(
            action_states=(("a1", ActionState.PURGED),),
            purged_logical_bytes=2048,
            completed_paths=(gone,),
            removed=((gone, 5, 2048),),
        ),
        None,
    )
    assert gone not in window._deletable_tree.rows
    assert window._status.value.startswith("正在清理… 已处理 1 / 3 项：已清理 1 项")
    assert progress == {"mode": "determinate", "maximum": 3, "value": 1}
    assert synced == [1]  # the report can be opened from now on

    window._item_done(
        2,
        3,
        CleanupExecutionResult(
            action_states=(("a2", ActionState.PURGED),),
            purged_logical_bytes=4096,
            completed_paths=(),
            partial=((partly, left),),
            removed=((partly, 40, 4096),),
        ),
        None,
    )
    window._item_done(3, 3, None, (refused, app._reason_of(CleanupRefusal("x" * 10))))

    assert window._deletable_tree.rows[partly].startswith(
        "上次清理未完成：已清理 40 个文件（4.0 KB），留下：正被程序使用 2 项"
    )
    assert window._deletable_tree.rows[refused] == "上次清理未完成：安全检查拒绝（xxxxxxxxxx）"
    status = window._status.value
    assert "已处理 3 / 3 项：已清理 1 项、删了一部分 1 项、未完成 1 项，已释放 6.0 KB" in status

    # The report can be read before the run is over.
    window._show_cleanup_report()
    report = window._cleanup_report
    assert report.startswith("全部删除 1 项，删了一部分 1 项，未删除 1 项；共释放 6.0 KB。")
    assert f"• {partly}" in report and f"• {refused}" in report and f"• {gone}" in report


def _linear_inside(index: live_owners._OwnerIndex, target: str) -> str | None:
    """The original lookup: the first used path (files, then folders) at or in *target*."""

    return next(
        (
            name
            for used, name in (*index.files.items(), *index.folders)
            if used == target or is_within(used, target)
        ),
        None,
    )


def test_the_used_inside_lookup_finds_what_the_scan_of_every_path_found() -> None:
    random.seed(1111)
    sep = os.sep
    base = f"c:{sep}" if os.name == "nt" else sep
    names = ["a", "b", "ab", "a b", "temp"]

    def path(depth: int) -> str:
        return base + sep.join(random.choice(names) for _ in range(depth))

    for _round in range(300):
        files = {
            path(random.randint(1, 5)): f"file{index}" for index in range(random.randint(0, 8))
        }
        folders = {
            path(random.randint(1, 4)): f"folder{index}" for index in range(random.randint(0, 8))
        }
        ordered = tuple(folders.items())
        used: dict[str, tuple[int, str]] = {}
        for position, (used_path, owner) in enumerate((*files.items(), *ordered)):
            used.setdefault(used_path, (position, owner))
        index = live_owners._OwnerIndex(files, ordered, {}, used, tuple(sorted(used)))
        for _target in range(40):
            target = path(random.randint(1, 5))
            assert index.first_used_inside(target) == _linear_inside(index, target), target
