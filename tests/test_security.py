from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import reclaimer.platform.windows.security as security_module
from reclaimer.platform.windows.security import (
    audit_private_directory,
    audit_private_file,
    secure_private_directory,
    secure_private_file,
)

EVERYONE_SID = "S-1-1-0"
SYSTEM_SID = "S-1-5-18"
BUILTIN_ADMINISTRATORS_SID = "S-1-5-32-544"
BUILTIN_USERS_SID = "S-1-5-32-545"


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL integration test")
def test_secure_private_directory_sets_exact_protected_dacl_and_keeps_owner(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "private"
    directory.mkdir()
    owner_before = audit_private_directory(directory).owner_sid

    audit = secure_private_directory(directory)

    assert audit.platform == "windows"
    assert audit.protected
    assert audit.dacl_present
    assert audit.policy_satisfied
    assert audit.owner_sid == owner_before
    assert audit.current_user_sid is not None
    assert set(audit.allowed_sids) == {
        audit.current_user_sid,
        SYSTEM_SID,
        BUILTIN_ADMINISTRATORS_SID,
    }
    assert EVERYONE_SID not in audit.allowed_sids
    assert BUILTIN_USERS_SID not in audit.allowed_sids
    assert all(entry.grants_full_control for entry in audit.entries)
    assert all(not entry.inherited for entry in audit.entries)
    assert all(entry.ace_flags & 0x03 == 0x03 for entry in audit.entries)

    repeated = secure_private_directory(directory)
    assert repeated.policy_satisfied
    assert repeated.entries == audit.entries


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL inheritance test")
def test_private_directory_children_inherit_only_the_private_allowlist(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "private"
    directory.mkdir()
    parent_audit = secure_private_directory(directory)
    child = directory / "child"
    child.mkdir()

    child_audit = audit_private_directory(child)

    assert set(child_audit.allowed_sids) == set(parent_audit.expected_sids)
    assert EVERYONE_SID not in child_audit.allowed_sids
    assert BUILTIN_USERS_SID not in child_audit.allowed_sids
    assert child_audit.protected is False
    assert all(entry.inherited for entry in child_audit.entries)


def test_private_directory_requires_absolute_existing_directory(tmp_path: Path) -> None:
    ordinary_file = tmp_path / "file.bin"
    ordinary_file.write_bytes(b"data")

    with pytest.raises(ValueError, match="absolute"):
        secure_private_directory(Path("relative-state"))
    with pytest.raises(FileNotFoundError):
        secure_private_directory(tmp_path / "missing")
    with pytest.raises(ValueError, match="directory"):
        secure_private_directory(ordinary_file)


@pytest.mark.skipif(os.name != "nt", reason="Windows file DACL integration test")
def test_secure_private_file_sets_exact_non_inheritable_dacl(tmp_path: Path) -> None:
    directory = tmp_path / "private"
    directory.mkdir()
    ordinary_file = directory / "state.db"
    ordinary_file.write_bytes(b"state")
    owner_before = audit_private_file(ordinary_file).owner_sid

    audit = secure_private_file(ordinary_file)

    assert audit.platform == "windows"
    assert audit.protected
    assert audit.dacl_present
    assert audit.policy_satisfied
    assert audit.owner_sid == owner_before
    assert audit.current_user_sid is not None
    assert set(audit.allowed_sids) == {
        audit.current_user_sid,
        SYSTEM_SID,
        BUILTIN_ADMINISTRATORS_SID,
    }
    assert EVERYONE_SID not in audit.allowed_sids
    assert BUILTIN_USERS_SID not in audit.allowed_sids
    assert all(entry.grants_full_control for entry in audit.entries)
    assert all(not entry.inherited for entry in audit.entries)
    assert all(entry.ace_flags & 0x03 == 0 for entry in audit.entries)


def test_private_file_rejects_directories_and_hard_links(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="ordinary file"):
        secure_private_file(tmp_path)

    ordinary_file = tmp_path / "state.db"
    alias = tmp_path / "state-alias.db"
    ordinary_file.write_bytes(b"state")
    os.link(ordinary_file, alias)
    with pytest.raises(ValueError, match="hard links"):
        secure_private_file(ordinary_file)


def test_private_directory_security_failure_is_not_downgraded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "private"
    directory.mkdir()

    if os.name == "nt":
        def fail_acl(_directory: Path, _sid: str) -> None:
            raise PermissionError("WRITE_DAC denied")

        monkeypatch.setattr(security_module, "_set_windows_private_dacl", fail_acl)
    else:
        def fail_chmod(*args, **kwargs) -> None:
            raise PermissionError("chmod denied")

        monkeypatch.setattr(security_module.os, "chmod", fail_chmod)

    with pytest.raises(PermissionError, match="denied"):
        secure_private_directory(directory)


def test_private_directory_reparse_boundary_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "target"
    redirected = tmp_path / "redirected"
    target.mkdir()

    if os.name == "nt":
        created = subprocess.run(
            ["cmd", "/d", "/c", "mklink", "/J", str(redirected), str(target)],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if created.returncode != 0:
            pytest.skip(f"junction creation unavailable: {created.stderr or created.stdout}")
    else:
        redirected.symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError, match="reparse"):
        secure_private_directory(redirected)
    with pytest.raises(ValueError, match="reparse"):
        audit_private_directory(redirected)


@pytest.mark.skipif(os.name == "nt", reason="POSIX fallback test")
def test_non_windows_fallback_sets_owner_only_mode(tmp_path: Path) -> None:
    directory = tmp_path / "private"
    directory.mkdir(mode=0o777)

    audit = secure_private_directory(directory)

    assert audit.platform == "posix"
    assert audit.posix_mode == 0o700
    assert audit.policy_satisfied


@pytest.mark.skipif(os.name == "nt", reason="POSIX fallback test")
def test_non_windows_private_file_fallback_sets_owner_only_mode(tmp_path: Path) -> None:
    ordinary_file = tmp_path / "state.db"
    ordinary_file.write_bytes(b"state")
    ordinary_file.chmod(0o666)

    audit = secure_private_file(ordinary_file)

    assert audit.platform == "posix"
    assert audit.posix_mode == 0o600
    assert audit.policy_satisfied
