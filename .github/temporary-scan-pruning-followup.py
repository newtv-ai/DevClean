from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
path = ROOT / "tests/test_scan_pruning_boundaries.py"
text = path.read_text(encoding="utf-8")
text = text.replace("from pathlib import Path\n\nfrom devclean", "from pathlib import Path\n\nimport pytest\n\nfrom devclean")
text = text.replace(
    "from devclean.scanner.filesystem import ScanOptions, ScanRecordKind, scan_roots",
    "from devclean.scanner.filesystem import ScanOptions, scan_roots",
)
text = text.replace("tmp_path: Path, monkeypatch\n", "tmp_path: Path, monkeypatch: pytest.MonkeyPatch\n")
path.write_text(text, encoding="utf-8")
