"""Produce a bounded, read-only G0 source/license boundary observation.

The observation proves mechanical repository facts only. It cannot prove code
originality or make the project owner's license decision; those remain separate
review/attestation evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tomllib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

MAX_FILES = 100_000
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_TOTAL_BYTES = 4 * 1024 * 1024 * 1024
REPARSE_POINT_ATTRIBUTE = 0x00000400
REVISION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{6,127}$")
FULL_GIT_OBJECT_RE = re.compile(r"^(?:[a-f0-9]{40}|[a-f0-9]{64})$")
CANONICAL_GPLV3_BYTES = 35_149
CANONICAL_GPLV3_SHA256 = (
    "3972dc9744f6499f0f9b2dbf76696f2ae7ad8af9b23dde66d6af86c9dfb36986"
)
EXCLUDED_DIRECTORIES = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "artifacts",
        "build",
        "dist",
        "htmlcov",
        "reports",
    }
)
PROHIBITED_VENDOR_SEGMENTS = frozenset(
    {"bleachbit", "cleanerml", "winapp2", "winapp2ool", "sifty"}
)


@dataclass(frozen=True, slots=True)
class FileObservation:
    relative_path: str
    sha256: str
    bytes: int


def audit_source_boundary(root: Path, source_revision: str) -> dict[str, object]:
    if not REVISION_RE.fullmatch(source_revision) or "replace" in source_revision.casefold():
        raise ValueError("source revision is invalid or still a placeholder")
    root = Path(os.path.abspath(root))
    if not root.is_dir():
        raise ValueError("source root must be an existing directory")
    _reject_reparse(root)
    _verify_revision_binding(root, source_revision)

    observations = tuple(_iter_source_files(root))
    by_path = {item.relative_path: item for item in observations}
    required = {"LICENSE", "THIRD_PARTY_NOTICES.md", "pyproject.toml"}
    missing = sorted(required - by_path.keys())
    if missing:
        raise ValueError("source root lacks required project files: " + ", ".join(missing))

    pyproject_bytes = _read_observed_file(
        root / "pyproject.toml", by_path["pyproject.toml"]
    )
    license_bytes = _read_observed_file(root / "LICENSE", by_path["LICENSE"])
    notices_bytes = _read_observed_file(
        root / "THIRD_PARTY_NOTICES.md", by_path["THIRD_PARTY_NOTICES.md"]
    )
    pyproject = tomllib.loads(pyproject_bytes.decode("utf-8", errors="strict"))
    project = pyproject.get("project")
    if not isinstance(project, dict):
        raise ValueError("pyproject project table is missing")
    dependencies = project.get("dependencies")
    scripts = project.get("scripts")
    entry_points = project.get("entry-points", {})
    declared_license_expression = project.get("license")
    declared_license_files = project.get("license-files")
    if not isinstance(dependencies, list) or not all(
        isinstance(item, str) for item in dependencies
    ):
        raise ValueError("project runtime dependencies are malformed")
    if not isinstance(scripts, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in scripts.items()
    ):
        raise ValueError("project scripts table is malformed")
    if not isinstance(entry_points, dict):
        raise ValueError("project entry-points table is malformed")

    notices_text = notices_bytes.decode("utf-8", errors="strict")
    prohibited_paths = sorted(
        observation.relative_path
        for observation in observations
        if any(
            segment.casefold() in PROHIBITED_VENDOR_SEGMENTS
            for segment in Path(observation.relative_path).parts
        )
    )
    runtime_plugin_groups = sorted(str(key) for key in entry_points)
    expected_scripts = {"reclaimer": "reclaimer.cli.main:main"}
    mechanical_pass = bool(
        not dependencies
        and scripts == expected_scripts
        and declared_license_expression == "GPL-3.0-or-later"
        and declared_license_files == ["LICENSE", "THIRD_PARTY_NOTICES.md"]
        and not runtime_plugin_groups
        and not prohibited_paths
        and len(license_bytes) == CANONICAL_GPLV3_BYTES
        and by_path["LICENSE"].sha256 == CANONICAL_GPLV3_SHA256
        and "BleachBit" in notices_text
    )

    tree_digest = hashlib.sha256()
    for observation in observations:
        tree_digest.update(observation.relative_path.encode("utf-8", errors="strict"))
        tree_digest.update(b"\0")
        tree_digest.update(bytes.fromhex(observation.sha256))
        tree_digest.update(observation.bytes.to_bytes(8, "big"))
    auditor_sha256 = _stable_sha256(Path(__file__))[0]
    return {
        "schema_version": "1.0.0",
        "evidence_kind": "G0_SOURCE_BOUNDARY_AUDIT",
        "captured_at": datetime.now(UTC).isoformat(),
        "source_revision": source_revision,
        "source_tree_sha256": tree_digest.hexdigest(),
        "auditor_sha256": auditor_sha256,
        "checked_file_count": len(observations),
        "checked_total_bytes": sum(item.bytes for item in observations),
        "license_sha256": by_path["LICENSE"].sha256,
        "third_party_notices_sha256": by_path["THIRD_PARTY_NOTICES.md"].sha256,
        "pyproject_sha256": by_path["pyproject.toml"].sha256,
        "runtime_dependencies": dependencies,
        "declared_license_expression": declared_license_expression,
        "declared_license_files": declared_license_files,
        "console_scripts": scripts,
        "runtime_plugin_groups": runtime_plugin_groups,
        "prohibited_vendored_paths": prohibited_paths,
        "mechanical_result": "PASS" if mechanical_pass else "FAIL",
        "owner_license_decision_proven": False,
        "originality_proven": False,
        "limitations": [
            "Mechanical scanning cannot prove code/rule originality.",
            "The project owner must separately attest the final license decision.",
            "A reviewer must compare source provenance and third-party notices before release.",
        ],
    }


def _iter_source_files(root: Path) -> Iterator[FileObservation]:
    stack = [root]
    count = 0
    total_bytes = 0
    while stack:
        directory = stack.pop()
        _reject_reparse(directory)
        with os.scandir(directory) as iterator:
            entries = sorted(iterator, key=lambda entry: entry.name.casefold(), reverse=True)
        for entry in entries:
            path = Path(entry.path)
            state = os.stat(path, follow_symlinks=False)
            if entry.is_symlink() or int(
                getattr(state, "st_file_attributes", 0)
            ) & REPARSE_POINT_ATTRIBUTE:
                raise ValueError(f"source tree contains a symlink/reparse point: {entry.name}")
            if entry.is_dir(follow_symlinks=False):
                lowered_name = entry.name.casefold()
                if (
                    lowered_name not in EXCLUDED_DIRECTORIES
                    and not lowered_name.endswith(".egg-info")
                ):
                    stack.append(path)
                continue
            if not entry.is_file(follow_symlinks=False) or _excluded_file(entry.name):
                continue
            count += 1
            if count > MAX_FILES:
                raise ValueError("source tree exceeds its file-count boundary")
            digest, size = _stable_sha256(path)
            total_bytes += size
            if total_bytes > MAX_TOTAL_BYTES:
                raise ValueError("source tree exceeds its total byte boundary")
            relative = path.relative_to(root).as_posix()
            yield FileObservation(relative, digest, size)


def _excluded_file(name: str) -> bool:
    lowered = name.casefold()
    return bool(
        lowered in {".coverage", "coverage.xml"}
        or lowered.endswith((".pyc", ".pyo", ".db", ".db-shm", ".db-wal", ".db-journal"))
    )


def _read_observed_file(path: Path, observation: FileObservation) -> bytes:
    """Read metadata only when it is still the exact file hashed for the tree."""

    _reject_reparse(path)
    before = path.stat(follow_symlinks=False)
    if not path.is_file() or before.st_size != observation.bytes:
        raise RuntimeError(f"observed source file changed before metadata read: {path.name}")
    payload = path.read_bytes()
    after = path.stat(follow_symlinks=False)
    if (
        len(payload) != observation.bytes
        or hashlib.sha256(payload).hexdigest() != observation.sha256
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or (before.st_ino and after.st_ino and before.st_ino != after.st_ino)
        or (before.st_dev and after.st_dev and before.st_dev != after.st_dev)
    ):
        raise RuntimeError(f"observed source file changed during metadata read: {path.name}")
    return payload


def _verify_revision_binding(root: Path, source_revision: str) -> None:
    """Bind release-shaped revisions to a clean checkout of that exact Git object."""

    if FULL_GIT_OBJECT_RE.fullmatch(source_revision) is None:
        return

    def run_git(*arguments: str) -> str:
        try:
            result = subprocess.run(
                ["git", "--no-optional-locks", "-C", str(root), *arguments],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="strict",
                timeout=15,
            )
        except (
            FileNotFoundError,
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
        ) as error:
            raise ValueError("unable to verify the requested Git source revision") from error
        return result.stdout.strip()

    top_level = run_git("rev-parse", "--show-toplevel")
    if os.path.normcase(os.path.abspath(top_level)) != os.path.normcase(str(root)):
        raise ValueError("source root must be the top level of the bound Git checkout")
    if run_git("rev-parse", "HEAD") != source_revision:
        raise ValueError("source revision does not match the checkout HEAD")
    if run_git("status", "--porcelain=v1", "--untracked-files=all"):
        raise ValueError("source checkout must be clean before revision-bound evidence is emitted")


def _stable_sha256(path: Path) -> tuple[str, int]:
    before = path.stat(follow_symlinks=False)
    if before.st_size > MAX_FILE_BYTES:
        raise ValueError(f"source file exceeds its byte boundary: {path.name}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    after = path.stat(follow_symlinks=False)
    if (
        before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or (before.st_ino and after.st_ino and before.st_ino != after.st_ino)
        or (before.st_dev and after.st_dev and before.st_dev != after.st_dev)
    ):
        raise RuntimeError(f"source file changed while hashing: {path.name}")
    return digest.hexdigest(), before.st_size


def _reject_reparse(path: Path) -> None:
    state = path.stat(follow_symlinks=False)
    if path.is_symlink() or int(
        getattr(state, "st_file_attributes", 0)
    ) & REPARSE_POINT_ATTRIBUTE:
        raise ValueError(f"source path is a symlink/reparse point: {path}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = audit_source_boundary(args.root, args.source_revision)
        rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if args.output is None:
            sys.stdout.write(rendered)
        else:
            if args.output.exists():
                raise FileExistsError(f"refusing to overwrite existing evidence: {args.output}")
            if not args.output.parent.is_dir():
                raise FileNotFoundError("evidence output parent does not exist")
            args.output.write_text(rendered, encoding="utf-8", newline="\n")
    except (OSError, RuntimeError, UnicodeError, ValueError, tomllib.TOMLDecodeError) as error:
        print(f"Source-boundary audit failed: {error}", file=sys.stderr)
        return 2
    return 0 if result["mechanical_result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
