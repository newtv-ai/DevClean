from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import devclean.core.delivery_optimization_cache_maintenance as do_cache
from devclean.core.delivery_optimization_cache_maintenance import (
    DeliveryOptimizationEntry,
    DeliveryOptimizationInventory,
    WindowsFileIdentity,
    delete_delivery_optimization_cache_file,
    inventory_delivery_optimization_cache,
)

_ENV = {
    "DEVCLEAN_TEST_WINDOWS": "1",
    "DEVCLEAN_WINDOWS_POWERSHELL": r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
    "DEVCLEAN_DELIVERY_OPTIMIZATION_MODULE": (
        "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\Modules\\DeliveryOptimization\\"
        "DeliveryOptimization.psd1"
    ),
}
_NOW = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)


def _identity(path: Path, seed: int = 1) -> WindowsFileIdentity:
    return WindowsFileIdentity(
        path=path,
        volume_serial=100 + seed,
        file_id=f"file-{seed}",
        file_id_kind="128",
        creation_time_ns=1000 + seed,
        last_write_time_ns=2000 + seed,
    )


def _entry(
    *,
    file_id: str = "file-a",
    status: str = "Caching",
    pinned: bool = False,
    expire_on: datetime | None = None,
    cache_bytes: int = 1024,
    decision: str = "USER_REVIEW",
    supported: bool = True,
) -> DeliveryOptimizationEntry:
    return DeliveryOptimizationEntry(
        file_id=file_id,
        file_size=2048,
        cache_bytes=cache_bytes,
        status=status,
        priority="Background",
        expire_on=expire_on,
        pinned=pinned,
        caller="WindowsUpdate",
        decision_class=decision,
        deletion_supported=supported,
        reason="review" if supported else "protected",
    )


def _inventory(
    entries: tuple[DeliveryOptimizationEntry, ...],
    *,
    powershell: Path | None = None,
) -> DeliveryOptimizationInventory:
    ps = powershell or Path(_ENV["DEVCLEAN_WINDOWS_POWERSHELL"])
    module = Path(_ENV["DEVCLEAN_DELIVERY_OPTIMIZATION_MODULE"])
    return DeliveryOptimizationInventory(
        elevated=True,
        powershell=_identity(ps, 1),
        module_manifest=_identity(module, 2),
        entries=entries,
    )


def _completed(stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["powershell.exe"], 0, stdout, "")


def test_entry_policy_separates_expired_user_review_pinned_and_active() -> None:
    expired = do_cache._entry_from_payload(
        {
            "FileId": "expired",
            "FileSize": 2000,
            "FileSizeInCache": 1000,
            "Status": "Caching",
            "Priority": "Background",
            "ExpireOn": (_NOW - timedelta(minutes=1)).isoformat(),
            "IsPinned": False,
            "Caller": "WindowsUpdate",
        },
        elevated=True,
        now=_NOW,
    )
    future = do_cache._entry_from_payload(
        {
            "FileId": "future",
            "FileSize": 2000,
            "FileSizeInCache": 1000,
            "Status": "Caching",
            "Priority": "Background",
            "ExpireOn": (_NOW + timedelta(days=2)).isoformat(),
            "IsPinned": False,
            "Caller": "WindowsUpdate",
        },
        elevated=True,
        now=_NOW,
    )
    pinned = do_cache._entry_from_payload(
        {
            "FileId": "pinned",
            "FileSize": 2000,
            "FileSizeInCache": 1000,
            "Status": "Caching",
            "Priority": "Background",
            "ExpireOn": (_NOW + timedelta(days=20)).isoformat(),
            "IsPinned": True,
            "Caller": "WindowsUpdate",
        },
        elevated=True,
        now=_NOW,
    )
    downloading = do_cache._entry_from_payload(
        {
            "FileId": "downloading",
            "FileSize": 2000,
            "FileSizeInCache": 1000,
            "Status": "Downloading",
            "Priority": "Foreground",
            "ExpireOn": None,
            "IsPinned": False,
            "Caller": "Store",
        },
        elevated=True,
        now=_NOW,
    )

    assert expired.decision_class == "DETERMINISTIC_CANDIDATE"
    assert expired.deletion_supported
    assert future.decision_class == "USER_REVIEW"
    assert future.deletion_supported
    assert not pinned.deletion_supported
    assert "pin" in pinned.reason
    assert not downloading.deletion_supported


def test_unknown_status_fails_closed() -> None:
    entry = do_cache._entry_from_payload(
        {
            "FileId": "unknown",
            "FileSize": 2000,
            "FileSizeInCache": 1000,
            "Status": "NewFutureState",
            "Priority": "Background",
            "ExpireOn": None,
            "IsPinned": False,
            "Caller": "WindowsUpdate",
        },
        elevated=True,
        now=_NOW,
    )

    assert entry.decision_class == "REPORT_ONLY"
    assert not entry.deletion_supported
    assert "fail closed" in entry.reason


def test_non_elevated_inventory_is_read_only_even_for_expired_cache() -> None:
    entry = do_cache._entry_from_payload(
        {
            "FileId": "expired",
            "FileSize": 2000,
            "FileSizeInCache": 1000,
            "Status": "Caching",
            "Priority": "Background",
            "ExpireOn": (_NOW - timedelta(days=1)).isoformat(),
            "IsPinned": False,
            "Caller": "WindowsUpdate",
        },
        elevated=False,
        now=_NOW,
    )

    assert entry.decision_class == "DETERMINISTIC_CANDIDATE"
    assert not entry.deletion_supported
    assert "管理员" in entry.reason


def test_inventory_rejects_duplicate_file_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    ps = Path(_ENV["DEVCLEAN_WINDOWS_POWERSHELL"])
    module = Path(_ENV["DEVCLEAN_DELIVERY_OPTIMIZATION_MODULE"])
    monkeypatch.setattr(do_cache, "_windows_powershell", lambda environment: ps)
    monkeypatch.setattr(do_cache, "_delivery_optimization_module", lambda environment: module)
    monkeypatch.setattr(
        do_cache,
        "_file_identity",
        lambda path, label: _identity(path, 1 if "PowerShell" in label else 2),
    )
    monkeypatch.setattr(do_cache, "_is_process_elevated", lambda: True)
    payload = {
        "Items": [
            {"FileId": "same", "FileSize": 10, "FileSizeInCache": 10, "Status": "Caching", "Priority": "Background", "ExpireOn": None, "IsPinned": False, "Caller": "Store"},
            {"FileId": "SAME", "FileSize": 10, "FileSizeInCache": 10, "Status": "Caching", "Priority": "Background", "ExpireOn": None, "IsPinned": False, "Caller": "Store"},
        ]
    }
    monkeypatch.setattr(
        do_cache,
        "_run_powershell",
        lambda *args, **kwargs: _completed(json.dumps(payload)),
    )

    with pytest.raises(RuntimeError, match="FileId 重复"):
        inventory_delivery_optimization_cache(_ENV, now=_NOW)


def test_inventory_rejects_tool_identity_race(monkeypatch: pytest.MonkeyPatch) -> None:
    ps = Path(_ENV["DEVCLEAN_WINDOWS_POWERSHELL"])
    module = Path(_ENV["DEVCLEAN_DELIVERY_OPTIMIZATION_MODULE"])
    monkeypatch.setattr(do_cache, "_windows_powershell", lambda environment: ps)
    monkeypatch.setattr(do_cache, "_delivery_optimization_module", lambda environment: module)
    identities = iter((_identity(ps, 1), _identity(module, 2), _identity(ps, 3), _identity(module, 2)))
    monkeypatch.setattr(do_cache, "_file_identity", lambda path, label: next(identities))
    monkeypatch.setattr(do_cache, "_run_powershell", lambda *args, **kwargs: _completed('{"Items":[]}'))

    with pytest.raises(RuntimeError, match="身份在检查期间发生变化"):
        inventory_delivery_optimization_cache(_ENV, now=_NOW)


def test_delete_uses_only_exact_file_id_force_without_include_pinned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = _entry(expire_on=_NOW + timedelta(days=1))
    before = _inventory((expected,))
    after = _inventory(())
    inventories = iter((before, before, after))
    monkeypatch.setattr(
        do_cache,
        "inventory_delivery_optimization_cache",
        lambda environment=None, now=None: next(inventories),
    )
    monkeypatch.setattr(do_cache, "_is_process_elevated", lambda: True)
    calls: list[tuple[Path, Path, str, dict[str, str]]] = []

    def fake_run(
        powershell: Path,
        module: Path,
        script: str,
        environment: dict[str, str],
        *,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        del timeout
        calls.append((powershell, module, script, environment))
        return _completed("deleted")

    monkeypatch.setattr(do_cache, "_run_powershell", fake_run)
    result = delete_delivery_optimization_cache_file(expected, before, _ENV, now=_NOW)

    assert result.entry.file_id == "file-a"
    assert len(calls) == 1
    _, _, script, env = calls[0]
    assert env["DEVCLEAN_DO_FILE_ID"] == "file-a"
    assert "-FileID $fileId" in script
    assert "-Force" in script
    assert "IncludePinnedFiles" not in script


def test_delete_refuses_reviewed_tool_change(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = _entry()
    reviewed = _inventory((expected,))
    changed = _inventory((expected,), powershell=Path(r"C:\Other\powershell.exe"))
    monkeypatch.setattr(
        do_cache,
        "inventory_delivery_optimization_cache",
        lambda environment=None, now=None: changed,
    )
    monkeypatch.setattr(do_cache, "_is_process_elevated", lambda: True)

    with pytest.raises(RuntimeError, match="身份已变化"):
        delete_delivery_optimization_cache_file(expected, reviewed, _ENV, now=_NOW)


def test_delete_refuses_pin_or_expiry_change(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = _entry(expire_on=_NOW + timedelta(days=1))
    reviewed = _inventory((expected,))
    changed_entry = replace(expected, pinned=True, deletion_supported=False, reason="pinned")
    changed = _inventory((changed_entry,))
    monkeypatch.setattr(
        do_cache,
        "inventory_delivery_optimization_cache",
        lambda environment=None, now=None: changed,
    )
    monkeypatch.setattr(do_cache, "_is_process_elevated", lambda: True)

    with pytest.raises(RuntimeError, match="identity 已变化"):
        delete_delivery_optimization_cache_file(expected, reviewed, _ENV, now=_NOW)


def test_delete_requires_exact_file_id_absent_after_success(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = _entry()
    inventory = _inventory((expected,))
    inventories = iter((inventory, inventory, inventory))
    monkeypatch.setattr(
        do_cache,
        "inventory_delivery_optimization_cache",
        lambda environment=None, now=None: next(inventories),
    )
    monkeypatch.setattr(do_cache, "_is_process_elevated", lambda: True)
    monkeypatch.setattr(do_cache, "_run_powershell", lambda *args, **kwargs: _completed("deleted"))

    with pytest.raises(RuntimeError, match="仍然存在"):
        delete_delivery_optimization_cache_file(expected, inventory, _ENV, now=_NOW)


def test_file_identity_rejects_reparse_or_missing_stable_id(monkeypatch: pytest.MonkeyPatch) -> None:
    path = Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
    monkeypatch.setattr(Path, "is_symlink", lambda self: False)
    monkeypatch.setattr(Path, "is_junction", lambda self: False)
    monkeypatch.setattr(
        do_cache,
        "read_file_metadata",
        lambda candidate: SimpleNamespace(
            is_directory=False,
            is_reparse_point=False,
            volume_serial=None,
            file_id=None,
            file_id_kind=None,
            creation_time_ns=None,
            last_write_time_ns=None,
        ),
    )
    monkeypatch.setattr(do_cache, "is_local_fixed_path", lambda candidate: True)

    with pytest.raises(RuntimeError, match="稳定文件身份"):
        do_cache._file_identity(path, "test")
