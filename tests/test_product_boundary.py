from __future__ import annotations

from pathlib import Path

from devclean.core.application_cleanup import (
    ApplicationCleanupRule,
    DecisionOwner,
    LastUseStrategy,
    MatchKind,
    RebuildCost,
)
from devclean.core.cleanup_catalog import CleanupCategory, CleanupPolicy, KnownCleanupRoot
from devclean.core.user_rules import default_rules
from devclean.ui.modern_app import smart_scan_targets


def test_smart_scan_does_not_invent_rebuild_cost_policy(tmp_path: Path) -> None:
    """A TOOL rule stays discoverable even when its rebuild cost is HIGH.

    Whether that exact item is currently deletable is the rule/evaluator's job;
    the UI must not silently replace that policy with a blanket exclusion.
    """

    root = tmp_path / "audited-index"
    root.mkdir()
    rule = ApplicationCleanupRule(
        rule_id="test-high-cost-tool-rule",
        app_id="test",
        root_key="TEST",
        relative_pattern="audited-index",
        match_kind=MatchKind.PREFIX,
        owner=DecisionOwner.TOOL,
        last_use=LastUseStrategy.FILE_MTIME,
        rebuild_cost=RebuildCost.HIGH,
        idle_days=30,
        allow_whole_tree=True,
        label="Audited high-cost tool data",
    )
    known = KnownCleanupRoot(
        path=root,
        category=CleanupCategory.OTHER,
        policy=CleanupPolicy.VENDOR_MANAGED,
        label="audited",
        delete_root_itself=True,
        application_rule=rule,
    )

    targets = smart_scan_targets((known,), (), default_rules())

    assert root.resolve() in targets
