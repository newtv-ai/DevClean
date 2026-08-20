from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import devclean.core.huggingface_maintenance as hf
from devclean.core.huggingface_maintenance import (
    HuggingFaceCacheKind,
    HuggingFaceDeletePreview,
    HuggingFaceHubInventory,
    HuggingFaceHubPrunePreview,
    HuggingFaceHubRepo,
    HuggingFaceHubRevision,
    HuggingFacePathIdentity,
    inventory_huggingface_storage,
)


def _identity(path: Path, *, directory: bool, seed: int) -> HuggingFacePathIdentity:
    return HuggingFacePathIdentity(
        path=path,
        volume_serial=100 + seed,
        file_id=f"id-{seed}",
        file_id_kind="test",
        creation_time_ns=1000 + seed,
        last_write_time_ns=2000 + seed,
        is_directory=directory,
    )


def _revision(
    cache_id: str = "model/openai/example",
    commit: str | None = None,
    *,
    refs: tuple[str, ...] = ("main",),
    supported: bool = True,
) -> HuggingFaceHubRevision:
    return HuggingFaceHubRevision(
        cache_id=cache_id,
        repo_id=cache_id.split("/", 1)[1],
        repo_type=cache_id.split("/", 1)[0],
        commit_hash=commit or "a" * 40,
        snapshot_path=f"C:/hf/hub/{cache_id}/snapshots/{commit or 'a' * 40}",
        vendor_size="1.0 GB",
        last_modified="1 day ago",
        refs=refs,
        deletion_supported=supported,
        reason="USER_REVIEW" if supported else "protected",
    )


def _repo(
    cache_id: str = "model/openai/example",
    revisions: tuple[HuggingFaceHubRevision, ...] | None = None,
) -> HuggingFaceHubRepo:
    values = revisions or (_revision(cache_id),)
    return HuggingFaceHubRepo(
        cache_id=cache_id,
        repo_id=cache_id.split("/", 1)[1],
        repo_type=cache_id.split("/", 1)[0],
        vendor_size="1.0 GB",
        last_accessed="today",
        last_modified="today",
        refs=("main",),
        revisions=values,
        deletion_supported=True,
        reason="USER_REVIEW",
    )


def _inventory(
    tmp_path: Path,
    repos: tuple[HuggingFaceHubRepo, ...] | None = None,
    *,
    warnings: tuple[str, ...] = (),
) -> HuggingFaceHubInventory:
    return HuggingFaceHubInventory(
        hf_tool=_identity(tmp_path / "hf.exe", directory=False, seed=1),
        hub_root=_identity(tmp_path / "hub", directory=True, seed=2),
        repos=repos or (_repo(),),
        warnings=warnings,
        revision_delete_proof_complete=not warnings,
    )


def test_huggingface_coarse_inventory_keeps_cache_kinds_separate(tmp_path: Path) -> None:
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
    }

    inventory = inventory_huggingface_storage(env)

    by_kind = {item.kind: item for item in inventory.caches}
    assert by_kind[HuggingFaceCacheKind.HUB].logical_bytes == 19
    assert by_kind[HuggingFaceCacheKind.XET].logical_bytes == 23
    assert by_kind[HuggingFaceCacheKind.ASSETS].logical_bytes == 29


def test_vendor_json_builds_repo_and_full_revision_positive_objects(tmp_path: Path) -> None:
    tool = _identity(tmp_path / "hf.exe", directory=False, seed=1)
    root = _identity(tmp_path / "hub", directory=True, seed=2)
    commit = "b" * 40
    repos = [
        {
            "id": "model/openai/example",
            "repo_id": "openai/example",
            "repo_type": "model",
            "size": "2.0 GB",
            "last_accessed": "today",
            "last_modified": "today",
            "refs": ["main"],
        }
    ]
    revisions = [
        {
            "id": "model/openai/example",
            "repo_id": "openai/example",
            "repo_type": "model",
            "revision": commit,
            "snapshot_path": f"C:/hf/hub/models--openai--example/snapshots/{commit}",
            "size": "2.0 GB",
            "last_modified": "today",
            "refs": ["main"],
        }
    ]

    inventory = hf._build_hub_inventory(tool, root, repos, revisions, ())

    assert len(inventory.repos) == 1
    repo = inventory.repos[0]
    assert repo.cache_id == "model/openai/example"
    assert repo.deletion_supported
    assert repo.revisions[0].commit_hash == commit
    assert repo.revisions[0].deletion_supported


def test_duplicate_revision_hash_disables_revision_delete_globally(tmp_path: Path) -> None:
    tool = _identity(tmp_path / "hf.exe", directory=False, seed=1)
    root = _identity(tmp_path / "hub", directory=True, seed=2)
    commit = "c" * 40
    repo_rows = []
    revision_rows = []
    for cache_id in ("model/org/one", "dataset/org/two"):
        repo_type, repo_id = cache_id.split("/", 1)
        repo_rows.append(
            {
                "id": cache_id,
                "repo_id": repo_id,
                "repo_type": repo_type,
                "size": "1 GB",
                "last_accessed": "",
                "last_modified": "today",
                "refs": [],
            }
        )
        revision_rows.append(
            {
                "id": cache_id,
                "repo_id": repo_id,
                "repo_type": repo_type,
                "revision": commit,
                "snapshot_path": f"C:/hf/{cache_id}/{commit}",
                "size": "1 GB",
                "last_modified": "today",
                "refs": [],
            }
        )

    inventory = hf._build_hub_inventory(tool, root, repo_rows, revision_rows, ())

    assert all(repo.deletion_supported for repo in inventory.repos)
    revisions = [revision for repo in inventory.repos for revision in repo.revisions]
    assert all(not revision.deletion_supported for revision in revisions)
    assert all("不唯一" in revision.reason for revision in revisions)


def test_hf_warning_disables_revision_uniqueness_authority(tmp_path: Path) -> None:
    tool = _identity(tmp_path / "hf.exe", directory=False, seed=1)
    root = _identity(tmp_path / "hub", directory=True, seed=2)
    commit = "d" * 40
    inventory = hf._build_hub_inventory(
        tool,
        root,
        [
            {
                "id": "model/org/one",
                "repo_id": "org/one",
                "repo_type": "model",
                "size": "1 GB",
                "last_accessed": "",
                "last_modified": "today",
                "refs": [],
            }
        ],
        [
            {
                "id": "model/org/one",
                "repo_id": "org/one",
                "repo_type": "model",
                "revision": commit,
                "snapshot_path": f"C:/hf/{commit}",
                "size": "1 GB",
                "last_modified": "today",
                "refs": [],
            }
        ],
        ("Found 1 cache inconsistencies",),
    )

    assert inventory.repos[0].deletion_supported
    assert not inventory.repos[0].revisions[0].deletion_supported
    assert not inventory.revision_delete_proof_complete


def test_repo_preview_uses_exact_cache_id_and_vendor_dry_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory = _inventory(tmp_path)
    repo = inventory.repos[0]
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(hf, "_validated_current_inventory", lambda reviewed, environment: inventory)
    monkeypatch.setattr(hf, "_require_process_idle", lambda: None)

    def fake_run(
        tool: HuggingFacePathIdentity,
        arguments: tuple[str, ...],
        environment: dict[str, str],
        *,
        timeout: int,
        allow_empty: bool = False,
    ) -> tuple[object, str, str]:
        del tool, environment, timeout, allow_empty
        calls.append(arguments)
        return (
            {"dry_run": True, "repos": 1, "revisions": 1, "size": "900 MB"},
            "",
            "{}",
        )

    monkeypatch.setattr(hf, "_run_hf_json", fake_run)

    preview = hf.preview_huggingface_repo_removal(inventory, repo)

    assert preview == HuggingFaceDeletePreview(repo.cache_id, 1, 1, "900 MB")
    assert calls == [
        (
            "cache",
            "rm",
            repo.cache_id,
            "--cache-dir",
            str(inventory.hub_root.path),
            "--dry-run",
            "--yes",
        )
    ]


def test_repo_preview_refuses_vendor_scope_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory = _inventory(tmp_path)
    repo = inventory.repos[0]
    monkeypatch.setattr(hf, "_validated_current_inventory", lambda reviewed, environment: inventory)
    monkeypatch.setattr(hf, "_require_process_idle", lambda: None)
    monkeypatch.setattr(
        hf,
        "_run_hf_json",
        lambda *args, **kwargs: (
            {"dry_run": True, "repos": 2, "revisions": 1, "size": "1 GB"},
            "",
            "{}",
        ),
    )

    with pytest.raises(RuntimeError, match="dry-run 范围"):
        hf.preview_huggingface_repo_removal(inventory, repo)


def test_revision_preview_requires_exact_single_revision_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _revision(commit="e" * 40)
    second = _revision(commit="f" * 40)
    repo = _repo(revisions=(first, second))
    inventory = _inventory(tmp_path, (repo,))
    monkeypatch.setattr(hf, "_validated_current_inventory", lambda reviewed, environment: inventory)
    monkeypatch.setattr(hf, "_require_process_idle", lambda: None)
    monkeypatch.setattr(
        hf,
        "_run_hf_json",
        lambda *args, **kwargs: (
            {"dry_run": True, "repos": 0, "revisions": 1, "size": "500 MB"},
            "",
            "{}",
        ),
    )

    preview = hf.preview_huggingface_revision_removal(inventory, first)

    assert preview.target == first.commit_hash
    assert preview.repos == 0
    assert preview.revisions == 1


def test_prune_preview_requires_vendor_detached_count_to_match_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    detached = _revision(commit="1" * 40, refs=())
    referenced = _revision(commit="2" * 40, refs=("main",))
    inventory = _inventory(tmp_path, (_repo(revisions=(detached, referenced)),))
    monkeypatch.setattr(hf, "_validated_current_inventory", lambda reviewed, environment: inventory)
    monkeypatch.setattr(hf, "_require_process_idle", lambda: None)
    monkeypatch.setattr(
        hf,
        "_run_hf_json",
        lambda *args, **kwargs: (
            {"dry_run": True, "revisions": 1, "incomplete": 2, "size": "600 MB"},
            "",
            "{}",
        ),
    )

    preview = hf.preview_huggingface_hub_prune(inventory)

    assert preview == HuggingFaceHubPrunePreview(1, 2, "600 MB", ("1" * 40,))


def test_prune_preview_refuses_hidden_scope_difference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    detached = _revision(commit="3" * 40, refs=())
    inventory = _inventory(tmp_path, (_repo(revisions=(detached,)),))
    monkeypatch.setattr(hf, "_validated_current_inventory", lambda reviewed, environment: inventory)
    monkeypatch.setattr(hf, "_require_process_idle", lambda: None)
    monkeypatch.setattr(
        hf,
        "_run_hf_json",
        lambda *args, **kwargs: (
            {"dry_run": True, "revisions": 2, "incomplete": 0, "size": "1 GB"},
            "",
            "{}",
        ),
    )

    with pytest.raises(RuntimeError, match="revision 范围"):
        hf.preview_huggingface_hub_prune(inventory)


def test_path_identity_rejects_reparse_or_missing_stable_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "hub"
    root.mkdir()
    monkeypatch.setattr(Path, "is_symlink", lambda self: False)
    monkeypatch.setattr(Path, "is_junction", lambda self: False)
    monkeypatch.setattr(hf, "is_local_fixed_path", lambda path: True)
    monkeypatch.setattr(
        hf,
        "read_file_metadata",
        lambda path: SimpleNamespace(
            is_directory=True,
            is_reparse_point=False,
            is_cloud_placeholder=False,
            volume_serial=None,
            file_id=None,
            file_id_kind=None,
            creation_time_ns=None,
            last_write_time_ns=None,
        ),
    )

    with pytest.raises(RuntimeError, match="稳定文件身份"):
        hf._path_identity(root, expect_directory=True, label="cache")
