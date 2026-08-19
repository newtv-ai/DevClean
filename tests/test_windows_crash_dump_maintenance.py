from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

import devclean.core.windows_crash_dump_maintenance as crash_dumps
from devclean.core.windows_crash_dump_maintenance import (
    WindowsCrashDumpKind,
    WindowsCrashDumpLocation,
    delete_windows_crash_dump,
    inventory_windows_crash_dumps,
)
from devclean.platform.windows.exact_cleanup import ExactMutationResult


def _location(
    kind: WindowsCrashDumpKind,
    path: Path,
    *,
    direct_file: bool,
    elevation: bool = False,
) -> WindowsCrashDumpLocation:
    return WindowsCrashDumpLocation(
        kind=kind,
        path=path,
        direct_file=direct_file,
        source="test source",
        configured_for=("test",),
        requires_elevation=elevation,
    )


def test_inventory_only_accepts_exact_dump_file_and_direct_dmp_children(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_root = tmp_path / "CrashDumps"
    user_root.mkdir()
    user_dump = user_root / "app.exe.123.dmp"
    user_dump.write_bytes(b"u" * 32)
    (user_root / "notes.txt").write_text("keep", encoding="utf-8")
    nested = user_root / "nested"
    nested.mkdir()
    (nested / "nested.dmp").write_bytes(b"nested")
    kernel_dump = tmp_path / "MEMORY.DMP"
    kernel_dump.write_bytes(b"k" * 64)

    locations = (
        _location(WindowsCrashDumpKind.USER_MODE, user_root, direct_file=False),
        _location(
            WindowsCrashDumpKind.KERNEL_MEMORY,
            kernel_dump,
            direct_file=True,
            elevation=True,
        ),
    )
    monkeypatch.setattr(crash_dumps, "_WINDOWS", True)
    monkeypatch.setattr(crash_dumps, "_is_process_elevated", lambda: False)
    monkeypatch.setattr(
        crash_dumps,
        "_discover_locations",
        lambda environment: (locations, ()),
    )
    monkeypatch.setattr(crash_dumps, "is_local_fixed_path", lambda path: True)

    inventory = inventory_windows_crash_dumps({})

    assert {entry.path for entry in inventory.entries} == {user_dump, kernel_dump}
    user_entry = next(entry for entry in inventory.entries if entry.path == user_dump)
    kernel_entry = next(entry for entry in inventory.entries if entry.path == kernel_dump)
    assert user_entry.deletion_supported
    assert user_entry.reason.startswith("崩溃转储是诊断证据")
    assert not kernel_entry.deletion_supported
    assert "管理员" in kernel_entry.reason


def test_live_kernel_default_root_accepts_only_root_and_one_component_level(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live_root = tmp_path / "LiveKernelReports"
    live_root.mkdir()
    full_dump = live_root / "full-live.dmp"
    full_dump.write_bytes(b"full")
    (live_root / "notes.txt").write_text("keep", encoding="utf-8")

    component = live_root / "WATCHDOG"
    component.mkdir()
    component_dump = component / "watchdog.dmp"
    component_dump.write_bytes(b"component")
    (component / "context.txt").write_text("keep", encoding="utf-8")

    deeper = component / "nested"
    deeper.mkdir()
    (deeper / "deep.dmp").write_bytes(b"deep")

    def missing_live_key(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise FileNotFoundError

    monkeypatch.setattr(crash_dumps.winreg, "OpenKey", missing_live_key)
    monkeypatch.setattr(crash_dumps, "is_local_fixed_path", lambda path: True)

    locations, warnings = crash_dumps._discover_live_kernel_locations(
        {"systemroot": str(tmp_path)}
    )

    assert not warnings
    assert len(locations) == 2
    assert locations[0].path == live_root
    assert locations[0].kind is WindowsCrashDumpKind.KERNEL_LIVE
    assert locations[0].requires_elevation
    assert locations[1].path == component
    assert "WATCHDOG" in locations[1].configured_for[0]

    entries = tuple(
        entry
        for location in locations
        for entry in crash_dumps._entries_for_location(location, elevated=True)
    )
    assert {entry.path for entry in entries} == {full_dump, component_dump}
    assert all(entry.kind is WindowsCrashDumpKind.KERNEL_LIVE for entry in entries)
    assert all(entry.deletion_supported for entry in entries)


def test_live_kernel_dump_requires_existing_elevation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "LiveKernelReports"
    root.mkdir()
    dump = root / "live.dmp"
    dump.write_bytes(b"diagnostic")
    location = _location(
        WindowsCrashDumpKind.KERNEL_LIVE,
        root,
        direct_file=False,
        elevation=True,
    )
    monkeypatch.setattr(crash_dumps, "_WINDOWS", True)
    monkeypatch.setattr(crash_dumps, "_is_process_elevated", lambda: False)
    monkeypatch.setattr(crash_dumps, "is_local_fixed_path", lambda path: True)
    monkeypatch.setattr(
        crash_dumps,
        "_discover_locations",
        lambda environment: ((location,), ()),
    )

    inventory = inventory_windows_crash_dumps({})

    assert len(inventory.entries) == 1
    assert inventory.entries[0].kind is WindowsCrashDumpKind.KERNEL_LIVE
    assert not inventory.entries[0].deletion_supported
    assert "管理员" in inventory.entries[0].reason


def test_inventory_rejects_nonlocal_or_unverifiable_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "CrashDumps"
    root.mkdir()
    dump = root / "app.dmp"
    dump.write_bytes(b"data")
    location = _location(WindowsCrashDumpKind.USER_MODE, root, direct_file=False)
    monkeypatch.setattr(crash_dumps, "_WINDOWS", True)
    monkeypatch.setattr(crash_dumps, "_is_process_elevated", lambda: True)
    monkeypatch.setattr(
        crash_dumps,
        "_discover_locations",
        lambda environment: ((location,), ()),
    )
    monkeypatch.setattr(crash_dumps, "is_local_fixed_path", lambda path: False)

    inventory = inventory_windows_crash_dumps({})

    assert len(inventory.entries) == 1
    assert not inventory.entries[0].deletion_supported
    assert inventory.entries[0].snapshot is None


def test_delete_revalidates_exact_entry_then_uses_handle_bound_purge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "CrashDumps"
    root.mkdir()
    dump = root / "app.dmp"
    dump.write_bytes(b"diagnostic")
    location = _location(WindowsCrashDumpKind.USER_MODE, root, direct_file=False)
    monkeypatch.setattr(crash_dumps, "_WINDOWS", True)
    monkeypatch.setattr(crash_dumps, "_is_process_elevated", lambda: True)
    monkeypatch.setattr(crash_dumps, "is_local_fixed_path", lambda path: True)
    monkeypatch.setattr(
        crash_dumps,
        "_discover_locations",
        lambda environment: ((location,), ()),
    )
    inventory = inventory_windows_crash_dumps({})
    expected = inventory.entries[0]
    monkeypatch.setattr(
        crash_dumps,
        "inventory_windows_crash_dumps",
        lambda environment=None: inventory,
    )
    calls: list[tuple[Path, object, object]] = []

    def fake_purge(path: Path, snapshot: object, boundary: object) -> ExactMutationResult:
        calls.append((path, snapshot, boundary))
        return ExactMutationResult(
            source_path=str(path),
            destination_path=None,
            source_name_absent=True,
            source_name_replaced=False,
            destination_matches=False,
        )

    monkeypatch.setattr(crash_dumps, "purge_exact_file", fake_purge)

    result = delete_windows_crash_dump(expected, {})

    assert result.logical_bytes_removed == len(b"diagnostic")
    assert calls == [(expected.path, expected.snapshot, expected.root_boundary)]


def test_delete_refuses_changed_identity_or_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "CrashDumps"
    root.mkdir()
    dump = root / "app.dmp"
    dump.write_bytes(b"diagnostic")
    location = _location(WindowsCrashDumpKind.USER_MODE, root, direct_file=False)
    monkeypatch.setattr(crash_dumps, "_WINDOWS", True)
    monkeypatch.setattr(crash_dumps, "_is_process_elevated", lambda: True)
    monkeypatch.setattr(crash_dumps, "is_local_fixed_path", lambda path: True)
    monkeypatch.setattr(
        crash_dumps,
        "_discover_locations",
        lambda environment: ((location,), ()),
    )
    inventory = inventory_windows_crash_dumps({})
    expected = inventory.entries[0]
    assert expected.snapshot is not None
    changed_entry = replace(
        expected,
        snapshot=replace(
            expected.snapshot,
            last_write_time_ns=expected.snapshot.last_write_time_ns + 1,
        ),
    )
    changed = replace(inventory, entries=(changed_entry,))
    monkeypatch.setattr(
        crash_dumps,
        "inventory_windows_crash_dumps",
        lambda environment=None: changed,
    )
    monkeypatch.setattr(
        crash_dumps,
        "purge_exact_file",
        lambda *args, **kwargs: pytest.fail("mutation must not run after identity change"),
    )

    with pytest.raises(RuntimeError, match="发生变化"):
        delete_windows_crash_dump(expected, {})


def test_delete_refuses_ambiguous_duplicate_semantic_match(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "CrashDumps"
    root.mkdir()
    dump = root / "same.dmp"
    dump.write_bytes(b"diagnostic")
    location = _location(WindowsCrashDumpKind.USER_MODE, root, direct_file=False)
    monkeypatch.setattr(crash_dumps, "_WINDOWS", True)
    monkeypatch.setattr(crash_dumps, "_is_process_elevated", lambda: True)
    monkeypatch.setattr(crash_dumps, "is_local_fixed_path", lambda path: True)
    monkeypatch.setattr(
        crash_dumps,
        "_discover_locations",
        lambda environment: ((location,), ()),
    )
    inventory = inventory_windows_crash_dumps({})
    expected = inventory.entries[0]
    ambiguous = replace(inventory, entries=(expected, expected))
    monkeypatch.setattr(
        crash_dumps,
        "inventory_windows_crash_dumps",
        lambda environment=None: ambiguous,
    )

    with pytest.raises(RuntimeError, match="唯一"):
        delete_windows_crash_dump(expected, {})


def test_path_resolution_accepts_only_audited_variable_shapes() -> None:
    env = {
        "systemroot": r"C:\Windows",
        "windir": r"C:\Windows",
        "systemdrive": "C:",
        "localappdata": r"C:\Users\test\AppData\Local",
        "temp": r"C:\Users\test\AppData\Local\Temp",
    }

    assert crash_dumps._resolve_crash_control_path(
        r"%SystemRoot%\MEMORY.DMP",
        env,
    ) == Path(r"C:\Windows\MEMORY.DMP")
    assert crash_dumps._resolve_crash_control_path(r"%TEMP%\MEMORY.DMP", env) is None
    assert crash_dumps._resolve_local_dump_path(
        r"%LOCALAPPDATA%\CrashDumps",
        env,
    ) == Path(r"C:\Users\test\AppData\Local\CrashDumps")
    assert crash_dumps._resolve_local_dump_path(r"%TEMP%\CrashDumps", env) is None
    assert crash_dumps._resolve_local_dump_path(r"D:\CrashDumps", env) == Path(
        r"D:\CrashDumps"
    )


def test_live_kernel_path_resolution_requires_documented_nt_dos_form() -> None:
    env = {"systemroot": r"C:\Windows", "windir": r"C:\Windows"}

    assert crash_dumps._resolve_live_kernel_path(None, env) == Path(
        r"C:\Windows\LiveKernelReports"
    )
    assert crash_dumps._resolve_live_kernel_path(
        r"\??\D:\LiveDumps",
        env,
    ) == Path(r"D:\LiveDumps")
    assert crash_dumps._resolve_live_kernel_path(r"D:\LiveDumps", env) is None
    assert crash_dumps._resolve_live_kernel_path(r"\Device\HarddiskVolume4\LiveDumps", env) is None
    assert crash_dumps._resolve_live_kernel_path(r"\??\UNC\server\share\LiveDumps", env) is None


def test_kernel_mode_labels_are_positive_whitelist() -> None:
    assert crash_dumps._crash_mode_label(0, None) == "disabled"
    assert crash_dumps._crash_mode_label(1, 1) == "active"
    assert crash_dumps._crash_mode_label(1, 0) == "complete"
    assert crash_dumps._crash_mode_label(2, 0) == "kernel"
    assert crash_dumps._crash_mode_label(3, 0) == "small"
    assert crash_dumps._crash_mode_label(7, 0) == "automatic"
    assert crash_dumps._crash_mode_label(99, 0) == "unknown"
