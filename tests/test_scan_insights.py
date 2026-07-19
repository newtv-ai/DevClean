from __future__ import annotations

from pathlib import Path

from devclean.core.cleanup_catalog import CleanupCategory
from devclean.core.triage import TriageItem, TriageSession, triage_file
from devclean.scanner import ScanRecord, ScanRecordKind


def _item(path: Path, *, size: int, category_root: Path) -> TriageItem:
    return triage_file(
        ScanRecord(
            root=str(category_root),
            path=str(path),
            kind=ScanRecordKind.FILE,
            depth=2,
            logical_size=size,
            allocated_size=size,
            raw_allocated_size=size,
            last_write_time_ns=0,
        ),
        temp_root=category_root,
    )


def test_insights_keep_exact_category_totals_and_top_level_directories(tmp_path: Path) -> None:
    root = tmp_path / "scan-root"
    session = TriageSession()
    session.add(_item(root / "models" / "first.bin", size=30, category_root=root))
    session.add(_item(root / "models" / "second.bin", size=20, category_root=root))
    session.add(_item(root / "logs" / "run.log", size=10, category_root=root))

    temp = session.insights.category_summary(CleanupCategory.USER_TEMP)
    top = session.insights.top_directories()

    assert temp.files == 3
    assert temp.allocated_bytes == 60
    assert [(Path(item.path).name, item.summary.allocated_bytes) for item in top] == [
        ("models", 50),
        ("logs", 10),
    ]
