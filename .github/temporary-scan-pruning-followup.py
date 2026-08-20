from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Repair generated test typing/case assumptions.
test_path = ROOT / "tests/test_scan_pruning_boundaries.py"
text = test_path.read_text(encoding="utf-8")
text = text.replace(
    "from pathlib import Path\n\nfrom devclean",
    "from pathlib import Path\n\nimport pytest\n\nfrom devclean",
)
text = text.replace(
    "from devclean.core.user_rules import UserRules, default_rules",
    "from devclean.core.user_rules import UserRules, default_rules, normalise_path",
)
text = text.replace(
    "from devclean.scanner.filesystem import ScanOptions, ScanRecordKind, scan_roots",
    "from devclean.scanner.filesystem import ScanOptions, scan_roots",
)
text = text.replace(
    "tmp_path: Path, monkeypatch\n",
    "tmp_path: Path, monkeypatch: pytest.MonkeyPatch\n",
)
text = text.replace(
    'assert "Documents" in rules.scan.skip_directory_names',
    'assert "documents" in rules.scan.skip_directory_names',
)
text = text.replace("app.normalise_path(explicit)", "normalise_path(explicit)")
text = text.replace("app.normalise_path(unrelated)", "normalise_path(unrelated)")
test_path.write_text(text, encoding="utf-8")

# Keep the generated app change Ruff-clean before the final gate.
app_path = ROOT / "src/devclean/ui/app.py"
app_text = app_path.read_text(encoding="utf-8")
app_text = app_text.replace(
    "        (normalized for path in _scan_specific_roots(known_roots, active_rules) if (normalized := usable(path)) is not None),\n",
    "        (\n            normalized\n            for path in _scan_specific_roots(known_roots, active_rules)\n            if (normalized := usable(path)) is not None\n        ),\n",
)
app_path.write_text(app_text, encoding="utf-8")
