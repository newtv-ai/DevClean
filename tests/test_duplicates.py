from __future__ import annotations

import os
from pathlib import Path

import pytest

from devclean.core.duplicates import find_large_duplicates


@pytest.mark.skipif(os.name != "nt", reason="Windows file-ID duplicate integration test")
def test_large_duplicate_scan_hashes_exact_matching_regular_files(tmp_path: Path) -> None:
    payload = b"DevClean-duplicate-canary" * 64
    first = tmp_path / "first.bin"
    second = tmp_path / "second.bin"
    other = tmp_path / "other.bin"
    first.write_bytes(payload)
    second.write_bytes(payload)
    other.write_bytes(b"different" * 256)

    result = find_large_duplicates((tmp_path,), minimum_size=1)

    assert result.files_hashed >= 2
    assert len(result.groups) == 1
    assert {Path(record.path).name for record in result.groups[0].records} == {
        "first.bin",
        "second.bin",
    }
    assert result.groups[0].reclaimable_logical_bytes == len(payload)


@pytest.mark.skipif(os.name != "nt", reason="Windows file-ID duplicate integration test")
def test_duplicate_scan_excludes_protected_paths(tmp_path: Path) -> None:
    protected = tmp_path / ".git"
    protected.mkdir()
    payload = b"DevClean-protected-canary" * 64
    (protected / "first.bin").write_bytes(payload)
    (protected / "second.bin").write_bytes(payload)

    result = find_large_duplicates((tmp_path,), minimum_size=1)

    assert result.groups == ()
