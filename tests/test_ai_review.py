from __future__ import annotations

import json
from pathlib import Path

import pytest

from reclaimer.core.ai_review import (
    AiDecision,
    AiReviewError,
    build_ai_review_packet,
    parse_ai_review_response,
)
from reclaimer.core.triage import triage_file
from reclaimer.scanner import ScanRecord, ScanRecordKind


def _item(path: Path):
    return triage_file(
        ScanRecord(
            root=str(path.parent),
            path=str(path),
            kind=ScanRecordKind.FILE,
            depth=1,
            logical_size=42,
            allocated_size=4096,
            raw_allocated_size=4096,
        ),
        temp_root=path.parent / "unrelated-temp",
    )


def test_packet_contains_metadata_only_and_response_binds_exact_ids(tmp_path: Path) -> None:
    packet = build_ai_review_packet((_item(tmp_path / "pip" / "cache.whl"),))
    payload = packet.payload()
    entry = payload["items"][0]
    assert "logical_size" in entry
    assert "contents" not in entry

    result = parse_ai_review_response(
        json.dumps(
            {
                "schema": "reclaimer-ai-decision-v1",
                "review_id": packet.review_id,
                "items": [
                    {
                        "id": "item_001",
                        "decision": "UNSURE",
                        "explanation": "The cache context alone is not sufficient.",
                    }
                ],
            }
        ),
        packet,
    )

    assert result[0].decision is AiDecision.UNSURE
    assert result[0].item.path.endswith("cache.whl")


def test_response_cannot_add_path_or_skip_a_scanned_item(tmp_path: Path) -> None:
    packet = build_ai_review_packet(
        (_item(tmp_path / "pip" / "first.whl"), _item(tmp_path / "pip" / "second.whl"))
    )
    response = {
        "schema": "reclaimer-ai-decision-v1",
        "review_id": packet.review_id,
        "items": [
            {
                "id": "item_001",
                "decision": "DELETE",
                "explanation": "cache file",
                "path": r"C:\untrusted\other-file",
            }
        ],
    }

    with pytest.raises(AiReviewError, match="only id, decision, and explanation"):
        parse_ai_review_response(json.dumps(response), packet)


def test_response_with_wrong_batch_id_is_rejected(tmp_path: Path) -> None:
    packet = build_ai_review_packet((_item(tmp_path / "pip" / "cache.whl"),))
    response = {
        "schema": "reclaimer-ai-decision-v1",
        "review_id": "other-batch",
        "items": [
            {
                "id": "item_001",
                "decision": "KEEP",
                "explanation": "keep it",
            }
        ],
    }

    with pytest.raises(AiReviewError, match="different review batch"):
        parse_ai_review_response(json.dumps(response), packet)
