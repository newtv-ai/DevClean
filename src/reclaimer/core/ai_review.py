"""Bounded, metadata-only AI review packets for exact scanned cache files."""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from reclaimer.adapters.json_contract import strict_json_loads
from reclaimer.core.triage import ReviewLane, TriageItem

MAX_AI_REVIEW_ITEMS = 50
_REQUEST_SCHEMA = "reclaimer-ai-review-v1"
_RESPONSE_SCHEMA = "reclaimer-ai-decision-v1"


class AiReviewError(ValueError):
    """A review packet or external model response breaks the fixed contract."""


class AiDecision(StrEnum):
    DELETE = "DELETE"
    KEEP = "KEEP"
    UNSURE = "UNSURE"


@dataclass(frozen=True, slots=True)
class AiReviewEntry:
    item_id: str
    item: TriageItem


@dataclass(frozen=True, slots=True)
class AiReviewPacket:
    review_id: str
    entries: tuple[AiReviewEntry, ...]

    def payload(self) -> dict[str, object]:
        """Return a model-facing packet with redacted paths and no file contents."""

        return {
            "schema": _REQUEST_SCHEMA,
            "review_id": self.review_id,
            "instructions": [
                "Explain every exact item using only the supplied metadata; never request or infer "
                "file contents.",
                "Return DELETE only when this exact cache file is recreatable and safe to remove.",
                "Return UNSURE whenever the metadata cannot establish that conclusion.",
                "Do not return paths, commands, wildcards, or any item IDs not present below.",
                "Return only the response JSON described by response_contract.",
            ],
            "items": [
                {
                    "id": entry.item_id,
                    "path": _redact_path(entry.item.path),
                    "logical_size": entry.item.logical_size,
                    "allocated_size": entry.item.allocated_size,
                    "scan_reason": entry.item.reason,
                }
                for entry in self.entries
            ],
            "response_contract": {
                "schema": _RESPONSE_SCHEMA,
                "review_id": self.review_id,
                "items": [
                    {
                        "id": "exact item id from items",
                        "decision": "DELETE | KEEP | UNSURE",
                        "explanation": "short item-specific explanation",
                    }
                ],
            },
        }


@dataclass(frozen=True, slots=True)
class AiReviewResult:
    item_id: str
    item: TriageItem
    decision: AiDecision
    explanation: str


def build_ai_review_packet(items: tuple[TriageItem, ...]) -> AiReviewPacket:
    """Create a non-persistent, exact batch from visible AI-review candidates."""

    if not items or len(items) > MAX_AI_REVIEW_ITEMS:
        raise AiReviewError(f"AI review batch must contain 1 to {MAX_AI_REVIEW_ITEMS} items")
    if any(item.lane is not ReviewLane.AI_REVIEW for item in items):
        raise AiReviewError("AI review packets accept only AI-review candidates")
    return AiReviewPacket(
        review_id=uuid.uuid4().hex,
        entries=tuple(
            AiReviewEntry(item_id=f"item_{index:03d}", item=item)
            for index, item in enumerate(items, start=1)
        ),
    )


def parse_ai_review_response(text: str, packet: AiReviewPacket) -> tuple[AiReviewResult, ...]:
    """Accept only a complete decision set for the current in-memory packet."""

    try:
        payload = strict_json_loads(text)
    except (TypeError, ValueError) as error:
        raise AiReviewError(f"AI response is not strict JSON: {error}") from error
    if not isinstance(payload, dict) or set(payload) != {"schema", "review_id", "items"}:
        raise AiReviewError("AI response must contain only schema, review_id, and items")
    if payload["schema"] != _RESPONSE_SCHEMA or payload["review_id"] != packet.review_id:
        raise AiReviewError("AI response belongs to a different review batch")
    raw_items = payload["items"]
    if not isinstance(raw_items, list):
        raise AiReviewError("AI response items must be an array")

    expected = {entry.item_id: entry.item for entry in packet.entries}
    parsed: dict[str, AiReviewResult] = {}
    for raw_item in raw_items:
        if not isinstance(raw_item, dict) or set(raw_item) != {"id", "decision", "explanation"}:
            raise AiReviewError("each AI decision must contain only id, decision, and explanation")
        item_id = raw_item["id"]
        decision = raw_item["decision"]
        explanation = raw_item["explanation"]
        if not isinstance(item_id, str) or item_id not in expected or item_id in parsed:
            raise AiReviewError("AI response has an unknown or duplicate item id")
        if not isinstance(decision, str) or decision not in AiDecision.__members__:
            raise AiReviewError("AI response has an invalid decision")
        if (
            not isinstance(explanation, str)
            or not explanation.strip()
            or len(explanation) > 1_000
        ):
            raise AiReviewError(
                "AI response explanation must be non-empty and at most 1000 characters"
            )
        parsed[item_id] = AiReviewResult(
            item_id=item_id,
            item=expected[item_id],
            decision=AiDecision(decision),
            explanation=explanation.strip(),
        )

    if set(parsed) != set(expected):
        raise AiReviewError(
            "AI response must decide every item in the current review batch exactly once"
        )
    return tuple(parsed[entry.item_id] for entry in packet.entries)


def _redact_path(path: str) -> str:
    home = str(Path.home())
    return path.replace(home, "<USER>", 1) if home else os.path.basename(path)


__all__ = [
    "MAX_AI_REVIEW_ITEMS",
    "AiDecision",
    "AiReviewError",
    "AiReviewPacket",
    "AiReviewResult",
    "build_ai_review_packet",
    "parse_ai_review_response",
]
