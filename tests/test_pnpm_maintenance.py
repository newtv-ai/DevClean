from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from devclean.core.pnpm_maintenance import inventory_pnpm_storage, prune_pnpm_store


def test_inventory_collapses_versioned_store_path_to_store_root(
    tmp_path: Path,
) -> None:
    home = tmp_path / "pnpm-home"
    store = home / "store"
    version = store / "v10"
    version.mkdir(parents=True)
    (version / "blob").write_bytes(b"x" * 300)
    env = {
        "USERPROFILE": str(tmp_path / "profile"),
        "APPDATA": str(tmp_path / "roaming"),
        "LOCALAPPDATA": str(tmp_path / "local"),
        "TEMP": str(tmp_path / "temp"),
        "PNPM_HOME": str(home),
        "PNPM_CONFIG_STORE_DIR": str(store),
    }

    inventory = inventory_pnpm_storage(env)
    visible = [entry for entry in inventory.stores if entry.exists]
    assert len(visible) == 1
    assert visible[0].path == store
    assert visible[0].logical_bytes == 300


def test_prune_uses_pnpm_store_prune_with_selected_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = tmp_path / "store"
    version = store / "v10"
    version.mkdir(parents=True)
    blob = version / "orphan"
    blob.write_bytes(b"x" * 500)
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        "devclean.core.pnpm_maintenance.pnpm_process_running",
        lambda: False,
    )

    def fake_run(args: list[str], **kwargs: object) -> SimpleNamespace:
        captured["args"] = args
        captured["env"] = kwargs["env"]
        blob.unlink()
        return SimpleNamespace(returncode=0, stdout="Removed 1 file\n", stderr="")

    monkeypatch.setattr("devclean.core.pnpm_maintenance.subprocess.run", fake_run)
    result = prune_pnpm_store(version)

    assert captured["args"][-2:] == ["store", "prune"]
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["PNPM_CONFIG_STORE_DIR"] == str(store)
    assert result.store_path == store
    assert result.before_bytes == 500
    assert result.after_bytes == 0
    assert result.reclaimed_bytes == 500


def test_prune_refuses_store_mutation_while_pnpm_is_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = tmp_path / "store"
    store.mkdir()
    monkeypatch.setattr(
        "devclean.core.pnpm_maintenance.pnpm_process_running",
        lambda: True,
    )
    with pytest.raises(RuntimeError, match="pnpm 正在运行"):
        prune_pnpm_store(store)
