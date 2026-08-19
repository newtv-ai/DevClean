from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import devclean.core.windows_previous_install_maintenance as previous_install
from devclean.core.windows_previous_install_maintenance import (
    PreviousInstallInventory,
    WindowsDirectoryIdentity,
    WindowsFileIdentity,
    cleanup_previous_windows_installation,
)


def _cleanmgr_identity(seed: int = 1) -> WindowsFileIdentity:
    return WindowsFileIdentity(
        path=Path(r"C:\Windows\System32\cleanmgr.exe"),
        volume_serial=100 + seed,
        file_id=f"cleanmgr-{seed}",
        file_id_kind="128",
        last_write_time_ns=1000 + seed,
    )


def _old_identity(seed: int = 1) -> WindowsDirectoryIdentity:
    return WindowsDirectoryIdentity(
        path=Path(r"C:\Windows.old"),
        volume_serial=200 + seed,
        file_id=f"old-{seed}",
        file_id_kind="128",
        creation_time_ns=2000 + seed,
    )


def _inventory(
    *,
    old: WindowsDirectoryIdentity | None = None,
    cleaner: WindowsFileIdentity | None = None,
    supported: bool = True,
    elevated: bool = True,
) -> PreviousInstallInventory:
    old_identity = _old_identity() if old is None and supported else old
    cleaner_identity = _cleanmgr_identity() if cleaner is None and supported else cleaner
    return PreviousInstallInventory(
        elevated=elevated,
        system_root=Path(r"C:\Windows"),
        windows_old=Path(r"C:\Windows.old"),
        windows_old_identity=old_identity,
        windows_old_logical_bytes=24 * 1024**3 if old_identity is not None else None,
        setup_rollback_root=Path(r"C:\$WINDOWS.~BT"),
        setup_rollback_present=old_identity is not None,
        os_uninstall_window_days=10 if old_identity is not None else None,
        cleanmgr_identity=cleaner_identity,
        cleanup_supported=supported,
        reason="review",
    )


def test_cleanup_requires_elevation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(previous_install, "_WINDOWS", True)
    monkeypatch.setattr(previous_install, "is_process_elevated", lambda: False)

    with pytest.raises(PermissionError, match="管理员"):
        cleanup_previous_windows_installation(_inventory())


def test_cleanup_requires_reviewed_supported_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(previous_install, "_WINDOWS", True)
    monkeypatch.setattr(previous_install, "is_process_elevated", lambda: True)
    unsupported = _inventory(old=None, cleaner=None, supported=False)

    with pytest.raises(ValueError, match="没有可执行"):
        cleanup_previous_windows_installation(unsupported)


def test_cleanup_refuses_setup_or_cleanup_activity_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(previous_install, "_WINDOWS", True)
    monkeypatch.setattr(previous_install, "is_process_elevated", lambda: True)
    monkeypatch.setattr(
        previous_install,
        "windows_setup_or_cleanup_activity_running",
        lambda environment=None: True,
    )
    monkeypatch.setattr(
        previous_install,
        "_run_cleanup",
        lambda *args, **kwargs: pytest.fail("vendor cleanup must not run"),
    )

    with pytest.raises(RuntimeError, match="安装/升级/磁盘清理"):
        cleanup_previous_windows_installation(_inventory())


def test_cleanup_refuses_windows_old_identity_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = _inventory()
    changed = _inventory(old=_old_identity(2), cleaner=expected.cleanmgr_identity)
    monkeypatch.setattr(previous_install, "_WINDOWS", True)
    monkeypatch.setattr(previous_install, "is_process_elevated", lambda: True)
    monkeypatch.setattr(
        previous_install,
        "windows_setup_or_cleanup_activity_running",
        lambda environment=None: False,
    )
    monkeypatch.setattr(
        previous_install,
        "inventory_previous_windows_installation",
        lambda environment=None: changed,
    )
    monkeypatch.setattr(
        previous_install,
        "_run_cleanup",
        lambda *args, **kwargs: pytest.fail("vendor cleanup must not run after identity race"),
    )

    with pytest.raises(RuntimeError, match="身份已变化"):
        cleanup_previous_windows_installation(expected)


def test_cleanup_refuses_cleanmgr_identity_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = _inventory()
    changed = _inventory(old=expected.windows_old_identity, cleaner=_cleanmgr_identity(2))
    monkeypatch.setattr(previous_install, "_WINDOWS", True)
    monkeypatch.setattr(previous_install, "is_process_elevated", lambda: True)
    monkeypatch.setattr(
        previous_install,
        "windows_setup_or_cleanup_activity_running",
        lambda environment=None: False,
    )
    monkeypatch.setattr(
        previous_install,
        "inventory_previous_windows_installation",
        lambda environment=None: changed,
    )

    with pytest.raises(RuntimeError, match="身份已变化"):
        cleanup_previous_windows_installation(expected)


def test_cleanup_uses_only_cleanmgr_autoclean_and_requires_absent_postcondition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = _inventory()
    current = expected
    after = _inventory(old=None, cleaner=None, supported=False)
    inventories = iter((current, after))
    commands: list[tuple[str, ...]] = []

    monkeypatch.setattr(previous_install, "_WINDOWS", True)
    monkeypatch.setattr(previous_install, "is_process_elevated", lambda: True)
    monkeypatch.setattr(
        previous_install,
        "windows_setup_or_cleanup_activity_running",
        lambda environment=None: False,
    )
    monkeypatch.setattr(
        previous_install,
        "inventory_previous_windows_installation",
        lambda environment=None: next(inventories),
    )
    monkeypatch.setattr(
        previous_install,
        "_run_cleanup",
        lambda command, environment: commands.append(command),
    )

    result = cleanup_previous_windows_installation(expected)

    assert commands == [(r"C:\Windows\System32\cleanmgr.exe", "/AUTOCLEAN")]
    assert result.windows_old_removed
    assert result.command == commands[0]
    joined = " ".join(result.command).casefold()
    assert "sageset" not in joined
    assert "sagerun" not in joined
    assert "verylowdisk" not in joined
    assert "lowdisk" not in joined


def test_cleanup_does_not_report_success_if_windows_old_remains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = _inventory()
    inventories = iter((expected, expected))
    monkeypatch.setattr(previous_install, "_WINDOWS", True)
    monkeypatch.setattr(previous_install, "is_process_elevated", lambda: True)
    monkeypatch.setattr(
        previous_install,
        "windows_setup_or_cleanup_activity_running",
        lambda environment=None: False,
    )
    monkeypatch.setattr(
        previous_install,
        "inventory_previous_windows_installation",
        lambda environment=None: next(inventories),
    )
    monkeypatch.setattr(previous_install, "_run_cleanup", lambda command, environment: None)

    with pytest.raises(RuntimeError, match="Windows.old 仍存在"):
        cleanup_previous_windows_installation(expected)


def test_get_os_uninstall_window_parses_configured_window_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        previous_install,
        "_file_identity",
        lambda path, label: _cleanmgr_identity(),
    )
    monkeypatch.setattr(
        previous_install.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=(
                "Deployment Image Servicing and Management tool\n"
                "Version: 10.0.26100.1\n\n"
                "Uninstall Window : 10\n"
                "The operation completed successfully.\n"
            ),
            stderr="",
        ),
    )

    assert previous_install._get_os_uninstall_window(None) == 10


def test_get_os_uninstall_window_fails_closed_on_ambiguous_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        previous_install,
        "_file_identity",
        lambda path, label: _cleanmgr_identity(),
    )
    monkeypatch.setattr(
        previous_install.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="Uninstall Window : 10\nUninstall Window : 30\n",
            stderr="",
        ),
    )

    assert previous_install._get_os_uninstall_window(None) is None


def test_process_snapshot_failure_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(previous_install, "_WINDOWS", True)
    monkeypatch.setattr(
        previous_install.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="denied"
        ),
    )

    assert previous_install.windows_setup_or_cleanup_activity_running()
