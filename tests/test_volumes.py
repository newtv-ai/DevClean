from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from devclean.platform.windows.volumes import (
    DriveType,
    drive_type,
    has_reparse_ancestor,
    is_local_fixed_path,
)


def test_unc_is_always_remote() -> None:
    assert drive_type(Path(r"\\server\share\state")) is DriveType.REMOTE
    assert not is_local_fixed_path(Path(r"\\server\share\state"))


@pytest.mark.skipif(os.name != "nt", reason="Windows fixed-volume classification")
def test_temporary_directory_is_on_a_fixed_local_volume(tmp_path: Path) -> None:
    assert drive_type(tmp_path) is DriveType.FIXED
    assert not has_reparse_ancestor(tmp_path)
    assert is_local_fixed_path(tmp_path / "not-created-yet")


@pytest.mark.skipif(os.name != "nt", reason="Windows junction integration test")
def test_junction_ancestor_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "target"
    junction = tmp_path / "junction"
    target.mkdir()
    result = subprocess.run(
        ["cmd", "/d", "/c", "mklink", "/J", str(junction), str(target)],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        pytest.skip(f"junction creation unavailable: {result.stderr or result.stdout}")

    assert has_reparse_ancestor(junction / "state")
    assert not is_local_fixed_path(junction / "state")
