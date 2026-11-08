"""Deleting an ordinary downloaded installer is plain file cleanup.

No application owns such a file, so the cleanup guard must not relaunch
package-manager CLIs or process probes for it. Windows resolves those tools by
bare name, starting in DevClean.exe's own folder, which is usually Downloads.
System installer caches stay protected.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

import devclean.core.cleanup_journal as cleanup_journal
import devclean.core.postscan_cleanup as postscan_cleanup
import devclean.core.visual_studio_installer_cleanup as visual_studio_installer_cleanup
from devclean.core.application_cleanup import (
    DecisionOwner,
    clear_process_cache,
    match_application_rule,
    process_guard_allows,
)
from devclean.core.cleanup_catalog import CleanupCategory, SourceDomain
from devclean.core.cleanup_journal import ActionState, CleanupJournal, CleanupMode
from devclean.core.triage import (
    Actionability,
    EvidenceKind,
    ExecutionPolicy,
    RecoveryCapability,
    ReviewLane,
    RiskTier,
    TriageItem,
    triage_file,
)
from devclean.core.user_rules import default_rules
from devclean.platform.windows.exact_cleanup import ExactMutationResult
from devclean.platform.windows.filesystem import FileSystemMetadata
from devclean.platform.windows.registry import RegistryStringLookup
from devclean.scanner import ScanOptions, scan_roots
from devclean.scanner.filesystem import ScanRecord, ScanRecordKind


@pytest.fixture
def launches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[list[tuple[str, ...]]]:
    """Record every attempted process start; nothing real is ever run."""

    started: list[tuple[str, ...]] = []

    class RecordingPopen:
        def __init__(self, args: object, *_args: object, **_kwargs: object) -> None:
            argv = args if isinstance(args, (list, tuple)) else (args,)
            started.append(tuple(str(part) for part in argv))
            raise FileNotFoundError(started[-1][0])

    def no_cache_path_policy(_keys: Iterable[str], _value_name: str) -> RegistryStringLookup:
        return RegistryStringLookup(None, True)

    monkeypatch.setattr(subprocess, "Popen", RecordingPopen)
    monkeypatch.setattr(
        visual_studio_installer_cleanup,
        "first_hklm_string_value",
        no_cache_path_policy,
    )
    home = tmp_path / "home"
    for name, value in {
        "USERPROFILE": home,
        "APPDATA": home / "AppData" / "Roaming",
        "LOCALAPPDATA": home / "AppData" / "Local",
        "TEMP": home / "AppData" / "Local" / "Temp",
        "TMP": home / "AppData" / "Local" / "Temp",
        "PROGRAMDATA": tmp_path / "ProgramData",
        "DEVCLEAN_DATA_DIR": tmp_path / "DevClean-data",
    }.items():
        monkeypatch.setenv(name, str(value))
    (home / "Downloads").mkdir(parents=True)
    clear_process_cache()
    yield started
    clear_process_cache()


def _metadata(path: Path) -> FileSystemMetadata:
    directory = path.is_dir()
    return FileSystemMetadata(
        is_directory=directory,
        logical_size=0 if directory else 10,
        allocation_size=0 if directory else 10,
        volume_serial=9,
        file_id=f"{abs(hash(str(path))):032x}"[-32:],
        file_id_kind="file_id_128",
        link_count=1,
        attributes=0x10 if directory else 0,
        reparse_tag=None,
        is_reparse_point=False,
        is_cloud_placeholder=False,
        creation_time_ns=100,
        last_write_time_ns=200,
    )


def _approved_download(path: Path, root: Path) -> TriageItem:
    """An installer archive the user approved after AI review."""

    metadata = _metadata(path)
    record = ScanRecord(
        root=str(root),
        path=str(path),
        kind=ScanRecordKind.FILE,
        depth=2,
        logical_size=metadata.logical_size,
        allocated_size=metadata.allocation_size,
        raw_allocated_size=metadata.allocation_size,
        volume_serial=metadata.volume_serial,
        file_id=metadata.file_id,
        file_id_kind=metadata.file_id_kind,
        link_count=metadata.link_count,
        attributes=metadata.attributes,
        creation_time_ns=metadata.creation_time_ns,
        last_write_time_ns=metadata.last_write_time_ns,
    )
    return TriageItem(
        record=record,
        path=str(path),
        logical_size=10,
        allocated_size=10,
        category=CleanupCategory.INSTALLERS_DOWNLOADS,
        source_domain=SourceDomain.INSTALLERS_DOWNLOADS,
        lane=ReviewLane.AI_REVIEW,
        risk_tier=RiskTier.MEDIUM,
        evidence_kind=EvidenceKind.FILESYSTEM_OBSERVATION,
        actionability=Actionability.AI_REVIEW,
        execution_policy=ExecutionPolicy.USER_CHOICE_DELETE,
        recovery=RecoveryCapability.UNKNOWN,
        reason="user-approved expired installer archive",
    )


def test_guard_allows_an_ordinary_installer_without_starting_any_process(
    tmp_path: Path,
    launches: list[tuple[str, ...]],
) -> None:
    installer = tmp_path / "home" / "Downloads" / "node-v18.20.0-x64.msi"
    # The completed scan already classified this path; no application owns it.
    assert match_application_rule(installer) is None
    launches.clear()

    assert process_guard_allows(installer)
    assert launches == []


def test_deleting_an_approved_downloaded_installer_starts_no_process(
    tmp_path: Path,
    launches: list[tuple[str, ...]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    installer = home / "Downloads" / "OldApp-Setup-1.0.zip"
    installer.write_bytes(b"expired installer archive")
    item = _approved_download(installer, home)
    rules = default_rules()
    scanned = triage_file(
        item.record,
        delete_config=rules.delete.classification,
        keep_config=rules.keep.classification,
        now=datetime.now(UTC),
        temp_root=home / "AppData" / "Local" / "Temp",
    )
    assert scanned.category is CleanupCategory.INSTALLERS_DOWNLOADS
    assert not any(tag.startswith("application:") for tag in scanned.tags)

    present = {str(installer): True}

    def read(path: Path) -> FileSystemMetadata:
        if not present.get(str(path), True):
            raise FileNotFoundError(str(path))
        return _metadata(Path(path))

    monkeypatch.setattr(postscan_cleanup, "read_file_metadata", read)
    monkeypatch.setattr(postscan_cleanup, "is_local_fixed_path", lambda _path: True)
    monkeypatch.setattr(cleanup_journal, "is_local_fixed_path", lambda _path: True)
    monkeypatch.setattr(cleanup_journal, "secure_private_directory", lambda _path: None)
    monkeypatch.setattr(cleanup_journal, "secure_private_file", lambda _path: None)
    candidate = postscan_cleanup.candidate_from_triage_item(
        item,
        delete_config=rules.delete.classification,
        keep_config=rules.keep.classification,
        review_approved=True,
    )
    purged: list[Path] = []

    def purge(path: Path, _snapshot: object, _boundary: object) -> ExactMutationResult:
        purged.append(path)
        present[str(path)] = False
        return ExactMutationResult(str(path), None, True, False, False)

    launches.clear()
    result = postscan_cleanup.execute_cleanup_batch(
        postscan_cleanup.prepare_cleanup_batch((candidate,)),
        CleanupMode.PERMANENT,
        journal=CleanupJournal(tmp_path / "cleanup.db"),
        purger=purge,
        delete_config=rules.delete.classification,
        keep_config=rules.keep.classification,
    )

    assert [state for _action, state in result.action_states] == [ActionState.PURGED]
    assert purged == [installer]
    assert launches == []


def test_visual_studio_installer_package_cache_stays_protected(
    tmp_path: Path,
    launches: list[tuple[str, ...]],
) -> None:
    payload = (
        tmp_path
        / "ProgramData"
        / "Microsoft"
        / "VisualStudio"
        / "Packages"
        / "Microsoft.VisualStudio.Example,version=17.0.0"
        / "payload.vsix"
    )
    rule = match_application_rule(payload)
    assert rule is not None
    assert rule.owner is DecisionOwner.KEEP

    assert not process_guard_allows(payload)
    rules = default_rules()
    item = triage_file(
        ScanRecord(
            root=str(tmp_path),
            path=str(payload),
            kind=ScanRecordKind.FILE,
            depth=6,
            logical_size=10,
            last_write_time_ns=1,
        ),
        delete_config=rules.delete.classification,
        keep_config=rules.keep.classification,
        now=datetime.now(UTC),
    )
    assert item.lane is ReviewLane.REPORT_ONLY
    assert item.execution_policy is ExecutionPolicy.NONE


def test_windows_installer_cache_is_protected_by_location_not_by_folder_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    msi_header = bytes.fromhex("D0CF11E0A1B11AE1") + b"\x00" * 504
    windows = tmp_path / "Windows"
    cached_package = windows / "Installer" / "1a2b3c.msi"
    cached_package.parent.mkdir(parents=True)
    cached_package.write_bytes(msi_header)
    home = tmp_path / "home"
    downloaded = home / "Downloads" / "Installer" / "old-tool-1.0.msi"
    downloaded.parent.mkdir(parents=True)
    downloaded.write_bytes(msi_header)
    monkeypatch.setenv("SYSTEMROOT", str(windows))
    monkeypatch.setenv("USERPROFILE", str(home))
    rules = default_rules()

    records = {
        Path(record.path): record
        for root in (windows, home)
        for record in scan_roots(
            (root,),
            ScanOptions(
                exact_file_identity=False,
                skip_directory_names=frozenset(
                    name.casefold() for name in rules.scan.skip_directory_names
                ),
            ),
        )
    }
    classified = {
        path: triage_file(
            records[path],
            delete_config=rules.delete.classification,
            keep_config=rules.keep.classification,
            now=datetime.now(UTC),
            temp_root=tmp_path / "Temp",
        )
        for path in (cached_package, downloaded)
    }

    cache_item = classified[cached_package]
    assert cache_item.lane is ReviewLane.REPORT_ONLY
    assert "system_installer_cache" in cache_item.tags
    assert "Windows Installer" in cache_item.reason
    download_item = classified[downloaded]
    assert download_item.lane is ReviewLane.DETERMINISTIC_CANDIDATE
    assert download_item.execution_policy is ExecutionPolicy.USER_CHOICE_DELETE
