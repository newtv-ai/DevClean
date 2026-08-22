from __future__ import annotations

from pathlib import Path

from devclean.scanner.tree_summary import summarize_tree


def test_tree_summary_counts_nested_files_without_materializing_records(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    nested = root / "nested"
    nested.mkdir(parents=True)
    (root / "a.bin").write_bytes(b"a" * 17)
    (nested / "b.bin").write_bytes(b"b" * 31)

    summary = summarize_tree(root)

    assert summary.files == 2
    assert summary.logical_bytes == 48
    assert summary.latest_activity_time_ns > 0
