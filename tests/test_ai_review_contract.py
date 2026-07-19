from __future__ import annotations

import ast
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import devclean.core.ai_review_contract as contract_module
from devclean.core.ai_review_contract import (
    AI_REVIEW_IMPORT_TYPE,
    AI_REVIEW_REQUEST_TYPE,
    AI_REVIEW_RESPONSE_TYPE,
    MAX_AI_REASON_CHARS,
    MAX_AI_REVIEW_ITEMS,
    AiRecommendation,
    AiReviewCandidateInput,
    AiReviewContractError,
    build_ai_review_package,
    parse_ai_review_response,
    response_template,
    serialize_ai_review_package,
    validate_ai_review_package_text,
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
from devclean.scanner import ScanRecord, ScanRecordKind

NOW = datetime(2026, 7, 16, 8, 30, tzinfo=UTC)


def _item(
    path: Path,
    *,
    lane: ReviewLane = ReviewLane.VENDOR_MANAGED,
    actionability: Actionability = Actionability.REVIEW_PLAN,
    risk: RiskTier = RiskTier.MEDIUM,
    logical_size: int = 123,
    last_write_time_ns: int | None = 456,
    reason: str = "Cache-like metadata needs a second opinion.",
    tags: tuple[str, ...] = ("cache",),
) -> TriageItem:
    record = ScanRecord(
        root=str(path.parent),
        path=str(path),
        kind=ScanRecordKind.FILE,
        depth=1,
        logical_size=logical_size,
        allocated_size=4096,
        raw_allocated_size=4096,
        volume_serial=7,
        file_id="ab" * 16,
        file_id_kind="file_id_128",
        link_count=1,
        attributes=32,
        creation_time_ns=111,
        last_write_time_ns=last_write_time_ns,
    )
    return TriageItem(
        record=record,
        path=str(path),
        logical_size=logical_size,
        allocated_size=4096,
        category=CleanupCategory.PIP_CACHE,
        source_domain=SourceDomain.PACKAGE_MANAGERS,
        lane=lane,
        risk_tier=risk,
        evidence_kind=EvidenceKind.PATH_HEURISTIC,
        actionability=actionability,
        execution_policy=ExecutionPolicy.RECYCLE_ONLY,
        recovery=RecoveryCapability.VENDOR_REDOWNLOAD_BEST_EFFORT,
        reason=reason,
        tags=tags,
    )


def _source(path: Path, *, protected: bool = False) -> AiReviewCandidateInput:
    return AiReviewCandidateInput(_item(path), hard_protected=protected)


def _package(tmp_path: Path, count: int = 1):
    return build_ai_review_package(
        tuple(
            _source(tmp_path / "pip" / "cache" / f"private-{index}.whl")
            for index in range(count)
        ),
        scan_session_id="scan-session-001",
        now=NOW,
    )


def _response_text(package, recommendations: list[dict[str, object]] | None = None) -> str:
    payload = response_template(package)
    if recommendations is not None:
        payload["recommendations"] = recommendations
    return json.dumps(payload, ensure_ascii=False, allow_nan=False)


def _recommendation(entry, token: str = "UNSURE", reason: str = "Metadata is inconclusive."):
    return {
        "candidate_id": entry.candidate_id,
        "recommendation": token,
        "reason": reason,
    }


def test_request_is_closed_private_bounded_and_has_zero_authority(tmp_path: Path) -> None:
    private_path = tmp_path / "pip" / "cache" / "private-customer-model-name.whl"
    package = build_ai_review_package(
        (AiReviewCandidateInput(_item(private_path), hard_protected=False),),
        scan_session_id="scan-session-private",
        now=NOW,
    )

    payload = package.payload()
    serialized = serialize_ai_review_package(package)

    assert set(payload) == {
        "schema_version",
        "document_type",
        "execution_authority",
        "review_session_id",
        "nonce",
        "scan_session_digest",
        "issued_at",
        "expires_at",
        "instructions",
        "response_contract",
        "candidates",
        "package_digest",
    }
    assert payload["document_type"] == AI_REVIEW_REQUEST_TYPE
    assert payload["execution_authority"] == "NONE"
    assert str(private_path) not in serialized
    assert "private-customer-model-name" not in serialized
    assert "filename=*.whl" in serialized
    candidate = payload["candidates"][0]
    assert candidate["candidate_id"].startswith("candidate_")
    assert candidate["hard_protected"] is False
    assert len(candidate["snapshot_identity_digest"]) == 64
    assert len(serialized.encode("utf-8")) < contract_module.MAX_AI_REQUEST_BYTES


def test_packet_tokens_and_keyed_snapshot_digests_are_fresh(tmp_path: Path) -> None:
    first = _package(tmp_path)
    second = _package(tmp_path)

    assert first.review_session_id != second.review_session_id
    assert first.nonce != second.nonce
    assert first.entries[0].candidate_id != second.entries[0].candidate_id
    assert first.entries[0].snapshot_identity_digest != second.entries[0].snapshot_identity_digest
    assert first.package_digest != second.package_digest


def test_snapshot_digest_binds_identity_and_metadata(tmp_path: Path) -> None:
    original = _item(tmp_path / "pip" / "cache" / "one.whl")
    changed_record = replace(original.record, logical_size=124)
    changed = replace(original, record=changed_record, logical_size=124)
    first = build_ai_review_package(
        (AiReviewCandidateInput(original, False),),
        scan_session_id="scan-one",
        now=NOW,
    )
    changed_with_same_nonce = replace(
        first,
        entries=(replace(first.entries[0], item=changed),),
    )

    with pytest.raises(AiReviewContractError, match="snapshot digest mismatch"):
        serialize_ai_review_package(changed_with_same_nonce)


def test_builder_requires_explicit_bounded_unique_candidates(tmp_path: Path) -> None:
    with pytest.raises(AiReviewContractError, match="1 to"):
        build_ai_review_package((), scan_session_id="scan", now=NOW)
    too_many = tuple(
        _source(tmp_path / f"cache-{index}" / "entry.bin")
        for index in range(MAX_AI_REVIEW_ITEMS + 1)
    )
    with pytest.raises(AiReviewContractError, match="1 to"):
        build_ai_review_package(too_many, scan_session_id="scan", now=NOW)
    duplicate = _source(tmp_path / "same.bin")
    with pytest.raises(AiReviewContractError, match="duplicate path"):
        build_ai_review_package((duplicate, duplicate), scan_session_id="scan", now=NOW)


@pytest.mark.parametrize("scan_id", ["", "contains space", "x" * 129, "../scan"])
def test_builder_rejects_unsafe_scan_session_id(tmp_path: Path, scan_id: str) -> None:
    with pytest.raises(AiReviewContractError, match="scan_session_id"):
        build_ai_review_package((_source(tmp_path / "one.bin"),), scan_session_id=scan_id, now=NOW)


def test_builder_rejects_invalid_clock_and_ttl(tmp_path: Path) -> None:
    candidate = (_source(tmp_path / "one.bin"),)
    with pytest.raises(AiReviewContractError, match="timezone-aware"):
        build_ai_review_package(
            candidate,
            scan_session_id="scan",
            now=datetime(2026, 7, 16),
        )
    for ttl in (timedelta(0), timedelta(hours=25)):
        with pytest.raises(AiReviewContractError, match="ttl"):
            build_ai_review_package(candidate, scan_session_id="scan", now=NOW, ttl=ttl)


def test_source_metadata_is_redacted_and_bounded_not_rejected(tmp_path: Path) -> None:
    item = _item(
        tmp_path / "pip" / "cache" / "secret.whl",
        reason="x" * 2_000,
        tags=tuple(f"tag-{index}-" + "z" * 100 for index in range(40)),
    )
    package = build_ai_review_package(
        (AiReviewCandidateInput(item, False),),
        scan_session_id="scan",
        now=NOW,
    )
    candidate = package.payload()["candidates"][0]

    assert len(candidate["reason"]) == contract_module.MAX_SOURCE_REASON_CHARS
    assert len(candidate["tags"]) == contract_module.MAX_TAGS_PER_ITEM
    assert all(len(tag) <= contract_module.MAX_TAG_CHARS for tag in candidate["tags"])


def test_path_or_command_bearing_source_text_is_redacted(tmp_path: Path) -> None:
    item = _item(
        tmp_path / "pip" / "cache" / "secret.whl",
        reason=r"Observed under C:\Users\private\cache",
        tags=("pip cache purge",),
    )
    package = build_ai_review_package(
        (AiReviewCandidateInput(item, False),),
        scan_session_id="scan-redaction",
        now=NOW,
    )
    candidate = package.payload()["candidates"][0]

    assert candidate["reason"] == "<redacted path-bearing source metadata>"
    assert candidate["tags"] == ["<redacted command-like source metadata>"]


@pytest.mark.parametrize("kind", ["path-mismatch", "root-escape", "negative-size"])
def test_builder_rejects_inconsistent_scan_observations(tmp_path: Path, kind: str) -> None:
    item = _item(tmp_path / "root" / "one.bin")
    if kind == "path-mismatch":
        item = replace(item, record=replace(item.record, path=str(tmp_path / "root" / "two.bin")))
    elif kind == "root-escape":
        item = replace(item, record=replace(item.record, root=str(tmp_path / "another-root")))
    else:
        item = replace(
            item,
            logical_size=-1,
            record=replace(item.record, logical_size=-1),
        )

    with pytest.raises(AiReviewContractError):
        build_ai_review_package(
            (AiReviewCandidateInput(item, False),),
            scan_session_id="scan-inconsistent",
            now=NOW,
        )


def test_valid_complete_response_imports_only_inert_recommendations(tmp_path: Path) -> None:
    package = _package(tmp_path, count=3)
    tokens = ("KEEP", "RECOMMEND_RECYCLE", "UNSURE")
    response = _response_text(
        package,
        [
            _recommendation(entry, token, f"Reason {index} is bounded and metadata-only.")
            for index, (entry, token) in enumerate(zip(package.entries, tokens, strict=True), 1)
        ],
    )
    assert json.loads(response)["document_type"] == AI_REVIEW_RESPONSE_TYPE

    imported = parse_ai_review_response(response, package, now=NOW + timedelta(minutes=1))

    assert imported.document_type == AI_REVIEW_IMPORT_TYPE
    assert imported.execution_authority == "NONE"
    assert imported.schema_version == 1
    assert [item.recommendation for item in imported.recommendations] == [
        AiRecommendation.KEEP,
        AiRecommendation.RECOMMEND_RECYCLE,
        AiRecommendation.UNSURE,
    ]
    assert [item.item for item in imported.recommendations] == [
        entry.item for entry in package.entries
    ]
    assert all(item.snapshot_identity_digest for item in imported.recommendations)
    # dataclasses.replace() reports init=False replacement as ValueError on
    # Python 3.11/3.12 and TypeError on Python 3.13.
    with pytest.raises((TypeError, ValueError)):
        replace(imported, execution_authority="DELETE")


@pytest.mark.parametrize("token", ["DELETE", "PURGE", "RECYCLE", "recommend_recycle", ""])
def test_response_rejects_unknown_action_vocabulary(tmp_path: Path, token: str) -> None:
    package = _package(tmp_path)
    with pytest.raises(AiReviewContractError, match="unknown recommendation"):
        parse_ai_review_response(
            _response_text(package, [_recommendation(package.entries[0], token)]),
            package,
            now=NOW,
        )


def test_response_requires_every_current_packet_id_exactly_once(tmp_path: Path) -> None:
    package = _package(tmp_path, count=2)
    first = _recommendation(package.entries[0])
    cases = [
        [first],
        [first, first],
        [first, {**_recommendation(package.entries[1]), "candidate_id": "candidate_" + "0" * 32}],
    ]
    for recommendations in cases:
        with pytest.raises(
            AiReviewContractError, match=r"every candidate|unknown or duplicate"
        ):
            parse_ai_review_response(
                _response_text(package, recommendations),
                package,
                now=NOW,
            )


def test_response_rejects_new_paths_commands_and_unknown_fields(tmp_path: Path) -> None:
    package = _package(tmp_path)
    entry = package.entries[0]
    injected_field = _recommendation(entry)
    injected_field["path"] = r"C:\new\target.bin"
    cases = [
        ([injected_field], "unknown or missing fields"),
        ([_recommendation(entry, reason=r"The target is C:\new\target.bin")], "contain a path"),
        ([_recommendation(entry, reason="Run pip cache purge first")], "contain a command"),
        ([_recommendation(entry, reason="Use Remove-Item on it")], "contain a command"),
    ]
    for recommendations, message in cases:
        with pytest.raises(AiReviewContractError, match=message):
            parse_ai_review_response(
                _response_text(package, recommendations),
                package,
                now=NOW,
            )


def test_response_rejects_unbounded_or_control_character_reason(tmp_path: Path) -> None:
    package = _package(tmp_path)
    entry = package.entries[0]
    for reason in ("", "x" * (MAX_AI_REASON_CHARS + 1), "line one\nline two"):
        with pytest.raises(AiReviewContractError, match="reason"):
            parse_ai_review_response(
                _response_text(package, [_recommendation(entry, reason=reason)]),
                package,
                now=NOW,
            )


def test_hard_protection_is_monotonic_and_blocks_recycle_advice(tmp_path: Path) -> None:
    protected_item = _item(
        tmp_path / ".git" / "config",
        lane=ReviewLane.PROTECTED,
        actionability=Actionability.PROTECTED,
        risk=RiskTier.PROTECTED,
    )
    package = build_ai_review_package(
        (AiReviewCandidateInput(protected_item, hard_protected=False),),
        scan_session_id="scan-protected",
        now=NOW,
    )
    assert package.entries[0].hard_protected is True
    assert package.payload()["candidates"][0]["hard_protected"] is True

    with pytest.raises(AiReviewContractError, match="hard-protected"):
        parse_ai_review_response(
            _response_text(
                package,
                [_recommendation(package.entries[0], "RECOMMEND_RECYCLE")],
            ),
            package,
            now=NOW,
        )
    imported = parse_ai_review_response(
        _response_text(package, [_recommendation(package.entries[0], "UNSURE")]),
        package,
        now=NOW,
    )
    assert imported.recommendations[0].hard_protected is True


def test_explicit_protection_also_blocks_recycle_advice(tmp_path: Path) -> None:
    package = build_ai_review_package(
        (AiReviewCandidateInput(_item(tmp_path / "ordinary.cache"), hard_protected=True),),
        scan_session_id="scan-explicit-protection",
        now=NOW,
    )
    with pytest.raises(AiReviewContractError, match="hard-protected"):
        parse_ai_review_response(
            _response_text(
                package,
                [_recommendation(package.entries[0], "RECOMMEND_RECYCLE")],
            ),
            package,
            now=NOW,
        )


def test_response_is_bound_to_session_nonce_and_package_digest(tmp_path: Path) -> None:
    package = _package(tmp_path)
    for field, value in (
        ("review_session_id", "review_" + "0" * 32),
        ("nonce", "0" * 64),
        ("package_digest", "0" * 64),
    ):
        payload = response_template(package)
        payload[field] = value
        with pytest.raises(AiReviewContractError, match=f"{field} mismatch"):
            parse_ai_review_response(json.dumps(payload), package, now=NOW)


def test_expired_or_not_yet_valid_package_is_rejected(tmp_path: Path) -> None:
    package = _package(tmp_path)
    valid_response = _response_text(package)
    with pytest.raises(AiReviewContractError, match="expired"):
        parse_ai_review_response(valid_response, package, now=NOW + timedelta(hours=3))
    with pytest.raises(AiReviewContractError, match="not yet valid"):
        parse_ai_review_response(valid_response, package, now=NOW - timedelta(minutes=6))


def test_tampered_local_snapshot_or_package_digest_is_rejected(tmp_path: Path) -> None:
    package = _package(tmp_path)
    bad_snapshot = replace(
        package.entries[0],
        snapshot_identity_digest="0" * 64,
    )
    cases = [
        replace(package, package_digest="0" * 64),
        replace(package, entries=(bad_snapshot,)),
    ]
    for tampered in cases:
        with pytest.raises(AiReviewContractError, match="digest mismatch"):
            parse_ai_review_response(_response_text(package), tampered, now=NOW)


def test_exported_request_round_trip_and_tamper_detection(tmp_path: Path) -> None:
    package = _package(tmp_path)
    text = serialize_ai_review_package(package)

    validate_ai_review_package_text(text, package, now=NOW)
    payload = json.loads(text)
    payload["execution_authority"] = "DELETE"
    with pytest.raises(AiReviewContractError, match="differs"):
        validate_ai_review_package_text(json.dumps(payload), package, now=NOW)


def test_strict_json_rejects_duplicate_keys_nan_bom_and_oversize(tmp_path: Path) -> None:
    package = _package(tmp_path)
    valid = _response_text(package)
    duplicate = valid.replace(
        '{"schema_version": 1',
        '{"schema_version": 1, "schema_version": 1',
        1,
    )
    cases = [
        duplicate,
        valid.replace('"schema_version": 1', '"schema_version": NaN', 1),
        "\ufeff" + valid,
        " " * (contract_module.MAX_AI_RESPONSE_BYTES + 1),
    ]
    for text in cases:
        with pytest.raises(AiReviewContractError):
            parse_ai_review_response(text, package, now=NOW)


def test_strict_json_turns_excessive_nesting_into_contract_failure(tmp_path: Path) -> None:
    package = _package(tmp_path)
    deeply_nested = "[" * 2_000 + "]" * 2_000
    with pytest.raises(AiReviewContractError, match="nesting limit"):
        parse_ai_review_response(deeply_nested, package, now=NOW)


def test_schema_integer_is_not_accepted_as_boolean(tmp_path: Path) -> None:
    package = _package(tmp_path)
    payload = response_template(package)
    payload["schema_version"] = True
    with pytest.raises(AiReviewContractError, match="schema version"):
        parse_ai_review_response(json.dumps(payload), package, now=NOW)


def test_contract_module_contains_no_filesystem_or_command_execution_surface() -> None:
    source = Path(contract_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    called: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                called.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                called.add(node.func.attr)

    assert imported.isdisjoint({"subprocess", "shutil", "ctypes"})
    assert called.isdisjoint(
        {
            "open",
            "unlink",
            "remove",
            "rmtree",
            "rename",
            "system",
            "run",
            "Popen",
            "DeleteFileW",
            "SHFileOperationW",
        }
    )
