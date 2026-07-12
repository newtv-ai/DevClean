"""Fail-closed validation for the v0.1 wheel, CycloneDX SBOM, and checksums."""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import re
import stat
import tomllib
import zipfile
from email import policy
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from typing import Any

from cyclonedx.schema import SchemaVersion
from cyclonedx.validation.json import JsonValidator
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.tags import Tag
from packaging.utils import canonicalize_name, parse_wheel_filename

ROOT = Path(__file__).resolve().parents[1]
SBOM_NAME = "reclaimer.cdx.json"
CHECKSUM_NAME = "SHA256SUMS.txt"
CHECKSUM_RE = re.compile(r"^(?P<digest>[a-f0-9]{64})  (?P<name>[^/\\]+)$")
WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)
MAX_WHEEL_MEMBER_BYTES = 128 * 1024 * 1024
MAX_WHEEL_TOTAL_BYTES = 256 * 1024 * 1024
MAX_LICENSE_FILE_BYTES = 4 * 1024 * 1024
FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
EXPECTED_LICENSE_FILES = ("LICENSE", "THIRD_PARTY_NOTICES.md")
CANONICAL_GPLV3_BYTES = 35_149
CANONICAL_GPLV3_SHA256 = (
    "3972dc9744f6499f0f9b2dbf76696f2ae7ad8af9b23dde66d6af86c9dfb36986"
)


def _fail(message: str) -> None:
    raise ValueError(message)


def _sha256(path: Path) -> str:
    before = path.stat(follow_symlinks=False)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    after = path.stat(follow_symlinks=False)
    if (
        before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or (before.st_ino and after.st_ino and before.st_ino != after.st_ino)
        or (before.st_dev and after.st_dev and before.st_dev != after.st_dev)
    ):
        _fail(f"file changed while hashing: {path.name}")
    return digest.hexdigest()


def _stable_read(path: Path, *, max_bytes: int) -> bytes:
    before = path.stat(follow_symlinks=False)
    if (
        not path.is_file()
        or path.is_symlink()
        or _is_windows_reparse(path)
        or before.st_size < 1
        or before.st_size > max_bytes
    ):
        _fail(f"project metadata file is not a bounded ordinary file: {path.name}")
    payload = path.read_bytes()
    after = path.stat(follow_symlinks=False)
    if len(payload) != before.st_size or (
        before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or (before.st_ino and after.st_ino and before.st_ino != after.st_ino)
        or (before.st_dev and after.st_dev and before.st_dev != after.st_dev)
    ):
        _fail(f"project metadata file changed while reading: {path.name}")
    return payload


def _validate_wheel_member_name(name: str) -> str:
    if not name or "\\" in name or "\x00" in name or len(name) > 512:
        _fail(f"wheel contains an unsafe member path: {name!r}")
    canonical = name[:-1] if name.endswith("/") else name
    if not canonical or name.startswith("/"):
        _fail(f"wheel contains an unsafe member path: {name!r}")
    parts = canonical.split("/")
    member = PurePosixPath(canonical)
    if member.as_posix() != canonical or any(part in {"", ".", ".."} for part in parts):
        _fail(f"wheel contains a non-canonical member path: {name!r}")
    for part in parts:
        if ":" in part or part.endswith((".", " ")):
            _fail(f"wheel member is unsafe on Windows: {name!r}")
        if part.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES:
            _fail(f"wheel member uses a reserved Windows name: {name!r}")
    return canonical.casefold()


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def _project_metadata() -> tuple[
    str,
    str,
    str,
    tuple[str, ...],
    str,
    dict[str, bytes],
]:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    license_files = tuple(str(item) for item in project.get("license-files", []))
    if license_files != EXPECTED_LICENSE_FILES:
        _fail("pyproject license-files must contain the closed license/notices pair")
    license_payloads = {
        name: _stable_read(ROOT / name, max_bytes=MAX_LICENSE_FILE_BYTES)
        for name in license_files
    }
    license_payload = license_payloads["LICENSE"]
    if (
        len(license_payload) != CANONICAL_GPLV3_BYTES
        or hashlib.sha256(license_payload).hexdigest() != CANONICAL_GPLV3_SHA256
    ):
        _fail("LICENSE must be the canonical complete GNU GPLv3 UTF-8/LF text")
    return (
        str(project["name"]),
        str(project["version"]),
        str(project["requires-python"]),
        tuple(str(item) for item in project.get("dependencies", [])),
        str(project["license"]),
        license_payloads,
    )


def _parse_checksums(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("checksum manifest must be UTF-8") from error
    if text.startswith("\ufeff"):
        _fail("checksum manifest must not contain a UTF-8 BOM")
    result: dict[str, str] = {}
    for line_number, line in enumerate(text.splitlines(), start=1):
        match = CHECKSUM_RE.fullmatch(line)
        if match is None:
            _fail(f"invalid checksum line {line_number}: expected lowercase SHA-256 and basename")
        name = match.group("name")
        if name in result:
            _fail(f"duplicate checksum entry: {name}")
        result[name] = match.group("digest")
    if not result:
        _fail("checksum manifest is empty")
    return result


def _validate_checksums(directory: Path, expected_names: set[str]) -> None:
    checksums = _parse_checksums(directory / CHECKSUM_NAME)
    if set(checksums) != expected_names:
        _fail(
            "checksum entries do not exactly match release payload: "
            f"expected {sorted(expected_names)}, got {sorted(checksums)}"
        )
    for name, expected in checksums.items():
        actual = _sha256(directory / name)
        if actual != expected:
            _fail(f"SHA-256 mismatch for {name}: expected {expected}, got {actual}")


def _validate_sbom(path: Path, *, expected_name: str, expected_version: str) -> None:
    try:
        text = path.read_text(encoding="utf-8")
        payload = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-standard JSON constant: {value}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid UTF-8 JSON SBOM: {error}") from error
    if not isinstance(payload, dict):
        _fail("SBOM root must be a JSON object")
    if payload.get("bomFormat") != "CycloneDX" or payload.get("specVersion") != "1.6":
        _fail("SBOM must declare CycloneDX 1.6")
    if payload.get("$schema") != "http://cyclonedx.org/schema/bom-1.6.schema.json":
        _fail("SBOM must identify the official CycloneDX 1.6 JSON Schema")

    validation_error = JsonValidator(SchemaVersion.V1_6).validate_str(text)
    if validation_error is not None:
        _fail(f"SBOM fails the bundled official CycloneDX 1.6 schema: {validation_error}")

    metadata = payload.get("metadata")
    component = metadata.get("component") if isinstance(metadata, dict) else None
    if not isinstance(component, dict):
        _fail("SBOM metadata.component is missing")
    if component.get("name") != expected_name or component.get("version") != expected_version:
        _fail("SBOM root component does not match pyproject.toml name/version")
    if component.get("type") != "application" or not component.get("bom-ref"):
        _fail("SBOM root component must be an application with a bom-ref")
    if component.get("licenses") != [
        {
            "license": {
                "acknowledgement": "declared",
                "id": "GPL-3.0-or-later",
            }
        }
    ]:
        _fail("SBOM root component must declare GPL-3.0-or-later")
    components = payload.get("components", [])
    if not isinstance(components, list) or components:
        _fail("v0.1 SBOM must not contain runtime dependency components")
    if payload.get("dependencies") != [{"ref": component["bom-ref"]}]:
        _fail("v0.1 SBOM dependency graph must contain only the root component")
    properties = metadata.get("properties", [])
    reproducible = any(
        isinstance(item, dict)
        and item.get("name") == "cdx:reproducible"
        and item.get("value") == "true"
        for item in properties
    )
    if not reproducible:
        _fail("SBOM must declare cdx:reproducible=true")


def _record_hash(data: bytes) -> str:
    digest = hashlib.sha256(data).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _validate_wheel(
    path: Path,
    *,
    expected_name: str,
    expected_version: str,
    expected_requires_python: str,
    project_dependencies: tuple[str, ...],
    expected_license_expression: str,
    expected_license_payloads: dict[str, bytes],
) -> None:
    wheel_name, wheel_version, build_tag, wheel_tags = parse_wheel_filename(path.name)
    if canonicalize_name(wheel_name) != canonicalize_name(expected_name):
        _fail("wheel filename project name does not match pyproject.toml")
    if str(wheel_version) != expected_version or build_tag:
        _fail("wheel filename version/build tag does not match pyproject.toml")
    if wheel_tags != {Tag("py3", "none", "any")}:
        _fail(f"v0.1 wheel must be py3-none-any, got {sorted(map(str, wheel_tags))}")
    try:
        with zipfile.ZipFile(path) as wheel:
            if wheel.comment:
                _fail("wheel ZIP comment must be empty")
            members = wheel.infolist()
            names = [member.filename for member in members]
            if len(names) != len(set(names)):
                _fail("wheel contains duplicate member names")
            normalized_names: set[str] = set()
            total_uncompressed = 0
            for member in members:
                normalized = _validate_wheel_member_name(member.filename)
                if normalized in normalized_names:
                    _fail("wheel contains case-insensitive or normalized path collisions")
                normalized_names.add(normalized)
                mode = (member.external_attr >> 16) & 0xFFFF
                if mode and stat.S_ISLNK(mode):
                    _fail(f"wheel contains a symbolic-link member: {member.filename!r}")
                if member.flag_bits & 0x01:
                    _fail(f"wheel contains an encrypted member: {member.filename!r}")
                if member.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                    _fail(f"wheel contains an unsupported compression method: {member.filename!r}")
                if member.file_size > MAX_WHEEL_MEMBER_BYTES:
                    _fail(f"wheel member exceeds its size bound: {member.filename!r}")
                total_uncompressed += member.file_size
                if total_uncompressed > MAX_WHEEL_TOTAL_BYTES:
                    _fail("wheel uncompressed payload exceeds its total size bound")
            corrupt = wheel.testzip()
            if corrupt is not None:
                _fail(f"wheel ZIP integrity check failed at {corrupt!r}")

            metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
            record_names = [name for name in names if name.endswith(".dist-info/RECORD")]
            wheel_names = [name for name in names if name.endswith(".dist-info/WHEEL")]
            if len(metadata_names) != 1 or len(record_names) != 1 or len(wheel_names) != 1:
                _fail("wheel must contain exactly one METADATA, WHEEL, and RECORD")

            dist_info_prefix = metadata_names[0].rsplit("/", 1)[0] + "/"
            for name in names:
                if not name.startswith(("reclaimer/", dist_info_prefix)):
                    _fail(f"wheel contains an unexpected top-level payload: {name!r}")

            metadata = BytesParser(policy=policy.default).parsebytes(wheel.read(metadata_names[0]))
            if metadata.get_all("Name", []) != [expected_name] or metadata.get_all(
                "Version", []
            ) != [expected_version]:
                _fail("wheel metadata name/version does not match pyproject.toml")
            requires_python_values = metadata.get_all("Requires-Python", [])
            if len(requires_python_values) != 1:
                _fail("wheel metadata must contain exactly one Requires-Python field")
            try:
                metadata_python = SpecifierSet(requires_python_values[0])
                project_python = SpecifierSet(expected_requires_python)
            except InvalidSpecifier as error:
                raise ValueError(f"invalid Requires-Python specifier: {error}") from error
            if metadata_python != project_python:
                _fail("wheel Requires-Python does not match pyproject.toml")
            requires_dist = tuple(metadata.get_all("Requires-Dist", []))
            if not project_dependencies and requires_dist:
                _fail("wheel unexpectedly declares runtime dependencies")
            if metadata.get_all("License-Expression", []) != [
                expected_license_expression
            ]:
                _fail("wheel License-Expression does not match pyproject.toml")
            if metadata.get_all("License", []):
                _fail("wheel must not contain the deprecated free-text License field")
            if any(
                value.startswith("License ::")
                for value in metadata.get_all("Classifier", [])
            ):
                _fail("wheel must not contain a deprecated license classifier")
            license_files = metadata.get_all("License-File", [])
            if len(license_files) != len(set(license_files)) or set(license_files) != set(
                expected_license_payloads
            ):
                _fail("wheel License-File entries do not exactly match pyproject.toml")
            for license_name, expected_payload in expected_license_payloads.items():
                member_name = f"{dist_info_prefix}licenses/{license_name}"
                if member_name not in names or wheel.read(member_name) != expected_payload:
                    _fail(f"wheel license payload mismatch for {license_name!r}")

            wheel_metadata = BytesParser(policy=policy.default).parsebytes(
                wheel.read(wheel_names[0])
            )
            if wheel_metadata.get_all("Wheel-Version", []) != ["1.0"]:
                _fail("wheel must declare exactly Wheel-Version 1.0")
            if wheel_metadata.get_all("Root-Is-Purelib", []) != ["true"]:
                _fail("wheel must declare exactly Root-Is-Purelib: true")
            if wheel_metadata.get_all("Tag", []) != ["py3-none-any"]:
                _fail("wheel metadata tag must be exactly py3-none-any")

            record_name = record_names[0]
            rows = list(csv.reader(io.StringIO(wheel.read(record_name).decode("utf-8"))))
            records: dict[str, tuple[str, str]] = {}
            for row in rows:
                if len(row) != 3 or row[0] in records:
                    _fail("wheel RECORD contains a malformed or duplicate row")
                records[row[0]] = (row[1], row[2])
            if set(records) != set(names):
                _fail("wheel RECORD entries do not exactly match ZIP members")
            for name in names:
                hash_field, size_field = records[name]
                if name == record_name:
                    if hash_field or size_field:
                        _fail("wheel RECORD self-entry must have empty hash and size")
                    continue
                data = wheel.read(name)
                if hash_field != f"sha256={_record_hash(data)}" or size_field != str(len(data)):
                    _fail(f"wheel RECORD integrity mismatch for {name!r}")
    except zipfile.BadZipFile as error:
        raise ValueError(f"invalid wheel ZIP: {error}") from error


def validate_release_directory(directory: Path) -> tuple[Path, Path]:
    directory = Path(directory.absolute())
    if directory.is_symlink() or _is_windows_reparse(directory):
        _fail("release directory must not be a symlink or reparse point")
    directory = directory.resolve(strict=True)
    if not directory.is_dir():
        _fail(f"release path is not a directory: {directory}")
    children = tuple(sorted(directory.iterdir()))
    if any(
        not path.is_file() or path.is_symlink() or _is_windows_reparse(path)
        for path in children
    ):
        _fail("release directory may contain only ordinary non-reparse files")
    files = children
    wheel_files = tuple(path for path in files if path.suffix == ".whl")
    if len(wheel_files) != 1:
        _fail(f"release directory must contain exactly one wheel, found {len(wheel_files)}")
    sbom = directory / SBOM_NAME
    checksum = directory / CHECKSUM_NAME
    if not sbom.is_file() or not checksum.is_file():
        _fail(f"release directory must contain {SBOM_NAME} and {CHECKSUM_NAME}")
    allowed_names = {wheel_files[0].name, SBOM_NAME, CHECKSUM_NAME}
    actual_names = {path.name for path in files}
    if actual_names != allowed_names:
        _fail(f"unexpected release files: {sorted(actual_names - allowed_names)}")

    (
        name,
        version,
        requires_python,
        dependencies,
        license_expression,
        license_payloads,
    ) = _project_metadata()
    _validate_wheel(
        wheel_files[0],
        expected_name=name,
        expected_version=version,
        expected_requires_python=requires_python,
        project_dependencies=dependencies,
        expected_license_expression=license_expression,
        expected_license_payloads=license_payloads,
    )
    _validate_sbom(sbom, expected_name=name, expected_version=version)
    _validate_checksums(directory, {wheel_files[0].name, SBOM_NAME})
    return wheel_files[0], sbom


def _is_windows_reparse(path: Path) -> bool:
    try:
        attributes = int(getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0))
    except OSError:
        return True
    return bool(attributes & FILE_ATTRIBUTE_REPARSE_POINT)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    wheel, sbom = validate_release_directory(args.directory)
    print(f"Validated release payload: {wheel.name}, {sbom.name}, {CHECKSUM_NAME}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as error:
        raise SystemExit(f"release validation failed: {error}") from error
