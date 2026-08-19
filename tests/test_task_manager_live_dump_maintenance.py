from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

import devclean.core.task_manager_live_dump_maintenance as task_dumps
from devclean.core.task_manager_live_dump_maintenance import (
    delete_task_manager_live_kernel_dump,
    inventory_task_manager_live_kernel_dumps,
)
from devclean.platform.windows.exact_cleanup import ExactMutationResult


def test_inventory_uses_known_folder_and_only_direct_dmp_children(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local = tmp_path / "Local"
    root = local / "Microsoft" / "Windows" / "TaskManager" / "LiveKernelDumps"
    root.mkdir(parents=True)
    dump = root / "TaskManagerLiveKernelDump.dmp"
    dump.write_bytes(b"diagnostic")
    (root / "readme.txt").write_text("keep", encoding="utf-8")
    nested = root / "nested"
    nested.mkdir()
    (nested / "nested.dmp").write_bytes(b"nested")

    monkeypatch.setattr(task_dumps, "_WINDOWS", True)
    monkeypatch.setattr(task_dumps, "local_appdata_path", lambda: local)
    monkeypatch.setattr(task_dumps, "is_local_fixed_path", lambda path: True)

    inventory = inventory_task_manager_live_kernel_dumps()

    assert inventory.root == root
    assert inventory.warning is None
    assert [entry.path for entry in inventory.entries] == [dump]
    assert inventory.entries[0].deletion_supported
    assert "诊断证据" in inventory.entries[0].reason


def test_inventory_does_not_use_localappdata_environment_as_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trusted = tmp_path / "Trusted"
    trusted_root = trusted / "Microsoft" / "Windows" / "TaskManager" / "LiveKernelDumps"
    trusted_root.mkdir(parents=True)
    trusted_dump = trusted_root / "trusted.dmp"
    trusted_dump.write_bytes(b"trusted")

    hostile = tmp_path / "Hostile"
    hostile_root = hostile / "Microsoft" / "Windows" / "TaskManager" / "LiveKernelDumps"
    hostile_root.mkdir(parents=True)
    (hostile_root / "hostile.dmp").write_bytes(b"hostile")
    monkeypatch.setenv("LOCALAPPDATA", str(hostile))

    monkeypatch.setattr(task_dumps, "_WINDOWS", True)
    monkeypatch.setattr(task_dumps, "local_appdata_path", lambda: trusted)
    monkeypatch.setattr(task_dumps, "is_local_fixed_path", lambda path: True)

    inventory = inventory_task_manager_live_kernel_dumps()

    assert inventory.root == trusted_root
    assert [entry.path for entry in inventory.entries] == [trusted_dump]


def test_inventory_rejects_nonlocal_or_reparse_root_without_scanning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local = tmp_path / "Local"
    root = local / "Microsoft" / "Windows" / "TaskManager" / "LiveKernelDumps"
    root.mkdir(parents=True)
    (root / "dump.dmp").write_bytes(b"data")

    monkeypatch.setattr(task_dumps, "_WINDOWS", True)
    monkeypatch.setattr(task_dumps, "local_appdata_path", lambda: local)
    monkeypatch.setattr(task_dumps, "is_local_fixed_path", lambda path: False)

    inventory = inventory_task_manager_live_kernel_dumps()

    assert inventory.entries == ()
    assert inventory.warning is not None
    assert "本地固定磁盘" in inventory.warning


def test_delete_revalidates_identity_then_uses_handle_bound_purge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local = tmp_path / "Local"
    root = local / "Microsoft" / "Windows" / "TaskManager" / "LiveKernelDumps"
    root.mkdir(parents=True)
    dump = root / "dump.dmp"
    dump.write_bytes(b"diagnostic")

    monkeypatch.setattr(task_dumps, "_WINDOWS", True)
    monkeypatch.setattr(task_dumps, "local_appdata_path", lambda: local)
    monkeypatch.setattr(task_dumps, "is_local_fixed_path", lambda path: True)
    inventory = inventory_task_manager_live_kernel_dumps()
    expected = inventory.entries[0]
    monkeypatch.setattr(
        task_dumps,
        "inventory_task_manager_live_kernel_dumps",
        lambda: inventory,
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

    monkeypatch.setattr(task_dumps, "purge_exact_file", fake_purge)

    result = delete_task_manager_live_kernel_dump(expected)

    assert result.logical_bytes_removed == len(b"diagnostic")
    assert calls == [(expected.path, expected.snapshot, expected.root_boundary)]


def test_delete_refuses_changed_file_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local = tmp_path / "Local"
    root = local / "Microsoft" / "Windows" / "TaskManager" / "LiveKernelDumps"
    root.mkdir(parents=True)
    dump = root / "dump.dmp"
    dump.write_bytes(b"diagnostic")

    monkeypatch.setattr(task_dumps, "_WINDOWS", True)
    monkeypatch.setattr(task_dumps, "local_appdata_path", lambda: local)
    monkeypatch.setattr(task_dumps, "is_local_fixed_path", lambda path: True)
    inventory = inventory_task_manager_live_kernel_dumps()
    expected = inventory.entries[0]
    changed_entry = replace(
        expected,
        snapshot=replace(
            expected.snapshot,
            last_write_time_ns=expected.snapshot.last_write_time_ns + 1,
        ),
    )
    changed = replace(inventory, entries=(changed_entry,))
    monkeypatch.setattr(
        task_dumps,
        "inventory_task_manager_live_kernel_dumps",
        lambda: changed,
    )
    monkeypatch.setattr(
        task_dumps,
        "purge_exact_file",
        lambda *args, **kwargs: pytest.fail("mutation must not run after identity change"),
    )

    with pytest.raises(RuntimeError, match="发生变化"):
        delete_task_manager_live_kernel_dump(expected)


def test_delete_refuses_ambiguous_duplicate_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local = tmp_path / "Local"
    root = local / "Microsoft" / "Windows" / "TaskManager" / "LiveKernelDumps"
    root.mkdir(parents=True)
    dump = root / "dump.dmp"
    dump.write_bytes(b"diagnostic")

    monkeypatch.setattr(task_dumps, "_WINDOWS", True)
    monkeypatch.setattr(task_dumps, "local_appdata_path", lambda: local)
    monkeypatch.setattr(task_dumps, "is_local_fixed_path", lambda path: True)
    inventory = inventory_task_manager_live_kernel_dumps()
    expected = inventory.entries[0]
    ambiguous = replace(inventory, entries=(expected, expected))
    monkeypatch.setattr(
        task_dumps,
        "inventory_task_manager_live_kernel_dumps",
        lambda: ambiguous,
    )

    with pytest.raises(RuntimeError, match="唯一"):
        delete_task_manager_live_kernel_dump(expected)
