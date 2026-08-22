from __future__ import annotations

from devclean.core.user_rules import default_rules


def test_packaged_default_scans_audited_roots_not_entire_profile() -> None:
    rules = default_rules()

    assert rules.scan.include_known_cleanup_roots is True
    assert rules.scan.include_user_profile is False
