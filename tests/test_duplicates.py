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
def test_duplicate_scan_covers_ordinary_user_directories(tmp_path: Path) -> None:
    """Duplicate analysis is read-only reporting, so it should not skip content.

    It used to exclude everything a path-shape deny-list matched, which meant
    duplicated photos and repository payloads -- some of the most useful things
    to report on a full disk -- were silently left out of the report.
    """

    directory = tmp_path / "Pictures"
    directory.mkdir()
    payload = b"DevClean-duplicate-canary" * 64
    (directory / "first.bin").write_bytes(payload)
    (directory / "second.bin").write_bytes(payload)

    result = find_large_duplicates((tmp_path,), minimum_size=1)

    assert len(result.groups) == 1
    assert len(result.groups[0].records) == 2


def test_duplicate_scan_skips_the_tools_own_quarantine(tmp_path: Path) -> None:
    """A quarantined object is a copy of its source by construction."""

    staging = tmp_path / ".DevClean-quarantine-v1-batch_abc123"
    staging.mkdir()
    payload = b"DevClean-quarantine-canary" * 64
    (staging / "first.bin").write_bytes(payload)
    (staging / "second.bin").write_bytes(payload)

    result = find_large_duplicates((tmp_path,), minimum_size=1)

    assert result.groups == ()
