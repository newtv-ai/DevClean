from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import devclean.core.conda_maintenance as conda_maintenance
from devclean.core.conda_maintenance import (
    clean_conda_package_cache,
    inventory_conda_storage,
)


def _layout(tmp_path: Path) -> tuple[dict[str, str], Path]:
    cache = tmp_path / "Miniconda3" / "pkgs"
    cache.mkdir(parents=True)
    env = {
        "USERPROFILE": str(tmp_path / "home"),
        "PROGRAMDATA": str(tmp_path / "ProgramData"),
        "CONDA_PKGS_DIRS": str(cache),
        "DEVCLEAN_CONDA_EXE": "conda-test",
    }
    return env, cache


def test_conda_inventory_is_read_only_and_sums_package_cache_bytes(
    tmp_path: Path,
) -> None:
    env, cache = _layout(tmp_path)
    (cache / "one.conda").write_bytes(b"a" * 17)
    extracted = cache / "numpy-2.0-py313_0"
    extracted.mkdir()
    (extracted / "python.dll").write_bytes(b"b" * 23)

    inventory = inventory_conda_storage(env)

    assert len(inventory.package_caches) == 1
    assert inventory.package_caches[0].path == cache
    assert inventory.package_caches[0].exists
    assert inventory.package_caches[0].logical_bytes == 40
    assert inventory.total_package_cache_bytes == 40
    assert (cache / "one.conda").exists()
    assert (extracted / "python.dll").exists()


def test_conda_clean_uses_vendor_cache_targets_and_exact_selected_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, cache = _layout(tmp_path)
    archive = cache / "numpy-2.0-py313_0.conda"
    extracted = cache / "numpy-2.0-py313_0"
    archive.write_bytes(b"x" * 31)
    extracted.mkdir()
    installed_source = extracted / "python.dll"
    installed_source.write_bytes(b"y" * 11)

    monkeypatch.setattr(conda_maintenance, "conda_process_running", lambda: False)

    def fake_run(
        command: list[str],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
        timeout: int,
        env: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        assert command[0] == "conda-test"
        assert command[1:] == [
            "clean",
            "--tarballs",
            "--index-cache",
            "--yes",
            "--json",
        ]
        assert "--packages" not in command
        assert "--all" not in command
        assert "--force-pkgs-dirs" not in command
        assert check is False
        assert capture_output is True
        assert text is True
        assert timeout == 600
        assert os.path.normcase(env["CONDA_PKGS_DIRS"]) == os.path.normcase(str(cache))
        archive.unlink()
        return subprocess.CompletedProcess(
            command,
            0,
            stdout='{"success": true}',
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = clean_conda_package_cache(cache, env)

    assert result.package_cache_path == cache
    assert result.before_bytes == 42
    assert result.after_bytes == 11
    assert result.reclaimed_bytes == 31
    assert result.stdout == '{"success": true}'
    assert installed_source.exists()


def test_conda_clean_refuses_unrecognized_directory(tmp_path: Path) -> None:
    env, _ = _layout(tmp_path)
    arbitrary = tmp_path / "not-a-conda-cache"
    arbitrary.mkdir()

    with pytest.raises(ValueError, match="已审计"):
        clean_conda_package_cache(arbitrary, env)


def test_conda_clean_refuses_while_conda_or_mamba_is_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, cache = _layout(tmp_path)
    monkeypatch.setattr(conda_maintenance, "conda_process_running", lambda: True)

    with pytest.raises(RuntimeError, match="Conda/Mamba 正在运行"):
        clean_conda_package_cache(cache, env)


def test_conda_clean_surfaces_vendor_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, cache = _layout(tmp_path)
    monkeypatch.setattr(conda_maintenance, "conda_process_running", lambda: False)

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            ["conda-test"],
            2,
            stdout="",
            stderr="cache locked",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="cache locked"):
        clean_conda_package_cache(cache, env)
