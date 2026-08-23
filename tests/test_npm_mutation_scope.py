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

_STABLE_KEYS = (
    "cache",
    "prefix",
    "userconfig",
    "globalconfig",
    "cafile",
    "init-module",
    "init.module",
    "node-gyp",
    "logs-dir",
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


def _default_paths(tmp_path: Path) -> dict[str, Path | None]:
    return {
        "prefix": tmp_path / "global-prefix",
        "userconfig": tmp_path / ".npmrc",
        "globalconfig": tmp_path / "global.npmrc",
        "cafile": tmp_path / "ca.pem",
        "init-module": tmp_path / "npm-init.js",
        "init.module": tmp_path / "legacy-npm-init.js",
        "node-gyp": tmp_path / "node-gyp.js",
        "logs-dir": tmp_path / "npm-logs",
        "global-ignore-file": tmp_path / "global.npmignore",
    }


def _output(
    inventory: NpmStorageInventory,
    paths: dict[str, Path | None],
    *,
    cache: Path | None = None,
    include_global_ignore: bool,
    omit: frozenset[str] = frozenset(),
) -> str:
    values: list[tuple[str, str]] = [
        ("cache", str(cache or inventory.cache_root)),
    ]
    for key in _STABLE_KEYS[1:]:
        if key in omit:
            continue
        value = paths[key]
        values.append((key, "null" if value is None else str(value)))
    if include_global_ignore and "global-ignore-file" not in omit:
        value = paths["global-ignore-file"]
        values.append(("global-ignore-file", "null" if value is None else str(value)))
    return "\n".join(f"{key} = {value}" for key, value in values)


def _mock_npm(
    monkeypatch: pytest.MonkeyPatch,
    *,
    major: int,
    config_stdout: str,
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
        if arguments == ("--version",):
            return subprocess.CompletedProcess([], 0, f"{major}.6.2\n", "")
        assert arguments[:2] == ("config", "get")
        return subprocess.CompletedProcess([], 0, config_stdout, "")

    monkeypatch.setattr(npm, "_run_npm", fake_run)
    return calls


@pytest.mark.skipif(os.name != "nt", reason="npm maintenance targets Windows paths")
def test_scope_guard_keeps_npm11_compatible_and_protects_stable_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory = _inventory(tmp_path)
    paths = _default_paths(tmp_path)
    calls = _mock_npm(
        monkeypatch,
        major=11,
        config_stdout=_output(
            inventory,
            paths,
            include_global_ignore=False,
        ),
    )

    npm._require_no_protected_overlap(inventory, inventory.content_cache.path, {})

    assert len(calls) == 2
    version_args, version_env = calls[0]
    config_args, config_env = calls[1]
    assert version_args == ("--version",)
    assert config_args == ("config", "get", *_STABLE_KEYS)
    assert "global-ignore-file" not in config_args
    for environment in (version_env, config_env):
        assert environment["NPM_CONFIG_CACHE"] == str(inventory.cache_root)
        assert environment["NPM_CONFIG_UPDATE_NOTIFIER"] == "false"


@pytest.mark.skipif(os.name != "nt", reason="npm maintenance targets Windows paths")
def test_scope_guard_queries_v12_global_ignore_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory = _inventory(tmp_path)
    paths = _default_paths(tmp_path)
    calls = _mock_npm(
        monkeypatch,
        major=12,
        config_stdout=_output(
            inventory,
            paths,
            include_global_ignore=True,
        ),
    )

    npm._require_no_protected_overlap(inventory, inventory.content_cache.path, {})

    assert calls[1][0] == (
        "config",
        "get",
        *_STABLE_KEYS,
        "global-ignore-file",
    )


@pytest.mark.skipif(os.name != "nt", reason="npm maintenance targets Windows paths")
@pytest.mark.parametrize(
    "protected_kind",
    [
        "prefix",
        "userconfig",
        "globalconfig",
        "cafile",
        "init-module",
        "init.module",
        "node-gyp",
        "logs-dir",
    ],
)
def test_scope_guard_refuses_stable_persistent_paths_inside_content_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    protected_kind: str,
) -> None:
    inventory = _inventory(tmp_path)
    paths = _default_paths(tmp_path)
    paths[protected_kind] = inventory.content_cache.path / "persistent"
    _mock_npm(
        monkeypatch,
        major=11,
        config_stdout=_output(
            inventory,
            paths,
            include_global_ignore=False,
        ),
    )

    with pytest.raises(RuntimeError, match="vendor mutation 范围内"):
        npm._require_no_protected_overlap(inventory, inventory.content_cache.path, {})


@pytest.mark.skipif(os.name != "nt", reason="npm maintenance targets Windows paths")
def test_scope_guard_refuses_v12_global_ignore_file_inside_content_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory = _inventory(tmp_path)
    paths = _default_paths(tmp_path)
    paths["global-ignore-file"] = inventory.content_cache.path / "global.npmignore"
    _mock_npm(
        monkeypatch,
        major=12,
        config_stdout=_output(
            inventory,
            paths,
            include_global_ignore=True,
        ),
    )

    with pytest.raises(RuntimeError, match="global ignore file"):
        npm._require_no_protected_overlap(inventory, inventory.content_cache.path, {})


@pytest.mark.skipif(os.name != "nt", reason="npm maintenance targets Windows paths")
def test_scope_guard_applies_to_exact_npx_entry_range(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory = _inventory(tmp_path)
    entry = inventory.npx_cache.path / "entry-key"
    paths = _default_paths(tmp_path)
    paths["logs-dir"] = entry / "diagnostics"
    _mock_npm(
        monkeypatch,
        major=11,
        config_stdout=_output(
            inventory,
            paths,
            include_global_ignore=False,
        ),
    )

    with pytest.raises(RuntimeError, match="logs-dir"):
        npm._require_no_protected_overlap(inventory, entry, {})


@pytest.mark.skipif(os.name != "nt", reason="npm maintenance targets Windows paths")
def test_scope_guard_allows_optional_boundary_paths_to_be_null(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory = _inventory(tmp_path)
    paths = _default_paths(tmp_path)
    for key in ("cafile", "init-module", "init.module", "node-gyp", "logs-dir"):
        paths[key] = None
    _mock_npm(
        monkeypatch,
        major=11,
        config_stdout=_output(
            inventory,
            paths,
            include_global_ignore=False,
        ),
    )

    npm._require_no_protected_overlap(inventory, inventory.content_cache.path, {})


@pytest.mark.skipif(os.name != "nt", reason="npm maintenance targets Windows paths")
def test_scope_guard_refuses_incomplete_vendor_config_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory = _inventory(tmp_path)
    paths = _default_paths(tmp_path)
    _mock_npm(
        monkeypatch,
        major=11,
        config_stdout=_output(
            inventory,
            paths,
            include_global_ignore=False,
            omit=frozenset({"globalconfig"}),
        ),
    )

    with pytest.raises(RuntimeError, match="globalconfig"):
        npm._require_no_protected_overlap(inventory, inventory.content_cache.path, {})


@pytest.mark.skipif(os.name != "nt", reason="npm maintenance targets Windows paths")
def test_scope_guard_refuses_cache_retarget_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory = _inventory(tmp_path)
    paths = _default_paths(tmp_path)
    _mock_npm(
        monkeypatch,
        major=11,
        config_stdout=_output(
            inventory,
            paths,
            cache=tmp_path / "other-cache",
            include_global_ignore=False,
        ),
    )

    with pytest.raises(RuntimeError, match="未再次确认固定"):
        npm._require_no_protected_overlap(inventory, inventory.content_cache.path, {})


@pytest.mark.skipif(os.name != "nt", reason="npm maintenance targets Windows paths")
@pytest.mark.parametrize("version_output", ["", "not-a-version\n", "12.0.2\nextra\n"])
def test_scope_guard_fails_closed_when_npm_major_cannot_be_proven(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    version_output: str,
) -> None:
    inventory = _inventory(tmp_path)

    def fake_run(
        tool: NpmPathIdentity,
        arguments: tuple[str, ...],
        environment: dict[str, str],
        *,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        del tool, environment
        assert arguments == ("--version",)
        assert timeout == 30
        return subprocess.CompletedProcess([], 0, version_output, "")

    monkeypatch.setattr(npm, "_run_npm", fake_run)

    with pytest.raises(RuntimeError, match=r"npm --version|npm major"):
        npm._require_no_protected_overlap(inventory, inventory.content_cache.path, {})
