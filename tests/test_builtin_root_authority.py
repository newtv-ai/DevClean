from __future__ import annotations

from devclean.core.rule_schema import CleanupPolicy
from devclean.core.user_rules import default_rules


def _root_policy(rule_id: str) -> CleanupPolicy:
    rules = default_rules()
    match = next(root for root in rules.scan.known_cleanup_roots if root.rule_id == rule_id)
    return CleanupPolicy(match.policy)


def test_windows_old_requires_user_review_and_has_no_whole_root_authority() -> None:
    rules = default_rules()

    assert _root_policy("windows-old") is CleanupPolicy.MANUAL_REVIEW
    assert "windows-old" not in rules.scan.delete_root_ids


def test_mixed_windows_maintenance_roots_are_not_universal_delete_targets() -> None:
    assert _root_policy("windows-maintenance") is CleanupPolicy.MANUAL_REVIEW
    assert _root_policy("windows-update-downloads") is CleanupPolicy.MANUAL_REVIEW


def test_system_crash_dumps_require_user_review() -> None:
    assert _root_policy("system-crash-dumps") is CleanupPolicy.MANUAL_REVIEW


def test_user_model_store_is_not_a_vendor_managed_cache() -> None:
    assert _root_policy("lmstudio") is CleanupPolicy.MANUAL_REVIEW


def test_broad_unowned_cache_parents_do_not_receive_deterministic_authority() -> None:
    assert _root_policy("ide-working-caches") is CleanupPolicy.MANUAL_REVIEW
    assert _root_policy("general-tool-caches") is CleanupPolicy.MANUAL_REVIEW
