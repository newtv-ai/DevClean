from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

import devclean.core.cypress_maintenance as cypress
from devclean.core.cypress_maintenance import (
    CypressBinaryCacheEntry,
    CypressPathIdentity,
    CypressStorageInventory,
)


def _identity(path: Path, *, directory: bool, seed: int) -> CypressPathIdentity:
    return CypressPathIdentity(
        path=path,
        volume_serial=100 + seed,
        file_id=f"id-{seed}",
        file_id_kind="test",
        is_directory=directory,
        creation_time_ns=None if directory else 1000 + seed,
        last_write_time_ns=None if directory else 2000 + seed,
    )


def _entry(
    root: Path,
    version: str,
    *,
    size: int,
    current: bool,
    seed: int,
) -> CypressBinaryCacheEntry:
    path = root / version
    return CypressBinaryCacheEntry(
        version=version,
        path=path,
        identity=_identity(path, directory=True, seed=seed),
        logical_bytes=size,
        file_count=2,
        current_package_version=current,
    )


def _inventory(tmp_path: Path) -> CypressStorageInventory:
    root = tmp_path / "Cypress" / "Cache"
    root.mkdir(parents=True, exist_ok=True)
    cli = tmp_path / "cypress.cmd"
    cli.touch(exist_ok=True)
    old = _entry(root, "14.5.0", size=80, current=False, seed=3)
    current = _entry(root, "15.0.0", size=120, current=True, seed=4)
    return CypressStorageInventory(
        cli_tool=_identity(cli, directory=False, seed=1),
        cache_root=root,
        cache_root_identity=_identity(root, directory=True, seed=2),
        package_version="15.0.0",
        versions=(old, current),
        external_entries=("bundles", "sessions"),
        unknown_entries=(),
    )


def _completed(
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def test_inventory_uses_exact_vendor_path_and_package_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "Cypress" / "Cache"
    old = root / "14.5.0"
    current = root / "15.0.0"
    bundles = root / "bundles"
    sessions = root / "sessions"
    for path in (old, current, bundles, sessions):
        path.mkdir(parents=True, exist_ok=True)
    (old / "old.bin").write_bytes(b"o" * 13)
    (current / "current.bin").write_bytes(b"c" * 17)

    cli = tmp_path / "cypress.cmd"
    cli.touch()
    tool = _identity(cli, directory=False, seed=1)
    root_identity = _identity(root, directory=True, seed=2)
    calls: list[tuple[tuple[str, ...], dict[str, str]]] = []
    monkeypatch.setattr(cypress, "_resolve_cypress_tool", lambda cli_path, environment: tool)

    def fake_identity(path: Path, *, expect_directory: bool, label: str) -> CypressPathIdentity:
        del label
        if path == cli:
            return tool
        if path == root:
            return root_identity
        seed = 3 if path.name == "14.5.0" else 4
        return _identity(path, directory=expect_directory, seed=seed)

    monkeypatch.setattr(cypress, "_path_identity", fake_identity)

    def fake_run(
        selected_tool: CypressPathIdentity,
        arguments: tuple[str, ...],
        environment: dict[str, str],
        *,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        del selected_tool, timeout
        calls.append((arguments, dict(environment)))
        if arguments == ("version", "--component", "package"):
            return _completed("15.0.0\n")
        if arguments == ("cache", "path"):
            return _completed(str(root) + "\n")
        raise AssertionError(arguments)

    monkeypatch.setattr(cypress, "_run_cypress", fake_run)

    inventory = cypress.inventory_cypress_storage(cli)

    assert inventory.package_version == "15.0.0"
    assert inventory.cache_root == root
    assert [entry.version for entry in inventory.versions] == ["14.5.0", "15.0.0"]
    assert inventory.versions[0].logical_bytes == 13
    assert inventory.versions[1].logical_bytes == 17
    assert inventory.prune_candidate_bytes == 13
    assert inventory.external_entries == ("bundles", "sessions")
    assert inventory.unknown_entries == ()
    assert inventory.prune_supported
    pinned = [env for args, env in calls if args == ("cache", "path")][-1]
    assert pinned["CYPRESS_CACHE_FOLDER"] == str(root)


def test_unknown_top_level_entry_disables_vendor_prune(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "Cypress" / "Cache"
    (root / "15.0.0").mkdir(parents=True)
    (root / "future-store").mkdir()
    cli = tmp_path / "cypress.cmd"
    cli.touch()
    tool = _identity(cli, directory=False, seed=1)
    root_identity = _identity(root, directory=True, seed=2)
    monkeypatch.setattr(cypress, "_resolve_cypress_tool", lambda cli_path, environment: tool)

    def fake_identity(path: Path, *, expect_directory: bool, label: str) -> CypressPathIdentity:
        del label
        if path == cli:
            return tool
        if path == root:
            return root_identity
        return _identity(path, directory=expect_directory, seed=3)

    monkeypatch.setattr(cypress, "_path_identity", fake_identity)
    monkeypatch.setattr(
        cypress,
        "_run_cypress",
        lambda tool, args, env, timeout: (
            _completed("15.0.0\n")
            if args == ("version", "--component", "package")
            else _completed(str(root) + "\n")
        ),
    )

    inventory = cypress.inventory_cypress_storage(cli)

    assert inventory.unknown_entries == ("future-store",)
    assert not inventory.prune_supported


def test_prune_runs_only_exact_vendor_command_after_two_reviews(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reviewed = _inventory(tmp_path)
    current_only = tuple(item for item in reviewed.versions if item.current_package_version)
    after = replace(reviewed, versions=current_only)
    inventories = iter((reviewed, reviewed, after))
    calls: list[tuple[str, ...]] = []

    monkeypatch.setattr(
        cypress,
        "inventory_cypress_storage",
        lambda cli_path=None, environment=None: next(inventories),
    )
    monkeypatch.setattr(cypress, "_require_process_idle", lambda: None)

    def fake_run(
        tool: CypressPathIdentity,
        arguments: tuple[str, ...],
        environment: dict[str, str],
        *,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        del tool, environment, timeout
        calls.append(arguments)
        return _completed("Deleted all binary caches except for the 15.0.0 binary cache.\n")

    monkeypatch.setattr(cypress, "_run_cypress", fake_run)

    result = cypress.prune_cypress_binary_cache(reviewed)

    assert calls == [("cache", "prune")]
    assert result.removed_versions == ("14.5.0",)
    assert result.before_binary_bytes == 200
    assert result.after_binary_bytes == 120
    assert result.logical_reclaimed_bytes == 80


def test_prune_refuses_review_change_before_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reviewed = _inventory(tmp_path)
    changed_old = replace(reviewed.versions[0], logical_bytes=81)
    changed = replace(reviewed, versions=(changed_old, reviewed.versions[1]))
    monkeypatch.setattr(
        cypress,
        "inventory_cypress_storage",
        lambda cli_path=None, environment=None: changed,
    )

    with pytest.raises(RuntimeError, match="自审核后发生变化"):
        cypress.prune_cypress_binary_cache(reviewed)


def test_prune_refuses_unknown_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reviewed = replace(_inventory(tmp_path), unknown_entries=("future-store",))
    monkeypatch.setattr(
        cypress,
        "inventory_cypress_storage",
        lambda cli_path=None, environment=None: reviewed,
    )

    with pytest.raises(RuntimeError, match="未知/不稳定"):
        cypress.prune_cypress_binary_cache(reviewed)


def test_prune_requires_old_version_absent_after_vendor_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reviewed = _inventory(tmp_path)
    inventories = iter((reviewed, reviewed, reviewed))
    monkeypatch.setattr(
        cypress,
        "inventory_cypress_storage",
        lambda cli_path=None, environment=None: next(inventories),
    )
    monkeypatch.setattr(cypress, "_require_process_idle", lambda: None)
    monkeypatch.setattr(cypress, "_run_cypress", lambda *args, **kwargs: _completed("done"))

    with pytest.raises(RuntimeError, match="仍存在"):
        cypress.prune_cypress_binary_cache(reviewed)


def test_cache_clear_is_deliberately_not_exposed() -> None:
    assert "clear_cypress_binary_cache" not in cypress.__all__


def test_semver_directory_gate_is_conservative() -> None:
    assert cypress._SEMVER_DIR_RE.fullmatch("15.0.0")
    assert cypress._SEMVER_DIR_RE.fullmatch("15.0.0-beta.1")
    assert not cypress._SEMVER_DIR_RE.fullmatch("beta-15.0.0-main-deadbeef")
    assert not cypress._SEMVER_DIR_RE.fullmatch("future-store")
