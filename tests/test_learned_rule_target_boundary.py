from __future__ import annotations

from pathlib import Path

import pytest

from devclean.core.cleanup_catalog import CleanupCategory, SourceDomain
from devclean.core.triage import (
    Actionability,
    CleanupTargetKind,
    DirectoryScope,
    EvidenceKind,
    ExecutionPolicy,
    RecoveryCapability,
    ReviewLane,
    RiskTier,
    TriageItem,
    TriageSession,
)
from devclean.core.user_rules import (
    RuleDecision,
    UserRules,
    add_ai_verdicts,
    add_user_directory_verdicts,
    default_rules,
)
from devclean.scanner.filesystem import ScanRecord, ScanRecordKind
from devclean.ui import app


def _item(path: str, *, directory: bool) -> TriageItem:
    kind = ScanRecordKind.DIRECTORY if directory else ScanRecordKind.FILE
    record = ScanRecord(
        root=str(Path(path).parent),
        path=path,
        kind=kind,
        depth=1,
        logical_size=10,
        allocated_size=10,
        raw_allocated_size=10,
        volume_serial=7,
        file_id="1" * 32,
        file_id_kind="file_id_128",
        link_count=1,
        attributes=0,
        creation_time_ns=100,
        last_write_time_ns=200,
    )
    return TriageItem(
        record=record,
        path=path,
        logical_size=10,
        allocated_size=10,
        category=CleanupCategory.OTHER,
        source_domain=SourceDomain.GENERAL_STORAGE,
        lane=ReviewLane.USER_REVIEW if directory else ReviewLane.AI_REVIEW,
        risk_tier=RiskTier.MEDIUM if directory else RiskTier.HIGH,
        evidence_kind=EvidenceKind.FILESYSTEM_OBSERVATION,
        actionability=Actionability.USER_REVIEW if directory else Actionability.AI_REVIEW,
        execution_policy=ExecutionPolicy.USER_CHOICE_DELETE,
        recovery=RecoveryCapability.UNKNOWN,
        reason="explicit review candidate",
        target_kind=(CleanupTargetKind.DIRECTORY if directory else CleanupTargetKind.FILE),
        directory_scope=DirectoryScope.REGENERABLE_TOOL_OUTPUT if directory else None,
    )


def _partition(
    item: TriageItem, rules: UserRules
) -> tuple[tuple[TriageItem, ...], tuple[TriageItem, ...]]:
    session = TriageSession(review_sample_per_category=10)
    session.add(item)
    return app._partition_items(session, rules)


def test_learned_file_delete_still_promotes_a_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEVCLEAN_DATA_DIR", str(tmp_path / "data"))
    path = r"G:\scratch\opaque.bin"
    rules = add_ai_verdicts(
        default_rules(),
        [(path, RuleDecision.DELETE, "confirmed common disposable file")],
    )

    deletable, unsure = _partition(_item(path, directory=False), rules)

    assert [item.path for item in deletable] == [path]
    assert unsure == ()


def test_learned_file_delete_cannot_promote_same_named_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEVCLEAN_DATA_DIR", str(tmp_path / "data"))
    path = r"G:\scratch\opaque.bin"
    rules = add_ai_verdicts(
        default_rules(),
        [(path, RuleDecision.DELETE, "confirmed common disposable file")],
    )

    deletable, unsure = _partition(_item(path, directory=True), rules)

    assert deletable == ()
    assert [item.path for item in unsure] == [path]
    assert rules.directory_decision_for(path) is None


def test_explicit_directory_delete_is_exact_and_actionable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEVCLEAN_DATA_DIR", str(tmp_path / "data"))
    path = r"G:\scratch\review-this-directory"
    rules = add_user_directory_verdicts(
        default_rules(),
        [(path, RuleDecision.DELETE, "user explicitly chose this directory")],
    )

    deletable, unsure = _partition(_item(path, directory=True), rules)

    assert [item.path for item in deletable] == [path]
    assert unsure == ()
    assert rules.directory_decision_for(path) is RuleDecision.DELETE
    assert rules.decision_for(path) is None


def test_explicit_directory_keep_does_not_hide_same_path_as_a_file_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEVCLEAN_DATA_DIR", str(tmp_path / "data"))
    path = r"G:\scratch\review-this-directory"
    rules = add_user_directory_verdicts(
        default_rules(),
        [(path, RuleDecision.KEEP, "user explicitly kept this directory")],
    )

    deletable, unsure = _partition(_item(path, directory=True), rules)

    assert deletable == ()
    assert unsure == ()
    assert rules.directory_decision_for(path) is RuleDecision.KEEP
    assert rules.decision_for(path) is None


def test_kept_directory_overrides_descendant_learned_file_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEVCLEAN_DATA_DIR", str(tmp_path / "data"))
    directory = r"G:\scratch\keep-this-directory"
    child = directory + r"\disposable.bin"
    rules = add_ai_verdicts(
        default_rules(),
        [(child, RuleDecision.DELETE, "learned file delete")],
    )
    rules = add_user_directory_verdicts(
        rules,
        [(directory, RuleDecision.KEEP, "user explicitly kept this directory")],
    )

    deletable, unsure = _partition(_item(child, directory=False), rules)

    assert deletable == ()
    assert unsure == ()


def test_product_file_rule_never_authorizes_same_named_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEVCLEAN_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\person\AppData\Local")
    path = r"C:\Users\person\AppData\Local\NVIDIA\DXCache\sample.bin"
    rules = default_rules()

    assert rules.decision_for(path) is RuleDecision.DELETE
    deletable, unsure = _partition(_item(path, directory=True), rules)

    assert deletable == ()
    assert [item.path for item in unsure] == [path]
    assert rules.directory_decision_for(path) is None
