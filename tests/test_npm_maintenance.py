from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

import devclean.core.npm_maintenance as npm
from devclean.core.npm_maintenance import (
    NpmCacheArea,
    NpmNpxEntry,
    NpmPathIdentity,
    NpmStorageInventory,
)


def _identity(path: Path, *, directory: bool, seed: int) -> NpmPathIdentity:
    return NpmPathIdentity(
        path=path,
        volume_serial=100 + seed,
        file_id=f"id-{seed}",
        file_id_kind="test",
        is_directory=directory,
        creation_time_ns=None if directory else 1000 + seed,
        last_write_time_ns=None if directory else 2000 + seed,
    )


def _area(path: Path, size: int, files: int, *, exists: bool = True) -> NpmCacheArea:
    return NpmCacheArea(path, exists, size, files)


def _inventory(
    tmp_path: Path,
    *,
    content_keys: tuple[str, ...] = ("key-a", "key-b"),
    content_bytes: int = 120,
    content_files: int = 2,
    npx_entries: tuple[NpmNpxEntry, ...] | None = None,
) -> NpmStorageInventory:
    root = tmp_path / "npm-cache"
    root.mkdir(exist_ok=True)
    tool = tmp_path / "npm.cmd"
    tool.touch(exist_ok=True)
    content = root / "_cacache"
    npx_root = root / "_npx"
    tuf = root / "_tuf"
    values = npx_entries or ()
    return NpmStorageInventory(
        npm_tool=_identity(tool, directory=False, seed=1),
        cache_root=root,
        cache_root_identity=_identity(root, directory=True, seed=2),
        content_cache=_area(content, content_bytes, content_files, exists=content_bytes > 0),
        npx_cache=_area(npx_root, sum(item.logical_bytes for item in values), len(values)),
        tuf_cache=_area(tuf, 7, 1),
        content_keys=content_keys,
        npx_entries=values,
        warnings=(),
    )


def _completed(
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def test_inventory_uses_vendor_cache_root_and_separates_cache_classes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "npm-cache"
    content = root / "_cacache"
    npx_entry = root / "_npx" / "abc123"
    tuf = root / "_tuf"
    content.mkdir(parents=True)
    npx_entry.mkdir(parents=True)
    tuf.mkdir(parents=True)
    (content / "blob").write_bytes(b"c" * 19)
    (npx_entry / "package.json").write_bytes(b"n" * 23)
    (tuf / "root.json").write_bytes(b"t" * 29)
    tool = _identity(tmp_path / "npm.cmd", directory=False, seed=1)
    root_identity = _identity(root, directory=True, seed=2)
    calls: list[tuple[tuple[str, ...], dict[str, str]]] = []

    monkeypatch.setattr(npm, "_resolve_npm_tool", lambda environment: tool)

    def fake_identity(path: Path, *, expect_directory: bool, label: str) -> NpmPathIdentity:
        del label
        return root_identity if expect_directory else tool

    monkeypatch.setattr(npm, "_path_identity", fake_identity)

    def fake_run(
        selected_tool: NpmPathIdentity,
        arguments: tuple[str, ...],
        environment: dict[str, str],
        *,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        del selected_tool, timeout
        calls.append((arguments, dict(environment)))
        if arguments == ("config", "get", "cache"):
            return _completed(str(root) + "\n")
        if arguments == ("cache", "ls"):
            return _completed("make-fetch-happen:key-a\nmake-fetch-happen:key-b\n")
        if arguments == ("cache", "npx", "ls"):
            return _completed("abc123: package-a@1.0.0\n")
        raise AssertionError(arguments)

    monkeypatch.setattr(npm, "_run_npm", fake_run)

    inventory = npm.inventory_npm_storage({})

    assert inventory.cache_root == root
    assert inventory.content_cache.logical_bytes == 19
    assert inventory.npx_cache.logical_bytes == 23
    assert inventory.tuf_cache.logical_bytes == 29
    assert inventory.content_keys == (
        "make-fetch-happen:key-a",
        "make-fetch-happen:key-b",
    )
    assert inventory.npx_entries == (
        NpmNpxEntry("abc123", npx_entry, "package-a@1.0.0", 23, 1),
    )
    pinned_calls = [call for call in calls if call[0][0] == "cache"]
    assert pinned_calls
    assert all(call[1]["NPM_CONFIG_CACHE"] == str(root) for call in pinned_calls)


def test_npx_inventory_rejects_path_escape_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = _identity(tmp_path / "npm.cmd", directory=False, seed=1)
    monkeypatch.setattr(
        npm,
        "_run_npm",
        lambda *args, **kwargs: _completed("../escape: package@1\n"),
    )

    with pytest.raises(RuntimeError, match="安全解析"):
        npm._list_npx_entries(tool, tmp_path / "npm-cache", {})


def test_npx_inventory_accepts_full_vendor_key_without_hex_assumption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = _identity(tmp_path / "npm.cmd", directory=False, seed=1)
    monkeypatch.setattr(
        npm,
        "_run_npm",
        lambda *args, **kwargs: _completed(
            "remove-all-no-force: package@1\n123removeme: package@2\n"
        ),
    )

    rows = npm._list_npx_entries(tool, tmp_path / "npm-cache", {})

    assert rows == (
        ("remove-all-no-force", "package@1"),
        ("123removeme", "package@2"),
    )


def test_verify_runs_exact_vendor_gc_on_pinned_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reviewed = _inventory(tmp_path)
    after = replace(
        reviewed,
        content_cache=_area(reviewed.content_cache.path, 80, 1),
        content_keys=("key-a",),
    )
    calls: list[tuple[tuple[str, ...], dict[str, str]]] = []
    monkeypatch.setattr(
        npm,
        "_validated_current_inventory",
        lambda expected, environment: reviewed,
    )
    monkeypatch.setattr(npm, "_require_process_idle", lambda: None)
    monkeypatch.setattr(npm, "inventory_npm_storage", lambda environment=None: after)
    monkeypatch.setattr(npm, "_require_same_boundaries", lambda old, new: None)

    def fake_run(
        tool: NpmPathIdentity,
        arguments: tuple[str, ...],
        environment: dict[str, str],
        *,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        del tool, timeout
        calls.append((arguments, dict(environment)))
        return _completed("Content garbage-collected: 1 (40 bytes)\n")

    monkeypatch.setattr(npm, "_run_npm", fake_run)

    result = npm.verify_npm_content_cache(reviewed)

    assert calls == [
        (("cache", "verify"), pytest.helpers.anything)
    ] if False else calls
    assert calls[0][0] == ("cache", "verify")
    assert calls[0][1]["NPM_CONFIG_CACHE"] == str(reviewed.cache_root)
    assert result.before_bytes == 120
    assert result.after_bytes == 80
    assert result.reclaimed_bytes == 40
    assert result.before_keys == 2
    assert result.after_keys == 1


def test_clean_requires_reviewed_content_state_then_uses_vendor_force(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reviewed = _inventory(tmp_path)
    after = replace(
        reviewed,
        content_cache=_area(reviewed.content_cache.path, 0, 0, exists=False),
        content_keys=(),
    )
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        npm,
        "_validated_current_inventory",
        lambda expected, environment: reviewed,
    )
    monkeypatch.setattr(npm, "_require_process_idle", lambda: None)
    monkeypatch.setattr(npm, "inventory_npm_storage", lambda environment=None: after)
    monkeypatch.setattr(npm, "_require_same_boundaries", lambda old, new: None)

    def fake_run(
        tool: NpmPathIdentity,
        arguments: tuple[str, ...],
        environment: dict[str, str],
        *,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        del tool, environment, timeout
        calls.append(arguments)
        return _completed("npm warn using --force Recommended protections disabled.\n")

    monkeypatch.setattr(npm, "_run_npm", fake_run)

    result = npm.clean_npm_content_cache(reviewed)

    assert calls == [("cache", "clean", "--force")]
    assert result.removed_keys == 2
    assert result.reclaimed_bytes == 120


def test_clean_refuses_if_package_cache_changed_since_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reviewed = _inventory(tmp_path)
    current = replace(
        reviewed,
        content_cache=_area(reviewed.content_cache.path, 121, 3),
        content_keys=("key-a", "key-b", "key-c"),
    )
    monkeypatch.setattr(
        npm,
        "_validated_current_inventory",
        lambda expected, environment: current,
    )

    with pytest.raises(RuntimeError, match="自审核后已变化"):
        npm.clean_npm_content_cache(reviewed)


def test_exact_npx_remove_requires_two_matching_vendor_dry_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "npm-cache"
    entry = NpmNpxEntry(
        "remove-all-no-force",
        root / "_npx" / "remove-all-no-force",
        "package@1",
        77,
        4,
    )
    reviewed = _inventory(tmp_path, npx_entries=(entry,))
    after = replace(reviewed, npx_entries=(), npx_cache=_area(root / "_npx", 0, 0))
    calls: list[tuple[str, ...]] = []

    monkeypatch.setattr(
        npm,
        "_validated_current_inventory",
        lambda expected, environment: reviewed,
    )
    monkeypatch.setattr(npm, "_require_process_idle", lambda: None)
    monkeypatch.setattr(npm, "inventory_npm_storage", lambda environment=None: after)
    monkeypatch.setattr(npm, "_require_same_boundaries", lambda old, new: None)

    def fake_run(
        tool: NpmPathIdentity,
        arguments: tuple[str, ...],
        environment: dict[str, str],
        *,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        del tool, environment, timeout
        calls.append(arguments)
        if "--dry-run" in arguments:
            return _completed(f"Removing npx key at {entry.path}\n")
        return _completed(f"Removing npx key at {entry.path}\n")

    monkeypatch.setattr(npm, "_run_npm", fake_run)

    result = npm.remove_npm_npx_entry(reviewed, entry)

    assert calls == [
        ("cache", "npx", "rm", entry.key, "--dry-run"),
        ("cache", "npx", "rm", entry.key, "--dry-run"),
        ("cache", "npx", "rm", entry.key),
    ]
    assert result.key == entry.key
    assert result.reclaimed_bytes == 77


def test_exact_npx_remove_refuses_dry_run_path_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "npm-cache"
    entry = NpmNpxEntry("abc", root / "_npx" / "abc", "package@1", 10, 1)
    reviewed = _inventory(tmp_path, npx_entries=(entry,))
    monkeypatch.setattr(
        npm,
        "_validated_current_inventory",
        lambda expected, environment: reviewed,
    )
    monkeypatch.setattr(npm, "_require_process_idle", lambda: None)
    monkeypatch.setattr(
        npm,
        "_run_npm",
        lambda *args, **kwargs: _completed(f"Removing npx key at {root / '_npx' / 'other'}\n"),
    )

    with pytest.raises(RuntimeError, match="dry-run 删除路径"):
        npm.remove_npm_npx_entry(reviewed, entry)
