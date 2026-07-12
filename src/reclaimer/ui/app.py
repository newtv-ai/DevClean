"""Tkinter GUI for bounded, no-database scan triage."""

# ruff: noqa: RUF001

from __future__ import annotations

import json
import queue
import sys
import tempfile
import threading
import tkinter as tk
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from typing import cast

from reclaimer.core.ai_review import (
    AiDecision,
    AiReviewError,
    AiReviewPacket,
    AiReviewResult,
    build_ai_review_packet,
    parse_ai_review_response,
)
from reclaimer.core.auto_clean import (
    permanently_clean_model_approved_record,
    permanently_clean_temp_record,
)
from reclaimer.core.recycle import RecycleRefusal, recycle_targets, target_from_scan_record
from reclaimer.core.triage import ReviewLane, TriageItem, TriageSession, triage_file
from reclaimer.platform.windows.permanent_delete import PermanentDeleteRefusal
from reclaimer.platform.windows.recycle_bin import RecycleBinError, recycle_file
from reclaimer.scanner import ScanOptions, ScanRecordKind, scan_roots

_LANE_TITLES = {
    ReviewLane.AUTO_CLEAN: "可自动清理",
    ReviewLane.AI_REVIEW: "需要 AI 解释",
    ReviewLane.USER_REVIEW: "需要你决定",
    ReviewLane.PROTECTED: "受保护，不清理",
}


class ReclaimerWindow:
    def __init__(self, root: tk.Tk) -> None:
        self._root = root
        self._root.title("Reclaimer — 安全磁盘清理")
        self._root.minsize(1040, 680)
        self._events: queue.Queue[tuple[str, object]] = queue.Queue()
        self._session: TriageSession | None = None
        self._ai_packet: AiReviewPacket | None = None
        self._resolved_ai_paths: set[str] = set()
        self._next_display_id = 0
        self._root_path = tk.StringVar(value=str(Path.home()))
        self._status = tk.StringVar(value="选择目录后开始扫描；扫描结果不会写入 SQLite。")
        self._counts = {lane: tk.StringVar(value="0 个文件 · 0 B") for lane in ReviewLane}
        self._trees: dict[ReviewLane, ttk.Treeview] = {}
        self._displayed_items: dict[str, TriageItem] = {}
        self._scan_button: ttk.Button | None = None
        self._build()
        self._root.after(80, self._drain_events)

    def _build(self) -> None:
        content = ttk.Frame(self._root, padding=16)
        content.pack(fill=tk.BOTH, expand=True)

        controls = ttk.Frame(content)
        controls.pack(fill=tk.X)
        ttk.Label(controls, text="扫描目录：").pack(side=tk.LEFT)
        ttk.Entry(controls, textvariable=self._root_path, width=80).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8)
        )
        ttk.Button(controls, text="选择目录", command=self._choose_root).pack(side=tk.LEFT)
        self._scan_button = ttk.Button(controls, text="开始扫描", command=self._start_scan)
        self._scan_button.pack(side=tk.LEFT, padx=(8, 0))

        ttk.Label(
            content,
            text=(
                "开始扫描后，仅会永久清理“当前用户 TEMP 目录、超过 7 天、未锁定、非重解析且"
                "通过句柄复核”的文件；"
                "开发缓存先交给 AI 逐项解释；其余项目由你决定。"
            ),
            wraplength=960,
        ).pack(fill=tk.X, pady=(10, 6))

        notebook = ttk.Notebook(content)
        notebook.pack(fill=tk.BOTH, expand=True)
        for lane in ReviewLane:
            frame = ttk.Frame(notebook, padding=8)
            notebook.add(frame, text=_LANE_TITLES[lane])
            ttk.Label(frame, textvariable=self._counts[lane]).pack(anchor=tk.W, pady=(0, 6))
            tree = ttk.Treeview(
                frame,
                columns=("size", "reason", "path"),
                show="headings",
                selectmode="extended",
            )
            tree.heading("size", text="大小")
            tree.heading("reason", text="判定依据")
            tree.heading("path", text="路径")
            tree.column("size", width=100, stretch=False, anchor=tk.E)
            tree.column("reason", width=250, stretch=False)
            tree.column("path", width=650, stretch=True)
            tree.pack(fill=tk.BOTH, expand=True)
            self._trees[lane] = tree

        actions = ttk.Frame(content)
        actions.pack(fill=tk.X, pady=(10, 0))
        ttk.Button(
            actions,
            text="复制 AI 审查包",
            command=self._copy_ai_review_packet,
        ).pack(side=tk.LEFT)
        ttk.Button(
            actions,
            text="粘贴 AI 回复并处理确定项",
            command=self._open_ai_response_dialog,
        ).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(
            actions,
            text="将选中的“需要你决定”项移到回收站",
            command=self._recycle_selected_user_items,
        ).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Label(
            actions,
            text="扫描不会创建 SQLite 索引；只保留每栏最大的 500 项用于显示和 AI 审查。",
        ).pack(side=tk.LEFT, padx=(12, 0))

        ttk.Separator(content).pack(fill=tk.X, pady=(10, 6))
        ttk.Label(content, textvariable=self._status).pack(anchor=tk.W)

    def _choose_root(self) -> None:
        selected = filedialog.askdirectory(initialdir=self._root_path.get() or str(Path.home()))
        if selected:
            self._root_path.set(selected)

    def _start_scan(self) -> None:
        root = Path(self._root_path.get()).expanduser()
        if not root.is_dir():
            messagebox.showerror("Reclaimer", "请选择一个存在的目录。")
            return
        if self._scan_button is not None:
            self._scan_button.configure(state=tk.DISABLED)
        self._ai_packet = None
        self._resolved_ai_paths.clear()
        self._clear_trees()
        self._status.set(
            "正在流式扫描；确定安全的旧 TEMP 文件会被立即永久清理；不会创建 SQLite 索引…"
        )
        worker = threading.Thread(target=self._scan_worker, args=(root,), daemon=True)
        worker.start()

    def _scan_worker(self, root: Path) -> None:
        session = TriageSession()
        observed = 0
        cleaned_files = 0
        cleaned_bytes = 0
        skipped_auto_clean = 0
        temp_root = Path(tempfile.gettempdir())
        try:
            for record in scan_roots((root,), ScanOptions(include_directories=False)):
                if record.kind is not ScanRecordKind.FILE:
                    continue
                item = triage_file(record, temp_root=temp_root)
                if item.lane is ReviewLane.AUTO_CLEAN:
                    try:
                        permanently_clean_temp_record(record, temp_root=temp_root)
                    except (OSError, PermanentDeleteRefusal, ValueError):
                        skipped_auto_clean += 1
                        item = replace(
                            item,
                            lane=ReviewLane.USER_REVIEW,
                            reason="自动清理跳过：文件已变化、被占用或无法句柄复核；请由你决定",
                        )
                    else:
                        cleaned_files += 1
                        cleaned_bytes += record.allocated_size or record.logical_size
                        item = replace(item, reason=f"已自动永久清理：{item.reason}")
                session.add(item)
                observed += 1
                if observed % 256 == 0:
                    self._events.put(
                        (
                            "progress",
                            (observed, cleaned_files, cleaned_bytes, skipped_auto_clean, session),
                        )
                    )
        except OSError as error:
            self._events.put(("error", str(error)))
            return
        self._events.put(
            (
                "finished",
                (observed, cleaned_files, cleaned_bytes, skipped_auto_clean, session),
            )
        )

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self._events.get_nowait()
                if kind == "progress":
                    observed, cleaned_files, cleaned_bytes, skipped_auto_clean, session = cast(
                        tuple[int, int, int, int, TriageSession], payload
                    )
                    self._update_summary(session)
                    self._status.set(
                        f"已扫描 {observed:,} 个文件；已自动清理 {cleaned_files:,} 个"
                        f"（{_format_bytes(cleaned_bytes)}）；跳过 {skipped_auto_clean:,} 个…"
                    )
                elif kind == "finished":
                    observed, cleaned_files, cleaned_bytes, skipped_auto_clean, session = cast(
                        tuple[int, int, int, int, TriageSession], payload
                    )
                    self._session = session
                    self._render_session(session)
                    self._status.set(
                        f"扫描完成：{observed:,} 个文件；已自动永久清理 {cleaned_files:,} 个"
                        f"（{_format_bytes(cleaned_bytes)}），跳过 {skipped_auto_clean:,} 个；"
                        "仅保留每栏最大的 500 项。"
                    )
                    if self._scan_button is not None:
                        self._scan_button.configure(state=tk.NORMAL)
                elif kind == "error":
                    messagebox.showerror("Reclaimer", f"扫描失败：{payload}")
                    if self._scan_button is not None:
                        self._scan_button.configure(state=tk.NORMAL)
        except queue.Empty:
            pass
        self._root.after(80, self._drain_events)

    def _render_session(self, session: TriageSession) -> None:
        self._clear_trees()
        self._update_summary(session)
        for lane in ReviewLane:
            tree = self._trees[lane]
            for index, item in enumerate(session.items(lane)):
                item_id = f"{lane.value}:{index}"
                self._displayed_items[item_id] = item
                tree.insert(
                    "",
                    tk.END,
                    iid=item_id,
                    values=(_format_bytes(item.logical_size), item.reason, item.path),
                )

    def _update_summary(self, session: TriageSession) -> None:
        for lane in ReviewLane:
            summary = session.summary(lane)
            self._counts[lane].set(
                f"{summary.files:,} 个文件 · {_format_bytes(summary.logical_bytes)}"
            )

    def _clear_trees(self) -> None:
        self._displayed_items.clear()
        for tree in self._trees.values():
            tree.delete(*tree.get_children())

    def _recycle_selected_user_items(self) -> None:
        tree = self._trees[ReviewLane.USER_REVIEW]
        item_ids = tree.selection()
        if not item_ids:
            messagebox.showinfo("Reclaimer", "请先在“需要你决定”栏选择要移到回收站的文件。")
            return
        if len(item_ids) > 32:
            messagebox.showerror("Reclaimer", "一次最多选择 32 个文件。")
            return
        items = [self._displayed_items[item_id] for item_id in item_ids]
        paths = "\n".join(item.path for item in items)
        approved = messagebox.askyesno(
            "确认移到回收站",
            "以下经过扫描快照复核的文件将移到 Windows 回收站：\n\n"
            f"{paths}\n\n"
            "若文件在扫描后发生变化、被占用或不再安全，操作会被拒绝。",
        )
        if not approved:
            return
        try:
            targets = tuple(
                target_from_scan_record(item.record, candidate_id=f"gui_recycle_{index}")
                for index, item in enumerate(items, start=1)
            )
            recycled = recycle_targets(targets, recycle_file)
        except (OSError, RecycleBinError, RecycleRefusal, ValueError) as error:
            messagebox.showerror("未移动", f"没有完成回收站操作：{error}")
            return
        for item_id in item_ids:
            tree.delete(item_id)
            self._displayed_items.pop(item_id, None)
        self._status.set(f"已将 {len(recycled)} 个你确认的文件移到 Windows 回收站。")

    def _copy_ai_review_packet(self) -> None:
        if self._session is None:
            messagebox.showinfo("Reclaimer", "请先完成一次扫描。")
            return
        pending = tuple(
            item
            for item in self._session.items(ReviewLane.AI_REVIEW)
            if item.path not in self._resolved_ai_paths
        )
        if not pending:
            messagebox.showinfo("Reclaimer", "当前显示的 AI 审查项都已处理。")
            return
        try:
            packet = build_ai_review_packet(pending[:50])
        except AiReviewError as error:
            messagebox.showerror("AI 审查包", str(error))
            return
        self._ai_packet = packet
        self._root.clipboard_clear()
        self._root.clipboard_append(json.dumps(packet.payload(), ensure_ascii=False, indent=2))
        self._status.set(
            f"已复制 {len(packet.entries)} 项 AI 审查包（不含文件内容）；"
            "将模型回复粘贴回来即可处理。"
        )

    def _open_ai_response_dialog(self) -> None:
        if self._ai_packet is None:
            messagebox.showinfo(
                "Reclaimer", "请先复制一个 AI 审查包，并让模型按其中的 response_contract 回复。"
            )
            return
        dialog = tk.Toplevel(self._root)
        dialog.title("粘贴 AI 审查回复")
        dialog.minsize(760, 480)
        dialog.transient(self._root)
        dialog.grab_set()
        content = ttk.Frame(dialog, padding=12)
        content.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            content,
            text="只接受当前审查包要求的 JSON。DELETE 会对该批次的精确扫描文件"
            "做句柄复核后永久清理；"
            "UNSURE 会转入“需要你决定”。",
            wraplength=720,
        ).pack(fill=tk.X, pady=(0, 8))
        response = ScrolledText(content, wrap=tk.WORD, height=20)
        response.pack(fill=tk.BOTH, expand=True)
        buttons = ttk.Frame(content)
        buttons.pack(fill=tk.X, pady=(8, 0))

        def apply_response() -> None:
            packet = self._ai_packet
            if packet is None:
                return
            try:
                results = parse_ai_review_response(response.get("1.0", tk.END), packet)
            except AiReviewError as error:
                messagebox.showerror("AI 回复无效", str(error), parent=dialog)
                return
            dialog.destroy()
            self._apply_ai_results(results)

        ttk.Button(buttons, text="处理此回复", command=apply_response).pack(side=tk.RIGHT)
        ttk.Button(buttons, text="取消", command=dialog.destroy).pack(side=tk.RIGHT, padx=(0, 8))

    def _apply_ai_results(self, results: tuple[AiReviewResult, ...]) -> None:
        deleted = 0
        kept = 0
        user_review = 0
        for result in results:
            self._resolved_ai_paths.add(result.item.path)
            if result.decision is AiDecision.DELETE:
                try:
                    permanently_clean_model_approved_record(result.item.record)
                except (OSError, PermanentDeleteRefusal, ValueError) as error:
                    self._move_ai_item_to_user_review(
                        result.item,
                        f"AI 建议删除但句柄复核失败；请由你决定：{_short_text(str(error))}",
                    )
                    user_review += 1
                else:
                    self._remove_displayed_ai_item(result.item)
                    deleted += 1
            elif result.decision is AiDecision.KEEP:
                self._mark_displayed_ai_item(
                    result.item, f"AI 建议保留：{_short_text(result.explanation)}"
                )
                kept += 1
            else:
                self._move_ai_item_to_user_review(
                    result.item, f"AI 未确认：{_short_text(result.explanation)}"
                )
                user_review += 1
        self._ai_packet = None
        self._status.set(
            f"AI 审查已处理：永久清理 {deleted} 项，建议保留 {kept} 项，"
            f"转入你决定 {user_review} 项。"
        )

    def _find_displayed_item_id(self, item: TriageItem) -> str | None:
        return next(
            (item_id for item_id, displayed in self._displayed_items.items() if displayed is item),
            None,
        )

    def _remove_displayed_ai_item(self, item: TriageItem) -> None:
        item_id = self._find_displayed_item_id(item)
        if item_id is not None:
            self._trees[ReviewLane.AI_REVIEW].delete(item_id)
            self._displayed_items.pop(item_id, None)

    def _mark_displayed_ai_item(self, item: TriageItem, reason: str) -> None:
        item_id = self._find_displayed_item_id(item)
        if item_id is None:
            return
        updated = replace(item, reason=reason)
        self._displayed_items[item_id] = updated
        self._trees[ReviewLane.AI_REVIEW].item(
            item_id,
            values=(_format_bytes(updated.logical_size), updated.reason, updated.path),
        )

    def _move_ai_item_to_user_review(self, item: TriageItem, reason: str) -> None:
        self._remove_displayed_ai_item(item)
        updated = replace(item, lane=ReviewLane.USER_REVIEW, reason=reason)
        item_id = f"{ReviewLane.USER_REVIEW}:review-{self._next_display_id}"
        self._next_display_id += 1
        self._displayed_items[item_id] = updated
        self._trees[ReviewLane.USER_REVIEW].insert(
            "",
            tk.END,
            iid=item_id,
            values=(_format_bytes(updated.logical_size), updated.reason, updated.path),
        )


def _review_item(item: TriageItem) -> dict[str, object]:
    return {
        "path": _redact_path(item.path),
        "logical_size": item.logical_size,
        "reason": item.reason,
    }


def _redact_path(path: str) -> str:
    home = str(Path.home())
    return path.replace(home, "<USER>", 1)


def _format_bytes(value: int) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    raise AssertionError("unreachable")


def _short_text(value: str, *, limit: int = 180) -> str:
    return value if len(value) <= limit else f"{value[: limit - 1]}…"


def main(argv: Sequence[str] | None = None) -> int:
    if tuple(sys.argv[1:] if argv is None else argv) == ("--smoke",):
        return 0
    root = tk.Tk()
    ReclaimerWindow(root)
    root.mainloop()
    return 0


__all__ = ["main"]
