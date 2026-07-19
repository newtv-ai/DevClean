from __future__ import annotations

import ast
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import devclean.ui.app as app_module
from devclean.core.cleanup_catalog import (
    CleanupCategory,
    CleanupPolicy,
    KnownCleanupRoot,
)
from devclean.core.cleanup_journal import ActionState, BatchState, CleanupMode
from devclean.core.postscan_cleanup import CleanupExecutionResult
from devclean.core.triage import ReviewLane, triage_file
from devclean.scanner import ScanRecord, ScanRecordKind
from devclean.ui.app import (
    build_non_executable_review_plan,
    cleanup_mode_for_user_choice,
    is_review_plan_eligible,
    write_non_executable_review_plan,
)


def _record(path: Path, *, last_write_time_ns: int | None = 0) -> ScanRecord:
    return ScanRecord(
        root=str(path.parent),
        path=str(path),
        kind=ScanRecordKind.FILE,
        depth=1,
        logical_size=128,
        allocated_size=4096,
        raw_allocated_size=4096,
        volume_serial=7,
        file_id="01" * 16,
        file_id_kind="file_id_128",
        creation_time_ns=0,
        last_write_time_ns=last_write_time_ns,
    )


def _deterministic_item(tmp_path: Path):
    now = datetime(2026, 7, 16, tzinfo=UTC)
    return triage_file(
        _record(tmp_path / "old.tmp"),
        now=now,
        temp_root=tmp_path,
    )


def _vendor_item(tmp_path: Path):
    cache = tmp_path / "pip-cache"
    return triage_file(
        _record(cache / "wheel.bin", last_write_time_ns=None),
        known_roots=(
            KnownCleanupRoot(
                cache,
                CleanupCategory.PIP_CACHE,
                CleanupPolicy.VENDOR_MANAGED,
                "pip cache",
            ),
        ),
    )


def test_review_plan_has_explicitly_zero_authority_and_no_actions(tmp_path: Path) -> None:
    deterministic = _deterministic_item(tmp_path / "temp")
    vendor = _vendor_item(tmp_path / "vendor")

    plan = build_non_executable_review_plan(
        (deterministic, vendor),
        scan_roots=(tmp_path,),
        created_at=datetime(2026, 7, 16, 9, 30, tzinfo=UTC),
    )

    assert plan["document_type"] == "DevClean_NON_EXECUTABLE_REVIEW_PLAN"
    assert plan["execution_authority"] == "NONE"
    assert plan["import_contract"] == "UNSUPPORTED"
    assert plan["execution_actions"] == []
    assert plan["default_selection_applied"] is False
    assert plan["selection_origin"] == "EXPLICIT_LOCAL_USER_MARKING"
    candidates = plan["review_candidates"]
    assert isinstance(candidates, list)
    assert {candidate["review_lane"] for candidate in candidates} == {
        ReviewLane.DETERMINISTIC_CANDIDATE.value,
        ReviewLane.AI_REVIEW.value,
    }
    assert all(
        candidate["observational_snapshot"]["valid_for_execution"] is False
        for candidate in candidates
    )


@pytest.mark.parametrize("kind", ["report_only", "protected"])
def test_review_plan_rejects_unselectable_observations(tmp_path: Path, kind: str) -> None:
    if kind == "protected":
        item = triage_file(_record(tmp_path / ".git" / "config"), temp_root=tmp_path / "temp")
    elif kind == "report_only":
        report_root = tmp_path / "system-cache"
        item = triage_file(
            _record(report_root / "system.bin"),
            known_roots=(
                KnownCleanupRoot(
                    report_root,
                    CleanupCategory.WINDOWS_UPDATE,
                    CleanupPolicy.REPORT_ONLY,
                    "system report",
                ),
            ),
        )
    assert not is_review_plan_eligible(item)
    with pytest.raises(ValueError, match="cannot enter this plan"):
        build_non_executable_review_plan((item,), scan_roots=(tmp_path,))


def test_review_plan_writer_is_new_file_only(tmp_path: Path) -> None:
    plan = build_non_executable_review_plan(
        (_deterministic_item(tmp_path / "temp"),),
        scan_roots=(tmp_path,),
        created_at=datetime(2026, 7, 16, tzinfo=UTC),
    )
    destination = tmp_path / "review.json"

    write_non_executable_review_plan(destination, plan)

    stored = json.loads(destination.read_text(encoding="utf-8"))
    assert stored["execution_authority"] == "NONE"
    with pytest.raises(FileExistsError):
        write_non_executable_review_plan(destination, plan)


def test_review_plan_writer_rejects_any_authority_escalation(tmp_path: Path) -> None:
    plan = build_non_executable_review_plan(
        (_deterministic_item(tmp_path / "temp"),), scan_roots=(tmp_path,)
    )
    plan["execution_authority"] = "DELETE"

    with pytest.raises(ValueError, match="zero execution authority"):
        write_non_executable_review_plan(tmp_path / "unsafe.json", plan)


def test_ui_module_has_no_cleanup_ai_or_external_command_imports() -> None:
    source_path = Path(app_module.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)

    forbidden = {
        "subprocess",
        "devclean.core.ai_review",
        "devclean.core.auto_clean",
        "devclean.core.recycle",
        "devclean.platform.windows.permanent_delete",
        "devclean.platform.windows.recycle_bin",
    }
    assert imported.isdisjoint(forbidden)


class _ModeProbe:
    """Duck-typed stand-in exposing the only field the mode mapping reads."""

    def __init__(self, *, permanent_eligible: bool) -> None:
        self.permanent_eligible = permanent_eligible


def _probes(*eligibility: bool) -> tuple[Any, ...]:
    return tuple(_ModeProbe(permanent_eligible=value) for value in eligibility)


def test_cleanup_mode_mapping_defaults_to_recoverable_quarantine() -> None:
    assert (
        cleanup_mode_for_user_choice(_probes(True, False), irreversible=False)
        is CleanupMode.RECYCLE
    )


def test_cleanup_mode_mapping_grants_permanent_only_to_fully_eligible_batches() -> None:
    assert (
        cleanup_mode_for_user_choice(_probes(True, True), irreversible=True)
        is CleanupMode.PERMANENT
    )


def test_cleanup_mode_mapping_uses_confirmed_purge_for_any_ineligible_file() -> None:
    assert (
        cleanup_mode_for_user_choice(_probes(True, False), irreversible=True)
        is CleanupMode.CONFIRMED_PURGE
    )
    assert (
        cleanup_mode_for_user_choice(_probes(False), irreversible=True)
        is CleanupMode.CONFIRMED_PURGE
    )


def test_cleanup_mode_mapping_rejects_empty_selection() -> None:
    with pytest.raises(ValueError, match="at least one exact candidate"):
        cleanup_mode_for_user_choice((), irreversible=False)


def test_cli_smoke_path_does_not_construct_the_ui() -> None:
    assert app_module.main(("--smoke",)) == 0


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, "0 B"),
        (1024, "1.0 KiB"),
        (1024**2, "1.0 MiB"),
        (1024**5, "1.0 PiB"),
    ],
)
def test_format_bytes_uses_binary_units(value: int, expected: str) -> None:
    assert app_module._format_bytes(value) == expected


def test_cleanup_summary_handles_no_actions() -> None:
    summary = app_module._cleanup_result_summary(())
    assert summary


def test_cleanup_summary_distinguishes_recoverable_and_irreversible_results() -> None:
    recoverable = CleanupExecutionResult(
        batch_id="recoverable",
        mode=CleanupMode.RECYCLE,
        batch_state=BatchState.NEEDS_REVIEW,
        action_states=(
            ("quarantined", ActionState.QUARANTINED),
            ("unchanged", ActionState.FAILED_UNCHANGED),
        ),
        selected_logical_bytes=20,
        purged_logical_bytes=0,
        immediate_reclaim_upper_bound=0,
    )
    irreversible = CleanupExecutionResult(
        batch_id="irreversible",
        mode=CleanupMode.CONFIRMED_PURGE,
        batch_state=BatchState.NEEDS_REVIEW,
        action_states=(
            ("purged", ActionState.PURGED),
            ("unknown", ActionState.UNKNOWN),
        ),
        selected_logical_bytes=40,
        purged_logical_bytes=10,
        immediate_reclaim_upper_bound=10,
    )

    summary = app_module._cleanup_result_summary((recoverable, irreversible))

    assert "10 B" in summary
    assert "SQLite" in summary


def test_review_plan_rejects_duplicate_paths_and_naive_timestamp(tmp_path: Path) -> None:
    item = _deterministic_item(tmp_path / "temp")
    with pytest.raises(ValueError, match="duplicate"):
        build_non_executable_review_plan((item, item), scan_roots=(tmp_path,))
    with pytest.raises(ValueError, match="timezone"):
        build_non_executable_review_plan(
            (item,),
            scan_roots=(tmp_path,),
            created_at=datetime(2026, 7, 16),
        )


def test_review_plan_records_unknown_allocation(tmp_path: Path) -> None:
    item = triage_file(
        _record(tmp_path / "temp" / "unknown.tmp"),
        temp_root=tmp_path / "temp",
    )
    item = replace(item, allocated_size=None)

    plan = build_non_executable_review_plan((item,), scan_roots=(tmp_path,))

    assert plan["summary"]["allocation_unknown_files"] == 1  # type: ignore[index]


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("document_type", "UNTRUSTED", "document type"),
        ("import_contract", "EXECUTABLE", "import contract"),
        ("execution_actions", [{"path": "anything"}], "execution actions"),
    ],
)
def test_review_plan_writer_rejects_closed_contract_changes(
    tmp_path: Path,
    field: str,
    value: object,
    match: str,
) -> None:
    plan = build_non_executable_review_plan(
        (_deterministic_item(tmp_path / "temp"),),
        scan_roots=(tmp_path,),
    )
    plan[field] = value

    with pytest.raises(ValueError, match=match):
        write_non_executable_review_plan(tmp_path / f"{field}.json", plan)
