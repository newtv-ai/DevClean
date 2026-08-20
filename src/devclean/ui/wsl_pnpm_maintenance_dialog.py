"""WSL pnpm store maintenance through pnpm's own garbage collector."""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from dataclasses import dataclass
from tkinter import messagebox, ttk

from devclean.core.wsl_inventory import WslInventory, inspect_wsl
from devclean.core.wsl_pnpm_maintenance import (
    WslPnpmPruneResult,
    WslPnpmStoreInventory,
    inventory_wsl_pnpm_store,
    prune_wsl_pnpm_store,
)


@dataclass(frozen=True, slots=True)
class _DistroEvent:
    inventory: WslInventory | None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class _PnpmEvent:
    inventory: WslPnpmStoreInventory | None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class _PruneEvent:
    result: WslPnpmPruneResult | None
    error: str | None = None


class _WslPnpmMaintenanceDialog:
    def __init__(self, parent: tk.Tk | tk.Toplevel) -> None:
        self._parent = parent
        self._window = tk.Toplevel(parent)
        self._window.title("WSL pnpm Store 维护")
        self._window.geometry("940x650")
        self._window.minsize(800, 560)
        self._events: queue.Queue[_DistroEvent | _PnpmEvent | _PruneEvent] = queue.Queue()
        self._status = tk.StringVar(value="正在读取 WSL 发行版列表...")
        self._selected = tk.StringVar()
        self._wsl_inventory: WslInventory | None = None
        self._pnpm_inventory: WslPnpmStoreInventory | None = None
        self._busy = False

        container = ttk.Frame(self._window, padding=12)
        container.pack(fill=tk.BOTH, expand=True)
        ttk.Label(container, text="WSL pnpm Store", font=("Segoe UI", 13, "bold")).pack(anchor=tk.W)
        ttk.Label(
            container,
            text=(
                "只使用所选 WSL 发行版中的 `pnpm store path` 和 `pnpm store prune`. "
                "DevClean 不扫描 store 内部来猜哪些包能删, 也不直接删除 store 目录."
            ),
            wraplength=900,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(4, 10))

        selector = ttk.LabelFrame(container, text="选择发行版", padding=10)
        selector.pack(fill=tk.X)
        self._combo = ttk.Combobox(
            selector, textvariable=self._selected, state="readonly", width=42
        )
        self._combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._inspect_button = ttk.Button(
            selector,
            text="检查 pnpm Store",
            command=self._start_pnpm_inventory,
            state=tk.DISABLED,
        )
        self._inspect_button.pack(side=tk.LEFT, padx=(8, 0))
        self._refresh_button = ttk.Button(
            selector, text="刷新发行版", command=self._start_distro_inventory
        )
        self._refresh_button.pack(side=tk.LEFT, padx=(8, 0))

        details = ttk.LabelFrame(container, text="pnpm 判定", padding=10)
        details.pack(fill=tk.X, pady=(10, 0))
        self._details = ttk.Label(
            details,
            text="先选择发行版并检查 pnpm store.",
            wraplength=870,
            justify=tk.LEFT,
        )
        self._details.pack(anchor=tk.W)

        explanation = ttk.LabelFrame(container, text="清理语义", padding=10)
        explanation.pack(fill=tk.BOTH, expand=True, pady=(10, 0))
        ttk.Label(
            explanation,
            text=(
                "`pnpm store prune` 由 pnpm 自己判断未被已登记项目引用的包, 因此属于 "
                "DETERMINISTIC_CANDIDATE, 不送 AI. 清理后未来仍可能重新下载这些包.\n\n"
                "执行前 DevClean 会重新确认 pnpm 版本、active store、store-dir 和 pnpm 运行"
                "状态, 并要求 active store 与 store-dir 都位于所选 WSL 发行版自己的根文件"
                "系统. 如果路径落到 /mnt/c、网络盘或其他独立挂载点, 只报告而不执行.\n\n"
                "真正删除始终由 pnpm 自己完成. WSL 2 中释放 Linux 逻辑空间也不等于 "
                "Windows 侧虚拟磁盘文件会同步缩小."
            ),
            wraplength=870,
            justify=tk.LEFT,
        ).pack(anchor=tk.W)

        ttk.Label(container, textvariable=self._status, wraplength=900, justify=tk.LEFT).pack(
            anchor=tk.W, pady=(10, 0)
        )

        footer = ttk.Frame(container)
        footer.pack(fill=tk.X, pady=(10, 0))
        self._prune_button = ttk.Button(
            footer,
            text="通过 pnpm store prune 垃圾收集",
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
        prune_state = tk.NORMAL if not busy and self._pnpm_inventory is not None else tk.DISABLED
        self._prune_button.configure(state=prune_state)

    def _start_distro_inventory(self) -> None:
        if self._busy:
            return
        self._pnpm_inventory = None
        self._set_busy(True)
        self._status.set("正在读取 WSL 已注册发行版和 running 状态...")

        def work() -> None:
            try:
                inventory = inspect_wsl()
            except Exception as error:
                self._events.put(_DistroEvent(None, str(error)))
            else:
                self._events.put(_DistroEvent(inventory))

        threading.Thread(target=work, name="DevClean-WSL-pnpm-distros", daemon=True).start()

    def _start_pnpm_inventory(self) -> None:
        if self._busy or self._wsl_inventory is None:
            return
        name = self._selected.get().strip()
        distro = next(
            (item for item in self._wsl_inventory.distributions if item.name == name),
            None,
        )
        if distro is None:
            messagebox.showinfo("WSL pnpm Store 维护", "请先选择一个已注册的 WSL 发行版.")
            return
        if not distro.running and not messagebox.askyesno(
            "WSL pnpm Store 维护",
            "所选发行版当前已停止. 检查 pnpm 需要执行 WSL 命令并可能启动它. 是否继续?",
        ):
            return

        self._pnpm_inventory = None
        self._set_busy(True)
        self._status.set(f"正在通过 {distro.name} 内的 pnpm 查询 active store...")

        def work(distribution: str = distro.name) -> None:
            try:
                inventory = inventory_wsl_pnpm_store(distribution)
            except Exception as error:
                self._events.put(_PnpmEvent(None, str(error)))
            else:
                self._events.put(_PnpmEvent(inventory))

        threading.Thread(target=work, name="DevClean-WSL-pnpm-inventory", daemon=True).start()

    def _start_prune(self) -> None:
        if self._busy or self._pnpm_inventory is None:
            return
        inventory = self._pnpm_inventory
        if not messagebox.askyesno(
            "确认维护 WSL pnpm Store",
            f"发行版: {inventory.distribution}\n"
            f"pnpm: {inventory.version_text}\n"
            f"active store: {inventory.active_store_path}\n"
            f"store-dir: {inventory.store_dir}\n\n"
            "DevClean 将重新确认身份、运行状态与 WSL 根文件系统范围, 然后固定 store-dir "
            "执行 `pnpm store prune`. 继续吗?",
        ):
            return

        self._set_busy(True)
        self._status.set("正在重新确认 pnpm/store 身份、运行状态和本地文件系统范围...")

        def work(expected: WslPnpmStoreInventory = inventory) -> None:
            try:
                result = prune_wsl_pnpm_store(expected)
            except Exception as error:
                self._events.put(_PruneEvent(None, str(error)))
            else:
                self._events.put(_PruneEvent(result))

        threading.Thread(target=work, name="DevClean-WSL-pnpm-prune", daemon=True).start()

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
        elif isinstance(event, _PnpmEvent):
            self._handle_pnpm(event)
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
        self._status.set(f"已确认 {len(names)} 个 WSL 发行版. 请选择一个检查 pnpm.")
        self._set_busy(False)

    def _handle_pnpm(self, event: _PnpmEvent) -> None:
        if event.error is not None or event.inventory is None:
            self._pnpm_inventory = None
            self._details.configure(text="未获得可执行的 WSL pnpm store 身份.")
            self._status.set(f"pnpm 检查失败: {event.error or 'unknown error'}")
            self._set_busy(False)
            return
        inventory = event.inventory
        self._pnpm_inventory = inventory
        state = "检查前已运行" if inventory.distribution_was_running else "检查前已停止"
        self._details.configure(
            text=(
                f"确定可维护 | {inventory.distribution} ({state})\n"
                f"pnpm 版本: {inventory.version_text}\n"
                f"active store: {inventory.active_store_path}\n"
                f"执行时固定 store-dir: {inventory.store_dir}"
            )
        )
        self._status.set("已确认 pnpm store. 未自动执行; 需要你明确运行 vendor prune.")
        self._set_busy(False)

    def _handle_prune(self, event: _PruneEvent) -> None:
        if event.error is not None or event.result is None:
            self._status.set(f"WSL pnpm 维护停止: {event.error or 'unknown error'}")
            self._set_busy(False)
            return
        self._pnpm_inventory = event.result.after
        output = event.result.output or "pnpm store prune returned success"
        self._status.set(f"pnpm 官方 prune 完成: {output}")
        self._set_busy(False)


def open_wsl_pnpm_maintenance_dialog(parent: tk.Tk | tk.Toplevel) -> None:
    _WslPnpmMaintenanceDialog(parent).show()


__all__ = ["open_wsl_pnpm_maintenance_dialog"]
