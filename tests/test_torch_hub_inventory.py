from __future__ import annotations

import os
from pathlib import Path

import pytest

import devclean.core.torch_hub_inventory as torch_hub
from devclean.core.torch_hub_inventory import (
    TorchHubDecision,
    TorchHubEntryKind,
    default_torch_hub_root,
    inventory_torch_hub,
)


def test_default_torch_hub_root_matches_vendor_environment_precedence() -> None:
    env = {
        "USERPROFILE": "C:/Users/tester",
        "XDG_CACHE_HOME": "D:/xdg",
        "TORCH_HOME": "E:/torch-home",
    }
    candidate = default_torch_hub_root(env)
    assert candidate.path == Path("E:/torch-home/hub")
    assert candidate.source == "TORCH_HOME"

    candidate = default_torch_hub_root(
        {"USERPROFILE": "C:/Users/tester", "XDG_CACHE_HOME": "D:/xdg"}
    )
    assert candidate.path == Path("D:/xdg/torch/hub")
    assert candidate.source == "XDG_CACHE_HOME"

    candidate = default_torch_hub_root({"USERPROFILE": "C:/Users/tester"})
    assert candidate.path == Path("C:/Users/tester/.cache/torch/hub")
    assert candidate.source == "default"


def test_devclean_explicit_root_is_only_a_read_only_candidate() -> None:
    candidate = default_torch_hub_root(
        {
            "USERPROFILE": "C:/Users/tester",
            "TORCH_HOME": "E:/torch-home",
            "DEVCLEAN_TORCH_HUB_ROOT": "F:/selected-hub",
        }
    )
    assert candidate.path == Path("F:/selected-hub")
    assert candidate.source == "DEVCLEAN_TORCH_HUB_ROOT"


def test_inventory_separates_trust_state_checkpoints_and_unknown_directories(
    tmp_path: Path,
) -> None:
    root = tmp_path / "hub"
    root.mkdir()
    (root / "trusted_list").write_text("pytorch_vision\n", encoding="utf-8")
    checkpoints = root / "checkpoints"
    checkpoints.mkdir()
    (checkpoints / "model.pth").write_bytes(b"m" * 23)
    repo_like = root / "owner_repo_branch_with_slash"
    repo_like.mkdir()
    (repo_like / "hubconf.py").write_text("def model(): pass\n", encoding="utf-8")
    (root / "main.zip").write_bytes(b"z" * 11)

    inventory = inventory_torch_hub(root)

    by_name = {item.name: item for item in inventory.entries}
    assert by_name["trusted_list"].kind is TorchHubEntryKind.TRUST_STATE
    assert by_name["trusted_list"].decision is TorchHubDecision.KEEP_PROTECTED
    assert by_name["checkpoints"].kind is TorchHubEntryKind.CHECKPOINTS
    assert by_name["checkpoints"].decision is TorchHubDecision.REPORT_ONLY
    assert by_name["checkpoints"].logical_bytes == 23
    assert by_name["owner_repo_branch_with_slash"].kind is TorchHubEntryKind.REPOSITORY_OR_UNKNOWN
    assert by_name["owner_repo_branch_with_slash"].decision is TorchHubDecision.REPORT_ONLY
    assert "不可可靠反解" in by_name["owner_repo_branch_with_slash"].reason
    assert by_name["main.zip"].kind is TorchHubEntryKind.DOWNLOAD_TEMP_OR_UNKNOWN
    assert by_name["main.zip"].decision is TorchHubDecision.REPORT_ONLY


def test_inventory_missing_root_is_report_only_context(tmp_path: Path) -> None:
    inventory = inventory_torch_hub(tmp_path / "missing")
    assert not inventory.exists
    assert not inventory.scannable
    assert inventory.entries == ()
    assert "不存在" in inventory.warnings[0]


def test_inventory_requires_absolute_root() -> None:
    with pytest.raises(ValueError, match="绝对路径"):
        inventory_torch_hub("relative/hub")


def test_inventory_skips_reparse_like_children(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "hub"
    root.mkdir()
    checkpoints = root / "checkpoints"
    checkpoints.mkdir()
    safe_file = checkpoints / "safe.bin"
    safe_file.write_bytes(b"s" * 7)
    skipped = checkpoints / "skip-me"
    skipped.mkdir()
    (skipped / "huge.bin").write_bytes(b"x" * 101)

    real_stat = os.stat

    class _FakeStat:
        def __init__(self, result: os.stat_result, attributes: int) -> None:
            self._result = result
            self.st_mode = result.st_mode
            self.st_size = result.st_size
            self.st_file_attributes = attributes

        def __getattr__(self, name: str) -> object:
            return getattr(self._result, name)

    def fake_stat(
        path: str | os.PathLike[str],
        *,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        result = real_stat(path, follow_symlinks=follow_symlinks)
        if Path(path) == skipped and not follow_symlinks:
            return _FakeStat(
                result,
                torch_hub.FILE_ATTRIBUTE_REPARSE_POINT,
            )  # type: ignore[return-value]
        return result

    monkeypatch.setattr(torch_hub.os, "stat", fake_stat)

    inventory = inventory_torch_hub(root)

    checkpoints_entry = next(item for item in inventory.entries if item.name == "checkpoints")
    assert checkpoints_entry.logical_bytes == 7
    assert checkpoints_entry.file_count == 1
    assert any("skip-me" in warning for warning in inventory.warnings)


def test_runtime_set_dir_ambiguity_is_explicit() -> None:
    candidate = default_torch_hub_root({"USERPROFILE": "C:/Users/tester"})
    assert "set_dir" in candidate.note
