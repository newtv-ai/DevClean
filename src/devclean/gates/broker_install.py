"""Read-only verifier for the artifact-installation subset of future gate G6.

This module does not implement, launch, elevate, or communicate with a broker.  It
validates a release manifest against one installed Program Files tree and probes
whether the current non-elevated token can obtain any mutation right.  IPC, UAC,
negative-command, crash, and supported-Windows matrix tests remain separate G6 work.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import json
import os
import re
import sys
from collections.abc import Callable, Sequence
from ctypes import wintypes
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Final
from uuid import UUID

from devclean.adapters.json_contract import strict_json_loads
from devclean.platform.windows.filesystem import read_file_metadata
from devclean.platform.windows.process import ProcessLimits, run_bounded_process
from devclean.platform.windows.security import is_process_elevated
from devclean.platform.windows.volumes import is_local_fixed_path

_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_THUMBPRINT = re.compile(r"^[a-f0-9]{40}$")
_RELEASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._()+@-]{0,126}[A-Za-z0-9_()+@-]$")
_SINGLE_CHARACTER_SEGMENT = re.compile(r"^[A-Za-z0-9]$")
_RESERVED_WINDOWS_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)
_MAX_MANIFEST_BYTES: Final = 1024 * 1024
_MAX_ARTIFACTS: Final = 1024
_MAX_TREE_ENTRIES: Final = 4096
_MAX_ARTIFACT_BYTES: Final = 1024 * 1024 * 1024
_HASH_CHUNK_BYTES: Final = 1024 * 1024
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_ERROR_ACCESS_DENIED = 5
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_FILE_SHARE_DELETE = 0x00000004
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_FLAG_OPEN_NO_RECALL = 0x00100000
_FOLDERID_PROGRAM_FILES_X64 = UUID("6d809377-6af0-444b-8957-a3773f02200e")

_MUTATION_RIGHTS: tuple[tuple[str, int], ...] = (
    ("WRITE_DATA_OR_ADD_FILE", 0x00000002),
    ("APPEND_DATA_OR_ADD_SUBDIRECTORY", 0x00000004),
    ("WRITE_EA", 0x00000010),
    ("DELETE_CHILD", 0x00000040),
    ("WRITE_ATTRIBUTES", 0x00000100),
    ("DELETE", 0x00010000),
    ("WRITE_DAC", 0x00040000),
    ("WRITE_OWNER", 0x00080000),
)


class ArtifactKind(StrEnum):
    BROKER = "BROKER"
    DLL = "DLL"


@dataclass(frozen=True, slots=True)
class BrokerArtifact:
    relative_path: str
    kind: ArtifactKind
    sha256: str


@dataclass(frozen=True, slots=True)
class BrokerInstallManifest:
    release_id: str
    publisher_thumbprint: str
    installer_sha256: str
    artifacts: tuple[BrokerArtifact, ...]


@dataclass(frozen=True, slots=True)
class TreeEntry:
    relative_path: str
    is_directory: bool
    volume_serial: int | None
    file_id: str | None
    file_id_kind: str | None


@dataclass(frozen=True, slots=True)
class AuthenticodeObservation:
    status: str
    signature_type: str
    thumbprint: str


class _Guid(ctypes.Structure):
    _fields_ = [
        ("data1", wintypes.DWORD),
        ("data2", wintypes.WORD),
        ("data3", wintypes.WORD),
        ("data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def from_uuid(cls, value: UUID) -> _Guid:
        return cls(
            value.time_low,
            value.time_mid,
            value.time_hi_version,
            (ctypes.c_ubyte * 8)(*value.bytes[8:]),
        )


def parse_manifest_bytes(content: bytes) -> BrokerInstallManifest:
    """Parse a bounded, exact-key release manifest without accepting path aliases."""

    if not isinstance(content, bytes):
        raise TypeError("broker manifest must be bytes")
    if not content or len(content) > _MAX_MANIFEST_BYTES:
        raise ValueError("broker manifest size is outside the accepted boundary")
    try:
        payload = strict_json_loads(content.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("broker manifest is not strict UTF-8 JSON") from error
    if not isinstance(payload, dict):
        raise ValueError("broker manifest root must be an object")
    expected_keys = {
        "schema_version",
        "release_id",
        "publisher_thumbprint",
        "installer_sha256",
        "artifacts",
    }
    if set(payload) != expected_keys:
        raise ValueError("broker manifest keys do not match the closed contract")
    if payload["schema_version"] != "1.0.0":
        raise ValueError("broker manifest schema version is unsupported")

    release_id = payload["release_id"]
    publisher_thumbprint = payload["publisher_thumbprint"]
    installer_sha256 = payload["installer_sha256"]
    raw_artifacts = payload["artifacts"]
    if not isinstance(release_id, str) or not _RELEASE_ID.fullmatch(release_id):
        raise ValueError("broker release_id is invalid")
    if not isinstance(publisher_thumbprint, str) or not _THUMBPRINT.fullmatch(
        publisher_thumbprint
    ):
        raise ValueError("publisher thumbprint must be 40 lowercase hexadecimal characters")
    if not isinstance(installer_sha256, str) or not _SHA256.fullmatch(installer_sha256):
        raise ValueError("installer SHA-256 is invalid")
    if (
        not isinstance(raw_artifacts, list)
        or not raw_artifacts
        or len(raw_artifacts) > _MAX_ARTIFACTS
    ):
        raise ValueError("broker artifact list is empty or exceeds its bound")

    artifacts: list[BrokerArtifact] = []
    seen_paths: set[str] = set()
    broker_count = 0
    for raw in raw_artifacts:
        if not isinstance(raw, dict) or set(raw) != {"relative_path", "kind", "sha256"}:
            raise ValueError("broker artifact does not match the closed contract")
        relative_path = _canonical_relative_path(raw["relative_path"])
        key = relative_path.casefold()
        if key in seen_paths:
            raise ValueError("broker artifact paths collide case-insensitively")
        seen_paths.add(key)
        try:
            kind = ArtifactKind(raw["kind"])
        except (TypeError, ValueError) as error:
            raise ValueError("broker artifact kind is unsupported") from error
        digest = raw["sha256"]
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise ValueError("broker artifact SHA-256 is invalid")
        if kind is ArtifactKind.BROKER:
            broker_count += 1
            if not relative_path.casefold().endswith(".exe"):
                raise ValueError("BROKER artifact must be an .exe")
        elif not relative_path.casefold().endswith(".dll"):
            raise ValueError("DLL artifact must be a .dll")
        artifacts.append(BrokerArtifact(relative_path, kind, digest))
    if broker_count != 1:
        raise ValueError("broker manifest must contain exactly one BROKER artifact")
    return BrokerInstallManifest(
        release_id=release_id,
        publisher_thumbprint=publisher_thumbprint,
        installer_sha256=installer_sha256,
        artifacts=tuple(artifacts),
    )


def load_manifest(path: Path, expected_sha256: str) -> tuple[BrokerInstallManifest, str]:
    if not _SHA256.fullmatch(expected_sha256):
        raise ValueError("expected manifest SHA-256 is invalid")
    manifest_path = _validated_ordinary_file(path)
    content = manifest_path.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    if digest != expected_sha256:
        raise ValueError("broker manifest SHA-256 does not match the published value")
    return parse_manifest_bytes(content), digest


def parse_authenticode_json(content: bytes) -> AuthenticodeObservation:
    try:
        payload = strict_json_loads(content.decode("utf-8-sig", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Authenticode observer returned invalid JSON") from error
    if not isinstance(payload, dict) or set(payload) != {
        "signature_type",
        "status",
        "thumbprint",
    }:
        raise ValueError("Authenticode observer returned an unexpected contract")
    if not all(isinstance(value, str) for value in payload.values()):
        raise ValueError("Authenticode observer fields must be text")
    thumbprint = payload["thumbprint"].lower()
    if not _THUMBPRINT.fullmatch(thumbprint):
        raise ValueError("Authenticode observer returned an invalid certificate thumbprint")
    return AuthenticodeObservation(
        status=payload["status"],
        signature_type=payload["signature_type"],
        thumbprint=thumbprint,
    )


def verify_broker_install(
    *,
    install_root: Path,
    installer: Path,
    manifest_path: Path,
    manifest_sha256: str,
    authenticode_observer: Callable[[Path], AuthenticodeObservation] | None = None,
) -> dict[str, Any]:
    """Verify the non-elevated artifact/ACL subset of G6 and return redacted evidence."""

    if os.name != "nt":
        raise RuntimeError("broker installation verification is Windows-only")
    if is_process_elevated():
        raise PermissionError("run broker installation verification as a standard user")

    manifest, observed_manifest_sha256 = load_manifest(manifest_path, manifest_sha256)
    root = _validated_install_root(install_root)
    installer_path = _validated_ordinary_file(installer)
    installer_digest = _stable_sha256(installer_path)
    if installer_digest != manifest.installer_sha256:
        raise ValueError("installer SHA-256 does not match the release manifest")

    observer = authenticode_observer or observe_authenticode
    installer_signature = observer(installer_path)
    _require_signature(installer_signature, manifest.publisher_thumbprint, "installer")
    if _stable_sha256(installer_path) != installer_digest:
        raise RuntimeError("installer changed during signature verification")

    root_parent_grants = probe_mutation_rights(
        root.parent,
        rights=(("DELETE_CHILD", 0x00000040),),
    )
    if root_parent_grants:
        raise PermissionError("standard user can delete children from the install parent")

    before = inventory_install_tree(root)
    expected_files = {artifact.relative_path.casefold() for artifact in manifest.artifacts}
    expected_directories = _expected_directories(manifest.artifacts)
    actual_files = {
        entry.relative_path.casefold() for entry in before if not entry.is_directory
    }
    actual_directories = {
        entry.relative_path.casefold() for entry in before if entry.is_directory
    }
    if actual_files != expected_files or actual_directories != expected_directories:
        raise ValueError("installed broker tree differs from the closed release manifest")

    paths_checked = 1
    if probe_mutation_rights(root):
        raise PermissionError("standard user has mutation rights on the install root")
    artifact_results: list[dict[str, Any]] = []
    by_relative = {entry.relative_path.casefold(): entry for entry in before}
    for relative in sorted(expected_directories):
        directory = root.joinpath(*PurePosixPath(relative).parts)
        if probe_mutation_rights(directory):
            raise PermissionError("standard user has mutation rights on an install directory")
        paths_checked += 1
    for artifact in manifest.artifacts:
        path = root.joinpath(*PurePosixPath(artifact.relative_path).parts)
        if probe_mutation_rights(path):
            raise PermissionError("standard user has mutation rights on an installed artifact")
        digest = _stable_sha256(path)
        if digest != artifact.sha256:
            raise ValueError("installed artifact SHA-256 differs from the release manifest")
        signature = observer(path)
        _require_signature(signature, manifest.publisher_thumbprint, artifact.relative_path)
        if _stable_sha256(path) != digest:
            raise RuntimeError("installed artifact changed during signature verification")
        entry = by_relative[artifact.relative_path.casefold()]
        artifact_results.append(
            {
                "relative_path": artifact.relative_path,
                "kind": artifact.kind.value,
                "sha256": digest,
                "volume_serial": (
                    None if entry.volume_serial is None else f"{entry.volume_serial:x}"
                ),
                "file_id": entry.file_id,
                "file_id_kind": entry.file_id_kind,
                "authenticode": {
                    "status": signature.status,
                    "signature_type": signature.signature_type,
                    "publisher_thumbprint": signature.thumbprint,
                },
            }
        )
        paths_checked += 1

    after = inventory_install_tree(root)
    if after != before:
        raise RuntimeError("installed broker tree changed during verification")
    return {
        "schema_version": "1.0.0",
        "verification": "G6_ARTIFACT_INSTALL_SUBGATE",
        "result": "PASS",
        "g6_gate_passed": False,
        "checked_at": datetime.now(UTC).isoformat(),
        "release_id": manifest.release_id,
        "manifest_sha256": observed_manifest_sha256,
        "install_root_sha256": hashlib.sha256(
            os.path.normcase(str(root)).encode("utf-8", errors="strict")
        ).hexdigest(),
        "program_files_boundary": True,
        "non_elevated_acl_probe": True,
        "mutation_rights_denied": True,
        "paths_checked": paths_checked,
        "installer": {
            "sha256": manifest.installer_sha256,
            "authenticode": {
                "status": installer_signature.status,
                "signature_type": installer_signature.signature_type,
                "publisher_thumbprint": installer_signature.thumbprint,
            },
        },
        "artifacts": artifact_results,
        "limitations": [
            "This verifier does not launch or communicate with the broker.",
            "G6 remains open until IPC, UAC cancellation, injection, crash, negative-input, "
            "and supported-Windows matrix evidence passes.",
            "The external action table must be absent; supported actions must be compiled into "
            "the signed broker contract.",
        ],
    }


def inventory_install_tree(root: Path) -> tuple[TreeEntry, ...]:
    entries: list[TreeEntry] = []
    stack: list[tuple[Path, PurePosixPath]] = [(root, PurePosixPath())]
    while stack:
        directory, relative_directory = stack.pop()
        with os.scandir(directory) as iterator:
            children = sorted(iterator, key=lambda entry: entry.name.casefold())
        for child in children:
            if len(entries) >= _MAX_TREE_ENTRIES:
                raise ValueError("installed broker tree exceeds its entry bound")
            relative = relative_directory / child.name
            canonical = _canonical_relative_path(relative.as_posix())
            metadata = read_file_metadata(child.path)
            if metadata.is_reparse_point or metadata.is_cloud_placeholder:
                raise ValueError("installed broker tree contains a reparse or Cloud boundary")
            entry = TreeEntry(
                relative_path=canonical,
                is_directory=metadata.is_directory,
                volume_serial=metadata.volume_serial,
                file_id=metadata.file_id,
                file_id_kind=metadata.file_id_kind,
            )
            entries.append(entry)
            if metadata.is_directory:
                stack.append((Path(child.path), relative))
    return tuple(sorted(entries, key=lambda entry: entry.relative_path.casefold()))


def probe_mutation_rights(
    path: Path,
    *,
    rights: tuple[tuple[str, int], ...] = _MUTATION_RIGHTS,
) -> tuple[str, ...]:
    """Request mutation access without performing a mutation; unknown errors fail closed."""

    if os.name != "nt":
        raise RuntimeError("Windows access probes are unavailable")
    metadata = read_file_metadata(path)
    if metadata.is_reparse_point or metadata.is_cloud_placeholder:
        raise ValueError("access probe target is a reparse or Cloud boundary")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    flags = _FILE_FLAG_OPEN_REPARSE_POINT | _FILE_FLAG_OPEN_NO_RECALL
    if metadata.is_directory:
        flags |= _FILE_FLAG_BACKUP_SEMANTICS
    granted: list[str] = []
    for name, access_mask in rights:
        ctypes.set_last_error(0)
        raw_handle = create_file(
            str(path),
            access_mask,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
            None,
            _OPEN_EXISTING,
            flags,
            None,
        )
        if raw_handle and raw_handle != _INVALID_HANDLE_VALUE:
            close_handle(raw_handle)
            granted.append(name)
            continue
        error_code = ctypes.get_last_error()
        if error_code != _ERROR_ACCESS_DENIED:
            raise OSError(
                error_code,
                f"mutation access probe was inconclusive for {name}",
                str(path),
            )
    return tuple(granted)


def observe_authenticode(path: Path) -> AuthenticodeObservation:
    """Use the fixed inbox PowerShell security module under a bounded Job Object."""

    system_directory = _system_directory()
    powershell = system_directory / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    module = (
        system_directory
        / "WindowsPowerShell"
        / "v1.0"
        / "Modules"
        / "Microsoft.PowerShell.Security"
        / "Microsoft.PowerShell.Security.psd1"
    )
    # Inbox Windows components may legitimately have servicing hard links. The
    # release installer and installed broker artifacts are still required to
    # have exactly one link by their separate validation paths.
    _validated_ordinary_file(powershell, allow_hardlinks=True)
    _validated_ordinary_file(module, allow_hardlinks=True)
    if probe_mutation_rights(powershell) or probe_mutation_rights(module):
        raise PermissionError("standard user can mutate an Authenticode observer component")
    target_b64 = base64.b64encode(str(path).encode("utf-8", errors="strict")).decode("ascii")
    module_b64 = base64.b64encode(str(module).encode("utf-8", errors="strict")).decode("ascii")
    script = (
        "$ErrorActionPreference='Stop';"
        f"$p=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{target_b64}'));"
        f"$m=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{module_b64}'));"
        "Import-Module -Name $m -Force -ErrorAction Stop;"
        "$s=Microsoft.PowerShell.Security\\Get-AuthenticodeSignature -LiteralPath $p;"
        "$t=if($null -eq $s.SignerCertificate){''}else{$s.SignerCertificate.Thumbprint};"
        "[ordered]@{status=[string]$s.Status;signature_type=[string]$s.SignatureType;"
        "thumbprint=[string]$t}|ConvertTo-Json -Compress"
    )
    windows_root = system_directory.parent
    environment = {
        "PATH": f"{powershell.parent}{os.pathsep}{system_directory}",
        "PATHEXT": ".COM;.EXE;.BAT;.CMD",
        "SYSTEMROOT": str(windows_root),
        "WINDIR": str(windows_root),
    }
    result = run_bounded_process(
        (
            str(powershell),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ),
        environment=environment,
        cwd=system_directory,
        limits=ProcessLimits(
            timeout_seconds=15,
            max_stdout_bytes=32 * 1024,
            max_stderr_bytes=32 * 1024,
        ),
    )
    if (
        result.returncode != 0
        or result.timed_out
        or result.output_limit_exceeded
        or result.stderr
    ):
        raise RuntimeError("bounded Authenticode observation failed")
    return parse_authenticode_json(result.stdout)


def _canonical_relative_path(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 240:
        raise ValueError("artifact relative path is invalid")
    if "\\" in value or "\x00" in value or value.startswith("/") or ":" in value:
        raise ValueError("artifact path must use canonical relative POSIX separators")
    path = PurePosixPath(value)
    if path.as_posix() != value or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("artifact path is not canonical")
    for part in path.parts:
        if not (_SINGLE_CHARACTER_SEGMENT.fullmatch(part) or _SEGMENT.fullmatch(part)):
            raise ValueError("artifact path contains an unsupported Windows segment")
        if part.split(".", 1)[0].upper() in _RESERVED_WINDOWS_NAMES:
            raise ValueError("artifact path uses a reserved Windows device name")
    return value


def _expected_directories(artifacts: Sequence[BrokerArtifact]) -> set[str]:
    result: set[str] = set()
    for artifact in artifacts:
        parts = PurePosixPath(artifact.relative_path).parts[:-1]
        for index in range(1, len(parts) + 1):
            result.add(PurePosixPath(*parts[:index]).as_posix().casefold())
    return result


def _validated_install_root(path: Path) -> Path:
    if not path.is_absolute():
        raise ValueError("broker install root must be absolute")
    root = Path(os.path.abspath(path))
    program_files = _program_files_x64()
    try:
        common = Path(os.path.commonpath((root, program_files)))
    except ValueError as error:
        raise ValueError("broker install root is on a different volume") from error
    normalized_root = os.path.normcase(str(root))
    normalized_program_files = os.path.normcase(str(program_files))
    if os.path.normcase(str(common)) != normalized_program_files or (
        normalized_root == normalized_program_files
    ):
        raise ValueError("broker install root must be a strict Program Files descendant")
    if not is_local_fixed_path(root):
        raise ValueError("broker install root must be local fixed with no reparse ancestors")
    metadata = read_file_metadata(root)
    if not metadata.is_directory or metadata.is_reparse_point or metadata.is_cloud_placeholder:
        raise ValueError("broker install root must be an ordinary directory")
    return root


def _validated_ordinary_file(path: Path, *, allow_hardlinks: bool = False) -> Path:
    if not path.is_absolute():
        raise ValueError("artifact path must be absolute")
    ordinary_file = Path(os.path.abspath(path))
    if not is_local_fixed_path(ordinary_file):
        raise ValueError("artifact must be local fixed with no reparse ancestors")
    metadata = read_file_metadata(ordinary_file)
    if metadata.is_directory or metadata.is_reparse_point or metadata.is_cloud_placeholder:
        raise ValueError("artifact must be an ordinary non-reparse file")
    if not allow_hardlinks and metadata.link_count not in {None, 1}:
        raise ValueError("artifact must not have multiple hard links")
    return ordinary_file


def _stable_sha256(path: Path) -> str:
    before_metadata = read_file_metadata(path)
    before_stat = os.stat(path, follow_symlinks=False)
    if before_stat.st_size > _MAX_ARTIFACT_BYTES:
        raise ValueError("artifact exceeds the verification size limit")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        opened_stat = os.fstat(stream.fileno())
        if _stat_changed(before_stat, opened_stat):
            raise RuntimeError("artifact changed before hashing")
        while chunk := stream.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
        closed_stat = os.fstat(stream.fileno())
        if _stat_changed(opened_stat, closed_stat):
            raise RuntimeError("artifact changed during hashing")
    after_stat = os.stat(path, follow_symlinks=False)
    after_metadata = read_file_metadata(path)
    if (
        _stat_changed(before_stat, after_stat)
        or before_metadata.identity != after_metadata.identity
    ):
        raise RuntimeError("artifact identity changed during hashing")
    return digest.hexdigest()


def _stat_changed(expected: os.stat_result, actual: os.stat_result) -> bool:
    return bool(
        (expected.st_dev and actual.st_dev and expected.st_dev != actual.st_dev)
        or (expected.st_ino and actual.st_ino and expected.st_ino != actual.st_ino)
        or expected.st_size != actual.st_size
        or expected.st_mtime_ns != actual.st_mtime_ns
    )


def _require_signature(
    observation: AuthenticodeObservation,
    expected_thumbprint: str,
    label: str,
) -> None:
    if (
        observation.status != "Valid"
        or observation.signature_type != "Authenticode"
        or observation.thumbprint != expected_thumbprint
    ):
        raise ValueError(f"{label} does not have the required valid publisher signature")


def _system_directory() -> Path:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_system_directory = kernel32.GetSystemDirectoryW
    get_system_directory.argtypes = (wintypes.LPWSTR, wintypes.UINT)
    get_system_directory.restype = wintypes.UINT
    buffer = ctypes.create_unicode_buffer(32768)
    length = int(get_system_directory(buffer, len(buffer)))
    if length == 0 or length >= len(buffer):
        raise ctypes.WinError(ctypes.get_last_error())
    return Path(buffer.value)


def _program_files_x64() -> Path:
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    get_known_folder = shell32.SHGetKnownFolderPath
    get_known_folder.argtypes = (
        ctypes.POINTER(_Guid),
        wintypes.DWORD,
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.LPWSTR),
    )
    get_known_folder.restype = ctypes.c_long
    ole32.CoTaskMemFree.argtypes = (wintypes.LPVOID,)
    ole32.CoTaskMemFree.restype = None
    folder_id = _Guid.from_uuid(_FOLDERID_PROGRAM_FILES_X64)
    value = wintypes.LPWSTR()
    status = int(get_known_folder(ctypes.byref(folder_id), 0, None, ctypes.byref(value)))
    text = value.value
    if status < 0 or not text:
        raise OSError(status, "SHGetKnownFolderPath(FOLDERID_ProgramFilesX64) failed")
    try:
        return Path(text)
    finally:
        ole32.CoTaskMemFree(value)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify the artifact-installation subset of future DevClean gate G6."
    )
    parser.add_argument("--install-root", required=True, type=Path)
    parser.add_argument("--installer", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        result = verify_broker_install(
            install_root=args.install_root,
            installer=args.installer,
            manifest_path=args.manifest,
            manifest_sha256=args.manifest_sha256,
        )
        rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if args.output is None:
            sys.stdout.write(rendered)
        else:
            args.output.write_text(rendered, encoding="utf-8", newline="\n")
    except (OSError, PermissionError, RuntimeError, TypeError, ValueError) as error:
        print(f"G6 artifact verification failed: {error}", file=sys.stderr)
        return 1
    return 0


__all__ = [
    "ArtifactKind",
    "AuthenticodeObservation",
    "BrokerArtifact",
    "BrokerInstallManifest",
    "TreeEntry",
    "inventory_install_tree",
    "load_manifest",
    "main",
    "observe_authenticode",
    "parse_authenticode_json",
    "parse_manifest_bytes",
    "probe_mutation_rights",
    "verify_broker_install",
]
