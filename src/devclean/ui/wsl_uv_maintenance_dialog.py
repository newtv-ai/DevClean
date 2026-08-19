"""WSL uv cache maintenance through uv's own periodic prune command."""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from dataclasses import dataclass
from tkinter import messagebox, ttk

from devclean.core.wsl_inventory import WslInventory, inspect_wsl
from devclean.core.wsl_uv_maintenance import (
    WslUvCacheInventory,
    WslUvPruneResult,
    inventory_wsl_uv_cache,
    prune_wsl_uv_cache,
)


@dataclass(frozen=True, slots=True)
class _DistroEvent:
    inventory: WslInventory | None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class _UvEvent:
    inventory: WslUvCacheInventory | None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class _PruneEvent:
    result: WslUvPruneResult | None
    error: str | None = None


class _WslUvMaintenanceDialog:
    def __init__(self, parent: tk.Tk | tk.Toplevel) -> None:
        self._parent = parent
        self._window = tk.Toplevel(parent)
        self._window.title("WSL uv 缓存维护")
        self._window.geometry("920x620")
        self._window.minsize(780, 540)
        self._events: queue.Queue[_DistroEvent | _UvEvent | _PruneEvent] = queue.Queue()
        self._status = tk.StringVar(value="正在读取 WSL 发行版列表...")
        self._selected = tk.StringVar()
        self._wsl_inventory: WslInventory | None = None
        self._uv_inventory: WslUvCacheInventory | None = None
        self._busy = False

        container = ttk.Frame(self._window, padding=12)
        container.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            container,
            text="WSL uv 缓存",
            font=("Segoe UI", 13, "bold"),
        ).pack(anchor=tk.W)
        ttk.Label(
            container,
            text=(
                "只使用所选 WSL 发行版中的 `uv cache dir` 和 `uv cache prune`. "
                "DevClean 不直接修改 uv cache, 不使用 `cache clean`, "
                "也绝不添加 `--force` 或 CI 专用 `--ci`."
            ),
            wraplength=880,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(4, 10))

        selector = ttk.LabelFrame(container, text="选择发行版", padding=10)
        selector.pack(fill=tk.X)
        self._combo = ttk.Combobox(
            selector,
            textvariable=self._selected,
            state="readonly",
            width=42,
        )
        self._combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._inspect_button = ttk.Button(
            selector,
            text="检查 uv 缓存",
            command=self._start_uv_inventory,
            state=tk.DISABLED,
        )
        self._inspect_button.pack(side=tk.LEFT, padx=(8, 0))
        self._refresh_button = ttk.Button(
            selector,
            text="刷新发行版",
            command=self._start_distro_inventory,
        )
        self._refresh_button.pack(side=tk.LEFT, padx=(8, 0))

        details = ttk.LabelFrame(container, text="uv 判定", padding=10)
        details.pack(fill=tk.X, pady=(10, 0))
        self._details = ttk.Label(
            details,
            text="先选择发行版并检查 uv 缓存.",
            wraplength=850,
            justify=tk.LEFT,
        )
        self._details.pack(anchor=tk.W)

        explanation = ttk.LabelFrame(container, text="清理语义", padding=10)
        explanation.pack(fill=tk.BOTH, expand=True, pady=(10, 0))
        ttk.Label(
            explanation,
            text=(
                "Astral 将 `uv cache prune` 定义为可周期运行的安全维护: 它只移除"
                "不再使用的 cache entries，以及可按需重建的 centralized project environments.\n\n"
                "uv 自己会给修改 cache 的操作加锁，并在其他 uv 命令运行时等待. "
                "DevClean 保留这套 vendor 并发保护，不用 `--force` 绕过锁.\n\n"
                "这条规则是 DETERMINISTIC_CANDIDATE，不送 AI. 清理后未来可能需要重新"
                "下载或重建部分缓存内容. 在 WSL 2 中，Linux 逻辑空间的释放也不等于"
                "Windows 侧虚拟磁盘文件会同步缩小."
            ),
            wraplength=850,
            justify=tk.LEFT,
        ).pack(anchor=tk.W)

        ttk.Label(
            container,
            textvariable=self._status,
            wraplength=880,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(10, 0))

        footer = ttk.Frame(container)
        footer.pack(fill=tk.X, pady=(10, 0))
        self._prune_button = ttk.Button(
            footer,
            text="通过 uv cache prune 维护",
            command=self._start_prune,
            state=tk.DISABLED,
        )
        self._prune_button.pack(side=tk.RIGHT, padx=(8, 0))
        ttk.Button(footer, text="关闭", command=self._window.destroy).pack(side=tk.RIGHT)

        self._window.after(100, self._poll)
        self._start_distro_inventory()

    def show(self) -> None:
        self._window.transient(self._parent)
        self._window.grab_set()
        self._window.focus_set()

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._refresh_button.configure(state=tk.DISABLED if busy else tk.NORMAL)
        inspect_state = tk.NORMAL if not busy and self._wsl_inventory is not None else tk.DISABLED
        self._inspect_button.configure(state=inspect_state)
        prune_state = tk.NORMAL if not busy and self._uv_inventory is not None else tk.DISABLED
        self._prune_button.configure(state=prune_state)

    def _start_distro_inventory(self) -> None:
        if self._busy:
            return
        self._uv_inventory = None
        self._set_busy(True)
        self._status.set("正在读取 WSL 已注册发行版和 running 状态...")

        def work() -> None:
            try:
                inventory = inspect_wsl()
            except Exception as error:
                self._events.put(_DistroEvent(None, str(error)))
            else:
                self._events.put(_DistroEvent(inventory))

        threading.Thread(target=work, name="DevClean-WSL-uv-distros", daemon=True).start()

    def _start_uv_inventory(self) -> None:
        if self._busy or self._wsl_inventory is None:
            return
        name = self._selected.get().strip()
        distro = next(
            (item for item in self._wsl_inventory.distributions if item.name == name),
            None,
        )
        if distro is None:
            messagebox.showinfo("WSL uv 缓存维护", "请先选择一个已注册的 WSL 发行版.")
            return
        if not distro.running and not messagebox.askyesno(
            "WSL uv 缓存维护",
            "所选发行版当前已停止. 检查 uv 需要在该发行版内执行命令, "
            "因此可能启动它. 是否继续?",
        ):
            return

        self._uv_inventory = None
        self._set_busy(True)
        self._status.set(f"正在通过 {distro.name} 内的 uv 查询有效 cache...")

        def work(distribution: str = distro.name) -> None:
            try:
                inventory = inventory_wsl_uv_cache(distribution)
            except Exception as error:
                self._events.put(_UvEvent(None, str(error)))
            else:
                self._events.put(_UvEvent(inventory))

        threading.Thread(target=work, name="DevClean-WSL-uv-inventory", daemon=True).start()

    def _start_prune(self) -> None:
        if self._busy or self._uv_inventory is None:
            return
        inventory = self._uv_inventory
        if not messagebox.askyesno(
            "确认维护 WSL uv 缓存",
            f"发行版: {inventory.distribution}\n"
            f"uv: {inventory.version_text}\n"
            f"cache: {inventory.cache_path}\n\n"
            "DevClean 将重新确认这些身份，然后固定该 cache 路径执行 "
            "`uv cache prune`. 不会执行 `clean`, `--force` 或 `--ci`. 继续吗?",
        ):
            return

        self._set_busy(True)
        self._status.set("正在重新确认 uv/cache 身份并等待 uv 自己的 cache lock...")

        def work(expected: WslUvCacheInventory = inventory) -> None:
            try:
                result = prune_wsl_uv_cache(expected)
            except Exception as error:
                self._events.put(_PruneEvent(None, str(error)))
            else:
                self._events.put(_PruneEvent(result))

        threading.Thread(target=work, name="DevClean-WSL-uv-prune", daemon=True).start()

    def _poll(self) -> None:
        if not self._window.winfo_exists():
            return
        try:
            event = self._events.get_nowait()
        except queue.Empty:
            self._window.after(100, self._poll)
            return

        if isinstance(event, _DistroEvent):
            self._handle_distros(event)
        elif isinstance(event, _UvEvent):
            self._handle_uv(event)
        else:
            self._handle_prune(event)
        self._window.after(100, self._poll)

    def _handle_distros(self, event: _DistroEvent) -> None:
        if event.error is not None or event.inventory is None:
            self._wsl_inventory = None
            self._combo.configure(values=())
            self._status.set(f"WSL 检查失败: {event.error or 'unknown error'}")
            self._set_busy(False)
            return
        self._wsl_inventory = event.inventory
        names = tuple(item.name for item in event.inventory.distributions)
        self._combo.configure(values=names)
        self._selected.set(names[0] if names else "")
        self._status.set(f"已确认 {len(names)} 个 WSL 发行版. 请选择一个检查 uv.")
        self._set_busy(False)

    def _handle_uv(self, event: _UvEvent) -> None:
        if event.error is not None or event.inventory is None:
            self._uv_inventory = None
            self._details.configure(text="未获得可执行的 WSL uv cache 身份.")
            self._status.set(f"uv 检查失败: {event.error or 'unknown error'}")
            self._set_busy(False)
            return
        inventory = event.inventory
        self._uv_inventory = inventory
        state = "检查前已运行" if inventory.distribution_was_running else "检查前已停止"
        self._details.configure(
            text=(
                f"确定可维护 | {inventory.distribution} ({state})\n"
                f"uv 版本: {inventory.version_text}\n"
                f"uv 自己报告的 cache: {inventory.cache_path}"
            )
        )
        self._status.set("已确认 uv cache. 未自动执行; 需要你明确运行 vendor prune.")
        self._set_busy(False)

    def _handle_prune(self, event: _PruneEvent) -> None:
        if event.error is not None or event.result is None:
            self._status.set(f"WSL uv 维护停止: {event.error or 'unknown error'}")
            self._set_busy(False)
            return
        self._uv_inventory = event.result.after
        output = event.result.output or "uv cache prune returned success"
        self._status.set(f"uv 官方 prune 完成: {output}")
        self._set_busy(False)


def open_wsl_uv_maintenance_dialog(parent: tk.Tk | tk.Toplevel) -> None:
    _WslUvMaintenanceDialog(parent).show()


__all__ = ["open_wsl_uv_maintenance_dialog"]
