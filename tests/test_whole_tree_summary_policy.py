from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from devclean.core.application_cleanup import (
    ApplicationCleanupRule,
    DecisionOwner,
    LastUseStrategy,
    MatchKind,
    RebuildCost,
)
from devclean.core.cleanup_catalog import CleanupCategory, CleanupPolicy, KnownCleanupRoot
from devclean.core.whole_tree_policy import (
    WholeTreePolicyEvidence,
    WholeTreePolicyRefusal,
    assess_application_whole_tree_policy,
)

_MIB = 1024**2


def _known(path: Path) -> KnownCleanupRoot:
    rule = ApplicationCleanupRule(
        rule_id="test-summary-rule",
        app_id="test",
        root_key="TEST",
        relative_pattern="cache",
        match_kind=MatchKind.PREFIX,
        owner=DecisionOwner.TOOL,
        last_use=LastUseStrategy.FILE_MTIME,
        rebuild_cost=RebuildCost.LOW,
        idle_days=7,
        min_reclaim_bytes=16 * _MIB,
        allow_whole_tree=True,
        label="Test cache",
    )
    return KnownCleanupRoot(
        path=path,
        category=CleanupCategory.OTHER,
        policy=CleanupPolicy.VENDOR_MANAGED,
        label="Test cache",
        delete_root_itself=True,
        application_rule=rule,
    )


def _evidence(*, size: int, age_days: int) -> WholeTreePolicyEvidence:
    observed = datetime.now(UTC) - timedelta(days=age_days)
    return WholeTreePolicyEvidence(
        files=10,
        logical_bytes=size,
        latest_activity_time_ns=int(observed.timestamp() * 1_000_000_000),
    )


def test_precomputed_summary_uses_same_idle_rule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import devclean.core.whole_tree_policy as policy

    root = tmp_path / "cache"
    known = _known(root)
    monkeypatch.setattr(policy, "evaluate_application_path", lambda *_args, **_kwargs: None)

    with pytest.raises(WholeTreePolicyRefusal, match="too recently"):
        assess_application_whole_tree_policy(
            root,
            (known,),
            _evidence(size=64 * _MIB, age_days=1),
        )

    accepted = assess_application_whole_tree_policy(
        root,
        (known,),
        _evidence(size=64 * _MIB, age_days=30),
    )
    assert accepted is not None
    assert accepted.logical_bytes == 64 * _MIB
