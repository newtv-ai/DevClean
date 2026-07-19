from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from devclean.adapters.base import AdapterContext, ProbeStatus
from devclean.adapters.conda import (
    CondaCacheAdapter,
    parse_clean_preview,
    read_conda_metadata_version,
)
from devclean.core.models import SemanticType
from devclean.evidence.store import EvidenceStore
from devclean.platform.windows.process import (
    BoundedProcessResult,
    ProcessTermination,
)

FIXTURES = Path(__file__).parent / "transcripts" / "conda"
FIXTURE_ROOT = Path(r"G:\fixtures\conda\pkgs")


@pytest.mark.parametrize(
    ("filename", "category", "count", "total"),
    [
        ("index-cache-26.5.json", "index_cache", 1, None),
        ("tarballs-26.5.json", "tarballs", 1, 1200),
        ("packages-26.5.json", "packages", 1, 2400),
        ("logfiles-26.5.json", "logfiles", 1, None),
    ],
)
def test_conda_26_shapes_parse_strictly(
    filename: str, category: str, count: int, total: int | None
) -> None:
    preview = parse_clean_preview(
        (FIXTURES / filename).read_text(encoding="utf-8"),
        category,
        (FIXTURE_ROOT,),
    )

    assert len(preview.candidates) == count
    assert preview.total_size == total


def test_conda_preview_rejects_warning_mismatch_and_escape() -> None:
    payload = json.loads((FIXTURES / "tarballs-26.5.json").read_text("utf-8"))
    payload["tarballs"]["warnings"] = ["size failed"]
    with pytest.raises(ValueError, match="incomplete"):
        parse_clean_preview(json.dumps(payload), "tarballs", (FIXTURE_ROOT,))

    payload["tarballs"]["warnings"] = []
    payload["tarballs"]["total_size"] = 999
    with pytest.raises(ValueError, match="size sum"):
        parse_clean_preview(json.dumps(payload), "tarballs", (FIXTURE_ROOT,))

    escaped = {"success": True, "logfiles": [r"G:\outside\.logs\private.log"]}
    with pytest.raises(ValueError, match="escapes"):
        parse_clean_preview(json.dumps(escaped), "logfiles", (FIXTURE_ROOT,))


def test_conda_metadata_version_is_read_without_launching_cli(tmp_path: Path) -> None:
    executable = _installation(tmp_path)
    assert read_conda_metadata_version(executable) == (26, 5, 3)


def test_conda_adapter_uses_only_no_plugin_dry_run_categories(tmp_path: Path) -> None:
    executable = _installation(tmp_path)
    root = tmp_path / "conda" / "pkgs"
    index_cache = root / "cache"
    tarball = root / "demo-1.0-0.conda"
    logfile = root / ".logs" / "fixture.log"
    package = root / "old-package-1.0-0"
    index_cache.mkdir(parents=True)
    logfile.parent.mkdir()
    package.mkdir()
    (index_cache / "repodata.json").write_bytes(b"index")
    tarball.write_bytes(b"tarball")
    logfile.write_bytes(b"log")
    (package / "payload.bin").write_bytes(b"package")
    responses = iter(
        (
            b"conda 26.5.3\n",
            b"--dry-run --json --index-cache --tarballs --logfiles --packages\n",
            json.dumps({"pkgs_dirs": [str(root)]}).encode(),
            json.dumps(
                {"success": True, "index_cache": {"files": [str(index_cache)]}}
            ).encode(),
            _package_preview("tarballs", root, tarball.name, len(b"tarball")),
            json.dumps({"success": True, "logfiles": [str(logfile)]}).encode(),
            _package_preview("packages", root, package.name, len(b"package")),
        )
    )
    commands: list[tuple[str, ...]] = []

    def runner(command) -> BoundedProcessResult:
        commands.append(command.argv)
        return _success(command.argv, next(responses))

    context = AdapterContext(
        "scan_fixture",
        EvidenceStore("scan_fixture", root=tmp_path / "evidence"),
        runner,
    )
    result = CondaCacheAdapter(executable).inventory(context)

    assert result.probe.status is ProbeStatus.AVAILABLE
    assert len(result.resources) == 4
    assert {resource.semantic_type for resource in result.resources} == {
        SemanticType.REBUILDABLE_CACHE,
        SemanticType.PACKAGE_STORE,
        SemanticType.APP_STATE,
    }
    assert all(resource.actionable is False for resource in result.resources)
    assert len(result.evidence) == 7
    assert all(command[1] == "--no-plugins" for command in commands)
    assert all("--yes" not in command for command in commands)
    assert all("--all" not in command for command in commands)
    assert all("--force-pkgs-dirs" not in command for command in commands)
    assert all(command[2] == "clean" for command in commands[3:])


def test_conda_missing_metadata_never_launches_unknown_cli(tmp_path: Path) -> None:
    executable = tmp_path / "conda.exe"
    shutil.copy2(sys.executable, executable)

    def forbidden_runner(command):
        raise AssertionError(command.argv)

    context = AdapterContext(
        "scan_fixture",
        EvidenceStore("scan_fixture", root=tmp_path / "evidence"),
        forbidden_runner,
    )
    result = CondaCacheAdapter(executable).inventory(context)

    assert result.probe.status is ProbeStatus.UNSUPPORTED_VERSION
    assert result.evidence == ()


def _installation(tmp_path: Path) -> Path:
    prefix = tmp_path / "conda"
    executable = prefix / "Scripts" / "conda.exe"
    metadata = prefix / "conda-meta" / "conda-26.5.3-py313_0.json"
    executable.parent.mkdir(parents=True)
    metadata.parent.mkdir()
    shutil.copy2(sys.executable, executable)
    metadata.write_text(
        json.dumps({"name": "conda", "version": "26.5.3"}), encoding="utf-8"
    )
    return executable


def _package_preview(category: str, root: Path, name: str, size: int) -> bytes:
    return json.dumps(
        {
            "success": True,
            category: {
                "warnings": [],
                "pkg_sizes": {str(root): {name: size}},
                "pkgs_dirs": {str(root): [name]},
                "total_size": size,
            },
        }
    ).encode()


def _success(argv: tuple[str, ...], stdout: bytes) -> BoundedProcessResult:
    return BoundedProcessResult(
        argv=argv,
        returncode=0,
        stdout=stdout,
        stderr=b"",
        duration_ms=1,
        termination=ProcessTermination.EXITED,
    )
