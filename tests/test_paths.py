from __future__ import annotations

from pathlib import Path

import pytest

from devclean.core import paths


def test_source_run_data_stays_in_visible_working_folder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DEVCLEAN_DATA_DIR", raising=False)
    project_root = Path(paths.__file__).resolve().parents[3]

    assert paths.data_dir() == project_root / "DevClean-data"
