"""WSL Go build-cache maintenance through Go's own clean command."""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from dataclasses import dataclass
from tkinter import messagebox, ttk

from devclean.core.wsl_go_build_cache import (
    WslGoBuildCacheCleanResult,
    WslGoBuildCacheInventory,
    clean_wsl_go_build_cache,
    inventory_wsl_go_build_cache,
)
from devclean.core.wsl_inventory import WslInventory, inspect_wsl


@dataclass(frozen=True, slots=True)
class _DistroEvent:
    inventory: WslInventory | None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class _GoEvent:
    inventory: WslGoBuildCacheInventory | None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class _CleanEvent:
    result: WslGoBuildCacheCleanResult | None
    error: str | None = None


class _WslGoBuildCacheDialog:
    def __init__(self, parent: tk.Tk | tk.Toplevel) -> None:
        self._parent = parent
        self._window = tk.Toplevel(parent)
        self._window.title("WSL Go 构建缓存维护")
        self._window.geometry("940x660")
        self._window.minsize(800, 570)
        self._events: queue.Queue[_DistroEvent | _GoEvent | _CleanEvent] = queue.Queue()
        self._status = tk.StringVar(value="正在读取 WSL 发行版列表...")
        self._selected = tk.StringVar()
        self._wsl_inventory: WslInventory | None = None
        self._go_inventory: WslGoBuildCacheInventory | None = None
        self._busy = False

        container = ttk.Frame(self._window, padding=12)
        container.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            container,
            text="WSL Go 构建缓存",
            font=("Segoe UI", 13, "bold"),
        ).pack(anchor=tk.W)
        ttk.Label(
            container,
            text=(
                "只维护 Go 自己报告的 GOCACHE，并且只执行 `go clean -cache`。"
                "模块下载缓存 GOMODCACHE、项目文件和安装内容不属于这个入口。"
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
            text="检查 Go 构建缓存",
            command=self._start_go_inventory,
            state=tk.DISABLED,
        )
        self._inspect_button.pack(side=tk.LEFT, padx=(8, 0))
        self._refresh_button = ttk.Button(
            selector,
            text="刷新发行版",
            command=self._start_distro_inventory,
        )
        self._refresh_button.pack(side=tk.LEFT, padx=(8, 0))

        details = ttk.LabelFrame(container, text="Go 判定", padding=10)
        details.pack(fill=tk.X, pady=(10, 0))
        self._details = ttk.Label(
            details,
            text="先选择发行版并检查 Go。",
            wraplength=870,
            justify=tk.LEFT,
        )
        self._details.pack(anchor=tk.W)

        explanation = ttk.LabelFrame(container, text="清理语义", padding=10)
        explanation.pack(fill=tk.BOTH, expand=True, pady=(10, 0))
        ttk.Label(
            explanation,
            text=(
                "Go 的普通 build cache 是可重建的加速状态，因此属于 "
                "DETERMINISTIC_CANDIDATE，不需要 AI 判断。\n\n"
                "不过 Go 自己会周期性淘汰旧缓存，官方也说明通常不需要手工清空，"
                "所以 DevClean 不把它当成日常默认清理项；这里保留为明确的维护操作。\n\n"
                "执行前会重新确认 Go 版本、GOCACHE 和 GOCACHEPROG，并检查 Go/gopls "
                "运行状态。目标还必须属于所选 WSL 发行版自己的根文件系统。"
                "如果设置了 GOCACHEPROG，说明存在外部缓存后端，本入口只报告、不执行。\n\n"
                "`go clean -cache` 会清普通 build cache；Go 的 fuzz 子目录由官方单独管理，"
                "不会被 `-cache` 清掉。本入口也不会调用 `-fuzzcache`。\n\n"
                "WSL 中释放的是 Linux 逻辑空间，不代表 Windows 侧 VHD 文件会同步缩小。"
            ),
            wraplength=870,
            justify=tk.LEFT,
        ).pack(anchor=tk.W)

        ttk.Label(
            container,
            textvariable=self._status,
            wraplength=900,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(10, 0))

        footer = ttk.Frame(container)
        footer.pack(fill=tk.X, pady=(10, 0))
        self._clean_button = ttk.Button(
            footer,
            text="通过 go clean -cache 维护",
            command=self._start_clean,
            state=tk.DISABLED,
        )
        self._clean_button.pack(side=tk.RIGHT, padx=(8, 0))
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
        clean_allowed = (
            not busy
            and self._go_inventory is not None
            and self._go_inventory.executable
        )
        self._clean_button.configure(state=tk.NORMAL if clean_allowed else tk.DISABLED)

    def _start_distro_inventory(self) -> None:
        if self._busy:
            return
        self._go_inventory = None
        self._set_busy(True)
        self._status.set("正在读取 WSL 已注册发行版和 running 状态...")

        def work() -> None:
            try:
                inventory = inspect_wsl()
            except Exception as error:
                self._events.put(_DistroEvent(None, str(error)))
            else:
                self._events.put(_DistroEvent(inventory))

        threading.Thread(target=work, name="DevClean-WSL-go-distros", daemon=True).start()

    def _start_go_inventory(self) -> None:
        if self._busy or self._wsl_inventory is None:
            return
        name = self._selected.get().strip()
        distro = next(
            (item for item in self._wsl_inventory.distributions if item.name == name),
            None,
        )
        if distro is None:
            messagebox.showinfo("WSL Go 构建缓存维护", "请先选择一个已注册的 WSL 发行版。")
            return
        if not distro.running and not messagebox.askyesno(
            "WSL Go 构建缓存维护",
            "所选发行版当前已停止。检查 Go 需要在该发行版内执行命令，"
            "因此可能启动它。是否继续？",
        ):
            return

        self._go_inventory = None
        self._set_busy(True)
        self._status.set(f"正在通过 {distro.name} 内的 Go 查询 GOCACHE/GOCACHEPROG...")

        def work(distribution: str = distro.name) -> None:
            try:
                inventory = inventory_wsl_go_build_cache(distribution)
            except Exception as error:
                self._events.put(_GoEvent(None, str(error)))
            else:
                self._events.put(_GoEvent(inventory))

        threading.Thread(target=work, name="DevClean-WSL-go-inventory", daemon=True).start()

    def _start_clean(self) -> None:
        if self._busy or self._go_inventory is None or not self._go_inventory.executable:
            return
        inventory = self._go_inventory
        if not messagebox.askyesno(
            "确认维护 WSL Go 构建缓存",
            f"发行版: {inventory.distribution}\n"
            f"Go: {inventory.version_text}\n"
            f"GOCACHE: {inventory.cache_path}\n\n"
            "DevClean 将重新确认身份和本地文件系统边界，然后把这个 GOCACHE 精确固定"
            "给 `go clean -cache`。不会执行 `-modcache`、`-testcache`、`-fuzzcache`、"
            "裸 `go clean` 或 raw delete。继续吗？",
        ):
            return

        self._set_busy(True)
        self._status.set("正在重新确认 Go/cache 身份、运行状态和 WSL 文件系统边界...")

        def work(expected: WslGoBuildCacheInventory = inventory) -> None:
            try:
                result = clean_wsl_go_build_cache(expected)
            except Exception as error:
                self._events.put(_CleanEvent(None, str(error)))
            else:
                self._events.put(_CleanEvent(result))

        threading.Thread(target=work, name="DevClean-WSL-go-clean", daemon=True).start()

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
        elif isinstance(event, _GoEvent):
            self._handle_go(event)
        else:
            self._handle_clean(event)
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
        self._status.set(f"已确认 {len(names)} 个 WSL 发行版。请选择一个检查 Go。")
        self._set_busy(False)

    def _handle_go(self, event: _GoEvent) -> None:
        if event.error is not None or event.inventory is None:
            self._go_inventory = None
            self._details.configure(text="未获得可维护的 WSL Go build-cache 身份。")
            self._status.set(f"Go 检查失败: {event.error or 'unknown error'}")
            self._set_busy(False)
            return

        inventory = event.inventory
        self._go_inventory = inventory
        state = "检查前已运行" if inventory.distribution_was_running else "检查前已停止"
        if inventory.cache_program:
            decision = "REPORT_ONLY | 检测到外部 GOCACHEPROG，不授予执行权限"
            cache_program = inventory.cache_program
            status = "已识别 Go build cache，但外部 cache program 保持只读报告。"
        else:
            decision = "DETERMINISTIC_CANDIDATE | 可重建，但通常低收益"
            cache_program = "未配置"
            status = "已确认普通 Go build cache。未自动执行，需要你明确启动维护。"
        self._details.configure(
            text=(
                f"{decision}\n"
                f"发行版: {inventory.distribution} ({state})\n"
                f"Go: {inventory.version_text}\n"
                f"GOCACHE: {inventory.cache_path}\n"
                f"GOCACHEPROG: {cache_program}"
            )
        )
        self._status.set(status)
        self._set_busy(False)

    def _handle_clean(self, event: _CleanEvent) -> None:
        if event.error is not None or event.result is None:
            self._status.set(f"WSL Go 维护停止: {event.error or 'unknown error'}")
            self._set_busy(False)
            return
        self._go_inventory = event.result.after
        output = event.result.output or "go clean -cache returned success"
        self._status.set(f"Go 官方 build-cache clean 完成: {output}")
        self._set_busy(False)


def open_wsl_go_build_cache_dialog(parent: tk.Tk | tk.Toplevel) -> None:
    _WslGoBuildCacheDialog(parent).show()


__all__ = ["open_wsl_go_build_cache_dialog"]
