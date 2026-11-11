"""End-to-end deletion workflow on real temporary files (Linux fixtures).

These tests drive the real product scan worker, the real three-way partition,
the real product delete worker (candidate, pre-delete checks, journal) and the
real exact-file purge (its non-Windows branch). Only Windows' installed-program
list, process list and service list are fixtures, and every program start is
recorded and refused.
"""

# Expected UI strings are Chinese prose with fullwidth punctuation.
# ruff: noqa: RUF001

from __future__ import annotations

import os
import subprocess
import tempfile
import time
import zipfile
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from queue import Queue
from typing import Any

import pytest

import devclean.core.edge_cleanup as edge_cleanup
import devclean.core.installer_files as installer_files
import devclean.core.live_owners as live_owners
import devclean.core.postscan_cleanup as postscan_cleanup
import devclean.core.triage as triage
import devclean.core.visual_studio_installer_cleanup as visual_studio_installer_cleanup
import devclean.platform.windows.command_paths as command_paths
import devclean.platform.windows.exact_cleanup as exact_cleanup
from devclean.core.cleanup_catalog import discover_known_cleanup_roots
from devclean.core.cleanup_journal import CleanupMode
from devclean.core.postscan_cleanup import (
    CleanupExecutionResult,
    CleanupRefusal,
    candidate_from_triage_item,
)
from devclean.core.triage import ReviewLane, TriageItem, TriageSession
from devclean.core.user_rules import (
    RuleDecision,
    UserRules,
    add_ai_verdicts,
    add_user_verdicts,
    default_rules,
    load_rules,
)
from devclean.platform.windows.processes import ProcessInfo, ProcessListError
from devclean.platform.windows.registry import RegistryStringLookup, ServiceEntry, UninstallEntry
from devclean.scanner import CancellationToken
from devclean.ui import app, product_app, product_vendor_app
from tests import pe_samples


@dataclass
class Workspace:
    root: Path
    home: Path
    downloads: Path
    installed: list[UninstallEntry]
    registry_reads: list[int]
    launches: list[str]
    processes: list[ProcessInfo]
    services: list[ServiceEntry]
    process_reads: list[int]


# Environment variables a test keeps from the host: DevClean's data folder,
# which conftest.py already points into tmp_path, and pytest's own marker.
_KEPT_ENVIRONMENT = frozenset({"DEVCLEAN_DATA_DIR", "PYTEST_CURRENT_TEST"})


@pytest.fixture
def ws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Workspace:
    home = tmp_path / "home"
    downloads = home / "Downloads"
    temp = home / "AppData" / "Local" / "Temp"
    windows = tmp_path / "Windows"
    for folder in (downloads, temp, windows, tmp_path / "ProgramData"):
        folder.mkdir(parents=True, exist_ok=True)
    # The scan finds program and tool folders through environment variables
    # (PROGRAMFILES(X86), ANDROID_HOME, UV_CACHE_DIR, ...). On a CI runner they
    # name real folders, so start from an empty environment and point only the
    # profile and Windows folders into tmp_path.
    for name in list(os.environ):
        if name.upper() not in _KEPT_ENVIRONMENT:
            monkeypatch.delenv(name)
    for name, value in {
        "USERPROFILE": home,
        "HOME": home,
        "APPDATA": home / "AppData" / "Roaming",
        "LOCALAPPDATA": home / "AppData" / "Local",
        "TEMP": temp,
        "TMP": temp,
        "PROGRAMDATA": tmp_path / "ProgramData",
        "SYSTEMROOT": windows,
        "WINDIR": windows,
    }.items():
        monkeypatch.setenv(name, str(value))
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(tempfile, "tempdir", str(temp))
    # Windows answers that do not come from the environment: the Downloads
    # Known Folder, and folder locations stored in the registry.
    monkeypatch.setattr(triage, "downloads_path", lambda: downloads)
    monkeypatch.setattr(edge_cleanup, "_edge_policy_paths", lambda _environment: (None, None))
    monkeypatch.setattr(
        visual_studio_installer_cleanup,
        "first_hklm_string_value",
        lambda _keys, _name: RegistryStringLookup(None, True),
    )
    workspace = Workspace(tmp_path, home, downloads, [], [], [], [], [], [])

    def entries() -> tuple[UninstallEntry, ...]:
        workspace.registry_reads.append(1)
        return tuple(workspace.installed)

    def processes() -> tuple[ProcessInfo, ...]:
        workspace.process_reads.append(1)
        return tuple(workspace.processes)

    monkeypatch.setattr(installer_files, "uninstall_entries", entries)
    monkeypatch.setattr(live_owners, "list_processes", processes)
    monkeypatch.setattr(live_owners, "list_services", lambda: tuple(workspace.services))

    class RecordingPopen(subprocess.Popen[Any]):
        def __init__(self, args: Any, *_args: Any, **_kwargs: Any) -> None:
            workspace.launches.append(os.path.basename(str(args[0])))
            self._child_created = False
            raise FileNotFoundError(str(args[0]))

    monkeypatch.setattr(subprocess, "Popen", RecordingPopen)
    return workspace


# ---- fixture files: structurally valid formats (tests/pe_samples.py), never run ----


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _nsis(path: Path) -> Path:
    return _write(path, pe_samples.nsis_installer())


def _inno(path: Path) -> Path:
    return _write(path, pe_samples.inno_installer())


def _versioned_setup(path: Path, description: str) -> Path:
    return _write(path, pe_samples.versioned_program(description))


def _msi(path: Path) -> Path:
    return _write(path, bytes.fromhex("D0CF11E0A1B11AE1") + b"\x00" * 1016)


def _msix(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("AppxManifest.xml", "<Package/>")
        archive.writestr("AppxBlockMap.xml", "<BlockMap/>")
    return path


def _plain_exe(path: Path) -> Path:
    return _write(path, pe_samples.portable_program())


# ---- the real product flow ----------------------------------------------------------


def _scan(ws: Workspace, *extra_roots: Path) -> tuple[Any, TriageSession]:
    rules = default_rules()
    if extra_roots:
        rules = replace(
            rules,
            scan=replace(rules.scan, additional_paths=tuple(str(root) for root in extra_roots)),
        )
    known = discover_known_cleanup_roots(rules.scan)
    roots = app.scan_targets(known, (), rules)
    # Every folder the scan knows about or walks must be a test folder.
    host = [str(path) for path in (*roots, *(root.path for root in known)) if not _inside(path, ws)]
    assert not host, f"the scan would read folders outside the test: {host}"
    window: Any = object.__new__(product_vendor_app.ProductDevCleanWindow)
    window._events = Queue()
    product_app.ProductDevCleanWindow._scan_worker(
        window, "scan", roots, CancellationToken(), rules, known
    )
    session: TriageSession | None = None
    while not window._events.empty():
        kind, payload = window._events.get_nowait()
        assert kind != "scan_error", payload
        if kind == "scan_done":
            session = payload[1]
    assert session is not None
    window._known_roots = known
    window._scan_rules = rules
    window._rules = rules
    window._review_approvals = set()
    window._session = session
    return window, session


def _inside(path: Path, ws: Workspace) -> bool:
    folded = os.path.normcase(os.path.abspath(path))
    root = os.path.normcase(os.path.abspath(ws.root))
    return folded == root or folded.startswith(root.rstrip(os.sep) + os.sep)


def _partition(window: Any) -> tuple[dict[str, TriageItem], dict[str, TriageItem]]:
    deletable, unsure = app._partition_items(window._session, window._rules)
    return (
        {Path(item.path).name: item for item in deletable},
        {Path(item.path).name: item for item in unsure},
    )


def _clean(
    window: Any,
    items: list[TriageItem],
) -> tuple[tuple[CleanupExecutionResult, ...], tuple[tuple[str, str], ...]]:
    window._product_delete_worker("delete", tuple(items), ())
    while True:
        kind, payload = window._events.get_nowait()
        if kind == "delete_done":
            return payload[1], payload[2]


def _execution_reasons(results: tuple[CleanupExecutionResult, ...]) -> dict[str, str]:
    return {
        Path(path).name: app._reason_of(error)
        for result in results
        for path, error in result.failures
    }


def _all_files(root: Path) -> set[Path]:
    return {path for path in root.rglob("*") if path.is_file() or path.is_symlink()}


# ---- rule-deletable installers anywhere in the scan range ---------------------------


def test_installers_are_deleted_by_rule_at_any_depth_and_in_any_scanned_folder(
    ws: Workspace,
) -> None:
    software = ws.root / "Software"
    installers = [
        _nsis(ws.downloads / "old-setup-1.0.exe"),
        _inno(ws.downloads / "sub" / "deeper" / "Tool-Setup.exe"),
        _msi(ws.downloads / "Installer" / "app-2.3.msi"),
        _msix(ws.downloads / "pkgs" / "app-1.0.msix"),
        _versioned_setup(software / "Archive" / "vendor.exe", "Vendor Product Setup"),
    ]
    window, _session = _scan(ws, software)
    deletable, unsure = _partition(window)

    for installer in installers:
        item = deletable[installer.name]
        assert item.lane is ReviewLane.DETERMINISTIC_CANDIDATE
        assert app.is_direct_cleanup_eligible(item)  # ticked by default, no extra review
        assert "不会卸载" in item.reason
    assert not set(unsure) & {path.name for path in installers}

    ws.launches.clear()
    results, failures = _clean(window, [deletable[path.name] for path in installers])

    assert failures == ()
    assert not any(result.failures for result in results)
    assert all(not path.exists() for path in installers)
    assert ws.launches == []  # deleting a file never starts an uninstaller or a tool


def test_mixed_folders_delete_only_what_the_rules_allow(ws: Workspace) -> None:
    apps = ws.root / "Apps"
    mixed = ws.downloads / "mixed"
    setup = _nsis(mixed / "setup.exe")
    portable = _plain_exe(mixed / "portable.exe")
    notes = _write(mixed / "notes.txt", b"old notes")
    library = _write(mixed / "helper.dll", b"MZ library")
    orphan_folder = ws.downloads / "OldApp"
    orphan_setup = _nsis(orphan_folder / "OldApp-Setup.exe")
    _inno(orphan_folder / "unins000.exe")
    _write(orphan_folder / "unins000.dat", b"inno uninstall log")
    foo = apps / "Foo"
    foo_setup = _msi(foo / "setup.msi")
    foo_uninstaller = _inno(foo / "unins000.exe")
    _plain_exe(foo / "Foo.exe")
    ws.installed.append(
        UninstallEntry("Foo App", str(foo), f'"{foo_uninstaller}"', str(foo / "Foo.exe"))
    )
    before = _all_files(ws.home) | _all_files(apps)

    window, _session = _scan(ws, apps)
    deletable, unsure = _partition(window)

    # Same folders, file by file: only the proven installer is rule-deletable,
    # the portable program waits for a decision, and owned files are not offered.
    assert set(deletable) == {"setup.exe"}
    assert unsure["portable.exe"].lane is ReviewLane.AI_REVIEW
    offered = set(deletable) | set(unsure)
    assert not offered & {"helper.dll", "OldApp-Setup.exe", "unins000.exe", "setup.msi", "Foo.exe"}

    results, failures = _clean(window, list(deletable.values()))
    assert failures == () and not any(result.failures for result in results)
    assert not setup.exists()
    assert _all_files(ws.home) | _all_files(apps) == before - {setup}
    for kept in (portable, notes, library, orphan_setup, foo_setup, foo_uninstaller):
        assert kept.exists()


def test_protected_files_carry_a_specific_rule_reason(ws: Workspace) -> None:
    from devclean.core.review_routing import route_unresolved_file_to_ai
    from devclean.core.triage import triage_file
    from devclean.scanner.filesystem import ScanRecord, ScanRecordKind

    foo = ws.root / "Apps" / "Foo"
    foo_setup = _msi(foo / "setup.msi")
    uninstaller = _inno(foo / "unins000.exe")
    ws.installed.append(UninstallEntry("Foo App", str(foo), f'"{uninstaller}"', ""))
    cache = _msi(ws.root / "Windows" / "Installer" / "1a2b.msi")
    orphan = _nsis(ws.downloads / "OldApp" / "OldApp-Setup.exe")
    _write(ws.downloads / "OldApp" / "unins000.dat", b"inno")
    library = _write(ws.downloads / "lib" / "helper.dll", b"MZ")
    rules = default_rules()

    def classify(path: Path) -> TriageItem:
        record = ScanRecord(
            root=str(ws.root),
            path=str(path),
            kind=ScanRecordKind.FILE,
            depth=3,
            logical_size=path.stat().st_size,
            last_write_time_ns=path.stat().st_mtime_ns,
        )
        return route_unresolved_file_to_ai(
            triage_file(
                record,
                delete_config=rules.delete.classification,
                keep_config=rules.keep.classification,
                now=datetime.now(UTC),
            )
        )

    expectations = {
        foo_setup: "已安装程序「Foo App」",
        uninstaller: "已安装程序「Foo App」",
        cache: "Windows Installer 缓存",
        orphan: "卸载",
        library: "程序文件、库文件",
    }
    for path, phrase in expectations.items():
        item = classify(path)
        assert item.lane is ReviewLane.REPORT_ONLY, path
        assert phrase in item.reason, (path, item.reason)


# ---- review items: AI / user decide, nothing runs before approval -------------------


def test_review_items_are_not_deleted_until_approved_then_deleted(ws: Workspace) -> None:
    software = ws.root / "Software"
    portable = _plain_exe(ws.downloads / "tools" / "portable.exe")
    archive = _write(software / "old-download.zip", b"PK old archive")
    window, _session = _scan(ws, software)
    _deletable, unsure = _partition(window)
    pending = [unsure["portable.exe"], unsure["old-download.zip"]]

    rules = window._rules
    with pytest.raises(CleanupRefusal, match="not approved"):
        candidate_from_triage_item(
            pending[0],
            delete_config=rules.delete.classification,
            keep_config=rules.keep.classification,
        )
    results, failures = _clean(window, pending)
    assert {Path(path).name: reason for path, reason in failures} == {
        "portable.exe": "需要 AI 或你批准后才能删除，尚未批准",
        "old-download.zip": "需要 AI 或你批准后才能删除，尚未批准",
    }
    assert portable.exists() and archive.exists()

    approved: UserRules = add_user_verdicts(
        load_rules(), [(str(portable), RuleDecision.DELETE, "用户确认不再需要")]
    )
    approved = add_ai_verdicts(
        approved, [(str(archive), RuleDecision.DELETE, "AI：过期的下载压缩包")]
    )
    window._rules = approved
    deletable, unsure = _partition(window)
    assert {"portable.exe", "old-download.zip"} <= set(deletable)

    results, failures = _clean(window, [deletable["portable.exe"], deletable["old-download.zip"]])
    assert failures == () and not any(result.failures for result in results)
    assert not portable.exists() and not archive.exists()


# ---- changes between scan and cleanup ------------------------------------------------


def test_ownership_change_after_scan_is_refused_with_a_visible_reason(ws: Workspace) -> None:
    tools = ws.downloads / "Tools"
    installer = _nsis(tools / "Big-Setup.exe")
    window, _session = _scan(ws)
    deletable, _unsure = _partition(window)
    assert "Big-Setup.exe" in deletable

    # The user installs a program into that folder before pressing cleanup.
    ws.installed.append(UninstallEntry("Big", str(tools), "", ""))
    results, failures = _clean(window, [deletable["Big-Setup.exe"]])

    assert failures == ()
    assert _execution_reasons(results) == {
        "Big-Setup.exe": "扫描后它已归属已安装的程序或安装缓存，已拒绝删除"
    }
    assert installer.exists()


def test_link_replacement_after_scan_never_deletes_the_link_target(ws: Workspace) -> None:
    file_case = _nsis(ws.downloads / "a" / "Setup-A.exe")
    parent_case = _nsis(ws.downloads / "b" / "Setup-B.exe")
    window, _session = _scan(ws)
    deletable, _unsure = _partition(window)
    items = [deletable["Setup-A.exe"], deletable["Setup-B.exe"]]

    victim_a = _nsis(ws.root / "victim-a" / "Setup-A.exe")
    file_case.unlink()
    file_case.symlink_to(victim_a)
    victim_b = _nsis(ws.root / "victim-b" / "Setup-B.exe")
    stat_b = parent_case.stat()
    os.utime(victim_b, ns=(stat_b.st_atime_ns, stat_b.st_mtime_ns))
    (ws.downloads / "b").rename(ws.downloads / "b-moved")
    (ws.downloads / "b").symlink_to(ws.root / "victim-b", target_is_directory=True)

    results, failures = _clean(window, items)

    reasons = {Path(path).name: reason for path, reason in failures}
    reasons.update(_execution_reasons(results))
    assert reasons == {
        "Setup-A.exe": "路径被替换成链接或离开了扫描范围，安全检查拒绝",
        "Setup-B.exe": "路径被替换成链接或离开了扫描范围，安全检查拒绝",
    }
    assert victim_a.exists() and victim_b.exists()
    assert (ws.downloads / "b-moved" / "Setup-B.exe").exists()


def test_link_swapped_in_after_the_last_check_is_refused_by_the_delete_itself(
    ws: Workspace,
) -> None:
    _nsis(ws.downloads / "c" / "Setup-C.exe")
    window, _session = _scan(ws)
    deletable, _unsure = _partition(window)
    candidate = window._candidate(deletable["Setup-C.exe"])
    victim = _nsis(ws.root / "victim-c" / "Setup-C.exe")

    def swap_then_purge(path: Path, snapshot: Any, boundary: Any) -> Any:
        # The race: the folder becomes a link after every earlier check passed.
        (ws.downloads / "c").rename(ws.downloads / "c-moved")
        (ws.downloads / "c").symlink_to(ws.root / "victim-c", target_is_directory=True)
        return exact_cleanup.purge_exact_file(path, snapshot, boundary)

    rules = window._scan_rules
    result = postscan_cleanup.execute_cleanup_batch(
        postscan_cleanup.prepare_cleanup_batch((candidate,)),
        CleanupMode.PERMANENT,
        purger=swap_then_purge,
        known_roots=window._known_roots,
        delete_config=rules.delete.classification,
        keep_config=rules.keep.classification,
    )

    assert _execution_reasons((result,)) == {
        "Setup-C.exe": "路径被替换成链接或离开了扫描范围，安全检查拒绝"
    }
    assert victim.exists()
    assert (ws.downloads / "c-moved" / "Setup-C.exe").exists()


# ---- cost: discovery once per scan and once per cleanup, no tool starts --------------


def test_discovery_runs_once_per_scan_and_once_per_cleanup(ws: Workspace) -> None:
    installers = [_nsis(ws.downloads / f"setup-{index}.exe") for index in range(6)]
    window, _session = _scan(ws)
    assert len(ws.registry_reads) == 1
    deletable, _unsure = _partition(window)

    ws.registry_reads.clear()
    ws.launches.clear()
    _clean(window, [deletable[path.name] for path in installers])

    assert len(ws.registry_reads) == 1
    assert ws.launches == []
    assert all(not path.exists() for path in installers)


# ---- programs and services that start using a location after the scan -------------


def _reasons(
    results: tuple[CleanupExecutionResult, ...], failures: tuple[tuple[str, str], ...]
) -> dict[str, str]:
    reasons = {Path(path).name: reason for path, reason in failures}
    reasons.update(_execution_reasons(results))
    return reasons


def test_program_started_after_scan_blocks_deletion_until_it_exits(ws: Workspace) -> None:
    data = ws.downloads / "tool-data"
    installer = _nsis(data / "Tool-Setup.exe")
    window, _session = _scan(ws)
    deletable, _unsure = _partition(window)
    item = deletable["Tool-Setup.exe"]

    tool = ws.root / "Apps" / "Tool" / "tool.exe"
    ws.processes.append(ProcessInfo("tool.exe", str(tool), f'"{tool}" --data-dir="{data}"'))
    results, failures = _clean(window, [item])

    assert _reasons(results, failures) == {
        "Tool-Setup.exe": (
            "正在运行的程序 tool.exe 使用它所在的位置，已拒绝删除；关闭该程序或停止该服务后重试"
        )
    }
    assert installer.exists()

    # Each cleanup reads the process list afresh: once the program exits it goes.
    ws.processes.clear()
    results, failures = _clean(window, [item])
    assert _reasons(results, failures) == {}
    assert not installer.exists()


def test_service_registered_after_scan_with_data_there_blocks_deletion(ws: Workspace) -> None:
    models = ws.root / "Models"
    model_setup = _msi(models / "runtime-setup.msi")
    runner = _nsis(ws.downloads / "Runner" / "runner-setup.exe")
    window, _session = _scan(ws, models)
    deletable, _unsure = _partition(window)

    service_program = ws.root / "Services" / "modelsvc.exe"
    ws.services.append(
        ServiceEntry("ModelSvc", f'"{service_program}" serve', (f"MODELS_DIR={models}",))
    )
    ws.processes.append(ProcessInfo("runner.exe", str(ws.downloads / "Runner" / "runner.exe"), ""))
    ws.services.append(ServiceEntry("RunnerSvc", f'"{ws.downloads / "Runner" / "runner.exe"}"', ()))
    results, failures = _clean(
        window, [deletable["runtime-setup.msi"], deletable["runner-setup.exe"]]
    )

    assert _reasons(results, failures) == {
        "runtime-setup.msi": (
            "已登记的 Windows 服务 ModelSvc 使用它所在的位置，已拒绝删除；"
            "关闭该程序或停止该服务后重试"
        ),
        "runner-setup.exe": (
            "正在运行的程序 runner.exe 使用它所在的位置，已拒绝删除；"
            "关闭该程序或停止该服务后重试"
        ),
    }
    assert model_setup.exists() and runner.exists()


def test_unreadable_process_lists_refuse_every_target_with_the_reason(
    ws: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installer = _nsis(ws.downloads / "a" / "Setup.exe")
    window, _session = _scan(ws)
    deletable, _unsure = _partition(window)
    item = deletable["Setup.exe"]
    ws.launches.clear()  # the scan asks package managers where caches live

    def broken() -> tuple[ProcessInfo, ...]:
        raise ProcessListError("process query failed with exit code 1")

    # No process list at all: no evidence to judge any target by, junk included.
    monkeypatch.setattr(live_owners, "list_processes", broken)
    results, failures = _clean(window, [item])
    assert _reasons(results, failures) == {
        "Setup.exe": (
            "无法确认正在运行的程序和服务是否在使用它（process query failed with exit code 1），"
            "已拒绝删除；以管理员身份运行 DevClean 或关闭相关程序后重试"
        )
    }

    def services_unreadable() -> tuple[ServiceEntry, ...]:
        raise PermissionError("access denied")

    monkeypatch.setattr(live_owners, "list_processes", lambda: tuple(ws.processes))
    monkeypatch.setattr(live_owners, "list_services", services_unreadable)
    results, failures = _clean(window, [item])
    assert _reasons(results, failures)["Setup.exe"].startswith(
        "无法确认正在运行的程序和服务是否在使用它（access denied）"
    )
    assert installer.exists()
    assert ws.launches == []


def test_unrelated_hidden_program_does_not_block_junk_but_guards_its_own_places(
    ws: Workspace,
) -> None:
    tools = ws.downloads / "tools"
    junk = _nsis(ws.downloads / "old" / "Old-Setup.exe")
    in_program_folder = _nsis(tools / "Studio" / "Studio-Update-Setup.exe")
    beside_portable = _nsis(ws.downloads / "Portable" / "data" / "Plugin-Setup.exe")
    _plain_exe(ws.downloads / "Portable" / "porta.exe")
    named = _msi(ws.root / "Data" / "Ollama-cache" / "runtime.msi")
    reviewed = _plain_exe(ws.root / "Data" / "misc" / "helper-tool.exe")
    window, _session = _scan(ws, ws.root / "Data")
    window._rules = add_user_verdicts(
        load_rules(), [(str(reviewed), RuleDecision.DELETE, "用户确认不再需要")]
    )
    deletable, _unsure = _partition(window)
    targets = ["Old-Setup.exe", "Studio-Update-Setup.exe", "Plugin-Setup.exe", "runtime.msi"]
    items = [deletable[name] for name in targets] + [deletable["helper-tool.exe"]]
    ws.launches.clear()

    # Elevated programs whose command lines Windows hides from DevClean:
    ws.processes[:] = [
        # program path visible: its folder is known
        ProcessInfo("studio.exe", str(tools / "Studio" / "studio.exe"), "", session_id=1),
        # no path: a program of that name sits above the target
        ProcessInfo("porta.exe", "", "", session_id=1),
        # no path: a distinctive name in the target's path
        ProcessInfo("ollama.exe", "", "", session_id=1),
    ]
    results, failures = _clean(window, items)
    reasons = _reasons(results, failures)
    retry = "已拒绝删除；关闭该程序或以管理员身份运行 DevClean 后重试"
    assert reasons == {
        # Its visible program path already shows its folder.
        "Studio-Update-Setup.exe": (
            "正在运行的程序 studio.exe 使用它所在的位置，已拒绝删除；关闭该程序或停止该服务后重试"
        ),
        "Plugin-Setup.exe": (
            "无法读取正在运行的程序 porta.exe 的启动参数，而同一文件夹"
            f"（{ws.downloads / 'Portable'}）里有同名程序，它可能正从那里运行，可能正被它使用，"
            f"{retry}"
        ),
        "runtime.msi": (
            "无法读取正在运行的程序 ollama.exe 的启动参数，而路径中含有它的名称"
            f"（Ollama-cache），可能正被它使用，{retry}"
        ),
        # Approved from the uncertain list: nothing shows these programs leave it alone.
        "helper-tool.exe": (
            "无法读取正在运行的程序 studio.exe、porta.exe、ollama.exe 的启动参数；"
            "这一项是经 AI 或你批准的不确定项，不能确认它没有被这些程序使用，已拒绝删除；"
            "关闭这些程序或以管理员身份运行 DevClean 后重试"
        ),
    }
    # Rule-proven junk unrelated to any of them is still cleaned.
    assert not junk.exists()
    assert in_program_folder.exists() and beside_portable.exists() and named.exists()
    assert reviewed.exists()
    assert ws.launches == []

    # Once they are closed, the next cleanup removes the rest.
    ws.processes.clear()
    results, failures = _clean(window, items[1:])
    assert _reasons(results, failures) == {}
    assert not any(path.exists() for path in (in_program_folder, beside_portable, named, reviewed))


def test_a_name_alone_never_vouches_for_a_hidden_process(ws: Workspace) -> None:
    reviewed = _plain_exe(ws.root / "Data" / "tool.exe")
    window, _session = _scan(ws, ws.root / "Data")
    window._rules = add_user_verdicts(
        load_rules(), [(str(reviewed), RuleDecision.DELETE, "用户确认不再需要")]
    )
    deletable, _unsure = _partition(window)
    item = deletable["tool.exe"]

    windows = ws.root / "Windows"
    _write(windows / "System32" / "svchost.exe", b"MZ")
    _write(windows / "System32" / "cmd.exe", b"MZ")
    ws.services.append(ServiceEntry("Dhcp", f'"{windows / "System32" / "svchost.exe"}" -k x', ()))
    impostors = [
        # same name as a Windows program and a service program, but in a user session
        ProcessInfo("svchost.exe", "", "", session_id=1),
        # unknown session
        ProcessInfo("svchost.exe", "", ""),
        # a Windows command host: what it runs is on the hidden command line
        ProcessInfo("cmd.exe", str(windows / "System32" / "cmd.exe"), "", session_id=1),
        ProcessInfo("cmd.exe", "", "", session_id=0),
    ]
    for impostor in impostors:
        ws.processes[:] = [impostor]
        results, failures = _clean(window, [item])
        reason = _reasons(results, failures)["tool.exe"]
        assert reason.startswith(f"无法读取正在运行的程序 {impostor.name} 的启动参数"), impostor
        assert reviewed.exists()

    # Trusted evidence does account for hidden processes.
    ws.processes[:] = [
        ProcessInfo("System", "", "", session_id=0),
        ProcessInfo("svchost.exe", "", "", session_id=0),  # services session, service program
        ProcessInfo("lsass.exe", str(windows / "System32" / "lsass.exe"), "", session_id=0),
        ProcessInfo("dwm.exe", str(windows / "System32" / "dwm.exe"), "", session_id=1),
    ]
    results, failures = _clean(window, [item])
    assert _reasons(results, failures) == {}
    assert not reviewed.exists()


def test_live_state_is_read_once_per_cleanup_run_not_per_file(ws: Workspace) -> None:
    installers = [_nsis(ws.downloads / f"pkg{index}" / "setup.exe") for index in range(5)]
    window, _session = _scan(ws)
    deletable, _unsure = _partition(window)
    # Five files share one name, so take the rows themselves.
    items = list(app._partition_items(window._session, window._rules)[0])
    assert len(items) == 5 and set(deletable) == {"setup.exe"}

    ws.process_reads.clear()
    ws.launches.clear()
    _clean(window, items[:3])
    assert len(ws.process_reads) == 1
    _clean(window, items[3:])
    assert len(ws.process_reads) == 2
    assert ws.launches == []
    assert not any(path.exists() for path in installers)


def test_application_discovery_shares_one_fresh_process_list_per_run(
    ws: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import types

    import devclean.core.chrome_cleanup as chrome_cleanup
    import devclean.core.vscode_cleanup as vscode_cleanup
    from devclean.core.application_cleanup import scan_root_snapshot

    windows_os = types.SimpleNamespace(**{**vars(os), "name": "nt"})
    monkeypatch.setattr(chrome_cleanup, "os", windows_os)
    monkeypatch.setattr(vscode_cleanup, "os", windows_os)

    with scan_root_snapshot():  # the scan: nothing running yet
        assert chrome_cleanup._running_override_roots() == ((), ())
        assert vscode_cleanup._running_override_roots() == ((), ())
    assert len(ws.process_reads) == 1

    ws.processes[:] = [
        ProcessInfo("chrome.exe", "", r'chrome.exe --user-data-dir="D:\Profiles\Work"'),
        ProcessInfo("Code.exe", "", r"Code.exe --extensions-dir=E:\Exts"),
    ]
    with scan_root_snapshot():  # the cleanup run sees programs started since
        chrome_data, _chrome_cache = chrome_cleanup._running_override_roots()
        _vscode_data, vscode_extensions = vscode_cleanup._running_override_roots()
    assert [str(path) for path in chrome_data] == [r"D:\Profiles\Work"]
    assert [str(path) for path in vscode_extensions] == [r"E:\Exts"]
    assert len(ws.process_reads) == 2


# ---- executables without installer evidence, in any scanned folder ------------------


def test_unrecognised_programs_anywhere_wait_for_a_decision_then_delete(ws: Workspace) -> None:
    tools = ws.root / "Tools"
    portable = _plain_exe(tools / "old" / "portable.exe")
    built = _plain_exe(tools / "proj" / "bin" / "Debug" / "app.exe")
    downloaded = _plain_exe(ws.downloads / "x" / "tool.exe")
    window, _session = _scan(ws, tools)
    deletable, unsure = _partition(window)
    ws.launches.clear()  # the scan asks package managers where caches live

    for path in (portable, built, downloaded):
        assert path.name not in deletable
        item = unsure[path.name]
        assert item.lane is ReviewLane.AI_REVIEW
        assert app.is_ai_review_eligible(item)
        assert "需要你或 AI 判断" in item.reason
    assert unsure["tool.exe"].reason.startswith("下载的可执行文件")
    assert unsure["portable.exe"].reason.startswith("可执行文件")

    results, failures = _clean(window, [unsure["portable.exe"], unsure["app.exe"]])
    assert set(_reasons(results, failures).values()) == {"需要 AI 或你批准后才能删除，尚未批准"}
    assert portable.exists() and built.exists()

    rules = add_ai_verdicts(load_rules(), [(str(built), RuleDecision.DELETE, "AI：旧构建产物")])
    window._rules = add_user_verdicts(
        rules, [(str(portable), RuleDecision.DELETE, "用户确认不再需要")]
    )
    deletable, _unsure = _partition(window)
    results, failures = _clean(window, [deletable["portable.exe"], deletable["app.exe"]])
    assert _reasons(results, failures) == {}
    assert not portable.exists() and not built.exists()
    assert downloaded.exists()  # not approved
    assert ws.launches == []


def test_programs_with_an_installed_or_system_purpose_stay_protected(
    ws: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from devclean.core.review_routing import route_unresolved_file_to_ai
    from devclean.core.triage import triage_file
    from devclean.scanner.filesystem import ScanRecord, ScanRecordKind

    program_files = ws.root / "Program Files"
    monkeypatch.setenv("PROGRAMFILES", str(program_files))
    cases = {
        _plain_exe(program_files / "Foo" / "foo.exe"): "位于Program Files 程序安装目录",
        _plain_exe(ws.root / "Windows" / "System32" / "tool.exe"): "位于Windows 系统文件夹",
        _plain_exe(ws.home / "AppData" / "Local" / "Programs" / "Bar" / "bar.exe"): (
            "位于用户程序安装目录"
        ),
        _plain_exe(ws.root / "Apps" / "Baz" / "baz.exe"): "已安装程序「Baz」",
        _plain_exe(ws.root / "Apps" / "Qux" / "unins000.exe"): "卸载程序",
    }
    ws.installed.append(UninstallEntry("Baz", str(ws.root / "Apps" / "Baz"), "", ""))
    rules = default_rules()
    for path, phrase in cases.items():
        record = ScanRecord(
            root=str(ws.root),
            path=str(path),
            kind=ScanRecordKind.FILE,
            depth=3,
            logical_size=path.stat().st_size,
            last_write_time_ns=path.stat().st_mtime_ns,
        )
        item = route_unresolved_file_to_ai(
            triage_file(
                record,
                delete_config=rules.delete.classification,
                keep_config=rules.keep.classification,
                now=datetime.now(UTC),
            )
        )
        assert item.lane is ReviewLane.REPORT_ONLY, path
        assert phrase in item.reason, (path, item.reason)


# ---- installed programs written in every Uninstall-entry shape ----------------------


def test_uninstall_entries_in_every_shape_protect_their_files_and_never_run(
    ws: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    apps = ws.root / "Apps"
    marker = ws.root / "uninstaller-ran"
    foo = apps / "Foo App"
    uninstaller = _write(foo / "uninst.exe", f"#!/bin/sh\ntouch '{marker}'\n".encode())
    uninstaller.chmod(0o755)
    protected = {
        _msi(foo / "repair-setup.msi"): "Foo App",  # quoted, spaces, switches
        _nsis(apps / "Bar Tool" / "bar-setup.exe"): "Bar Tool",  # unquoted with spaces
        _nsis(apps / "Baz" / "baz-setup.exe"): "Baz",  # %VARIABLE% in a plain string
        _msi(apps / "Qux" / "data" / "qux.msi"): "Qux",  # rundll32 "dll",Entry
        _nsis(apps / "Program Files" / "Short" / "short-setup.exe"): "Short",  # 8.3 name
        _nsis(apps / "NsisDir" / "nsis-setup.exe"): "NsisDir",  # _?=install folder
        _nsis(ws.downloads / "Helper" / "helper.exe"): "Helper",  # exact file in Downloads
    }
    free = _nsis(ws.downloads / "Other" / "other-setup.exe")
    monkeypatch.setenv("APPS_ROOT", str(apps))

    def fake_long_path(path: str) -> str:
        return path.replace("PROGRA~1", "Program Files")

    monkeypatch.setattr(command_paths, "long_path", fake_long_path)
    ws.installed.extend(
        [
            UninstallEntry("Foo App", "", f'"{uninstaller}" /S /LOG="{ws.root}/log.txt"', ""),
            UninstallEntry("Bar Tool", "", f"{apps}/Bar Tool/uninstall.exe /S", ""),
            UninstallEntry("Baz", "", "%APPS_ROOT%/Baz/uninstall.exe", ""),
            UninstallEntry("Qux", "", f'rundll32.exe "{apps}/Qux/data/qux.dll",Uninstall', ""),
            UninstallEntry("Short", f"{apps}/PROGRA~1/Short", "", ""),
            UninstallEntry("NsisDir", "", f'"{ws.root}/u/Un_A.exe" _?={apps}/NsisDir', ""),
            UninstallEntry("Helper", "", "", f'"{ws.downloads}/Helper/helper.exe",0'),
            UninstallEntry("MsiApp", "", "MsiExec.exe /X{12345678-ABCD-1234-ABCD-1234}", ""),
            UninstallEntry("Sloppy", str(ws.downloads), "", ""),
        ]
    )
    window, _session = _scan(ws, apps)
    deletable, unsure = _partition(window)
    scan_launches = [name for name in ws.launches if name.startswith("uninst")]
    ws.launches.clear()

    for path in protected:
        assert path.name not in deletable and path.name not in unsure, path
        assert installer_files.installed_program_owner(path) == protected[path], path
    assert "other-setup.exe" in deletable  # Downloads itself is too broad to own

    results, failures = _clean(window, [deletable["other-setup.exe"]])
    assert _reasons(results, failures) == {}
    assert not free.exists()
    assert all(path.exists() for path in protected)
    assert not marker.exists()  # parsing an uninstall command never runs it
    assert scan_launches == [] and ws.launches == []


def test_a_reviewed_item_cannot_be_relabelled_as_rule_proven(ws: Workspace) -> None:
    import dataclasses

    reviewed = _plain_exe(ws.root / "Data" / "tool.exe")
    installer = _nsis(ws.downloads / "a" / "Setup.exe")
    window, _session = _scan(ws, ws.root / "Data")
    window._rules = add_user_verdicts(
        load_rules(), [(str(reviewed), RuleDecision.DELETE, "用户确认不再需要")]
    )
    deletable, _unsure = _partition(window)

    assert window._candidate(deletable["Setup.exe"]).rule_proven
    candidate = window._candidate(deletable["tool.exe"])
    assert not candidate.rule_proven
    with pytest.raises(CleanupRefusal, match="altered"):
        dataclasses.replace(candidate, rule_proven=True)
    assert installer.exists() and reviewed.exists()


# ---- temporary folders: one item, emptied around what other rules claim ----------------


def test_temp_folders_are_one_safe_item_emptied_around_what_others_claim(ws: Workspace) -> None:
    from devclean.core.triage import DirectoryScope

    temp = ws.home / "AppData" / "Local" / "Temp"
    junk = [
        _write(temp / "a.tmp", b"x" * 100),
        _write(temp / "_MEI1234" / "python.dll", b"y" * 100),
        _write(temp / "deep" / "er" / "c.log", b"z" * 50),
    ]
    claimed = _write(temp / "proj" / "package.json", b"{}")  # npm project metadata: KEEP
    user_decides = _write(temp / "npm-debug.log", b"log")  # npm debug log: the user decides
    running = _write(temp / "setup-run" / "setup.exe", b"MZ")
    payload = _write(temp / "setup-run" / "payload.bin", b"p" * 10)
    app_tmp = ws.home / "AppData" / "Local" / "Vendor" / "Product" / "tmp"
    app_junk = _write(app_tmp / "cache.bin", b"q" * 30)
    project_note = _write(ws.root / "Projects" / "app" / "tmp" / "notes.txt", b"mine")
    window, session = _scan(ws, ws.root / "Projects")
    deletable, unsure = _partition(window)

    # Each temp folder is one ticked "safe" row sized from the listing; what an
    # application rule claims is not part of it and keeps its own classification.
    folders = {item.path: item for item in deletable.values() if item.directory_scope}
    assert set(folders) == {str(temp), str(app_tmp)}
    assert all(item.directory_scope is DirectoryScope.TEMP_FOLDER for item in folders.values())
    assert all(app.is_direct_cleanup_eligible(item) for item in folders.values())
    assert session.subtree_totals(str(temp)).files == 5
    assert session.subtree_totals(str(temp)).logical_bytes == 100 + 100 + 50 + 2 + 10
    assert session.subtree_totals(str(app_tmp)).logical_bytes == 30
    assert "npm-debug.log" in unsure
    # A tmp folder in a project is not a temporary folder: its files stay per file.
    assert "notes.txt" in unsure
    assert not any(item.path == str(project_note.parent) for item in deletable.values())

    # A program now runs from Temp: its folder is left, everything else goes.
    ws.launches.clear()  # the scan asks package managers where caches live
    ws.processes.append(ProcessInfo("setup.exe", str(running), f'"{running}"'))
    results, failures = _clean(window, [folders[str(temp)], folders[str(app_tmp)]])

    assert failures == () and not any(result.failures for result in results)
    assert temp.is_dir() and app_tmp.is_dir()  # the folders themselves stay
    assert not any(path.exists() for path in junk) and not app_junk.exists()
    assert not (temp / "deep").exists() and not (temp / "_MEI1234").exists()
    assert claimed.exists() and user_decides.exists()
    assert running.exists() and payload.exists()
    assert project_note.exists()
    partial = {path: outcome for result in results for path, outcome in result.partial}
    assert set(partial) == {str(temp)}  # the application's tmp was emptied completely
    outcome = partial[str(temp)]
    assert (outcome.files_removed, outcome.bytes_removed) == (3, 250)
    assert outcome.kept == 3 and outcome.in_use == 0 and outcome.denied == 0
    reasons = dict(outcome.examples)
    assert reasons[str(temp / "setup-run")] == "正在运行的程序 setup.exe 在使用"
    completed = {path for result in results for path in result.completed_paths}
    assert completed == {str(app_tmp)}  # the partly emptied folder stays listed
    assert app._temp_folder_note(outcome).startswith("已清理 3 个文件")
    assert ws.launches == []


class _RowTree:
    """The few Treeview calls the safe list makes when it is redrawn."""

    def __init__(self) -> None:
        self.rows: dict[str, tuple[str, ...]] = {}

    def __str__(self) -> str:
        return ".rows"

    def get_children(self) -> tuple[str, ...]:
        return tuple(self.rows)

    def delete(self, *rows: str) -> None:
        for row in rows:
            del self.rows[row]

    def insert(
        self, _parent: str, _index: object, *, iid: str, values: tuple[str, ...], tags: object
    ) -> None:
        self.rows[iid] = values


class _Text:
    def __init__(self) -> None:
        self.value = ""

    def set(self, value: str) -> None:
        self.value = value

    def get(self) -> str:
        return self.value


def test_a_temp_folder_row_is_ticked_by_default_and_stays_ticked(ws: Workspace) -> None:
    # User decision 2026-11-10: temporary folders are ticked by default.  On the
    # c20d81a run the AppData\Local\Temp row showed unticked; this follows the
    # row through what the window does with it: publish, a cleanup that leaves
    # files in use, and a republish (rules saved, AI answer imported).
    from devclean.core.cleanup_journal import ActionState
    from devclean.core.triage import DirectoryScope

    temp = ws.home / "AppData" / "Local" / "Temp"
    _write(temp / "a.tmp", b"x" * 100)
    _write(temp / "sub" / "b.tmp", b"y" * 100)
    _nsis(ws.downloads / "old-setup.exe")
    window, session = _scan(ws)
    window._deletable = []
    window._unsure = []
    window._checked = set()
    window._cleanup_failures = {}
    window._deletable_tree = _RowTree()
    window._unsure_tree = _RowTree()
    window._deletable_total = _Text()
    window._unsure_total = _Text()
    window._status = _Text()
    window._vendor_candidates = {}
    window._vendor_scan_candidates = {}
    window._scan_token = "scan"
    window._sync_buttons = lambda: None

    window._publish(session, app.published_partition(session, window._rules))

    temp_rows = [
        item.path
        for item in window._deletable
        if item.directory_scope is DirectoryScope.TEMP_FOLDER
    ]
    assert temp_rows == [str(temp)]
    assert window._checked == {item.path for item in window._deletable}
    assert window._deletable_tree.rows[str(temp)][0] == window._TICKED

    left = exact_cleanup.TempFolderPurgeResult(
        root_path=str(temp),
        files_removed=1,
        links_removed=0,
        directories_removed=0,
        bytes_removed=100,
        in_use=1,
        denied=0,
        kept=0,
        completed=True,
    )
    window._report_deletion(
        (
            postscan_cleanup.CleanupExecutionResult(
                action_states=(("a1", ActionState.PURGED),),
                purged_logical_bytes=100,
                completed_paths=(),
                partial=((str(temp), left),),
                removed=((str(temp), 1, 100),),
            ),
        ),
        (),
    )
    assert str(temp) in window._checked
    assert window._deletable_tree.rows[str(temp)][0] == window._TICKED

    window._publish(session)
    assert str(temp) in window._checked
    assert window._deletable_tree.rows[str(temp)][0] == window._TICKED


def test_the_scan_worker_hands_over_totals_and_the_finished_partition(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = [_nsis(ws.downloads / f"pkg{index}" / "setup.exe") for index in range(3)]
    files.append(_write(ws.downloads / "notes" / "todo.txt", b"later"))
    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    window: Any = object.__new__(product_vendor_app.ProductDevCleanWindow)
    window._events = Queue()
    window._vendor_candidates = {}
    window._vendor_scan_candidates = {}
    window._active_scan_drives = ()
    rules = default_rules()
    known = discover_known_cleanup_roots(rules.scan)
    roots = app.scan_targets(known, (), rules)

    product_vendor_app.ProductDevCleanWindow._scan_worker(
        window, "scan", roots, CancellationToken(), rules, known
    )

    events: list[tuple[str, Any]] = []
    while not window._events.empty():
        events.append(window._events.get_nowait())
    kinds = [kind for kind, _payload in events]
    assert kinds[-1] == "scan_done"
    # The clock never moved, so only the final progress report got through.
    assert kinds.count("progress") == 1
    _token, session, cancelled, totals, partition = events[-1][1]
    assert not cancelled
    walked = [path for path in ws.home.rglob("*") if path.is_file()]
    assert totals.files == len(walked) == len(files)
    assert totals.logical_bytes == sum(path.stat().st_size for path in walked)
    assert totals.directories == len([path for path in ws.home.rglob("*") if path.is_dir()]) + 1
    expected = app.published_partition(session, rules)
    assert [item.path for item in partition.deletable] == [item.path for item in expected.deletable]
    assert [item.path for item in partition.unsure] == [item.path for item in expected.unsure]
    assert {Path(item.path).name for item in partition.deletable} == {"setup.exe"}


# ---- the product cleanup: every row reported as it ends (2026-11-11) ----------------
# User report on 9459930: "正在清理 128 项…" for a long time and nothing removed.


def _drain(window: Any) -> list[tuple[str, Any]]:
    events = []
    while not window._events.empty():
        events.append(window._events.get_nowait())
    return events


def _hold_open(path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Make *path* undeletable as if another program held it open."""

    if os.name == "nt":
        return path.open("rb")  # Python opens files without delete sharing
    real_unlink = os.unlink

    def unlink(name: Any, *, dir_fd: int | None = None) -> None:
        if name == path.name:
            raise OSError(32, "The process cannot access the file because it is in use", name)
        real_unlink(name, dir_fd=dir_fd)

    monkeypatch.setattr(os, "unlink", unlink)
    return None


def _row_paths(payload: Any) -> tuple[str, ...]:
    _token, _count, _total, result, failure = payload
    if failure is not None:
        return (failure[0],)
    return (*result.completed_paths, *(path for path, _left in result.partial))


def test_every_row_is_reported_as_it_ends_and_temp_folders_go_last(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pathlib import PureWindowsPath

    from devclean.core.triage import DirectoryScope

    temp = ws.home / "AppData" / "Local" / "Temp"
    entries = [_write(temp / f"t{i % 6}" / f"u{i % 5}" / f"f{i}.tmp", b"x") for i in range(600)]
    held = _write(temp / "held.tmp", b"held")
    installers = [_nsis(ws.downloads / f"old-setup-{i}.exe") for i in range(3)]
    window, _session = _scan(ws)
    deletable, _unsure = _partition(window)
    temp_row = next(
        item for item in deletable.values() if item.directory_scope is DirectoryScope.TEMP_FOLDER
    )
    # Temp first, as the window lists it (the largest row).
    rows = (temp_row, *(deletable[path.name] for path in installers))
    # What a desktop runs: programs with folders of their own, some whose
    # command line Windows hides.
    programs = ws.root / "programs"
    for index in range(150):
        program = programs / f"App{index}" / f"app{index}.exe"
        data = programs / f"Data{index}"
        ws.processes.append(
            ProcessInfo(f"app{index}.exe", str(program), f'"{program}" --data "{data}"')
        )
    for index in range(10):
        hidden = programs / f"Hidden{index}" / f"hidden{index}.exe"
        ws.processes.append(ProcessInfo(f"hidden{index}.exe", str(hidden), "", 1))
    counts = {"checked": 0, "parts": 0, "is_within": 0, "edge_walks": 0}

    def counting(name: str, function: Any) -> Any:
        def counted(*args: Any, **kwargs: Any) -> Any:
            counts[name] += 1
            return function(*args, **kwargs)

        return counted

    monkeypatch.setattr(
        postscan_cleanup, "_entry_in_use", counting("checked", postscan_cleanup._entry_in_use)
    )
    monkeypatch.setattr(live_owners, "_parts", counting("parts", live_owners._parts))
    monkeypatch.setattr(live_owners, "is_within", counting("is_within", installer_files.is_within))
    relative_to = PureWindowsPath.relative_to

    # Only Edge's own PureWindowsPath lookups below this Temp folder count.  On
    # Windows every pathlib.Path is a PureWindowsPath too, and pytest's own
    # folder lies in %TEMP%, so a bare "\\temp\\" test would count every path.
    temp_below = str(temp).casefold().replace("/", "\\").rstrip("\\") + "\\"

    def edge_walk(self: PureWindowsPath, *args: Any, **kwargs: Any) -> PureWindowsPath:
        if type(self) is PureWindowsPath and (
            str(self).casefold().replace("/", "\\").startswith(temp_below)
        ):
            counts["edge_walks"] += 1
        return relative_to(self, *args, **kwargs)

    monkeypatch.setattr(PureWindowsPath, "relative_to", edge_walk)
    holder = _hold_open(held, monkeypatch)
    try:
        window._product_delete_worker("delete", rows, ())
    finally:
        if holder is not None:
            holder.close()

    events = _drain(window)
    kinds = [kind for kind, _payload in events]
    # The window hears from the cleanup at once, not only at the end.
    assert kinds[0] == "delete_prepare_progress" and kinds[-1] == "delete_done"
    finished = [
        (position, payload)
        for position, (kind, payload) in enumerate(events)
        if kind == "delete_item_done"
    ]
    assert [payload[1:3] for _position, payload in finished] == [(1, 4), (2, 4), (3, 4), (4, 4)]
    # The installers were reported before the Temp folder, which goes last.
    reported = [_row_paths(payload) for _position, payload in finished]
    assert reported[-1] == (str(temp),)
    assert sorted(path for paths in reported[:-1] for path in paths) == sorted(map(str, installers))
    temp_progress = [
        position
        for position, (kind, payload) in enumerate(events)
        if kind == "delete_action_progress" and payload[4].path == str(temp)
    ]
    assert temp_progress and temp_progress[0] < finished[-1][0]

    assert held.exists() and not any(path.exists() for path in (*entries, *installers))
    _token, results, failures = events[-1][1]
    assert failures == ()
    left = {path: outcome for result in results for path, outcome in result.partial}
    assert left[str(temp)].in_use == 1

    # Each entry costs a few lookups, not one per running program (160 here):
    # the path is split twice, not twice per hidden program; program folders
    # are looked up by ancestor and by sorted range, not scanned; what is left
    # is one folder test per hidden program (10 here).  Edge never walks the
    # parents of a Temp path.
    assert counts["checked"] >= len(entries)
    assert counts["parts"] <= 2 * counts["checked"] + 20
    assert counts["is_within"] <= 20 * counts["checked"]
    assert counts["edge_walks"] == 0


def test_a_hanging_official_command_holds_back_no_other_row(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading
    from types import SimpleNamespace

    installers = [_nsis(ws.downloads / f"old-setup-{i}.exe") for i in range(3)]
    window, _session = _scan(ws)
    deletable, _unsure = _partition(window)
    rows = tuple(deletable[path.name] for path in installers)
    release = threading.Event()

    def command(candidate: Any) -> Any:
        if candidate.candidate_id == "slow":
            assert release.wait(timeout=60)
            return SimpleNamespace(reclaimed_bytes=10, path=ws.root / "go-build")
        raise RuntimeError("无法执行 dotnet nuget locals: Command timed out after 600 seconds")

    monkeypatch.setattr(product_vendor_app, "execute_vendor_cleanup", command)
    vendor = (SimpleNamespace(candidate_id="slow"), SimpleNamespace(candidate_id="stuck"))
    worker = threading.Thread(target=window._product_delete_worker, args=("delete", rows, vendor))
    worker.start()
    try:
        seen: list[tuple[str, Any]] = []
        while sum(kind == "delete_item_done" for kind, _payload in seen) < 3:
            seen.append(window._events.get(timeout=60))
        # Every file row is over while the first command still runs.
        filesystem = [_row_paths(payload) for kind, payload in seen if kind == "delete_item_done"]
        reported = sorted(path for paths in filesystem for path in paths)
        assert reported == sorted(map(str, installers))
        assert worker.is_alive() and not any(path.exists() for path in installers)
    finally:
        release.set()
        worker.join(timeout=60)
    assert not worker.is_alive()
    rest = _drain(window)
    assert rest[-1][0] == "delete_done"
    commands = [payload for kind, payload in rest if kind == "delete_item_done"]
    assert {payload[1] for payload in commands} == {4, 5}
    failed = [payload[4] for payload in commands if payload[4] is not None]
    timed_out = "无法执行 dotnet nuget locals: Command timed out after 600 seconds"
    assert failed == [("vendor:stuck", f"其他（RuntimeError：{timed_out}）")]


def test_an_unexpected_error_still_reports_every_row_and_ends_the_run(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    installers = [_nsis(ws.downloads / f"old-setup-{i}.exe") for i in range(3)]
    window, _session = _scan(ws)
    deletable, _unsure = _partition(window)
    rows = tuple(deletable[path.name] for path in installers)

    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise KeyError("journal went away")

    monkeypatch.setattr(product_vendor_app, "execute_cleanup_batch", broken)
    window._product_delete_worker("delete", rows, ())
    events = _drain(window)
    assert events[-1][0] == "delete_done"
    failures = [payload[4] for kind, payload in events if kind == "delete_item_done"]
    assert sorted(path for path, _reason in failures) == sorted(map(str, installers))
    assert {reason for _path, reason in failures} == {"其他（KeyError：'journal went away'）"}
    assert all(path.exists() for path in installers)

    def crashed(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("worker crashed")

    monkeypatch.setattr(window, "_clean_filesystem_rows", crashed)
    window._product_delete_worker("delete", rows, ())
    events = _drain(window)
    assert events[-1][0] == "delete_done"  # the window is never left waiting
    failures = [payload[4] for kind, payload in events if kind == "delete_item_done"]
    assert sorted(path for path, _reason in failures) == sorted(map(str, installers))
