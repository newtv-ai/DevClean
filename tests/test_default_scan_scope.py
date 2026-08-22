from __future__ import annotations

from dataclasses import replace

from devclean.core.scan_scope_migration import _legacy_default_scan_matches
from devclean.core.user_rules import default_rules


def test_packaged_default_scans_audited_roots_not_entire_profile() -> None:
    rules = default_rules()

    assert rules.scan.include_known_cleanup_roots is True
    assert rules.scan.include_user_profile is False


def test_legacy_untouched_whole_profile_default_is_migratable() -> None:
    packaged = default_rules()
    legacy = replace(
        packaged,
        scan=replace(packaged.scan, include_user_profile=True),
    )

    assert _legacy_default_scan_matches(legacy, packaged) is True


def test_custom_scan_scope_is_not_silently_migrated() -> None:
    packaged = default_rules()
    custom = replace(
        packaged,
        scan=replace(
            packaged.scan,
            include_user_profile=True,
            additional_paths=(r"D:\my-explicit-scan-root",),
        ),
    )

    assert _legacy_default_scan_matches(custom, packaged) is False
