from __future__ import annotations

from pathlib import Path

import pytest

import devclean.core.unity_asset_store_maintenance as asset_store
import devclean.core.unity_project_maintenance as project_maintenance


def _unity_project(tmp_path: Path) -> Path:
    root = tmp_path / "UnityProject"
    (root / "Assets").mkdir(parents=True)
    settings = root / "ProjectSettings"
    settings.mkdir()
    (settings / "ProjectVersion.txt").write_text(
        "m_EditorVersion: 6000.0.65f1\n",
        encoding="utf-8",
    )
    library = root / "Library"
    library.mkdir()
    (library / "state.bin").write_bytes(b"x")
    return root


def test_project_library_delete_refuses_non_local_fixed_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _unity_project(tmp_path)
    monkeypatch.setattr(project_maintenance, "is_local_fixed_path", lambda path: False)

    with pytest.raises(ValueError, match="本地固定磁盘"):
        project_maintenance.delete_unity_project_library(root)

    assert (root / "Library" / "state.bin").exists()


def test_asset_store_delete_refuses_non_local_fixed_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "Asset Store-5.x"
    package = root / "Publisher" / "Product" / "pkg.unitypackage"
    package.parent.mkdir(parents=True)
    package.write_bytes(b"package")
    monkeypatch.setattr(asset_store, "is_local_fixed_path", lambda path: False)

    with pytest.raises(ValueError, match="本地固定磁盘"):
        asset_store.delete_unity_asset_store_package(root, package)

    assert package.read_bytes() == b"package"
