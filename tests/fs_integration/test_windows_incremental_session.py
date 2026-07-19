from __future__ import annotations

import os
import time
from collections.abc import Iterable
from pathlib import Path

import pytest

from devclean.scanner import (
    IncrementalScanSession,
    ScanRecord,
    SessionScanMode,
    SessionScanStatus,
    scan_roots,
)


def _record_map(records: Iterable[ScanRecord]) -> dict[tuple[str, str], ScanRecord]:
    return {
        (os.path.normcase(os.path.abspath(record.path)), record.kind.value): record
        for record in records
    }


@pytest.mark.skipif(os.name != "nt", reason="ReadDirectoryChangesW Windows canary")
def test_windows_session_refresh_converges_to_independent_full_scan(tmp_path: Path) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    original = nested / "before.bin"
    original.write_bytes(b"before")
    session = IncrementalScanSession([tmp_path])
    baseline = session.baseline()
    if not baseline.stats.incremental_ready:
        session.close()
        pytest.skip(f"live directory monitoring unavailable: {baseline.fallbacks!r}")

    temporary = nested / "temporary.bin"
    temporary.write_bytes(b"temporary")
    original.write_bytes(b"modified payload")
    renamed = nested / "after.bin"
    original.rename(renamed)
    temporary.unlink()

    deadline = time.monotonic() + 5
    latest = baseline
    expected = _record_map(scan_roots([tmp_path]))
    while time.monotonic() < deadline:
        latest = session.refresh()
        if _record_map(latest.records) == expected and latest.stats.events_observed > 0:
            break
        time.sleep(0.05)
    session.close()

    assert latest.status is SessionScanStatus.COMMITTED
    assert latest.mode is SessionScanMode.INCREMENTAL
    assert latest.stats.events_observed > 0
    assert _record_map(latest.records) == expected

