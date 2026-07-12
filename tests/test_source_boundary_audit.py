from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "audit_source_boundary",
    ROOT / "scripts" / "audit_source_boundary.py",
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("unable to load source-boundary auditor")
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_current_source_tree_meets_mechanical_g0_boundary() -> None:
    result = cast(dict[str, Any], MODULE.audit_source_boundary(ROOT, "abcdef1234567890"))

    assert result["mechanical_result"] == "PASS"
    assert result["runtime_dependencies"] == []
    assert result["declared_license_expression"] == "GPL-3.0-or-later"
    assert result["declared_license_files"] == ["LICENSE", "THIRD_PARTY_NOTICES.md"]
    assert result["license_sha256"] == (
        "3972dc9744f6499f0f9b2dbf76696f2ae7ad8af9b23dde66d6af86c9dfb36986"
    )
    assert result["runtime_plugin_groups"] == []
    assert result["prohibited_vendored_paths"] == []
    assert result["owner_license_decision_proven"] is False
    assert result["originality_proven"] is False


def test_prohibited_vendor_segment_is_reported_without_claiming_originality(
    tmp_path: Path,
) -> None:
    (tmp_path / "bleachbit").mkdir()
    (tmp_path / "bleachbit" / "rules.py").write_text("rules = []\n", encoding="utf-8")
    (tmp_path / "LICENSE").write_text(
        "GNU GENERAL PUBLIC LICENSE\nVersion 3\n", encoding="utf-8"
    )
    (tmp_path / "THIRD_PARTY_NOTICES.md").write_text("BleachBit boundary\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        """
[project]
name = "reclaimer"
version = "0.0.1"
dependencies = []

[project.scripts]
reclaimer = "reclaimer.cli.main:main"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    result = cast(
        dict[str, Any], MODULE.audit_source_boundary(tmp_path, "abcdef1234567890")
    )

    assert result["mechanical_result"] == "FAIL"
    assert result["prohibited_vendored_paths"] == ["bleachbit/rules.py"]


def test_short_license_notice_cannot_pass_as_complete_gplv3(tmp_path: Path) -> None:
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
license = "GPL-3.0-or-later"
license-files = ["LICENSE", "THIRD_PARTY_NOTICES.md"]
dependencies = []

[project.scripts]
reclaimer = "reclaimer.cli.main:main"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    result = cast(
        dict[str, Any], MODULE.audit_source_boundary(tmp_path, "abcdef1234567890")
    )

    assert result["mechanical_result"] == "FAIL"
    assert result["prohibited_vendored_paths"] == []


def test_release_shaped_revision_requires_a_matching_git_checkout(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="verify the requested Git source revision"):
        MODULE.audit_source_boundary(tmp_path, "a" * 40)


def test_release_revision_is_bound_to_matching_clean_git_head(tmp_path: Path) -> None:
    (tmp_path / "LICENSE").write_bytes((ROOT / "LICENSE").read_bytes())
    (tmp_path / "THIRD_PARTY_NOTICES.md").write_text(
        "BleachBit boundary\n", encoding="utf-8"
    )
    (tmp_path / "pyproject.toml").write_text(
        """
[project]
name = "reclaimer"
version = "0.0.1"
license = "GPL-3.0-or-later"
license-files = ["LICENSE", "THIRD_PARTY_NOTICES.md"]
dependencies = []

[project.scripts]
reclaimer = "reclaimer.cli.main:main"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    def git(*arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "git",
                "-c",
                "user.name=Reclaimer Test",
                "-c",
                "user.email=test@example.invalid",
                *arguments,
            ],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
        )

    git("init", "--initial-branch=main")
    git("add", ".")
    git("commit", "-m", "fixture")
    revision = git("rev-parse", "HEAD").stdout.strip()

    result = cast(dict[str, Any], MODULE.audit_source_boundary(tmp_path, revision))
    assert result["source_revision"] == revision
    assert result["mechanical_result"] == "PASS"

    (tmp_path / "untracked.txt").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(ValueError, match="source checkout must be clean"):
        MODULE.audit_source_boundary(tmp_path, revision)
