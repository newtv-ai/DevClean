from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import devclean.core.npm_maintenance as npm
from devclean.core.npm_maintenance import (
    NpmCacheArea,
    NpmPathIdentity,
    NpmStorageInventory,
)


def _inventory(tmp_path: Path) -> NpmStorageInventory:
    root = tmp_path / "npm-cache"
    root.mkdir()
    return NpmStorageInventory(
        npm_tool=NpmPathIdentity(
            tmp_path / "npm.cmd",
            1,
            "tool",
            "test",
            False,
            1,
            2,
        ),
        cache_root=root,
        cache_root_identity=NpmPathIdentity(root, 1, "root", "test", True),
        content_cache=NpmCacheArea(root / "_cacache", True, 100, 3),
        npx_cache=NpmCacheArea(root / "_npx", True, 80, 2),
        tuf_cache=NpmCacheArea(root / "_tuf", True, 20, 1),
        content_keys=("a",),
        npx_entries=(),
        warnings=(),
    )


def _output(
    inventory: NpmStorageInventory,
    *,
    prefix: Path,
    userconfig: Path,
    logs_dir: Path | None,
    cache: Path | None = None,
) -> str:
    return "\n".join(
        (
            f"cache = {cache or inventory.cache_root}",
            f"prefix = {prefix}",
            f"userconfig = {userconfig}",
            f"logs-dir = {logs_dir if logs_dir is not None else 'null'}",
        )
    )


def _mock_config(
    monkeypatch: pytest.MonkeyPatch,
    stdout: str,
) -> list[tuple[tuple[str, ...], dict[str, str]]]:
    calls: list[tuple[tuple[str, ...], dict[str, str]]] = []

    def fake_run(
        tool: NpmPathIdentity,
        arguments: tuple[str, ...],
        environment: dict[str, str],
        *,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        del tool
        calls.append((arguments, dict(environment)))
        assert timeout == 30
        return subprocess.CompletedProcess([], 0, stdout, "")

    monkeypatch.setattr(npm, "_run_npm", fake_run)
    return calls


@pytest.mark.skipif(os.name != "nt", reason="npm maintenance targets Windows paths")
def test_scope_guard_allows_persistent_paths_outside_content_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory = _inventory(tmp_path)
    prefix = tmp_path / "global-prefix"
    userconfig = tmp_path / ".npmrc"
    logs = tmp_path / "npm-logs"
    calls = _mock_config(
        monkeypatch,
        _output(
            inventory,
            prefix=prefix,
            userconfig=userconfig,
            logs_dir=logs,
        ),
    )

    npm._require_no_protected_overlap(inventory, inventory.content_cache.path, {})

    assert calls == [
        (
            ("config", "get", "cache", "prefix", "userconfig", "logs-dir"),
            {
                "NPM_CONFIG_CACHE": str(inventory.cache_root),
                "NPM_CONFIG_UPDATE_NOTIFIER": "false",
            },
        )
    ]


@pytest.mark.skipif(os.name != "nt", reason="npm maintenance targets Windows paths")
@pytest.mark.parametrize("protected_kind", ["prefix", "userconfig", "logs-dir"])
def test_scope_guard_refuses_redirected_persistent_state_inside_content_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    protected_kind: str,
) -> None:
    inventory = _inventory(tmp_path)
    outside_prefix = tmp_path / "global-prefix"
    outside_userconfig = tmp_path / ".npmrc"
    outside_logs = tmp_path / "logs"
    inside = inventory.content_cache.path / "persistent"
    values = {
        "prefix": outside_prefix,
        "userconfig": outside_userconfig,
        "logs-dir": outside_logs,
    }
    values[protected_kind] = inside
    _mock_config(
        monkeypatch,
        _output(
            inventory,
            prefix=values["prefix"],
            userconfig=values["userconfig"],
            logs_dir=values["logs-dir"],
        ),
    )

    with pytest.raises(RuntimeError, match="vendor mutation 范围内"):
        npm._require_no_protected_overlap(inventory, inventory.content_cache.path, {})


@pytest.mark.skipif(os.name != "nt", reason="npm maintenance targets Windows paths")
def test_scope_guard_applies_to_exact_npx_entry_range(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory = _inventory(tmp_path)
    entry = inventory.npx_cache.path / "entry-key"
    _mock_config(
        monkeypatch,
        _output(
            inventory,
            prefix=tmp_path / "prefix",
            userconfig=tmp_path / ".npmrc",
            logs_dir=entry / "diagnostics",
        ),
    )

    with pytest.raises(RuntimeError, match="logs-dir"):
        npm._require_no_protected_overlap(inventory, entry, {})


@pytest.mark.skipif(os.name != "nt", reason="npm maintenance targets Windows paths")
def test_scope_guard_refuses_incomplete_vendor_config_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory = _inventory(tmp_path)
    _mock_config(
        monkeypatch,
        "\n".join(
            (
                f"cache = {inventory.cache_root}",
                f"prefix = {tmp_path / 'prefix'}",
                "logs-dir = null",
            )
        ),
    )

    with pytest.raises(RuntimeError, match="未完整返回"):
        npm._require_no_protected_overlap(inventory, inventory.content_cache.path, {})


@pytest.mark.skipif(os.name != "nt", reason="npm maintenance targets Windows paths")
def test_scope_guard_refuses_cache_retarget_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory = _inventory(tmp_path)
    _mock_config(
        monkeypatch,
        _output(
            inventory,
            prefix=tmp_path / "prefix",
            userconfig=tmp_path / ".npmrc",
            logs_dir=None,
            cache=tmp_path / "other-cache",
        ),
    )

    with pytest.raises(RuntimeError, match="未再次确认固定"):
        npm._require_no_protected_overlap(inventory, inventory.content_cache.path, {})
