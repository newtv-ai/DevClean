from __future__ import annotations

import json
from pathlib import Path

import pytest

from devclean.core import ai_sessions
from devclean.core.ai_review_contract import (
    AI_REVIEW_RESPONSE_TYPE,
    AiRecommendation,
    AiReviewCandidateInput,
    AiReviewContractError,
    AiReviewPackage,
    AiReviewSimilarityGroup,
    build_ai_review_package,
    parse_ai_review_response,
    parse_partial_ai_review_response,
)
from devclean.core.cleanup_catalog import CleanupCategory, SourceDomain
from devclean.core.triage import (
    Actionability,
    EvidenceKind,
    ExecutionPolicy,
    RecoveryCapability,
    ReviewLane,
    RiskTier,
    TriageItem,
)
from devclean.core.user_rules import (
    RuleDecision,
    RuleMatch,
    add_ai_verdicts,
    load_rules,
)
from devclean.scanner.filesystem import ScanRecord, ScanRecordKind
from devclean.ui import app


def _item(path: str, *, size: int = 10) -> TriageItem:
    root = str(Path(path).parent)
    record = ScanRecord(
        root=root,
        path=path,
        kind=ScanRecordKind.FILE,
        depth=1,
        logical_size=size,
        allocated_size=size,
        raw_allocated_size=size,
        volume_serial=7,
        file_id=f"{size:032x}",
        file_id_kind="file_id_128",
        link_count=1,
        attributes=0,
        creation_time_ns=100,
        last_write_time_ns=200,
    )
    return TriageItem(
        record=record,
        path=path,
        logical_size=size,
        allocated_size=size,
        category=CleanupCategory.OTHER,
        source_domain=SourceDomain.GENERAL_STORAGE,
        lane=ReviewLane.AI_REVIEW,
        risk_tier=RiskTier.HIGH,
        evidence_kind=EvidenceKind.FILESYSTEM_OBSERVATION,
        actionability=Actionability.AI_REVIEW,
        execution_policy=ExecutionPolicy.USER_CHOICE_DELETE,
        recovery=RecoveryCapability.UNKNOWN,
        reason="unknown file",
    )


def _response(package: AiReviewPackage, rows: list[dict[str, str]]) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "document_type": AI_REVIEW_RESPONSE_TYPE,
            "review_session_id": package.review_session_id,
            "nonce": package.nonce,
            "package_digest": package.package_digest,
            "recommendations": rows,
        }
    )


def test_same_run_import_requires_every_candidate_exactly_once() -> None:
    package = build_ai_review_package(
        (
            AiReviewCandidateInput(_item(r"G:\scan\a.bin", size=10), False),
            AiReviewCandidateInput(_item(r"G:\scan\b.bin", size=11), False),
        ),
        scan_session_id="scan-1",
    )
    first = package.entries[0]
    partial = _response(
        package,
        [
            {
                "candidate_id": first.candidate_id,
                "recommendation": "DELETE",
                "reason": "regenerable cache",
            }
        ],
    )

    with pytest.raises(AiReviewContractError, match="every candidate"):
        parse_ai_review_response(partial, package)


def test_current_delete_token_imports_and_old_token_is_rejected() -> None:
    package = build_ai_review_package(
        (AiReviewCandidateInput(_item(r"G:\scan\a.bin"), False),),
        scan_session_id="scan-2",
    )
    entry = package.entries[0]
    valid = _response(
        package,
        [
            {
                "candidate_id": entry.candidate_id,
                "recommendation": "DELETE",
                "reason": "safe to recreate",
            }
        ],
    )
    imported = parse_ai_review_response(valid, package)
    assert imported.recommendations[0].recommendation is AiRecommendation.DELETE

    old = valid.replace('"DELETE"', '"RECOMMEND_RECYCLE"')
    with pytest.raises(AiReviewContractError, match="unknown recommendation"):
        parse_ai_review_response(old, package)


def test_similar_file_group_is_explicit_and_covered_by_package_digest() -> None:
    group = AiReviewSimilarityGroup(
        filename_pattern="urlsoceng.store.4_*",
        member_count=12,
        total_logical_size_bytes=1_200,
        minimum_logical_size_bytes=50,
        maximum_logical_size_bytes=200,
        oldest_last_write_time_ns=100,
        newest_last_write_time_ns=300,
    )
    package = build_ai_review_package(
        (
            AiReviewCandidateInput(
                _item(
                    r"G:\scan\urlsoceng.store.4_13429459072546848",
                    size=100,
                ),
                False,
                similar_group=group,
            ),
        ),
        scan_session_id="scan-group",
    )

    metadata = package.entries[0].model_metadata
    assert metadata["similar_path_group"] == {
        "decision_scope": "ALL_GROUP_MEMBERS",
        "filename_pattern": "urlsoceng.store.4_*",
        "member_count": 12,
        "total_logical_size_bytes": 1_200,
        "minimum_logical_size_bytes": 50,
        "maximum_logical_size_bytes": 200,
        "oldest_last_write_time_ns": 100,
        "newest_last_write_time_ns": 300,
    }
    instructions = package.payload()["instructions"]
    assert isinstance(instructions, list)
    assert any(
        isinstance(instruction, str) and "uniformly to all members" in instruction
        for instruction in instructions
    )


def test_restart_import_allows_strict_partial_coverage_only() -> None:
    package = build_ai_review_package(
        (
            AiReviewCandidateInput(_item(r"G:\scan\a.bin", size=10), False),
            AiReviewCandidateInput(_item(r"G:\scan\b.bin", size=11), False),
        ),
        scan_session_id="scan-3",
    )
    paths = {entry.candidate_id: entry.item.path for entry in package.entries}
    first = package.entries[0]
    text = _response(
        package,
        [
            {
                "candidate_id": first.candidate_id,
                "recommendation": "KEEP",
                "reason": "belongs to the application",
            }
        ],
    )

    result = parse_partial_ai_review_response(
        text,
        expected_session_id=package.review_session_id,
        expected_nonce=package.nonce,
        expected_package_digest=package.package_digest,
        candidate_paths=paths,
    )
    assert result == ((first.item.path, AiRecommendation.KEEP, "belongs to the application"),)

    tampered = json.loads(text)
    tampered["nonce"] = "0" * 64
    with pytest.raises(AiReviewContractError, match="nonce mismatch"):
        parse_partial_ai_review_response(
            json.dumps(tampered),
            expected_session_id=package.review_session_id,
            expected_nonce=package.nonce,
            expected_package_digest=package.package_digest,
            candidate_paths=paths,
        )


def test_export_index_survives_restart_and_forgets_consumed_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEVCLEAN_DATA_DIR", str(tmp_path / "data"))
    session = "review_" + "1" * 32
    nonce = "2" * 64
    digest = "3" * 64
    paths = {"candidate_" + "4" * 32: r"G:\scan\a.bin"}

    ai_sessions.remember_export(session, nonce, digest, paths)
    recalled = ai_sessions.recall_export(session)
    assert recalled is not None
    assert recalled.candidate_paths == paths
    assert recalled.candidate_members == {
        candidate_id: (path,) for candidate_id, path in paths.items()
    }

    ai_sessions.forget_export(session)
    assert ai_sessions.recall_export(session) is None


def test_grouped_restart_import_expands_contextual_answer_to_every_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEVCLEAN_DATA_DIR", str(tmp_path / "DevClean-data"))
    paths = (
        r"G:\scan\build-12345678.log",
        r"G:\scan\build-87654321.log",
    )
    group = AiReviewSimilarityGroup(
        filename_pattern="build-*.log",
        member_count=2,
        total_logical_size_bytes=20,
        minimum_logical_size_bytes=10,
        maximum_logical_size_bytes=10,
        oldest_last_write_time_ns=200,
        newest_last_write_time_ns=200,
    )
    package = build_ai_review_package(
        (
            AiReviewCandidateInput(
                _item(paths[0]),
                False,
                similar_group=group,
            ),
        ),
        scan_session_id="scan-group-restart",
    )
    entry = package.entries[0]
    ai_sessions.remember_export(
        package.review_session_id,
        package.nonce,
        package.package_digest,
        {entry.candidate_id: paths[0]},
        {entry.candidate_id: paths},
    )
    response = _response(
        package,
        [
            {
                "candidate_id": entry.candidate_id,
                "recommendation": "DELETE",
                "reason": "这些日志超过 30 天未使用并且可以安全删除",
            }
        ],
    )

    session, complete, recovered = app._verdicts_from_session_index(response)
    assert session == package.review_session_id
    assert complete is True
    assert recovered == {
        path: ("DELETE", "这些日志超过 30 天未使用并且可以安全删除") for path in paths
    }

    baseline = load_rules()
    baseline_exact = sum(rule.match is RuleMatch.EXACT_PATH for rule in baseline.delete.rules)
    baseline_templates = {
        rule.value for rule in baseline.delete.rules if rule.match is RuleMatch.PATH_GLOB
    }
    updated = add_ai_verdicts(
        baseline,
        [(path, RuleDecision(verdict), reason) for path, (verdict, reason) in recovered.items()],
    )
    assert all(updated.decision_for(path) is RuleDecision.DELETE for path in paths)
    assert (
        sum(rule.match is RuleMatch.EXACT_PATH for rule in updated.delete.rules)
        == baseline_exact + 2
    )
    assert {
        rule.value for rule in updated.delete.rules if rule.match is RuleMatch.PATH_GLOB
    } == baseline_templates


def test_grouped_same_run_import_expands_to_every_member() -> None:
    paths = (
        r"G:\scan\build-12345678.log",
        r"G:\scan\build-87654321.log",
    )
    group = AiReviewSimilarityGroup(
        filename_pattern="build-*.log",
        member_count=2,
        total_logical_size_bytes=20,
        minimum_logical_size_bytes=10,
        maximum_logical_size_bytes=10,
        oldest_last_write_time_ns=200,
        newest_last_write_time_ns=200,
    )
    package = build_ai_review_package(
        (
            AiReviewCandidateInput(
                _item(paths[0]),
                False,
                similar_group=group,
            ),
        ),
        scan_session_id="scan-group-live",
    )
    entry = package.entries[0]
    imported = parse_ai_review_response(
        _response(
            package,
            [
                {
                    "candidate_id": entry.candidate_id,
                    "recommendation": "KEEP",
                    "reason": "这些文件属于同一组并且都应保留",
                }
            ],
        ),
        package,
    )

    assert app._expanded_live_verdicts(
        imported,
        {entry.candidate_id: paths},
    ) == tuple((path, "KEEP", "这些文件属于同一组并且都应保留") for path in paths)
