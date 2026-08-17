from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import devclean.core.huggingface_maintenance as hf_maintenance
from devclean.core.huggingface_maintenance import (
    HuggingFaceCacheKind,
    inventory_huggingface_storage,
    prune_huggingface_hub_cache,
)


def _layout(tmp_path: Path) -> tuple[dict[str, str], Path, Path, Path]:
    hub = tmp_path / "hub"
    xet = tmp_path / "xet"
    assets = tmp_path / "assets"
    for root in (hub, xet, assets):
        root.mkdir()
    (hub / "blob.bin").write_bytes(b"h" * 19)
    (xet / "chunk.bin").write_bytes(b"x" * 23)
    (assets / "asset.bin").write_bytes(b"a" * 29)
    env = {
        "USERPROFILE": str(tmp_path / "home"),
        "HF_HUB_CACHE": str(hub),
        "HF_XET_CACHE": str(xet),
        "HF_ASSETS_CACHE": str(assets),
        "DEVCLEAN_HF_EXE": "C:/Tools/hf.exe",
    }
    return env, hub, xet, assets


def test_huggingface_inventory_is_read_only(tmp_path: Path) -> None:
    env, hub, xet, assets = _layout(tmp_path)

    inventory = inventory_huggingface_storage(env)

    by_kind = {item.kind: item for item in inventory.caches}
    assert by_kind[HuggingFaceCacheKind.HUB].path == hub
    assert by_kind[HuggingFaceCacheKind.HUB].logical_bytes == 19
    assert by_kind[HuggingFaceCacheKind.XET].path == xet
    assert by_kind[HuggingFaceCacheKind.XET].logical_bytes == 23
    assert by_kind[HuggingFaceCacheKind.ASSETS].path == assets
    assert by_kind[HuggingFaceCacheKind.ASSETS].logical_bytes == 29
    assert inventory.total_cache_bytes == 71
    assert (hub / "blob.bin").exists()
    assert (xet / "chunk.bin").exists()
    assert (assets / "asset.bin").exists()


def test_huggingface_prune_rejects_arbitrary_directory(tmp_path: Path) -> None:
    env, _, _, _ = _layout(tmp_path)
    arbitrary = tmp_path / "other"
    arbitrary.mkdir()

    with pytest.raises(ValueError, match="已审计"):
        prune_huggingface_hub_cache(arbitrary, env)


def test_huggingface_prune_requires_existing_hub_root(tmp_path: Path) -> None:
    env, hub, _, _ = _layout(tmp_path)
    (hub / "blob.bin").unlink()
    hub.rmdir()

    with pytest.raises(FileNotFoundError, match="不存在"):
        prune_huggingface_hub_cache(hub, env)


def test_huggingface_prune_blocks_while_related_process_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, hub, _, _ = _layout(tmp_path)
    monkeypatch.setattr(hf_maintenance, "huggingface_process_running", lambda: True)

    with pytest.raises(RuntimeError, match="正在运行"):
        prune_huggingface_hub_cache(hub, env)


def test_huggingface_prune_scopes_vendor_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, hub, _, _ = _layout(tmp_path)
    monkeypatch.setattr(hf_maintenance, "huggingface_process_running", lambda: False)
    calls: list[tuple[list[str], dict[str, str]]] = []

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        run_env = kwargs["env"]
        assert isinstance(run_env, dict)
        calls.append((command, run_env))
        (hub / "blob.bin").unlink()
        return SimpleNamespace(returncode=0, stdout="freed 19B", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = prune_huggingface_hub_cache(hub, env)

    assert calls[0][0] == [
        "C:/Tools/hf.exe",
        "cache",
        "prune",
        "--cache-dir",
        str(hub),
        "--yes",
    ]
    assert calls[0][1]["HF_HUB_CACHE"] == str(hub)
    assert result.before_bytes == 19
    assert result.after_bytes == 0
    assert result.reclaimed_bytes == 19
    assert result.stdout == "freed 19B"


def test_huggingface_prune_surfaces_vendor_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, hub, _, _ = _layout(tmp_path)
    monkeypatch.setattr(hf_maintenance, "huggingface_process_running", lambda: False)

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        del command, kwargs
        return SimpleNamespace(returncode=2, stdout="", stderr="cache locked")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="cache locked"):
        prune_huggingface_hub_cache(hub, env)
