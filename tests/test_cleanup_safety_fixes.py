"""Regression tests for docs/cleanup-safety-fixes-2026-11-07.md.

Everything runs on temporary fixtures. Program starts are recorded or refused,
never executed, and the only real deletion is of a file inside ``tmp_path``.
"""

# Expected UI strings are Chinese prose with fullwidth punctuation.
# ruff: noqa: RUF001

from __future__ import annotations

import os
import stat
import subprocess
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from queue import Queue
from types import MethodType, SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest

from devclean.core._application_cleanup_impl import ConfigCachedQuery
from devclean.core.application_cleanup import (
    PROTECTED_OWNER_REFUSAL,
    RUNNING_OWNER_REFUSAL,
    file_time_utc,
    match_application_rule,
    process_guard_allows,
    process_guard_refusal,
)
from devclean.core.cleanup_journal import ActionState
from devclean.core.npm_maintenance import _resolve_npm_tool
from devclean.core.postscan_cleanup import CleanupExecutionResult, CleanupRefusal
from devclean.core.review_routing import route_unresolved_file_to_ai
from devclean.core.triage import (
    TriageItem,
    triage_file,
)
from devclean.core.user_rules import default_rules
from devclean.platform.windows.executables import path_tool, resolve_command
from devclean.platform.windows.filesystem import FileSystemMetadata
from devclean.platform.windows.subprocess_policy import _absolute_program_arguments
from devclean.scanner import CancellationToken, ScanOptions, scan_roots
from devclean.scanner.filesystem import ScanRecord, ScanRecordKind
from devclean.ui import app, product_app

_RECENT_NS = 1_700_000_000 * 1_000_000_000
_WINDOWS_DEFAULT_PATHEXT = ".COM;.EXE;.BAT;.CMD"
_UNREPRESENTABLE_NS = 400_000_000_000 * 1_000_000_000  # about year 14645


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    profile = tmp_path / "home"
    (profile / "Downloads").mkdir(parents=True)
    for name, value in {
        "USERPROFILE": profile,
        "APPDATA": profile / "AppData" / "Roaming",
        "LOCALAPPDATA": profile / "AppData" / "Local",
        "TEMP": profile / "AppData" / "Local" / "Temp",
        "TMP": profile / "AppData" / "Local" / "Temp",
        "PROGRAMDATA": tmp_path / "ProgramData",
    }.items():
        monkeypatch.setenv(name, str(value))
    return profile


@pytest.fixture
def launches(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Record the program name of every attempted start; nothing is run."""

    started: list[str] = []

    def fake_run(args: Any, *_args: object, **_kwargs: object) -> object:
        started.append(os.path.basename(str(args[0])))
        raise FileNotFoundError(str(args[0]))

    monkeypatch.setattr(subprocess, "run", fake_run)
    yield started


def _metadata(path: Path) -> FileSystemMetadata:
    directory = path.is_dir()
    return FileSystemMetadata(
        is_directory=directory,
        logical_size=0 if directory else 10,
        allocation_size=0 if directory else 10,
        volume_serial=9,
        file_id=f"{abs(hash(str(path))):032x}"[-32:],
        file_id_kind="file_id_128",
        link_count=1,
        attributes=0x10 if directory else 0,
        reparse_tag=None,
        is_reparse_point=False,
        is_cloud_placeholder=False,
        creation_time_ns=100,
        last_write_time_ns=200,
    )


def _record(path: Path, root: Path, *, mtime_ns: int = 200) -> ScanRecord:
    metadata = _metadata(path)
    return ScanRecord(
        root=str(root),
        path=str(path),
        kind=ScanRecordKind.FILE,
        depth=len(path.relative_to(root).parts),
        logical_size=10,
        allocated_size=10,
        raw_allocated_size=10,
        volume_serial=metadata.volume_serial,
        file_id=metadata.file_id,
        file_id_kind=metadata.file_id_kind,
        link_count=1,
        attributes=0,
        creation_time_ns=100,
        last_write_time_ns=mtime_ns,
    )


def _classify(path: Path, root: Path, home: Path, *, mtime_ns: int = 200) -> TriageItem:
    rules = default_rules()
    return route_unresolved_file_to_ai(
        triage_file(
            _record(path, root, mtime_ns=mtime_ns),
            delete_config=rules.delete.classification,
            keep_config=rules.keep.classification,
            now=datetime.now(UTC),
            temp_root=home / "AppData" / "Local" / "Temp",
        )
    )


def _executable(path: Path, marker: Path) -> Path:
    """A program that would leave *marker* behind if it were ever started.

    Windows starts a bare name only through a PATHEXT extension (npm is
    ``npm.cmd``), so there the file gets one; elsewhere it is a shell script.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        path = path.with_name(path.name + ".cmd")
        path.write_text(f'@echo planted > "{marker}"\r\n', encoding="utf-8")
        return path
    path.write_text(f"#!/bin/sh\necho planted > '{marker}'\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


# ---- links: a symlink is a scan boundary, never a file candidate ------------


def test_symlinked_installer_in_downloads_is_a_boundary_not_a_file(home: Path) -> None:
    target = home / "AppData" / "Local" / "Programs" / "App" / "App.exe"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"installed program")
    link = home / "Downloads" / "App-Setup.exe"
    link.symlink_to(target)

    records = tuple(scan_roots((home / "Downloads",), ScanOptions(exact_file_identity=False)))
    by_path = {Path(record.path): record for record in records}

    assert by_path[link].kind is ScanRecordKind.BOUNDARY
    assert target not in by_path


# ---- H-1: programs are started by absolute path only --------------------------


def test_path_tool_never_picks_a_program_planted_in_the_current_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "ran.txt"
    planted = tmp_path / "Downloads"
    _executable(planted / "npm", marker)
    monkeypatch.chdir(planted)
    monkeypatch.setenv("PATHEXT", _WINDOWS_DEFAULT_PATHEXT)

    assert path_tool("npm", search_path=os.pathsep.join([".", "", "Downloads"])) is None
    assert path_tool("./npm") is None
    trusted = _executable(tmp_path / "tools" / "npm", marker)
    assert path_tool("npm", search_path=os.pathsep.join([".", str(trusted.parent)])) == str(trusted)
    assert not marker.exists()


def test_windows_tools_resolve_to_system32(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SYSTEMROOT", r"C:\Windows")

    assert resolve_command(["powershell.exe", "-NoProfile"]) == [
        r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
        "-NoProfile",
    ]
    assert resolve_command(["tasklist", "/FO", "CSV"])[0] == r"C:\Windows\System32\tasklist.exe"


def test_gui_process_policy_starts_programs_by_absolute_path_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "ran.txt"
    planted = tmp_path / "Downloads"
    _executable(planted / "go", marker)
    monkeypatch.chdir(planted)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setenv("PATHEXT", _WINDOWS_DEFAULT_PATHEXT)

    with pytest.raises(FileNotFoundError):
        _absolute_program_arguments([["go", "env"]], {})
    with pytest.raises(FileNotFoundError):
        _absolute_program_arguments([["./go", "env"]], {})
    with pytest.raises(ValueError, match="argument list"):
        _absolute_program_arguments(["go env"], {})

    trusted = _executable(tmp_path / "tools" / "go", marker)
    monkeypatch.setenv("PATH", str(trusted.parent))
    positional: list[Any] = [["go", "env"]]
    _absolute_program_arguments(positional, {})
    assert positional[0] == [str(trusted), "env"]
    explicit: dict[str, Any] = {"args": ["anything"], "executable": str(trusted)}
    _absolute_program_arguments([], explicit)
    assert explicit["args"] == ["anything"]
    assert not marker.exists()


def test_maintenance_tool_lookup_skips_relative_path_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "ran.txt"
    _executable(tmp_path / "Downloads" / "npm", marker)
    monkeypatch.chdir(tmp_path / "Downloads")

    with pytest.raises(FileNotFoundError, match="npm"):
        _resolve_npm_tool({"PATH": "."})
    assert not marker.exists()


# ---- M-1 / M-2: delete-time ownership and cache freshness ---------------------


def test_config_cached_query_asks_again_only_when_its_config_changes(tmp_path: Path) -> None:
    config = tmp_path / ".npmrc"
    calls: list[int] = []

    def query() -> int:
        calls.append(1)
        return len(calls)

    cached = ConfigCachedQuery(query, lambda: (str(config),))
    assert cached() == 1
    assert cached() == 1
    config.write_text("cache=elsewhere\n", encoding="utf-8")
    assert cached() == 2
    assert cached() == 2
    cached.cache_clear()
    assert cached() == 3


def test_package_manager_moved_after_scan_is_caught_without_starting_other_tools(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = {"cache": r"D:\old-npm-cache"}
    started: list[str] = []

    def fake_run(args: Any, *_args: object, **_kwargs: object) -> object:
        program = os.path.basename(str(args[0]))
        started.append(program)
        if program.startswith("npm") and list(args[1:3]) == ["config", "get"]:
            output = f"cache={state['cache']}\nprefix=\nuserconfig=\nlogs-dir=\n"
            return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")
        raise FileNotFoundError(program)

    monkeypatch.setattr(subprocess, "run", fake_run)
    target = r"E:\shared\npm-cache\_cacache\content-v2\sha512\aa\blob"
    assert match_application_rule(target) is None  # the completed scan

    # The user moves npm's cache after the scan; npm writes the new location
    # into the user config file.
    (home / ".npmrc").write_text("cache=E:\\shared\\npm-cache\n", encoding="utf-8")
    state["cache"] = r"E:\shared\npm-cache"
    started.clear()

    assert process_guard_refusal(target) == PROTECTED_OWNER_REFUSAL
    assert not process_guard_allows(target)
    # Only tools that read the changed .npmrc are asked again (pnpm reads it too);
    # go, uv, pip, conda, dotnet, yarn and PowerShell are not started.
    asked = {name.removesuffix(".cmd") for name in started}
    assert "npm" in asked
    assert asked <= {"npm", "pnpm"}


def test_owned_item_refresh_leaves_other_tools_cached_for_the_next_ordinary_file(
    monkeypatch: pytest.MonkeyPatch,
    launches: list[str],
) -> None:
    for name, value in {
        "USERPROFILE": r"C:\Users\person",
        "APPDATA": r"C:\Users\person\AppData\Roaming",
        "LOCALAPPDATA": r"C:\Users\person\AppData\Local",
        "TEMP": r"C:\Users\person\AppData\Local\Temp",
    }.items():
        monkeypatch.setenv(name, value)
    ordinary = r"C:\Users\person\Downloads\OldApp-Setup-1.0.exe"
    owned = r"C:\Users\person\AppData\Local\JetBrains\Toolbox\cache\download\idea.zip"
    match_application_rule(ordinary)
    owner = match_application_rule(owned)
    assert owner is not None and owner.app_id == "toolbox"

    process_guard_allows(owned)
    launches.clear()
    assert process_guard_allows(ordinary)
    assert launches == []


def test_refusal_reasons_are_named_for_the_user() -> None:
    assert app._reason_of(CleanupRefusal(PROTECTED_OWNER_REFUSAL)) == (
        "扫描后它已归属受保护的应用数据，已拒绝删除"
    )
    assert (
        app._reason_of(CleanupRefusal(RUNNING_OWNER_REFUSAL)) == "所属程序正在运行，请关闭后再清理"
    )
    assert app._reason_of(CleanupRefusal("candidate changed since the completed scan")) == (
        "扫描后内容已改变，安全检查拒绝"
    )
    named = {
        "selected file was modified since the completed scan": "扫描后内容已改变，安全检查拒绝",
        "hard-linked files cannot become cleanup actions": (
            "文件还有其他硬链接名称，删除这一处不会释放空间，已跳过"
        ),
        "target is outside the approved root": "路径被替换成链接或离开了扫描范围，安全检查拒绝",
        "this item awaits an AI or user decision and was not approved": (
            "需要 AI 或你批准后才能删除，尚未批准"
        ),
    }
    for message, reason in named.items():
        assert app._reason_of(CleanupRefusal(message)) == reason


# ---- M-3: odd file times and unclassifiable files ------------------------------


def test_unrepresentable_file_time_means_unknown_age() -> None:
    assert file_time_utc(None) is None
    assert file_time_utc(_UNREPRESENTABLE_NS) is None
    observed = file_time_utc(_RECENT_NS)
    assert observed is not None and observed.year == 2023


def test_odd_file_time_is_classified_instead_of_raising(home: Path) -> None:
    item = _classify(home / "Documents2" / "odd.bin", home, home, mtime_ns=_UNREPRESENTABLE_NS)
    assert "stale_metadata" not in item.tags


def test_product_scan_survives_odd_times_and_an_unclassifiable_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = "/fixture/scan"
    broken = f"{root}/broken.bin"

    def fake_scan_roots(*_args: object, **_kwargs: object) -> Iterator[ScanRecord]:
        for name, mtime in (
            ("a.txt", _RECENT_NS),
            ("odd.bin", _UNREPRESENTABLE_NS),
            ("broken.bin", _RECENT_NS),
        ):
            yield ScanRecord(
                root=root,
                path=f"{root}/{name}",
                kind=ScanRecordKind.FILE,
                depth=1,
                logical_size=1,
                last_write_time_ns=mtime,
            )

    def triage(record: ScanRecord, **kwargs: Any) -> TriageItem:
        if record.path == broken:
            raise KeyError("unexpected data")
        return triage_file(record, **kwargs)

    monkeypatch.setattr(product_app, "scan_roots", fake_scan_roots)
    monkeypatch.setattr("devclean.ui.product_app.triage_file", triage)
    window = object.__new__(product_app.ProductDevCleanWindow)
    events: Queue[tuple[str, Any]] = Queue()
    window._events = events

    window._scan_worker("scan", (), CancellationToken(), default_rules(), ())

    received = []
    while not events.empty():
        received.append(events.get_nowait())
    kinds = [kind for kind, _payload in received]
    assert "scan_error" not in kinds
    session = next(payload[1] for kind, payload in received if kind == "scan_done")
    assert session.unclassified_files == 1


# ---- L-2 and row display --------------------------------------------------------


def test_scan_error_resets_the_scan_clock() -> None:
    events: Queue[tuple[str, Any]] = Queue()
    events.put(("scan_error", ("token", "OSError: disk vanished")))
    status = Mock()
    fake = SimpleNamespace(
        _events=events,
        _scan_token="token",
        _progress=Mock(),
        _busy="scanning",
        _sync_buttons=Mock(),
        _status=status,
        _scan_failed=Mock(),
        _root=Mock(),
        _drain_events=Mock(),
    )
    app.DevCleanWindow._drain_events(cast(app.DevCleanWindow, fake))

    fake._scan_failed.assert_called_once()
    assert fake._busy is None
    assert "扫描失败" in status.set.call_args.args[0]

    duration = Mock()
    window = object.__new__(product_app.ProductDevCleanWindow)
    window._scan_started_at = cast(float | None, time.monotonic() - 2)
    window._scan_duration = duration
    window._scan_failed()
    assert window._scan_started_at is None
    assert "扫描失败" in duration.set.call_args.args[0]


class _Tree:
    def __init__(self) -> None:
        self.rows: list[tuple[str, tuple[str, ...]]] = []

    def get_children(self) -> tuple[str, ...]:
        return ()

    def delete(self, *_rows: str) -> None:
        self.rows.clear()

    def insert(
        self, _parent: str, _index: object, *, iid: str, values: tuple[str, ...], tags: object
    ) -> None:
        self.rows.append((iid, values))


def _window_with_trees() -> Any:
    fake = SimpleNamespace(
        _deletable_tree=_Tree(),
        _unsure_tree=_Tree(),
        _checked=set(),
        _cleanup_failures={},
        _TICKED=app.DevCleanWindow._TICKED,
        _UNTICKED=app.DevCleanWindow._UNTICKED,
        _size_of=lambda item: item.logical_size,
    )
    for name in ("_fill", "_fill_rows", "_row_reason"):
        setattr(fake, name, MethodType(getattr(app.DevCleanWindow, name), fake))
    return fake


def test_rows_show_action_size_location_and_reason(home: Path) -> None:
    item = _classify(home / "Downloads" / "OldApp-Setup-1.0.exe", home, home)
    fake = _window_with_trees()

    fake._fill(fake._unsure_tree, [item])
    fake._fill(fake._deletable_tree, [item])

    ((_iid, pending),) = fake._unsure_tree.rows
    assert pending[1:] == ("10 B", "待你决定：删除此文件", item.path, item.reason)
    ((_iid, ready),) = fake._deletable_tree.rows
    assert ready[2:] == ("删除此文件", item.path, item.reason)


def test_failed_rows_keep_their_failure_reason(home: Path) -> None:
    item = _classify(home / "Downloads" / "OldApp-Setup-1.0.exe", home, home)
    fake = _window_with_trees()
    fake._deletable = [item]
    fake._unsure = []
    fake._refresh_totals = Mock()
    fake._sync_buttons = Mock()
    fake._status = Mock()
    result = CleanupExecutionResult(
        action_states=(("action", ActionState.FAILED_UNCHANGED),),
        purged_logical_bytes=0,
        completed_paths=(),
        failures=((item.path, CleanupRefusal(RUNNING_OWNER_REFUSAL)),),
    )

    app.DevCleanWindow._report_deletion(
        cast(app.DevCleanWindow, fake),
        (result,),
        (("C:/gone.bin", "扫描后已自行消失"),),
    )

    assert fake._cleanup_failures[item.path] == "所属程序正在运行，请关闭后再清理"
    ((_iid, values),) = fake._deletable_tree.rows
    assert values[4] == "上次清理未完成：所属程序正在运行，请关闭后再清理"
    status = fake._status.set.call_args.args[0]
    assert "2 项未完成" in status
    assert "所属程序正在运行，请关闭后再清理 1 项" in status
    assert "扫描后已自行消失 1 项" in status


def test_windows_codes_carried_in_errno_are_named_like_winerror() -> None:
    # exact_cleanup._raise_windows_error builds OSError(GetLastError(), ...): the
    # Windows code lands in errno, winerror stays None, 32 becomes BrokenPipeError.
    in_use = OSError(32, "The process cannot access the file", "x")
    assert isinstance(in_use, BrokenPipeError)
    assert getattr(in_use, "winerror", None) is None
    assert app._reason_of(in_use) == "被程序占用或权限不足"
    assert app._reason_of(OSError(33, "lock violation", "x")) == "被程序占用或权限不足"
    assert app._reason_of(OSError(5, "access denied", "x")) == "被程序占用或权限不足"
    assert app._reason_of(OSError(3, "path not found", "x")) == "扫描后已自行消失"
    # Other subclasses keep their own meaning: errno is not a Windows code there.
    assert app._reason_of(ConnectionResetError(33, "reset", "x")) != "被程序占用或权限不足"
