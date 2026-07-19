from __future__ import annotations

from pathlib import Path

from devclean.core import paths


def test_data_dir_prefers_explicit_override(
    tmp_path: Path, monkeypatch
) -> None:
    configured = tmp_path / "configured" / ".." / "state-root"
    monkeypatch.setenv("DEVCLEAN_DATA_DIR", str(configured))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "ignored"))

    assert paths.data_dir() == configured.resolve()
    assert paths.state_path() == configured.resolve() / "state" / "DevClean.db"
    assert paths.reports_dir() == configured.resolve() / "reports"


def test_data_dir_uses_local_appdata_without_override(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("DEVCLEAN_DATA_DIR", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))

    assert paths.data_dir() == tmp_path / "local" / "DevClean"


def test_data_dir_has_owner_home_fallback(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("DEVCLEAN_DATA_DIR", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(paths.Path, "home", classmethod(lambda cls: tmp_path))

    assert paths.data_dir() == tmp_path / ".local" / "share" / "DevClean"
