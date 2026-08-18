from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import devclean.core.android_sdk_package_maintenance as android_packages
from devclean.core.android_sdk_package_maintenance import (
    AndroidSdkPackageEntry,
    AndroidSdkRootInventory,
    inventory_android_sdk_packages,
    uninstall_android_sdk_package,
)

_LIST_OUTPUT = """Installed packages:
  Path                 | Version | Description                 | Location
  -------              | ------- | -------                     | -------
  build-tools;35.0.0   | 35.0.0  | Android SDK Build-Tools 35 | build-tools\\35.0.0
  cmdline-tools;latest | 19.0    | Android SDK Command-line   | cmdline-tools\\latest
  platforms;android-35 | 2       | Android SDK Platform 35    | platforms\\android-35

Available Packages:
  Path | Version | Description
  ------- | ------- | -------
  platforms;android-36 | 1 | Android SDK Platform 36
"""


def _completed(
    executable: Path,
    arguments: tuple[str, ...],
    stdout: str = _LIST_OUTPUT,
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        [str(executable), *arguments],
        0,
        stdout=stdout,
        stderr="",
    )


def test_inventory_uses_sdkmanager_installed_table_and_exact_locations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "Android" / "Sdk"
    sdkmanager = root / "cmdline-tools" / "latest" / "bin" / "sdkmanager.bat"
    sdkmanager.parent.mkdir(parents=True)
    sdkmanager.write_text("@echo off\n", encoding="utf-8")
    (root / "build-tools" / "35.0.0").mkdir(parents=True)
    (root / "build-tools" / "35.0.0" / "aapt2.exe").write_bytes(b"x" * 31)
    (root / "platforms" / "android-35").mkdir(parents=True)
    (root / "platforms" / "android-35" / "android.jar").write_bytes(b"x" * 43)

    monkeypatch.setattr(
        android_packages,
        "android_sdk_roots",
        lambda environment: type("Roots", (), {"sdk_roots": (root,)})(),
    )
    monkeypatch.setattr(android_packages, "_find_sdkmanager", lambda path: sdkmanager)
    monkeypatch.setattr(android_packages, "is_local_fixed_path", lambda path: True)
    monkeypatch.setattr(
        android_packages,
        "_run_sdkmanager",
        lambda executable, arguments, environment, timeout: _completed(
            executable,
            arguments,
        ),
    )

    inventory = inventory_android_sdk_packages({})

    assert len(inventory.roots) == 1
    sdk = inventory.roots[0]
    assert sdk.sdk_root == root
    assert sdk.sdkmanager == sdkmanager
    assert [package.package_id for package in sdk.packages] == [
        "platforms;android-35",
        "build-tools;35.0.0",
        "cmdline-tools;latest",
    ]
    platform = next(
        package for package in sdk.packages if package.package_id == "platforms;android-35"
    )
    assert platform.installed_path == root / "platforms" / "android-35"
    assert platform.logical_bytes == 43
    assert platform.deletion_supported
    command_line = next(
        package for package in sdk.packages if package.package_id == "cmdline-tools;latest"
    )
    assert not command_line.deletion_supported
    assert "sdkmanager 自身" in command_line.protected_reason


def test_parser_does_not_mix_available_packages_into_installed_table(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "Sdk"
    root.mkdir()
    monkeypatch.setattr(android_packages, "is_local_fixed_path", lambda path: True)

    packages = android_packages._parse_installed_packages(_LIST_OUTPUT, root, True)

    assert {package.package_id for package in packages} == {
        "build-tools;35.0.0",
        "cmdline-tools;latest",
        "platforms;android-35",
    }
    assert "platforms;android-36" not in {package.package_id for package in packages}


def test_inventory_preserves_root_error_when_sdkmanager_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "Sdk"
    root.mkdir()
    monkeypatch.setattr(
        android_packages,
        "android_sdk_roots",
        lambda environment: type("Roots", (), {"sdk_roots": (root,)})(),
    )
    monkeypatch.setattr(android_packages, "_find_sdkmanager", lambda path: None)
    monkeypatch.setattr(android_packages, "is_local_fixed_path", lambda path: True)

    inventory = inventory_android_sdk_packages({})

    assert len(inventory.roots) == 1
    assert inventory.roots[0].packages == ()
    assert "没有找到" in inventory.roots[0].error


def test_uninstall_revalidates_exact_package_and_uses_vendor_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "Sdk"
    package_path = root / "platforms" / "android-35"
    package_path.mkdir(parents=True)
    (package_path / "android.jar").write_bytes(b"x" * 47)
    sdkmanager = root / "cmdline-tools" / "latest" / "bin" / "sdkmanager.bat"
    package = AndroidSdkPackageEntry(
        sdk_root=root,
        package_id="platforms;android-35",
        version="2",
        description="Android SDK Platform 35",
        location="platforms\\android-35",
        installed_path=package_path,
        logical_bytes=47,
        deletion_supported=True,
    )
    before = AndroidSdkRootInventory(
        sdk_root=root,
        sdkmanager=sdkmanager,
        local_fixed=True,
        packages=(package,),
    )
    after = AndroidSdkRootInventory(
        sdk_root=root,
        sdkmanager=sdkmanager,
        local_fixed=True,
        packages=(),
    )
    inventories = iter((before, before, after))
    commands: list[tuple[str, ...]] = []

    monkeypatch.setattr(
        android_packages,
        "_inventory_exact_root",
        lambda sdk_root, environment: next(inventories),
    )
    monkeypatch.setattr(android_packages, "clear_android_sdk_process_cache", lambda: None)
    monkeypatch.setattr(android_packages, "android_sdk_process_running", lambda: False)
    monkeypatch.setattr(android_packages, "_android_runtime_process_running", lambda: False)

    def fake_run(
        executable: Path,
        arguments: tuple[str, ...],
        environment: object,
        *,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        del environment, timeout
        commands.append(arguments)
        assert executable == sdkmanager
        return _completed(executable, arguments, "Uninstalled platforms;android-35\n")

    monkeypatch.setattr(android_packages, "_run_sdkmanager", fake_run)
    monkeypatch.setattr(android_packages, "_path_bytes", lambda path: 0)

    result = uninstall_android_sdk_package(
        root,
        "platforms;android-35",
        expected_version="2",
        expected_location="platforms\\android-35",
        environment={},
    )

    assert result.package_id == "platforms;android-35"
    assert result.before_bytes == 47
    assert result.after_bytes == 0
    assert result.reclaimed_bytes == 47
    assert commands == [
        ("--uninstall", "platforms;android-35", f"--sdk_root={root}")
    ]


def test_uninstall_refuses_changed_package_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "Sdk"
    sdkmanager = root / "cmdline-tools" / "latest" / "bin" / "sdkmanager.bat"
    changed = AndroidSdkPackageEntry(
        sdk_root=root,
        package_id="platforms;android-35",
        version="3",
        description="Android SDK Platform 35",
        location="platforms\\android-35",
        installed_path=root / "platforms" / "android-35",
        logical_bytes=1,
        deletion_supported=True,
    )
    inventory = AndroidSdkRootInventory(
        sdk_root=root,
        sdkmanager=sdkmanager,
        local_fixed=True,
        packages=(changed,),
    )
    monkeypatch.setattr(
        android_packages,
        "_inventory_exact_root",
        lambda sdk_root, environment: inventory,
    )

    with pytest.raises(ValueError, match="已变化"):
        uninstall_android_sdk_package(
            root,
            "platforms;android-35",
            expected_version="2",
            expected_location="platforms\\android-35",
            environment={},
        )


def test_uninstall_refuses_sdkmanager_own_package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "Sdk"
    sdkmanager = root / "cmdline-tools" / "latest" / "bin" / "sdkmanager.bat"
    package = AndroidSdkPackageEntry(
        sdk_root=root,
        package_id="cmdline-tools;latest",
        version="19.0",
        description="Command-line Tools",
        location="cmdline-tools\\latest",
        installed_path=root / "cmdline-tools" / "latest",
        logical_bytes=10,
        deletion_supported=False,
        protected_reason="这是 sdkmanager 自身所属的命令行工具 package；不能让执行器卸载自己",
    )
    inventory = AndroidSdkRootInventory(
        sdk_root=root,
        sdkmanager=sdkmanager,
        local_fixed=True,
        packages=(package,),
    )
    monkeypatch.setattr(
        android_packages,
        "_inventory_exact_root",
        lambda sdk_root, environment: inventory,
    )

    with pytest.raises(ValueError, match="执行器卸载自己"):
        uninstall_android_sdk_package(
            root,
            "cmdline-tools;latest",
            expected_version="19.0",
            expected_location="cmdline-tools\\latest",
            environment={},
        )


def test_uninstall_refuses_when_android_runtime_is_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "Sdk"
    sdkmanager = root / "cmdline-tools" / "latest" / "bin" / "sdkmanager.bat"
    package = AndroidSdkPackageEntry(
        sdk_root=root,
        package_id="platform-tools",
        version="36.0.0",
        description="Android SDK Platform-Tools",
        location="platform-tools",
        installed_path=root / "platform-tools",
        logical_bytes=10,
        deletion_supported=True,
    )
    inventory = AndroidSdkRootInventory(
        sdk_root=root,
        sdkmanager=sdkmanager,
        local_fixed=True,
        packages=(package,),
    )
    monkeypatch.setattr(
        android_packages,
        "_inventory_exact_root",
        lambda sdk_root, environment: inventory,
    )
    monkeypatch.setattr(android_packages, "clear_android_sdk_process_cache", lambda: None)
    monkeypatch.setattr(android_packages, "android_sdk_process_running", lambda: False)
    monkeypatch.setattr(android_packages, "_android_runtime_process_running", lambda: True)

    with pytest.raises(RuntimeError, match="正在使用 SDK"):
        uninstall_android_sdk_package(
            root,
            "platform-tools",
            expected_version="36.0.0",
            expected_location="platform-tools",
            environment={},
        )


def test_uninstall_refuses_non_local_sdk_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "Sdk"
    inventory = AndroidSdkRootInventory(
        sdk_root=root,
        sdkmanager=root / "sdkmanager.bat",
        local_fixed=False,
        packages=(),
    )
    monkeypatch.setattr(
        android_packages,
        "_inventory_exact_root",
        lambda sdk_root, environment: inventory,
    )

    with pytest.raises(ValueError, match="本地固定磁盘"):
        uninstall_android_sdk_package(
            root,
            "platform-tools",
            expected_version="36.0.0",
            expected_location="platform-tools",
            environment={},
        )
