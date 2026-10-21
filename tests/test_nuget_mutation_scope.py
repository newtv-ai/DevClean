from __future__ import annotations

import subprocess
from pathlib import Path, PureWindowsPath

import pytest

from devclean.core import nuget_maintenance as nuget
from devclean.core.nuget_cleanup import NuGetRootSet
from devclean.core.vendor_cleanup_actions import (
    VendorCleanupKind,
    inventory_vendor_cleanup_candidates,
)


def _roots(packages: str, cache: str) -> NuGetRootSet:
    return NuGetRootSet(
        global_packages_roots=(PureWindowsPath(packages),),
        http_cache_roots=(PureWindowsPath(cache),),
        temp_roots=(PureWindowsPath(r"C:\scratch"),),
        plugins_cache_roots=(PureWindowsPath(r"C:\plugins-cache"),),
        config_paths=(PureWindowsPath(r"C:\user\NuGet.Config"),),
    )


@pytest.mark.parametrize(
    "packages",
    [
        r"C:\shared",
        r"C:\shared\packages",
        r"C:\SHARED\..\shared",
        "C:\\",
    ],
)
def test_overlapping_dependency_root_never_becomes_a_vendor_candidate(
    monkeypatch: pytest.MonkeyPatch,
    packages: str,
) -> None:
    roots = _roots(packages, r"C:\shared")
    monkeypatch.setattr(nuget, "nuget_roots", lambda _environment=None: roots)
    monkeypatch.setattr(Path, "is_dir", lambda _self: True)
    monkeypatch.setattr(nuget, "_directory_bytes", lambda _path: 123)
    inventory = inventory_vendor_cleanup_candidates(
        {},
        kinds=frozenset({VendorCleanupKind.NUGET_HTTP_CACHE_CLEAR}),
    )
    assert inventory.candidates == ()
    with pytest.raises(RuntimeError, match="重叠"):
        nuget.clear_nuget_local(nuget.NuGetLocalKind.HTTP_CACHE, Path(r"C:\shared"), {})


def test_boundary_check_preserves_a_separate_sibling() -> None:
    nuget._require_separate_resource(
        nuget.NuGetLocalKind.HTTP_CACHE,
        Path(r"C:\cache"),
        _roots(r"C:\cache-other", r"C:\cache"),
    )


def test_config_file_inside_clear_range_is_protected() -> None:
    with pytest.raises(RuntimeError, match="重叠"):
        nuget._require_separate_resource(
            nuget.NuGetLocalKind.HTTP_CACHE,
            Path(r"C:\user"),
            _roots(r"C:\packages", r"C:\user"),
        )


@pytest.mark.parametrize(
    "output",
    [
        "http-cache: C:\\cache\n",
        "http-cache: C:\\cache\nglobal-packages: C:\\cache\\packages\n"
        "temp: C:\\scratch\nplugins-cache: C:\\plugins-cache\n",
        "http-cache: C:\\changed\nglobal-packages: C:\\packages\n"
        "temp: C:\\scratch\nplugins-cache: C:\\plugins-cache\n",
        "http-cache: C:\\cache\nhttp-cache: C:\\cache\nglobal-packages: C:\\packages\n"
        "temp: C:\\scratch\nplugins-cache: C:\\plugins-cache\n",
    ],
)
def test_live_vendor_config_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    output: str,
) -> None:
    monkeypatch.setattr(nuget, "nuget_roots", lambda _env: _roots(r"C:\packages", r"C:\cache"))
    monkeypatch.setattr(
        nuget,
        "_run_dotnet",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, stdout=output, stderr=""),
    )
    with pytest.raises(RuntimeError):
        nuget._require_live_mutation_scope(
            nuget.NuGetLocalKind.HTTP_CACHE,
            Path(r"C:\dotnet.exe"),
            Path(r"C:\cache"),
            {},
        )


def test_scope_check_resolves_directory_aliases(tmp_path: Path) -> None:
    target = tmp_path / "cache"
    target.mkdir()
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink privilege unavailable")
    assert nuget._paths_overlap(target, alias / "packages")
