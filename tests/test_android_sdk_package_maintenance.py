from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import devclean.core.android_sdk_package_maintenance as sdk_packages
from devclean.core.android_sdk_package_maintenance import (
    AndroidAvdSystemReference,
    AndroidPathIdentity,
    AndroidSdkPackageEntry,
    AndroidSdkRootInventory,
    inventory_android_sdk_packages,
    uninstall_android_sdk_package,
)


def _identity(seed: int = 1) -> AndroidPathIdentity:
    return AndroidPathIdentity(
        volume_serial=100 + seed,
        file_id=f"id-{seed}",
        file_id_kind="128",
        creation_time_ns=1000 + seed,
        last_write_time_ns=2000 + seed,
    )


def _installed_output(rows: list[tuple[str, str, str, str]]) -> str:
    body = "\n".join(" | ".join(row) for row in rows)
    return f"""Installed packages:
  Path | Version | Description | Location
  ------- | ------- | ------- | -------
{body}

Available Packages:
  Path | Version | Description
"""


def _avd(name: str, package_path: Path) -> AndroidAvdSystemReference:
    return AndroidAvdSystemReference(
        avd_name=name,
        content_root=package_path.parent / f"{name}.avd",
        config_path=package_path.parent / f"{name}.avd" / "config.ini",
        raw_system_dirs=("system-images/android-35/google_apis/x86_64/",),
        resolved_system_dirs=(package_path,),
    )


def _package(
    root: Path,
    *,
    package_id: str = "platforms;android-35",
    version: str = "1",
    location: str = "platforms/android-35",
    avd_names: tuple[str, ...] = (),
    supported: bool = True,
) -> AndroidSdkPackageEntry:
    installed = root.joinpath(*location.split("/"))
    return AndroidSdkPackageEntry(
        sdk_root=root,
        package_id=package_id,
        version=version,
        description="demo",
        location=location,
        installed_path=installed,
        installed_identity=_identity(3),
        logical_bytes=1024,
        avd_names=avd_names,
        deletion_supported=supported,
        protected_reason="" if supported else "protected",
    )


def _root_inventory(
    root: Path,
    packages: tuple[AndroidSdkPackageEntry, ...],
    *,
    manager: Path | None = None,
) -> AndroidSdkRootInventory:
    return AndroidSdkRootInventory(
        sdk_root=root,
        sdk_root_identity=_identity(1),
        sdkmanager=manager or root / "cmdline-tools" / "latest" / "bin" / "sdkmanager.bat",
        sdkmanager_identity=_identity(2),
        local_fixed=True,
        packages=packages,
        avd_reference_proof_complete=True,
        avd_reference_proof_reason="",
    )


def test_system_image_referenced_by_avd_is_protected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "Sdk"
    image = root / "system-images" / "android-35" / "google_apis" / "x86_64"
    image.mkdir(parents=True)
    platform = root / "platforms" / "android-35"
    platform.mkdir(parents=True)
    monkeypatch.setattr(sdk_packages, "is_local_fixed_path", lambda path: True)

    packages = sdk_packages._parse_installed_packages(
        _installed_output(
            [
                (
                    "system-images;android-35;google_apis;x86_64",
                    "12",
                    "Google APIs Intel x86_64 Atom System Image",
                    "system-images/android-35/google_apis/x86_64",
                ),
                ("platforms;android-35", "2", "Android SDK Platform 35", "platforms/android-35"),
            ]
        ),
        root,
        True,
        (_avd("Pixel_8_API_35", image),),
        True,
        "",
    )

    by_id = {package.package_id: package for package in packages}
    system_image = by_id["system-images;android-35;google_apis;x86_64"]
    assert not system_image.deletion_supported
    assert system_image.avd_names == ("Pixel_8_API_35",)
    assert "1 个 AVD" in system_image.protected_reason
    assert by_id["platforms;android-35"].deletion_supported


def test_incomplete_avd_proof_protects_all_system_images(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "Sdk"
    image = root / "system-images" / "android-34" / "default" / "x86_64"
    image.mkdir(parents=True)
    monkeypatch.setattr(sdk_packages, "is_local_fixed_path", lambda path: True)

    packages = sdk_packages._parse_installed_packages(
        _installed_output(
            [
                (
                    "system-images;android-34;default;x86_64",
                    "9",
                    "Android x86_64 System Image",
                    "system-images/android-34/default/x86_64",
                )
            ]
        ),
        root,
        True,
        (),
        False,
        "一个 AVD config.ini 无法读取",
    )

    assert not packages[0].deletion_supported
    assert "引用证明不完整" in packages[0].protected_reason


def test_sdkmanager_hosting_package_and_outside_location_are_protected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "Sdk"
    cmdline = root / "cmdline-tools" / "13.0"
    cmdline.mkdir(parents=True)
    monkeypatch.setattr(sdk_packages, "is_local_fixed_path", lambda path: True)

    packages = sdk_packages._parse_installed_packages(
        _installed_output(
            [
                ("cmdline-tools;13.0", "13", "Android SDK Command-line Tools", "cmdline-tools/13.0"),
                ("extras;vendor;outside", "1", "Outside", "../outside"),
            ]
        ),
        root,
        True,
        (),
        True,
        "",
    )

    by_id = {package.package_id: package for package in packages}
    assert not by_id["cmdline-tools;13.0"].deletion_supported
    assert "卸载自己" in by_id["cmdline-tools;13.0"].protected_reason
    assert not by_id["extras;vendor;outside"].deletion_supported
    assert by_id["extras;vendor;outside"].installed_path is None


def test_avd_config_relative_system_dir_correlates_against_all_known_sdk_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdk_a = tmp_path / "SdkA"
    sdk_b = tmp_path / "SdkB"
    sdk_a.mkdir()
    sdk_b.mkdir()
    content = tmp_path / "Pixel.avd"
    content.mkdir()
    (content / "config.ini").write_text(
        "AvdId=Pixel_API_35\n"
        "image.sysdir.1=system-images/android-35/google_apis/x86_64/\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sdk_packages, "_source_avd_content_roots", lambda environment: (content,))
    monkeypatch.setattr(sdk_packages, "_source_avd_registry_roots", lambda environment: ())

    references, complete, reason = sdk_packages._inventory_avd_references(
        (sdk_a, sdk_b),
        None,
    )

    assert complete
    assert not reason
    assert references[0].avd_name == "Pixel_API_35"
    expected = {
        sdk_a / "system-images" / "android-35" / "google_apis" / "x86_64",
        sdk_b / "system-images" / "android-35" / "google_apis" / "x86_64",
    }
    assert set(references[0].resolved_system_dirs) == expected


def test_unreadable_or_missing_avd_config_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = tmp_path / "Broken.avd"
    content.mkdir()
    monkeypatch.setattr(sdk_packages, "_source_avd_content_roots", lambda environment: (content,))
    monkeypatch.setattr(sdk_packages, "_source_avd_registry_roots", lambda environment: ())

    references, complete, reason = sdk_packages._inventory_avd_references((tmp_path / "Sdk",), None)

    assert not references
    assert not complete
    assert "config.ini" in reason


def test_registry_descriptor_failure_makes_avd_proof_incomplete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = tmp_path / ".android" / "avd"
    registry.mkdir(parents=True)
    (registry / "Broken.ini").write_text("target=android-35\n", encoding="utf-8")
    monkeypatch.setattr(sdk_packages, "_source_avd_content_roots", lambda environment: ())
    monkeypatch.setattr(sdk_packages, "_source_avd_registry_roots", lambda environment: (registry,))

    references, complete, reason = sdk_packages._inventory_avd_references((tmp_path / "Sdk",), None)

    assert not references
    assert not complete
    assert "path/path.rel" in reason


def test_registry_direct_avd_directory_is_discovered_even_if_shared_helper_misses_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdk = tmp_path / "Sdk"
    sdk.mkdir()
    registry = tmp_path / ".android" / "avd"
    content = registry / "Pixel_API_35.avd"
    content.mkdir(parents=True)
    (content / "config.ini").write_text(
        "AvdId=Pixel_API_35\nimage.sysdir.1=system-images/android-35/google_apis/x86_64/\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sdk_packages, "_source_avd_content_roots", lambda environment: ())
    monkeypatch.setattr(sdk_packages, "_source_avd_registry_roots", lambda environment: (registry,))

    references, complete, reason = sdk_packages._inventory_avd_references((sdk,), None)

    assert complete
    assert not reason
    assert len(references) == 1
    assert references[0].avd_name == "Pixel_API_35"


def test_registry_path_rel_maps_from_android_user_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdk = tmp_path / "Sdk"
    sdk.mkdir()
    android_home = tmp_path / ".android"
    registry = android_home / "avd"
    content = android_home / "custom" / "Pixel.avd"
    registry.mkdir(parents=True)
    content.mkdir(parents=True)
    (registry / "Pixel.ini").write_text("path.rel=custom/Pixel.avd\n", encoding="utf-8")
    (content / "config.ini").write_text(
        "AvdId=Pixel\nimage.sysdir.1=system-images/android-35/default/x86_64/\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sdk_packages, "_source_avd_content_roots", lambda environment: ())
    monkeypatch.setattr(sdk_packages, "_source_avd_registry_roots", lambda environment: (registry,))

    references, complete, reason = sdk_packages._inventory_avd_references((sdk,), None)

    assert complete
    assert not reason
    assert references[0].content_root == content.resolve()


def test_duplicate_avd_image_key_fails_closed(tmp_path: Path) -> None:
    config = tmp_path / "config.ini"
    config.write_text(
        "image.sysdir.1=system-images/a\nimage.sysdir.1=system-images/b\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="重复 key"):
        sdk_packages._read_ini(config)


def test_inventory_root_without_sdkmanager_is_report_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "Sdk"
    root.mkdir()
    monkeypatch.setattr(sdk_packages, "_source_sdk_roots", lambda environment: (root,))
    monkeypatch.setattr(sdk_packages, "_source_avd_content_roots", lambda environment: ())
    monkeypatch.setattr(sdk_packages, "_source_avd_registry_roots", lambda environment: ())
    monkeypatch.setattr(sdk_packages, "is_local_fixed_path", lambda path: True)
    monkeypatch.setattr(sdk_packages, "_find_sdkmanager", lambda sdk_root: None)

    inventory = inventory_android_sdk_packages()

    assert len(inventory.roots) == 1
    assert inventory.roots[0].sdkmanager is None
    assert not inventory.roots[0].packages
    assert "不提供卸载" in inventory.roots[0].error


def test_uninstall_revalidates_root_package_and_uses_only_exact_vendor_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "Sdk"
    installed = root / "platforms" / "android-35"
    installed.mkdir(parents=True)
    manager = root / "cmdline-tools" / "latest" / "bin" / "sdkmanager.bat"
    manager.parent.mkdir(parents=True)
    manager.write_text("@echo off", encoding="utf-8")
    package = _package(root)
    before = _root_inventory(root, (package,), manager=manager)
    after = _root_inventory(root, (), manager=manager)
    inventories = iter((before, before, after))
    monkeypatch.setattr(sdk_packages, "_inventory_exact_root", lambda sdk_root, environment: next(inventories))
    monkeypatch.setattr(sdk_packages, "android_sdk_process_running", lambda: False)
    monkeypatch.setattr(sdk_packages, "_android_runtime_process_running", lambda: False)
    monkeypatch.setattr(sdk_packages, "clear_android_sdk_process_cache", lambda: None)
    commands: list[tuple[str, ...]] = []

    def fake_run(
        command: tuple[str, ...],
        environment: object,
        *,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        del environment, timeout
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "Done", "")

    monkeypatch.setattr(sdk_packages, "_run_sdkmanager", fake_run)
    monkeypatch.setattr(sdk_packages, "_path_bytes", lambda path: 0)

    result = uninstall_android_sdk_package(package, before)

    assert result.package_id == "platforms;android-35"
    assert commands == [
        (
            str(manager),
            "--uninstall",
            "platforms;android-35",
            f"--sdk_root={root}",
        )
    ]


def test_uninstall_refuses_new_avd_reference_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "Sdk"
    manager = root / "cmdline-tools" / "latest" / "bin" / "sdkmanager.bat"
    manager.parent.mkdir(parents=True)
    manager.write_text("@echo off", encoding="utf-8")
    package = _package(
        root,
        package_id="system-images;android-35;google_apis;x86_64",
        location="system-images/android-35/google_apis/x86_64",
    )
    reviewed = _root_inventory(root, (package,), manager=manager)
    changed_package = AndroidSdkPackageEntry(
        sdk_root=package.sdk_root,
        package_id=package.package_id,
        version=package.version,
        description=package.description,
        location=package.location,
        installed_path=package.installed_path,
        installed_identity=package.installed_identity,
        logical_bytes=package.logical_bytes,
        avd_names=("Pixel",),
        deletion_supported=False,
        protected_reason="system image 正被 1 个 AVD 配置引用：Pixel",
    )
    changed = _root_inventory(root, (changed_package,), manager=manager)
    monkeypatch.setattr(sdk_packages, "_inventory_exact_root", lambda sdk_root, environment: changed)
    monkeypatch.setattr(sdk_packages, "android_sdk_process_running", lambda: False)
    monkeypatch.setattr(sdk_packages, "_android_runtime_process_running", lambda: False)
    monkeypatch.setattr(sdk_packages, "clear_android_sdk_process_cache", lambda: None)
    monkeypatch.setattr(
        sdk_packages,
        "_run_sdkmanager",
        lambda *args, **kwargs: pytest.fail("uninstall must not run after AVD reference changed"),
    )

    with pytest.raises(RuntimeError, match="AVD 引用已变化"):
        uninstall_android_sdk_package(package, reviewed)


def test_uninstall_refuses_when_android_tooling_is_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "Sdk"
    package = _package(root)
    reviewed = _root_inventory(root, (package,))
    monkeypatch.setattr(sdk_packages, "android_sdk_process_running", lambda: True)
    monkeypatch.setattr(sdk_packages, "clear_android_sdk_process_cache", lambda: None)

    with pytest.raises(RuntimeError, match="正在使用 SDK"):
        uninstall_android_sdk_package(package, reviewed)


def test_identity_requires_stable_file_id() -> None:
    metadata = SimpleNamespace(
        volume_serial=None,
        file_id=None,
        file_id_kind=None,
        creation_time_ns=None,
        last_write_time_ns=None,
    )
    with pytest.raises(RuntimeError, match="稳定文件身份"):
        sdk_packages._identity_from_metadata(metadata)
