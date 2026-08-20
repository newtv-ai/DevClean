"""WSL pip cache maintenance through pip's own cache commands."""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from dataclasses import dataclass
from tkinter import messagebox, ttk

from devclean.core.wsl_inventory import WslInventory, inspect_wsl
from devclean.core.wsl_pip_maintenance import (
    WslPipCacheInventory,
    WslPipPurgeResult,
    inventory_wsl_pip_cache,
    purge_wsl_pip_cache,
)


@dataclass(frozen=True, slots=True)
class _DistroEvent:
    inventory: WslInventory | None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class _PipEvent:
    inventory: WslPipCacheInventory | None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class _PurgeEvent:
    result: WslPipPurgeResult | None
    error: str | None = None


class _WslPipMaintenanceDialog:
    def __init__(self, parent: tk.Tk | tk.Toplevel) -> None:
        self._parent = parent
        self._window = tk.Toplevel(parent)
        self._window.title("WSL pip 缓存维护")
        self._window.geometry("940x700")
        self._window.minsize(800, 580)
        self._events: queue.Queue[_DistroEvent | _PipEvent | _PurgeEvent] = queue.Queue()
        self._status = tk.StringVar(value="正在读取 WSL 发行版列表...")
        self._selected = tk.StringVar()
        self._wsl_inventory: WslInventory | None = None
        self._pip_inventory: WslPipCacheInventory | None = None
        self._busy = False

        container = ttk.Frame(self._window, padding=12)
        container.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            container,
            text="WSL pip 缓存",
            font=("Segoe UI", 13, "bold"),
        ).pack(anchor=tk.W)
        ttk.Label(
            container,
            text=(
                "这条通道只让所选 WSL 发行版里的 pip 自己定位并清理自己的缓存. "
                "DevClean 不猜测 Linux cache 路径, 不从 Windows 侧删除目录, 也不调用 shell."
            ),
            wraplength=900,
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
            text="检查 pip 缓存",
            command=self._start_pip_inventory,
            state=tk.DISABLED,
        )
        self._inspect_button.pack(side=tk.LEFT, padx=(8, 0))
        self._refresh_button = ttk.Button(
            selector,
            text="刷新发行版",
            command=self._start_distro_inventory,
        )
        self._refresh_button.pack(side=tk.LEFT, padx=(8, 0))

        details = ttk.LabelFrame(container, text="pip 判定", padding=10)
        details.pack(fill=tk.X, pady=(10, 0))
        self._details = ttk.Label(
            details,
            text="先选择发行版并检查 pip 缓存.",
            wraplength=870,
            justify=tk.LEFT,
        )
        self._details.pack(anchor=tk.W)

        info_frame = ttk.LabelFrame(container, text="pip cache info (原始输出)", padding=8)
        info_frame.pack(fill=tk.BOTH, expand=True, pady=(10, 0))
        self._info = tk.Text(info_frame, height=13, wrap=tk.WORD, state=tk.DISABLED)
        self._info.pack(fill=tk.BOTH, expand=True)

        ttk.Label(
            container,
            text=(
                "语义: DETERMINISTIC_CANDIDATE, 不送 AI. `pip cache purge` 会删除 pip 的"
                " HTTP/wheel 缓存, 不会卸载已安装包, 但之后可能需要重新下载或重建 wheel. "
                "在 WSL 2 中释放的是 Linux 文件系统逻辑空间; DevClean 不承诺 Windows 上的"
                "虚拟磁盘文件会同步缩小."
            ),
            wraplength=900,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(10, 0))
        ttk.Label(
            container,
            textvariable=self._status,
            wraplength=900,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(6, 0))

        footer = ttk.Frame(container)
        footer.pack(fill=tk.X, pady=(10, 0))
        self._purge_button = ttk.Button(
            footer,
            text="通过 pip cache purge 清理",
            command=self._start_purge,
            state=tk.DISABLED,
        )
        self._purge_button.pack(side=tk.RIGHT, padx=(8, 0))
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
        purge_state = tk.NORMAL if not busy and self._pip_inventory is not None else tk.DISABLED
        self._purge_button.configure(state=purge_state)

    def _start_distro_inventory(self) -> None:
        if self._busy:
            return
        self._pip_inventory = None
        self._set_busy(True)
        self._status.set("正在读取 WSL 已注册发行版和 running 状态...")

        def work() -> None:
            try:
                inventory = inspect_wsl()
            except Exception as error:
                self._events.put(_DistroEvent(None, str(error)))
            else:
                self._events.put(_DistroEvent(inventory))

        threading.Thread(target=work, name="DevClean-WSL-pip-distros", daemon=True).start()

    def _start_pip_inventory(self) -> None:
        if self._busy or self._wsl_inventory is None:
            return
        name = self._selected.get().strip()
        distro = next(
            (item for item in self._wsl_inventory.distributions if item.name == name),
            None,
        )
        if distro is None:
            messagebox.showinfo("WSL pip 缓存维护", "请先选择一个已注册的 WSL 发行版.")
            return
        if not distro.running and not messagebox.askyesno(
            "WSL pip 缓存维护",
            "所选发行版当前已停止. 检查 pip 需要在该发行版内执行命令, 因此可能启动它. 是否继续?",
        ):
            return

        self._pip_inventory = None
        self._set_busy(True)
        self._status.set(f"正在通过 {distro.name} 内的 pip 查询有效 cache...")

        def work(distribution: str = distro.name) -> None:
            try:
                inventory = inventory_wsl_pip_cache(distribution)
            except Exception as error:
                self._events.put(_PipEvent(None, str(error)))
            else:
                self._events.put(_PipEvent(inventory))

        threading.Thread(target=work, name="DevClean-WSL-pip-inventory", daemon=True).start()

    def _start_purge(self) -> None:
        if self._busy or self._pip_inventory is None:
            return
        inventory = self._pip_inventory
        if not messagebox.askyesno(
            "确认清理 WSL pip 缓存",
            f"发行版: {inventory.distribution}\n"
            f"pip: {inventory.entrypoint.display}\n"
            f"cache: {inventory.cache_path}\n\n"
            "DevClean 将重新确认这些身份, 检查 pip 是否正在运行, 然后只执行 "
            "`pip cache purge`. 继续吗?",
        ):
            return

        self._set_busy(True)
        self._status.set("正在重新确认 pip/cache 身份和运行状态...")

        def work(expected: WslPipCacheInventory = inventory) -> None:
            try:
                result = purge_wsl_pip_cache(expected)
            except Exception as error:
                self._events.put(_PurgeEvent(None, str(error)))
            else:
                self._events.put(_PurgeEvent(result))

        threading.Thread(target=work, name="DevClean-WSL-pip-purge", daemon=True).start()

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
        elif isinstance(event, _PipEvent):
            self._handle_pip(event)
        else:
            self._handle_purge(event)
        self._window.after(100, self._poll)

    def _handle_distros(self, event: _DistroEvent) -> None:
        self._set_busy(False)
        if event.error is not None or event.inventory is None:
            self._wsl_inventory = None
            self._combo.configure(values=())
            self._status.set(f"WSL 检查失败: {event.error or 'unknown error'}")
            return
        self._wsl_inventory = event.inventory
        names = tuple(item.name for item in event.inventory.distributions)
        self._combo.configure(values=names)
        if names:
            self._selected.set(names[0])
        else:
            self._selected.set("")
        self._set_busy(False)
        self._status.set(f"已确认 {len(names)} 个 WSL 发行版. 请选择一个检查 pip.")

    def _handle_pip(self, event: _PipEvent) -> None:
        if event.error is not None or event.inventory is None:
            self._pip_inventory = None
            self._details.configure(text="未获得可执行的 WSL pip cache 身份.")
            self._set_info("")
            self._status.set(f"pip 检查失败: {event.error or 'unknown error'}")
            self._set_busy(False)
            return
        self._pip_inventory = event.inventory
        inventory = event.inventory
        state = "检查前已运行" if inventory.distribution_was_running else "检查前已停止"
        self._details.configure(
            text=(
                f"确定可清理 | {inventory.distribution} ({state})\n"
                f"pip 入口: {inventory.entrypoint.display}\n"
                f"pip 版本: {inventory.entrypoint.version_text}\n"
                f"pip 自己报告的 cache: {inventory.cache_path}"
            )
        )
        self._set_info(inventory.cache_info)
        self._status.set("已确认 pip cache. 未自动清理; 需要你明确执行 vendor purge.")
        self._set_busy(False)

    def _handle_purge(self, event: _PurgeEvent) -> None:
        if event.error is not None or event.result is None:
            self._status.set(f"WSL pip 清理停止: {event.error or 'unknown error'}")
            self._set_busy(False)
            return
        self._pip_inventory = event.result.after
        self._set_info(event.result.after.cache_info)
        output = event.result.output or "pip cache purge returned success"
        self._status.set(f"pip 官方 purge 完成: {output}")
        self._set_busy(False)

    def _set_info(self, text: str) -> None:
        self._info.configure(state=tk.NORMAL)
        self._info.delete("1.0", tk.END)
        self._info.insert("1.0", text)
        self._info.configure(state=tk.DISABLED)


def open_wsl_pip_maintenance_dialog(parent: tk.Tk | tk.Toplevel) -> None:
    _WslPipMaintenanceDialog(parent).show()


__all__ = ["open_wsl_pip_maintenance_dialog"]
