"""Ollama local model inventory and explicit vendor-owned removal UI."""

# ruff: noqa: RUF001

from __future__ import annotations

import queue
import threading
import tkinter as tk
from dataclasses import dataclass
from tkinter import messagebox, ttk

from devclean.core.ollama_model_maintenance import (
    OllamaModelDeleteResult,
    OllamaModelEntry,
    OllamaModelInventory,
    delete_ollama_model,
    inventory_ollama_models,
)


@dataclass(frozen=True, slots=True)
class _InventoryEvent:
    inventory: OllamaModelInventory


@dataclass(frozen=True, slots=True)
class _CleanupEvent:
    results: tuple[OllamaModelDeleteResult, ...]
    error: str | None = None


class _OllamaModelMaintenanceDialog:
    def __init__(self, parent: tk.Tk | tk.Toplevel) -> None:
        self._parent = parent
        self._window = tk.Toplevel(parent)
        self._window.title("Ollama 本机模型维护")
        self._window.geometry("1060x690")
        self._window.minsize(880, 560)
        self._events: queue.Queue[_InventoryEvent | _CleanupEvent | Exception] = queue.Queue()
        self._status = tk.StringVar(value="正在通过 Ollama 本机 API 读取模型…")
        self._inventory: OllamaModelInventory | None = None
        self._rows: dict[str, OllamaModelEntry] = {}
        self._busy = False

        container = ttk.Frame(self._window, padding=12)
        container.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            container,
            text="Ollama 本机模型",
            font=("Segoe UI", 13, "bold"),
        ).pack(anchor=tk.W)
        ttk.Label(
            container,
            text=(
                "Ollama 模型库不是普通缓存：manifest 和 blob 可能被多个模型共享。"
                "DevClean 不删除内部文件，只通过 Ollama 官方本机 API 删除你明确选择的模型。"
                "这属于“你来决定”，默认不选，也不需要 AI。"
            ),
            wraplength=1020,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(4, 8))

        summary = ttk.LabelFrame(container, text="本机 Ollama", padding=8)
        summary.pack(fill=tk.X, pady=(0, 8))
        self._summary = ttk.Label(
            summary,
            text="尚未完成统计。",
            wraplength=1000,
            justify=tk.LEFT,
        )
        self._summary.pack(anchor=tk.W)

        table_frame = ttk.Frame(container)
        table_frame.pack(fill=tk.BOTH, expand=True)
        self._tree = ttk.Treeview(
            table_frame,
            columns=("size", "state", "params", "quant", "family", "digest"),
            show="tree headings",
            selectmode="extended",
        )
        self._tree.heading("#0", text="模型")
        self._tree.heading("size", text="逻辑大小")
        self._tree.heading("state", text="状态")
        self._tree.heading("params", text="参数")
        self._tree.heading("quant", text="量化")
        self._tree.heading("family", text="Family")
        self._tree.heading("digest", text="Digest")
        self._tree.column("#0", width=260, stretch=True)
        self._tree.column("size", width=105, anchor=tk.E, stretch=False)
        self._tree.column("state", width=90, stretch=False)
        self._tree.column("params", width=90, stretch=False)
        self._tree.column("quant", width=95, stretch=False)
        self._tree.column("family", width=110, stretch=False)
        self._tree.column("digest", width=220, stretch=True)
        scrollbar = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self._tree.yview)
        self._tree.configure(yscrollcommand=scrollbar.set)
        self._tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        ttk.Label(
            container,
            text=(
                "安全边界：只连接 loopback Ollama API；非本机 OLLAMA_HOST 会被拒绝。"
                "模型库必须位于本地固定磁盘；已加载模型不能删除。删除前会重新读取模型"
                " digest 和运行状态，删除后再通过 /api/tags 验证模型确实消失。"
            ),
            wraplength=1020,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(8, 0))
        ttk.Label(
            container,
            text=(
                "表中的大小是模型逻辑大小，不等于真正可释放空间，因为 blob 可能被其他模型共享。"
                "删除后 DevClean 只在能读取本地模型库时报告实测目录差值。"
            ),
            wraplength=1020,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(4, 0))
        ttk.Label(
            container,
            textvariable=self._status,
            wraplength=1020,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(6, 0))

        footer = ttk.Frame(container)
        footer.pack(fill=tk.X, pady=(10, 0))
        self._delete_button = ttk.Button(
            footer,
            text="删除选中的模型…",
            command=self._start_cleanup,
            state=tk.DISABLED,
        )
        self._delete_button.pack(side=tk.RIGHT, padx=(8, 0))
        self._refresh_button = ttk.Button(
            footer,
            text="重新统计",
            command=self._start_inventory,
        )
        self._refresh_button.pack(side=tk.RIGHT, padx=(8, 0))
        ttk.Button(footer, text="关闭", command=self._window.destroy).pack(side=tk.RIGHT)

        self._window.after(100, self._poll)
        self._start_inventory()

    def show(self) -> None:
        self._window.transient(self._parent)
        self._window.grab_set()
        self._window.focus_set()

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._refresh_button.configure(state=tk.DISABLED if busy else tk.NORMAL)
        enabled = (
            not busy
            and self._inventory is not None
            and self._inventory.deletion_supported
            and bool(self._inventory.models)
        )
        self._delete_button.configure(state=tk.NORMAL if enabled else tk.DISABLED)

    def _start_inventory(self) -> None:
        if self._busy:
            return
        self._set_busy(True)
        self._status.set("正在读取 Ollama /api/version、/api/tags 和 /api/ps…")

        def work() -> None:
            try:
                inventory = inventory_ollama_models()
            except Exception as error:
                self._events.put(error)
            else:
                self._events.put(_InventoryEvent(inventory))

        threading.Thread(
            target=work,
            name="DevClean-Ollama-model-inventory",
            daemon=True,
        ).start()

    def _start_cleanup(self) -> None:
        inventory = self._inventory
        if self._busy or inventory is None or not inventory.deletion_supported:
            return
        selected = [self._rows[item] for item in self._tree.selection() if item in self._rows]
        if not selected:
            messagebox.showinfo(
                "Ollama 本机模型维护",
                "请先选择一个或多个要删除的模型。",
                parent=self._window,
            )
            return
        running = [model.name for model in selected if model.running]
        if running:
            messagebox.showwarning(
                "模型仍在运行",
                "以下模型当前已加载，不能删除：\n" + "\n".join(running),
                parent=self._window,
            )
            return

        logical = sum(model.logical_bytes for model in selected)
        names = "\n".join(f"• {model.name}" for model in selected[:12])
        if len(selected) > 12:
            names += f"\n…另有 {len(selected) - 12} 个"
        if not messagebox.askyesno(
            "确认删除 Ollama 模型",
            (
                f"将通过 Ollama 官方 API 删除 {len(selected)} 个本机模型：\n\n"
                f"{names}\n\n逻辑大小合计约 {_format_bytes(logical)}。"
                "共享 blob 会由 Ollama 自己处理，因此实际释放空间可能明显更小。\n\n"
                "确定继续吗？"
            ),
            icon=messagebox.WARNING,
            parent=self._window,
        ):
            return

        self._set_busy(True)
        self._status.set(f"正在重新验证并删除 {len(selected)} 个 Ollama 模型…")

        def work(models: tuple[OllamaModelEntry, ...] = tuple(selected)) -> None:
            results: list[OllamaModelDeleteResult] = []
            error_text: str | None = None
            for model in models:
                try:
                    results.append(
                        delete_ollama_model(
                            model.name,
                            expected_digest=model.digest,
                        )
                    )
                except Exception as error:
                    error_text = str(error)
                    break
            self._events.put(_CleanupEvent(tuple(results), error_text))

        threading.Thread(
            target=work,
            name="DevClean-Ollama-model-delete",
            daemon=True,
        ).start()

    def _poll(self) -> None:
        if not self._window.winfo_exists():
            return
        try:
            outcome = self._events.get_nowait()
        except queue.Empty:
            self._window.after(100, self._poll)
            return

        if isinstance(outcome, Exception):
            self._status.set(f"Ollama 模型统计失败：{outcome}")
            self._set_busy(False)
        elif isinstance(outcome, _InventoryEvent):
            self._inventory = outcome.inventory
            self._render(outcome.inventory)
            self._set_busy(False)
        else:
            reclaimed_values = [
                result.measured_reclaimed_bytes
                for result in outcome.results
                if result.measured_reclaimed_bytes is not None
            ]
            measured = sum(reclaimed_values) if reclaimed_values else None
            suffix = (
                f"，本地模型库实测释放约 {_format_bytes(measured)}" if measured is not None else ""
            )
            if outcome.error is None:
                self._status.set(f"已删除 {len(outcome.results)} 个模型{suffix}；正在重新统计…")
            else:
                self._status.set(
                    f"已删除 {len(outcome.results)} 个模型{suffix}，随后停止："
                    f"{outcome.error}；正在重新统计…"
                )
            self._busy = False
            self._start_inventory()
        self._window.after(100, self._poll)

    def _render(self, inventory: OllamaModelInventory) -> None:
        for item in self._tree.get_children():
            self._tree.delete(item)
        self._rows.clear()
        for index, model in enumerate(inventory.models):
            item_id = f"model-{index}"
            self._rows[item_id] = model
            self._tree.insert(
                "",
                tk.END,
                iid=item_id,
                text=model.name,
                values=(
                    _format_bytes(model.logical_bytes),
                    "已加载" if model.running else "未加载",
                    model.parameter_size or "-",
                    model.quantization_level or "-",
                    model.family or "-",
                    _short_digest(model.digest),
                ),
            )

        root = str(inventory.model_root) if inventory.model_root is not None else "未知"
        delete_state = (
            "可由你选择模型删除"
            if inventory.deletion_supported
            else "模型库不是本地固定磁盘：只报告，不提供删除"
        )
        self._summary.configure(
            text=(
                f"Ollama 版本: {inventory.version}\n"
                f"本机 API: {inventory.endpoint}\n"
                f"模型目录: {root}\n"
                f"状态: {delete_state}"
            )
        )
        self._status.set(
            f"共 {len(inventory.models)} 个模型，逻辑大小合计约 "
            f"{_format_bytes(inventory.logical_model_bytes)}。默认不选择；由你决定。"
        )


def open_ollama_model_maintenance_dialog(parent: tk.Tk | tk.Toplevel) -> None:
    _OllamaModelMaintenanceDialog(parent).show()


def _short_digest(value: str) -> str:
    return value if len(value) <= 24 else value[:24] + "…"


def _format_bytes(value: int) -> str:
    amount = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value:,} B"


__all__ = ["open_ollama_model_maintenance_dialog"]
