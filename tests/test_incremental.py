from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from devclean.core.incremental import (
    INCREMENTAL_CONTRACT_VERSION,
    ChangeSource,
    FallbackReason,
    ScanCoverage,
    ScanGeneration,
    ScanMode,
    SessionCheckpoint,
    evaluate_session_checkpoint,
    new_generation_id,
)

_SCOPE = "a" * 64
_OTHER_SCOPE = "b" * 64
_TOKEN = "session_" + "1" * 32
_OTHER_TOKEN = "session_" + "2" * 32


def _checkpoint(*, sequence: int = 7) -> SessionCheckpoint:
    return SessionCheckpoint(
        contract_version=INCREMENTAL_CONTRACT_VERSION,
        session_token=_TOKEN,
        scope_digest=_SCOPE,
        last_sequence=sequence,
        created_at=datetime.now(UTC),
    )


def test_session_checkpoint_strict_json_round_trip() -> None:
    checkpoint = _checkpoint()

    restored = SessionCheckpoint.from_json(checkpoint.to_json())

    assert restored == checkpoint
    assert restored.reusable_across_processes is False
    assert restored.source is ChangeSource.READ_DIRECTORY_CHANGES_W_SESSION


def test_session_checkpoint_rejects_unknown_fields_and_cross_process_reuse() -> None:
    payload = _checkpoint().to_dict()
    payload["unexpected"] = True
    with pytest.raises(ValueError, match="unexpected or missing"):
        SessionCheckpoint.from_dict(payload)

    payload = _checkpoint().to_dict()
    payload["reusable_across_processes"] = True
    with pytest.raises(ValueError, match="persistence flag"):
        SessionCheckpoint.from_dict(payload)


def test_session_checkpoint_rejects_bad_types_and_naive_time() -> None:
    with pytest.raises(ValueError, match="last_sequence"):
        SessionCheckpoint(
            contract_version=INCREMENTAL_CONTRACT_VERSION,
            session_token=_TOKEN,
            scope_digest=_SCOPE,
            last_sequence=True,
            created_at=datetime.now(UTC),
        )

    with pytest.raises(ValueError, match="timezone-aware"):
        SessionCheckpoint(
            contract_version=INCREMENTAL_CONTRACT_VERSION,
            session_token=_TOKEN,
            scope_digest=_SCOPE,
            last_sequence=0,
            created_at=datetime.now(),
        )


@pytest.mark.parametrize(
    ("checkpoint", "token", "scope", "next_sequence", "monitor_reason", "expected"),
    [
        (None, _TOKEN, _SCOPE, 0, None, FallbackReason.NO_BASELINE),
        (
            _checkpoint(),
            _OTHER_TOKEN,
            _SCOPE,
            7,
            None,
            FallbackReason.SESSION_TOKEN_MISMATCH,
        ),
        (
            _checkpoint(),
            _TOKEN,
            _OTHER_SCOPE,
            7,
            None,
            FallbackReason.SCOPE_CHANGED,
        ),
        (
            _checkpoint(sequence=8),
            _TOKEN,
            _SCOPE,
            7,
            None,
            FallbackReason.SESSION_SEQUENCE_REGRESSION,
        ),
        (
            _checkpoint(),
            _TOKEN,
            _SCOPE,
            7,
            FallbackReason.MONITOR_BUFFER_OVERFLOW,
            FallbackReason.MONITOR_BUFFER_OVERFLOW,
        ),
    ],
)
def test_checkpoint_failures_always_require_full_scan(
    checkpoint: SessionCheckpoint | None,
    token: str,
    scope: str,
    next_sequence: int,
    monitor_reason: FallbackReason | None,
    expected: FallbackReason,
) -> None:
    decision = evaluate_session_checkpoint(
        checkpoint,
        live_session_token=token,
        scope_digest=scope,
        next_sequence=next_sequence,
        monitor_fallback=monitor_reason,
    )

    assert decision.requires_full_scan is True
    assert decision.effective_mode is ScanMode.FULL_RECONCILIATION
    assert decision.fallback_reason is expected
    assert decision.checkpoint_usable is False
    assert decision.may_skip_directories is False


def test_live_checkpoint_is_shadow_only_and_never_prunes_directories() -> None:
    decision = evaluate_session_checkpoint(
        _checkpoint(),
        live_session_token=_TOKEN,
        scope_digest=_SCOPE,
        next_sequence=9,
    )

    assert decision.requires_full_scan is False
    assert decision.effective_mode is ScanMode.SESSION_SHADOW
    assert decision.checkpoint_usable is True
    assert decision.may_skip_directories is False


def test_scan_generation_strict_round_trip_preserves_shadow_boundary() -> None:
    started = datetime.now(UTC)
    generation = ScanGeneration(
        contract_version=INCREMENTAL_CONTRACT_VERSION,
        generation_id=new_generation_id(),
        scope_digest=_SCOPE,
        requested_mode=ScanMode.SESSION_SHADOW,
        actual_mode=ScanMode.SESSION_SHADOW,
        change_source=ChangeSource.READ_DIRECTORY_CHANGES_W_SESSION,
        coverage=ScanCoverage.COMPLETE,
        started_at=started,
        finished_at=started + timedelta(seconds=1),
        checkpoint=_checkpoint(),
        events_observed=4,
        paths_invalidated=3,
        directories_reobserved=2,
        entries_reused=5,
    )

    restored = ScanGeneration.from_json(generation.to_json())

    assert restored == generation
    assert restored.used_for_directory_pruning is False


def test_scan_generation_rejects_pruning_and_unexplained_fallback() -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValueError, match="cannot prune"):
        ScanGeneration(
            contract_version=INCREMENTAL_CONTRACT_VERSION,
            generation_id=new_generation_id(),
            scope_digest=_SCOPE,
            requested_mode=ScanMode.SESSION_SHADOW,
            actual_mode=ScanMode.SESSION_SHADOW,
            change_source=ChangeSource.READ_DIRECTORY_CHANGES_W_SESSION,
            coverage=ScanCoverage.COMPLETE,
            started_at=now,
            finished_at=now,
            checkpoint=_checkpoint(),
            used_for_directory_pruning=True,
        )

    with pytest.raises(ValueError, match="must record its reason"):
        ScanGeneration(
            contract_version=INCREMENTAL_CONTRACT_VERSION,
            generation_id=new_generation_id(),
            scope_digest=_SCOPE,
            requested_mode=ScanMode.SESSION_SHADOW,
            actual_mode=ScanMode.FULL_RECONCILIATION,
            change_source=ChangeSource.READ_DIRECTORY_CHANGES_W_SESSION,
            coverage=ScanCoverage.COMPLETE,
            started_at=now,
            finished_at=now,
        )


def test_fallback_generation_is_explicit_and_json_safe() -> None:
    now = datetime.now(UTC)
    generation = ScanGeneration(
        contract_version=INCREMENTAL_CONTRACT_VERSION,
        generation_id=new_generation_id(),
        scope_digest=_SCOPE,
        requested_mode=ScanMode.SESSION_SHADOW,
        actual_mode=ScanMode.FULL_RECONCILIATION,
        change_source=ChangeSource.READ_DIRECTORY_CHANGES_W_SESSION,
        coverage=ScanCoverage.PARTIAL,
        started_at=now,
        finished_at=now,
        fallback_reason=FallbackReason.MONITOR_BUFFER_OVERFLOW,
    )

    assert ScanGeneration.from_json(generation.to_json()) == generation


def test_v0_1_contract_has_no_usn_change_source() -> None:
    assert all("USN" not in source.value for source in ChangeSource)
