from __future__ import annotations

import tkinter as tk
from datetime import UTC, datetime
from pathlib import Path
from tkinter import TclError
from types import SimpleNamespace
from typing import cast

import pytest

from devclean.core.cleanup_catalog import CleanupCategory
from devclean.core.cleanup_journal import CleanupMode
from devclean.core.postscan_cleanup import ScanCleanupCandidate
from devclean.core.triage import (
    Actionability,
    EvidenceKind,
    ExecutionPolicy,
    ReviewLane,
    RiskTier,
    TriageSession,
    triage_file,
)
from devclean.scanner import ScanRecord, ScanRecordKind
from devclean.ui.app import (
    DevCleanWindow,
    WorkbenchState,
    cleanup_mode_for_user_choice,
    is_ai_review_eligible,
    is_direct_cleanup_eligible,
    is_low_risk_cleanup_eligible,
)
from devclean.ui.app import main as gui_main


def _record(path: Path, *, last_write_time_ns: int | None) -> ScanRecord:
    return ScanRecord(
        root=str(path.parent),
        path=str(path),
        kind=ScanRecordKind.FILE,
        depth=1,
        logical_size=100,
        allocated_size=4096,
        raw_allocated_size=4096,
        last_write_time_ns=last_write_time_ns,
    )


def test_old_user_temp_file_enters_human_deterministic_candidate_lane(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 12, tzinfo=UTC)
    item = triage_file(
        _record(tmp_path / "old.tmp", last_write_time_ns=0),
        now=now,
        temp_root=tmp_path,
    )

    assert item.lane is ReviewLane.DETERMINISTIC_CANDIDATE
    assert item.actionability is Actionability.REVIEW_PLAN
    assert item.risk_tier is RiskTier.LOW
    assert item.evidence_kind is EvidenceKind.AGE_AND_APPROVED_ROOT
    assert item.execution_policy is ExecutionPolicy.PERMANENT_APPROVED_CACHE


def test_unverified_development_cache_hint_enters_ai_review_without_execution(
    tmp_path: Path,
) -> None:
    item = triage_file(
        _record(tmp_path / "huggingface" / "model.bin", last_write_time_ns=None),
        temp_root=tmp_path / "temp",
    )

    assert item.lane is ReviewLane.AI_REVIEW
    assert item.actionability is Actionability.AI_REVIEW
    assert item.risk_tier is RiskTier.HIGH
    assert item.evidence_kind is EvidenceKind.PATH_HEURISTIC
    assert item.execution_policy is ExecutionPolicy.RECYCLE_ONLY


def test_protected_asset_is_never_a_review_plan_candidate(tmp_path: Path) -> None:
    item = triage_file(
        _record(tmp_path / ".git" / "config", last_write_time_ns=0),
        temp_root=tmp_path,
    )

    assert item.lane is ReviewLane.PROTECTED
    assert item.actionability is Actionability.PROTECTED
    assert item.risk_tier is RiskTier.PROTECTED
    assert item.execution_policy is ExecutionPolicy.NONE


def test_generic_executable_is_not_mislabeled_as_an_installer(tmp_path: Path) -> None:
    item = triage_file(
        _record(tmp_path / "tools" / "compiler.exe", last_write_time_ns=None),
        temp_root=tmp_path / "temp",
    )

    assert item.category is CleanupCategory.OTHER
    assert item.lane is ReviewLane.AI_REVIEW
    assert item.execution_policy is ExecutionPolicy.RECYCLE_ONLY


@pytest.mark.parametrize("suffix", [".msi", ".msixbundle", ".appxbundle", ".iso"])
def test_explicit_package_formats_are_grouped_as_installers(
    tmp_path: Path, suffix: str
) -> None:
    item = triage_file(
        _record(tmp_path / f"package{suffix}", last_write_time_ns=None),
        temp_root=tmp_path / "temp",
    )

    assert item.category is CleanupCategory.INSTALLERS_DOWNLOADS
    assert item.lane is ReviewLane.AI_REVIEW
    assert item.actionability is Actionability.AI_REVIEW


def test_session_keeps_bounded_display_items_but_exact_totals() -> None:
    session = TriageSession(display_limit=2)
    for size in (1, 3, 2):
        session.add(
            triage_file(
                ScanRecord(
                    root=r"C:\Temp",
                    path=rf"C:\Temp\{size}.tmp",
                    kind=ScanRecordKind.FILE,
                    depth=1,
                    logical_size=size,
                    allocated_size=size,
                    raw_allocated_size=size,
                    last_write_time_ns=0,
                ),
                now=datetime(2026, 7, 12, tzinfo=UTC),
                temp_root=Path(r"C:\Temp"),
            )
        )

    lane = ReviewLane.DETERMINISTIC_CANDIDATE
    assert session.summary(lane).files == 3
    assert session.summary(lane).logical_bytes == 6
    assert [item.logical_size for item in session.items(lane)] == [3, 2]


def test_gui_smoke_does_not_create_a_window_or_state_database() -> None:
    assert gui_main(["--smoke"]) == 0


def test_explicit_irreversible_choice_uses_stronger_mode_for_uncertain_items() -> None:
    low = cast(ScanCleanupCandidate, SimpleNamespace(permanent_eligible=True))
    uncertain = cast(ScanCleanupCandidate, SimpleNamespace(permanent_eligible=False))

    assert cleanup_mode_for_user_choice((low,), irreversible=False) is CleanupMode.RECYCLE
    assert cleanup_mode_for_user_choice((low,), irreversible=True) is CleanupMode.PERMANENT
    assert (
        cleanup_mode_for_user_choice((low, uncertain), irreversible=True)
        is CleanupMode.CONFIRMED_PURGE
    )


def test_gui_constructs_native_widgets_and_requires_post_scan_human_marking(
    tmp_path: Path,
) -> None:
    try:
        root = tk.Tk()
    except TclError as error:
        pytest.skip(f"Tk desktop is unavailable: {error}")
    root.withdraw()
    try:
        window = DevCleanWindow(root)
        assert window._trees
        assert window._mark_button is not None
        assert window._mark_button.instate(("disabled",))
        assert window._export_button is not None
        assert window._export_button.instate(("disabled",))
        assert window._execute_button is not None
        assert window._execute_button.instate(("disabled",))

        session = TriageSession()
        eligible = triage_file(
            _record(tmp_path / "old.tmp", last_write_time_ns=0),
            now=datetime(2026, 7, 16, tzinfo=UTC),
            temp_root=tmp_path,
        )
        ai_candidate = triage_file(
            _record(tmp_path / "documents" / "notes.txt", last_write_time_ns=None),
            temp_root=tmp_path / "different-temp",
        )
        assert is_direct_cleanup_eligible(eligible)
        assert is_low_risk_cleanup_eligible(eligible)
        assert is_ai_review_eligible(ai_candidate)
        assert is_direct_cleanup_eligible(ai_candidate)
        assert not is_low_risk_cleanup_eligible(ai_candidate)
        session.add(eligible)
        session.add(ai_candidate)
        window._last_scan_roots = (tmp_path,)
        window._render_session(session)
        window._scan_complete = True
        window._set_state(WorkbenchState.REVIEW)

        assert not window._marked_ids
        assert window._result_tree is not None
        eligible_id = next(
            item_id
            for item_id, item in window._displayed_items.items()
            if item is eligible
        )
        window._result_tree.selection_set(eligible_id)
        window._show_selected_details()
        window._toggle_selected_mark()
        assert window._marked_ids == {eligible_id}
        assert not window._ai_review_ids
        assert not window._execute_button.instate(("disabled",))

        ai_candidate_id = next(
            item_id
            for item_id, item in window._displayed_items.items()
            if item is ai_candidate
        )
        window._result_tree.selection_set(ai_candidate_id)
        window._show_selected_details()
        window._toggle_selected_mark()
        assert window._marked_ids == {eligible_id, ai_candidate_id}
        window._toggle_selected_mark()
        assert window._marked_ids == {eligible_id}
        window._toggle_selected_ai_review()
        assert window._ai_review_ids == {ai_candidate_id}
        assert not window._export_button.instate(("disabled",))

        window._set_state(WorkbenchState.SCANNING)
        assert window._mark_button.instate(("disabled",))
        assert window._export_button.instate(("disabled",))
        assert window._execute_button.instate(("disabled",))
    finally:
        root.destroy()
