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


def _inventory(
    tmp_path: Path,
    *,
    npx_entry: NpmNpxEntry | None = None,
) -> NpmStorageInventory:
    root = tmp_path / "npm-cache"
    root.mkdir(exist_ok=True)
    entries = () if npx_entry is None else (npx_entry,)
    return NpmStorageInventory(
        npm_tool=_identity(tmp_path / "npm.cmd", directory=False, seed=1),
        cache_root=root,
        cache_root_identity=_identity(root, directory=True, seed=2),
        content_cache=NpmCacheArea(root / "_cacache", True, 120, 2),
        npx_cache=NpmCacheArea(
            root / "_npx",
            sum(entry.logical_bytes for entry in entries),
            len(entries),
        ),
        tuf_cache=NpmCacheArea(root / "_tuf", True, 7, 1),
        content_keys=("key-a", "key-b"),
        npx_entries=entries,
        warnings=(),
    )


def _completed(stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, stdout, "")


@pytest.mark.parametrize("operation", ["verify", "clean"])
def test_content_cache_vendor_actions_guard_exact_content_root_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    reviewed = _inventory(tmp_path)
    after = replace(
        reviewed,
        content_cache=NpmCacheArea(reviewed.content_cache.path, False, 0, 0),
        content_keys=(),
    )
    events: list[object] = []

    monkeypatch.setattr(
        npm,
        "_validated_current_inventory",
        lambda expected, environment: reviewed,
    )
    monkeypatch.setattr(npm, "_require_same_boundaries", lambda old, new: None)
    monkeypatch.setattr(npm, "inventory_npm_storage", lambda environment=None: after)
    monkeypatch.setattr(npm, "_require_process_idle", lambda: events.append("idle"))

    def guard(
        inventory: NpmStorageInventory,
        mutation_root: Path,
        environment: object,
    ) -> None:
        del environment
        assert inventory is reviewed
        events.append(("guard", mutation_root))

    monkeypatch.setattr(npm, "_require_no_protected_overlap", guard)

    def run(
        tool: NpmPathIdentity,
        arguments: tuple[str, ...],
        environment: dict[str, str],
        *,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        del tool, environment, timeout
        events.append(("run", arguments))
        return _completed()

    monkeypatch.setattr(npm, "_run_npm", run)

    if operation == "verify":
        npm.verify_npm_content_cache(reviewed)
        command = ("cache", "verify")
    else:
        npm.clean_npm_content_cache(reviewed)
        command = ("cache", "clean", "--force")

    assert events[:3] == [
        "idle",
        ("guard", reviewed.content_cache.path),
        "idle",
    ]
    assert events[3] == ("run", command)


def test_exact_npx_remove_rechecks_scope_and_process_immediately_before_real_rm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "npm-cache"
    entry = NpmNpxEntry(
        "entry-key",
        root / "_npx" / "entry-key",
        "package@1",
        77,
        4,
    )
    reviewed = _inventory(tmp_path, npx_entry=entry)
    after = replace(
        reviewed,
        npx_entries=(),
        npx_cache=NpmCacheArea(root / "_npx", True, 0, 0),
    )
    events: list[object] = []

    monkeypatch.setattr(
        npm,
        "_validated_current_inventory",
        lambda expected, environment: reviewed,
    )
    monkeypatch.setattr(npm, "_require_same_boundaries", lambda old, new: None)
    monkeypatch.setattr(npm, "inventory_npm_storage", lambda environment=None: after)
    monkeypatch.setattr(npm, "_require_process_idle", lambda: events.append("idle"))

    def guard(
        inventory: NpmStorageInventory,
        mutation_root: Path,
        environment: object,
    ) -> None:
        del environment
        assert inventory is reviewed
        events.append(("guard", mutation_root))

    monkeypatch.setattr(npm, "_require_no_protected_overlap", guard)

    def run(
        tool: NpmPathIdentity,
        arguments: tuple[str, ...],
        environment: dict[str, str],
        *,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        del tool, environment, timeout
        events.append(("run", arguments))
        if "--dry-run" in arguments:
            return _completed(f"Removing npx key at {entry.path}\n")
        return _completed(f"Removing npx key at {entry.path}\n")

    monkeypatch.setattr(npm, "_run_npm", run)

    npm.remove_npm_npx_entry(reviewed, entry)

    real_rm = ("run", ("cache", "npx", "rm", entry.key))
    real_index = events.index(real_rm)
    assert events[real_index - 3 : real_index] == [
        "idle",
        ("guard", entry.path),
        "idle",
    ]
