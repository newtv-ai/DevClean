from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import cast

import pytest

from reclaimer import __version__

ROOT = Path(__file__).resolve().parents[1]
SPEC = spec_from_file_location(
    "validate_release_artifacts",
    ROOT / "scripts" / "validate_release_artifacts.py",
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("unable to load release artifact validator")
MODULE = module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
_parse_checksums = cast(Callable[[Path], dict[str, str]], MODULE._parse_checksums)
_validate_wheel_member_name = cast(Callable[[str], str], MODULE._validate_wheel_member_name)

_GATE_TEMPLATES = (
    "g0-release-manifest.template.json",
    "g1-physical-manifest.template.json",
    "g2-machine-manifest.template.json",
    "g5-race-manifest.template.json",
)


def test_product_version_is_single_sourced_across_release_contracts() -> None:
    """Prevent divergent public release and gate-evidence version labels."""

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["version"] == __version__ == "0.1.0"
    for template_name in _GATE_TEMPLATES:
        template_path = ROOT / "docs" / "evidence" / "templates" / template_name
        template = json.loads(template_path.read_text(encoding="utf-8"))
        assert template["product"]["version"] == __version__


def test_all_checked_in_schemas_pass_offline_validator() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "validate_schemas.py")],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert "local JSON Schemas (draft 2020-12)" in result.stdout


def test_checksum_manifest_parser_is_strict(tmp_path: Path) -> None:
    manifest = tmp_path / "SHA256SUMS.txt"
    digest = "a" * 64
    manifest.write_text(f"{digest}  reclaimer.whl\n", encoding="utf-8")
    assert _parse_checksums(manifest) == {"reclaimer.whl": digest}

    manifest.write_text(f"{digest} *reclaimer.whl\n", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid checksum line"):
        _parse_checksums(manifest)


@pytest.mark.parametrize(
    "name",
    (
        "../payload.py",
        "/payload.py",
        "reclaimer\\payload.py",
        "reclaimer//payload.py",
        "reclaimer/./payload.py",
        "C:/payload.py",
        "reclaimer/NUL.py",
        "reclaimer/payload.py.",
    ),
)
def test_wheel_member_validator_rejects_windows_aliases(name: str) -> None:
    with pytest.raises(ValueError):
        _validate_wheel_member_name(name)


def test_wheel_member_validator_normalizes_for_collision_detection() -> None:
    assert _validate_wheel_member_name("Reclaimer/Payload.py") == (
        "reclaimer/payload.py"
    )


def test_release_builder_enforces_clean_runtime_sbom_and_validation() -> None:
    script = (ROOT / "scripts" / "build_release.ps1").read_text(encoding="utf-8")
    assert "uv build --wheel --clear --no-create-gitignore" in script
    assert "uv lock --check" in script
    assert "uv sync --frozen" in script
    assert "validate_schemas.py" in script
    assert "--no-build-isolation" in script
    assert "--no-deps --no-index" in script
    assert "--spec-version 1.6" in script
    assert "--output-reproducible" in script
    assert "--validate" in script
    assert "byte-for-byte reproducible" in script
    assert "validate_release_artifacts.py" in script
    assert "release-validation.json" in script
    assert "inventory_only_surface_validated" in script
    assert "builder_sha256" in script
    assert "uv_lock_sha256" in script
    assert "Invoke-GitProbe" in script
    assert 'SourceRevision = "WORKTREE_UNCOMMITTED"' in script
    assert "EvidenceOutput must stay beneath the repository artifacts directory" in script
    assert "EvidenceOutput must be a direct child of the repository artifacts directory" in script
    assert "EvidenceOutput must stay outside the validated release payload directory" in script
    assert "[IO.File]::Replace" in script


@pytest.mark.parametrize(
    ("evidence_output", "expected_message"),
    (
        (
            "..\\outside-release-validation.json",
            "must stay beneath the repository artifacts directory",
        ),
        (
            "artifacts\\release\\extra.json",
            "must stay outside the validated release payload directory",
        ),
    ),
)
def test_release_builder_rejects_unsafe_evidence_destinations(
    evidence_output: str, expected_message: str
) -> None:
    powershell = shutil.which("powershell.exe")
    if powershell is None:
        pytest.skip("Windows PowerShell is unavailable")
    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ROOT / "scripts" / "build_release.ps1"),
            "-EvidenceOutput",
            evidence_output,
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode != 0
    assert expected_message in result.stdout + result.stderr


def test_build_backend_is_exactly_pinned_in_project_and_lockfile() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    build_requirements = project["build-system"]["requires"]
    development_requirements = project["dependency-groups"]["dev"]
    assert build_requirements == ["hatchling==1.28.0"]
    assert "hatchling==1.28.0" in development_requirements
    assert project["tool"]["uv"]["required-version"] == "==0.11.6"
    assert project["project"]["license"] == "GPL-3.0-or-later"
    assert project["project"]["license-files"] == [
        "LICENSE",
        "THIRD_PARTY_NOTICES.md",
    ]
    assert not any(
        classifier.startswith("License ::")
        for classifier in project["project"]["classifiers"]
    )
    assert (ROOT / ".gitattributes").read_text(encoding="utf-8") == (
        "LICENSE text eol=lf\n"
    )
    editor_config = (ROOT / ".editorconfig").read_text(encoding="utf-8")
    assert "[LICENSE]\nend_of_line = lf\n" in editor_config.replace("\r\n", "\n")

    lockfile = (ROOT / "uv.lock").read_text(encoding="utf-8")
    assert 'name = "hatchling"' in lockfile
    assert 'version = "1.28.0"' in lockfile


def test_release_validator_rejects_synchronized_but_incomplete_license(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "LICENSE").write_text(
        "GNU GENERAL PUBLIC LICENSE\nVersion 3\n", encoding="utf-8"
    )
    (tmp_path / "THIRD_PARTY_NOTICES.md").write_text(
        "BleachBit boundary\n", encoding="utf-8"
    )
    (tmp_path / "pyproject.toml").write_text(
        """
[project]
name = "reclaimer"
version = "0.0.1"
requires-python = ">=3.11,<3.14"
license = "GPL-3.0-or-later"
license-files = ["LICENSE", "THIRD_PARTY_NOTICES.md"]
dependencies = []
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(MODULE, "ROOT", tmp_path)

    with pytest.raises(ValueError, match="canonical complete GNU GPLv3"):
        MODULE._project_metadata()
