"""Single product surface that also executes audited vendor cleanup actions.

Vendor commands are not exposed as a second tool center. During the normal scan
DevClean inventories them, prunes their roots from generic per-file traversal,
and surfaces only operations whose pre-clean reclaim amount is actually known.
Partial garbage collectors stay out of the byte-counted safe list until their
junk can be quantified without mutating storage.
"""

from __future__ import annotations

import os
import threading
import tkinter as tk
from dataclasses import replace
from pathlib import Path
from tkinter import messagebox, ttk
from typing import cast
from uuid import uuid4

from devclean.core.cleanup_journal import ActionState, CleanupMode
from devclean.core.postscan_cleanup import (
    CleanupExecutionResult,
    CleanupRefusal,
    ScanCleanupCandidate,
    execute_cleanup_batch,
    prepare_cleanup_plan,
)
from devclean.core.triage import CleanupTargetKind, TriageItem, TriageSession
from devclean.core.user_rules import UserRules, normalise_path
from devclean.core.vendor_cleanup_actions import (
    VendorCleanupCandidate,
    VendorCleanupKind,
    execute_vendor_cleanup,
    inventory_vendor_cleanup_candidates,
)
from devclean.scanner import CancellationToken
from devclean.ui import app
from devclean.ui.product_app import ProductDevCleanWindow as _BaseProductDevCleanWindow

# These operations clear the audited provider resource represented by the row,
# so its observed bytes are a truthful pre-clean reclaim figure. Partial-GC
# operations (uv prune, pnpm prune, Conda tarball/index cleanup) are deliberately
# not listed here: a non-empty provider root does not prove reclaimable junk.
_QUANTIFIED_VENDOR_KINDS = frozenset(
    {
        VendorCleanupKind.PIP_CACHE_PURGE,
        VendorCleanupKind.GO_BUILD_CACHE_CLEAN,
        VendorCleanupKind.NUGET_HTTP_CACHE_CLEAR,
        VendorCleanupKind.NUGET_TEMP_CLEAR,
        VendorCleanupKind.NUGET_PLUGINS_CACHE_CLEAR,
    }
)
_VENDOR_PREFIX = "vendor:"


def _vendor_row_id(candidate: VendorCleanupCandidate) -> str:
    return f"{_VENDOR_PREFIX}{candidate.candidate_id}"


def _candidate_reachable(candidate: VendorCleanupCandidate, roots: tuple[Path, ...]) -> bool:
    target = normalise_path(candidate.path)
    for root in roots:
        base = normalise_path(root)
        if target == base or target.startswith(base.rstrip(os.sep) + os.sep):
            return True
    return False


def _surfaceable_vendor_candidates(
    candidates: tuple[VendorCleanupCandidate, ...],
    drives: tuple[Path, ...],
) -> tuple[VendorCleanupCandidate, ...]:
    return tuple(
        candidate
        for candidate in candidates
        if candidate.kind in _QUANTIFIED_VENDOR_KINDS
        and _candidate_reachable(candidate, drives)
    )


class ProductDevCleanWindow(_BaseProductDevCleanWindow):
    """Normal DevClean scan with quantified vendor actions in the safe lane."""

    def __init__(self, root: tk.Tk) -> None:
        self._vendor_candidates: dict[str, VendorCleanupCandidate] = {}
        self._vendor_scan_candidates: dict[str, tuple[VendorCleanupCandidate, ...]] = {}
        self._vendor_scan_warnings: dict[str, tuple[str, ...]] = {}
        self._active_scan_drives: tuple[Path, ...] = ()
        super().__init__(root)

    def _build_results(self, page: ttk.Frame) -> None:
        super()._build_results(page)
        self._deletable_tree.heading("size", text="可清理")
        cleanup = self._buttons.get("recycle")
        if cleanup is not None:
            cleanup.configure(text="清理所选", command=lambda: self._delete(irreversible=False))
        purge = self._buttons.get("purge")
        if purge is not None:
            purge.pack_forget()

    def _start_scan(self) -> None:
        self._active_scan_drives = tuple(
            drive for drive, state in self._drive_vars.items() if state.get()
        )
        self._vendor_candidates.clear()
        self._vendor_scan_candidates.clear()
        self._vendor_scan_warnings.clear()
        super()._start_scan()

    def _scan_worker(
        self,
        token: str,
        roots: tuple[Path, ...],
        cancel: CancellationToken,
        active_rules: UserRules,
        known_roots: tuple[object, ...],
    ) -> None:
        """Inventory vendor roots once, then prune them from generic traversal."""

        try:
            inventory = inventory_vendor_cleanup_candidates()
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            inventory_candidates: tuple[VendorCleanupCandidate, ...] = ()
            warnings = (f"vendor inventory: {error}",)
        else:
            inventory_candidates = inventory.candidates
            warnings = inventory.warnings

        drives = self._active_scan_drives
        visible = _surfaceable_vendor_candidates(inventory_candidates, drives)
        self._vendor_scan_candidates[token] = visible
        self._vendor_scan_warnings[token] = warnings

        # Every inventoried vendor root has already been walked by its provider
        # inventory. Scanning the same tree again file-by-file would only add
        # latency and can never create stronger authority than the provider.
        skip_vendor_paths = tuple(
            str(candidate.path)
            for candidate in inventory_candidates
            if _candidate_reachable(candidate, drives)
        )
        scan_rules = replace(
            active_rules.scan,
            excluded_paths=tuple(
                dict.fromkeys((*active_rules.scan.excluded_paths, *skip_vendor_paths))
            ),
        )
        effective_rules = replace(active_rules, scan=scan_rules)
        _BaseProductDevCleanWindow._scan_worker(
            self,
            token,
            roots,
            cancel,
            effective_rules,
            cast(tuple, known_roots),
        )

    def _publish(self, session: TriageSession) -> None:
        pending = self._vendor_scan_candidates.pop(self._scan_token, None)
        if pending is not None:
            self._vendor_candidates = {
                _vendor_row_id(candidate): candidate for candidate in pending
            }
        super()._publish(session)
        warnings = self._vendor_scan_warnings.pop(self._scan_token, ())
        if warnings and self._scan_started_at is None:
            self._status.set(
                f"扫描完成；{len(warnings):,} 个可选工具无法读取，其余结果不受影响。"
            )

    def _fill(self, tree: ttk.Treeview, items: tuple[TriageItem, ...] | list[TriageItem]) -> None:
        if not hasattr(self, "_deletable_tree") or tree is not self._deletable_tree:
            super()._fill(tree, items)
            return

        tree.delete(*tree.get_children())
        rows: list[tuple[int, str, str, str]] = []
        for item in items:
            size = self._size_of(item)
            label = (
                f"[整个目录] {item.path}"
                if item.target_kind is CleanupTargetKind.DIRECTORY
                else item.path
            )
            rows.append((size, item.path, app._format_bytes(size), label))
        for row_id, candidate in self._vendor_candidates.items():
            size = candidate.observed_bytes
            label = f"[官方清理] {candidate.label} · {candidate.path}"
            rows.append((size, row_id, app._format_bytes(size), label))

        rows.sort(key=lambda row: (-row[0], row[3].casefold()))
        for index, (_sort_size, row_id, rendered_size, label) in enumerate(
            rows[: app._ROWS_DRAWN]
        ):
            mark = self._TICKED if row_id in self._checked else self._UNTICKED
            tree.insert(
                "",
                tk.END,
                iid=row_id,
                values=(mark, rendered_size, label),
                tags=("odd",) if index % 2 else (),
            )

    def _on_row_double_click(self, event: tk.Event) -> str | None:
        tree = cast(ttk.Treeview, event.widget)
        if tree.identify_region(event.x, event.y) != "cell":
            return None
        row = tree.identify_row(event.y)
        candidate = self._vendor_candidates.get(row)
        if candidate is None:
            return super()._on_row_double_click(event)
        try:
            opened = app._open_path_in_explorer(str(candidate.path))
        except OSError as error:
            messagebox.showerror("无法打开位置", str(error))
            return "break"
        self._status.set(f"已在资源管理器中打开：{opened}")
        return "break"

    def _all_safe_row_ids(self) -> set[str]:
        return {item.path for item in self._deletable} | set(self._vendor_candidates)

    def _selected_vendor_candidates(self) -> tuple[VendorCleanupCandidate, ...]:
        return tuple(
            candidate
            for row_id, candidate in self._vendor_candidates.items()
            if row_id in self._checked
        )

    def _check_all(self, checked: bool) -> None:
        self._checked = self._all_safe_row_ids() if checked else set()
        mark = self._TICKED if checked else self._UNTICKED
        for row in self._deletable_tree.get_children():
            self._deletable_tree.set(row, "check", mark)
        self._refresh_totals()
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        super()._sync_buttons()
        if not hasattr(self, "_buttons"):
            return
        busy = self._busy is not None
        selected = len(self._selected_items()) + len(self._selected_vendor_candidates())
        all_rows = self._all_safe_row_ids()
        for key, enabled in (
            ("all", bool(all_rows)),
            ("none", bool(self._checked)),
            ("recycle", selected > 0),
        ):
            button = self._buttons.get(key)
            if button is not None:
                button.configure(state=tk.NORMAL if not busy and enabled else tk.DISABLED)
        purge = self._buttons.get("purge")
        if purge is not None:
            purge.configure(state=tk.DISABLED)

    def _refresh_totals(self) -> None:
        effective = app._drop_targets_covered_by_directory(self._deletable)
        direct_total = sum(self._size_of(item) for item in effective)
        vendor_total = sum(candidate.observed_bytes for candidate in self._vendor_candidates.values())
        found = direct_total + vendor_total
        all_rows = self._all_safe_row_ids()

        selected_direct = self._selected_items()
        selected_vendor = self._selected_vendor_candidates()
        selected_bytes = sum(self._size_of(item) for item in selected_direct) + sum(
            candidate.observed_bytes for candidate in selected_vendor
        )
        if all_rows and all_rows.issubset(self._checked):
            self._deletable_total.set(f"{app._format_bytes(found)}（{len(all_rows):,} 项）")
        elif all_rows:
            self._deletable_total.set(
                f"{app._format_bytes(selected_bytes)} / {app._format_bytes(found)}"
                f"（已勾选 {len(self._checked & all_rows):,} / {len(all_rows):,} 项）"
            )
        else:
            self._deletable_total.set("0 B（0 项）")

        unsure_shown = (
            f"，表中显示前 {app._ROWS_DRAWN:,} 项"
            if len(self._unsure) > app._ROWS_DRAWN
            else ""
        )
        self._unsure_total.set(
            f"{app._format_bytes(sum(self._size_of(item) for item in self._unsure))}"
            f"（{len(self._unsure):,} 项{unsure_shown}）"
        )

    def _delete(self, *, irreversible: bool) -> None:
        del irreversible  # Product cleanup has one clear action, not two mutation modes.
        items = self._selected_items()
        vendor = self._selected_vendor_candidates()
        if not items and not vendor:
            messagebox.showinfo("DevClean", "没有勾选任何安全清理项。")
            return

        self._busy = "deleting"
        self._sync_buttons()
        self._progress.configure(mode="indeterminate", value=0)
        self._progress.start(60)
        self._status.set(
            f"正在清理 {len(items) + len(vendor):,} 项；普通安全项直接释放空间，"
            "包管理器缓存由官方命令处理…"
        )
        token = uuid4().hex
        self._delete_token = token
        threading.Thread(
            target=self._product_delete_worker,
            args=(token, items, vendor),
            daemon=True,
        ).start()

    def _product_delete_worker(
        self,
        token: str,
        items: tuple[TriageItem, ...],
        vendor: tuple[VendorCleanupCandidate, ...],
    ) -> None:
        results: list[CleanupExecutionResult] = []
        reasons: dict[str, int] = {}
        filesystem_candidates: list[ScanCleanupCandidate] = []

        for item in items:
            try:
                filesystem_candidates.append(self._candidate(item))
            except (CleanupRefusal, OSError, TypeError, ValueError) as error:
                reason = app._reason_of(error)
                reasons[reason] = reasons.get(reason, 0) + 1

        if filesystem_candidates:
            try:
                plan = prepare_cleanup_plan(tuple(filesystem_candidates))
            except Exception as error:
                reason = app._reason_of(error)
                reasons[reason] = reasons.get(reason, 0) + len(filesystem_candidates)
            else:
                for batch in plan.batches:
                    try:
                        results.append(
                            execute_cleanup_batch(
                                batch,
                                CleanupMode.PERMANENT,
                                known_roots=self._known_roots,
                                delete_config=self._scan_rules.delete.classification,
                                keep_config=self._scan_rules.keep.classification,
                            )
                        )
                    except Exception as error:
                        reason = app._reason_of(error)
                        reasons[reason] = reasons.get(reason, 0) + len(batch.actions)

        for candidate in vendor:
            try:
                vendor_result = execute_vendor_cleanup(candidate)
            except (OSError, RuntimeError, TypeError, ValueError) as error:
                reason = app._reason_of(error)
                reasons[reason] = reasons.get(reason, 0) + 1
                continue
            results.append(
                CleanupExecutionResult(
                    action_states=((candidate.candidate_id, ActionState.PURGED),),
                    purged_logical_bytes=vendor_result.reclaimed_bytes,
                    completed_paths=(str(vendor_result.path),),
                )
            )

        self._events.put(("delete_done", (token, tuple(results), reasons)))

    def _report_deletion(
        self,
        results: tuple[CleanupExecutionResult, ...],
        reasons: dict[str, int],
    ) -> None:
        successful_vendor_ids = {
            action_id
            for result in results
            for action_id, state in result.action_states
            if state is ActionState.PURGED
        }
        super()._report_deletion(results, reasons)
        removed_rows = {
            row_id
            for row_id, candidate in self._vendor_candidates.items()
            if candidate.candidate_id in successful_vendor_ids
        }
        if removed_rows:
            self._vendor_candidates = {
                row_id: candidate
                for row_id, candidate in self._vendor_candidates.items()
                if row_id not in removed_rows
            }
            self._checked -= removed_rows
            self._fill(self._deletable_tree, self._deletable)
            self._refresh_totals()
            self._sync_buttons()
        current = self._status.get()
        if current.startswith("已删除"):
            self._status.set("已清理" + current.removeprefix("已删除"))


__all__ = [
    "ProductDevCleanWindow",
    "_surfaceable_vendor_candidates",
    "_vendor_row_id",
]
