from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import devclean.core.android_project_sdk_references as project_refs


def _package_ids(source: str, tmp_path: Path) -> set[str]:
    root = tmp_path / "project"
    script = root / "app" / "build.gradle.kts"
    return {
        reference.package_id for reference in project_refs._parse_script_text(root, script, source)
    }


def test_literal_android_sdk_references_map_to_exact_sdkmanager_packages(tmp_path: Path) -> None:
    source = r"""
android {
    compileSdk = 35
    buildToolsVersion = "35.0.0"
    ndkVersion = "27.0.12077973"
    externalNativeBuild {
        cmake {
            path = file("CMakeLists.txt")
            version = "3.22.1"
        }
    }
}
"""

    assert _package_ids(source, tmp_path) == {
        "platforms;android-35",
        "build-tools;35.0.0",
        "ndk;27.0.12077973",
        "cmake;3.22.1",
    }


def test_groovy_compile_sdk_version_and_settings_plugin_release_are_positive_evidence(
    tmp_path: Path,
) -> None:
    source = r"""
android {
    compileSdkVersion "android-34"
    compileSdk {
        version = release(36) {
            minorApiLevel = 1
        }
    }
}
"""

    assert _package_ids(source, tmp_path) == {
        "platforms;android-34",
        "platforms;android-36",
    }


def test_dynamic_values_are_not_guessed_into_package_ids(tmp_path: Path) -> None:
    source = r"""
android {
    compileSdk = libs.versions.compileSdk.get().toInt()
    buildToolsVersion = versions.buildTools
    ndkVersion = rootProject.extra["ndkVersion"] as String
    externalNativeBuild {
        cmake {
            version = libs.versions.cmake.get()
        }
    }
}
"""

    assert not _package_ids(source, tmp_path)


def test_comments_unrelated_blocks_and_string_examples_do_not_create_evidence(
    tmp_path: Path,
) -> None:
    source = r"""
// compileSdk = 99
/* buildToolsVersion = "99.0.0" */
val example = "compileSdk = 98; ndkVersion = '98.0.0'"
foo {
    cmake {
        version = "9.9.9"
    }
}
android {
    compileSdk = 35 // ndkVersion = "99.0.0"
}
"""

    assert _package_ids(source, tmp_path) == {"platforms;android-35"}


def test_scan_reads_only_selected_gradle_files_and_reports_installed_literals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "Demo"
    app = root / "app"
    generated = app / "build"
    app.mkdir(parents=True)
    generated.mkdir()
    (root / "settings.gradle.kts").write_text(
        "pluginManagement { repositories { google() } }\n",
        encoding="utf-8",
    )
    (app / "build.gradle.kts").write_text(
        'android { compileSdk = 35; ndkVersion = "27.0.12077973" }\n',
        encoding="utf-8",
    )
    (generated / "build.gradle.kts").write_text(
        "android { compileSdk = 99 }\n",
        encoding="utf-8",
    )

    def metadata(path: Path) -> SimpleNamespace:
        return SimpleNamespace(
            is_directory=path.is_dir(),
            is_reparse_point=False,
            volume_serial=1,
            file_id=str(path),
            file_id_kind="test",
            creation_time_ns=1,
            last_write_time_ns=2,
        )

    monkeypatch.setattr(project_refs, "read_file_metadata", metadata)
    monkeypatch.setattr(project_refs, "is_local_fixed_path", lambda path: True)

    result = project_refs.scan_android_project_sdk_references(root)

    assert result.files_scanned == 2
    assert {reference.package_id for reference in result.references} == {
        "platforms;android-35",
        "ndk;27.0.12077973",
    }
    assert not result.warnings


def test_scan_rejects_directory_without_direct_gradle_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "not-a-gradle-root"
    root.mkdir()

    monkeypatch.setattr(project_refs, "_ordinary_local_directory", lambda path, label: path)

    with pytest.raises(ValueError, match=r"settings\.gradle"):
        project_refs.scan_android_project_sdk_references(root)


def test_line_numbers_are_retained_for_review(tmp_path: Path) -> None:
    root = tmp_path / "project"
    script = root / "build.gradle"
    references = project_refs._parse_script_text(
        root,
        script,
        "plugins {}\nandroid {\n  compileSdk 35\n  buildToolsVersion '35.0.0'\n}\n",
    )

    by_kind = {reference.kind: reference for reference in references}
    assert by_kind["compileSdk"].line_number == 3
    assert by_kind["buildToolsVersion"].line_number == 4
