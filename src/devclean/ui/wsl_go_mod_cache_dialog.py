"""WSL Go module-cache maintenance through Go's own clean command."""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from dataclasses import dataclass
from tkinter import messagebox, ttk

from devclean.core.wsl_go_mod_cache import (
    WslGoModCacheCleanResult,
    WslGoModCacheInventory,
    clean_wsl_go_mod_cache,
    inventory_wsl_go_mod_cache,
)
from devclean.core.wsl_inventory import WslInventory, inspect_wsl


@dataclass(frozen=True, slots=True)
class _DistroEvent:
    inventory: WslInventory | None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class _GoEvent:
    inventory: WslGoModCacheInventory | None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class _CleanEvent:
    result: WslGoModCacheCleanResult | None
    error: str | None = None


class _WslGoModCacheDialog:
    def __init__(self, parent: tk.Tk | tk.Toplevel) -> None:
        self._parent = parent
        self._window = tk.Toplevel(parent)
        self._window.title("WSL Go 模块缓存维护")
        self._window.geometry("940x650")
        self._window.minsize(800, 560)
        self._events: queue.Queue[_DistroEvent | _GoEvent | _CleanEvent] = queue.Queue()
        self._status = tk.StringVar(value="正在读取 WSL 发行版列表...")
        self._selected = tk.StringVar()
        self._wsl_inventory: WslInventory | None = None
        self._go_inventory: WslGoModCacheInventory | None = None
        self._busy = False

        container = ttk.Frame(self._window, padding=12)
        container.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            container,
            text="WSL Go 模块下载缓存",
            font=("Segoe UI", 13, "bold"),
        ).pack(anchor=tk.W)
        ttk.Label(
            container,
            text=(
                "只维护 Go 自己报告的 GOMODCACHE, 并且只执行 `go clean -modcache`. "
                "这个缓存包含下载内容和解包后的版本化依赖源码, 因此必须由用户决定."
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
            text="检查 Go 模块缓存",
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
            text="先选择发行版并检查 Go.",
            wraplength=870,
            justify=tk.LEFT,
        )
        self._details.pack(anchor=tk.W)

        explanation = ttk.LabelFrame(container, text="为什么需要用户确认", padding=10)
        explanation.pack(fill=tk.BOTH, expand=True, pady=(10, 0))
        ttk.Label(
            explanation,
            text=(
                "GOMODCACHE 不是项目源码, 但它也不只是普通编译加速数据. Go 官方说明 "
                "`go clean -modcache` 会移除整个 module cache, 包括已解包的版本化依赖源码.\n\n"
                "这些内容可以重新下载, 但旧分支, 私有模块, 离线开发或较慢网络都可能让保留缓存有实际价值. "
                "因此这里是 USER_REVIEW: 不自动选择, 不交给 AI 判断, 只在你明确确认后执行.\n\n"
                "执行前 DevClean 会重新确认 Go 版本和 GOMODCACHE, 检查 go/gopls 运行状态, "
                "并要求目标属于所选 WSL 发行版自己的根文件系统. `/mnt/c` 和其他独立挂载点只报告不执行.\n\n"
                "清理命令会把 GOFLAGS 固定为空, 避免用户持久配置额外注入 `-cache`, `-testcache` 或 "
                "`-fuzzcache`. 本入口不会 raw delete, 也不会触碰 GOPATH 其他内容, 项目文件或安装的 Go 工具.\n\n"
                "WSL 中释放的是 Linux 逻辑空间, 不保证 Windows 侧 VHD 文件同步缩小."
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
            text="清空这个模块缓存",
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
        clean_allowed = not busy and self._go_inventory is not None
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

        threading.Thread(target=work, name="DevClean-WSL-go-mod-distros", daemon=True).start()

    def _start_go_inventory(self) -> None:
        if self._busy or self._wsl_inventory is None:
            return
        name = self._selected.get().strip()
        distro = next(
            (item for item in self._wsl_inventory.distributions if item.name == name),
            None,
        )
        if distro is None:
            messagebox.showinfo("WSL Go 模块缓存维护", "请先选择一个已注册的 WSL 发行版.")
            return
        if not distro.running and not messagebox.askyesno(
            "WSL Go 模块缓存维护",
            "所选发行版当前已停止. 检查 Go 需要在该发行版内执行命令, 因此可能启动它. 是否继续?",
        ):
            return

        self._go_inventory = None
        self._set_busy(True)
        self._status.set(f"正在通过 {distro.name} 内的 Go 查询 GOMODCACHE...")

        def work(distribution: str = distro.name) -> None:
            try:
                inventory = inventory_wsl_go_mod_cache(distribution)
            except Exception as error:
                self._events.put(_GoEvent(None, str(error)))
            else:
                self._events.put(_GoEvent(inventory))

        threading.Thread(target=work, name="DevClean-WSL-go-mod-inventory", daemon=True).start()

    def _start_clean(self) -> None:
        if self._busy or self._go_inventory is None:
            return
        inventory = self._go_inventory
        if not messagebox.askyesno(
            "确认清空 WSL Go 模块缓存",
            f"发行版: {inventory.distribution}\n"
            f"Go: {inventory.version_text}\n"
            f"GOMODCACHE: {inventory.module_cache_path}\n\n"
            "`go clean -modcache` 会移除整个 module cache, 包括已下载文件和解包后的版本化依赖源码. "
            "之后需要时必须重新下载, 私有模块还可能再次需要相应网络和凭据.\n\n"
            "DevClean 会重新确认身份和 WSL 本地文件系统边界, 然后只执行这一条官方清理动作. 继续?",
        ):
            return

        self._set_busy(True)
        self._status.set("正在重新确认 Go/module-cache 身份, 运行状态和 WSL 文件系统边界...")

        def work(expected: WslGoModCacheInventory = inventory) -> None:
            try:
                result = clean_wsl_go_mod_cache(expected)
            except Exception as error:
                self._events.put(_CleanEvent(None, str(error)))
            else:
                self._events.put(_CleanEvent(result))

        threading.Thread(target=work, name="DevClean-WSL-go-mod-clean", daemon=True).start()

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
        self._status.set(f"已确认 {len(names)} 个 WSL 发行版. 请选择一个检查 Go 模块缓存.")
        self._set_busy(False)

    def _handle_go(self, event: _GoEvent) -> None:
        if event.error is not None or event.inventory is None:
            self._go_inventory = None
            self._details.configure(text="未获得可维护的 WSL Go module-cache 身份.")
            self._status.set(f"Go 检查失败: {event.error or 'unknown error'}")
            self._set_busy(False)
            return

        inventory = event.inventory
        self._go_inventory = inventory
        state = "检查前已运行" if inventory.distribution_was_running else "检查前已停止"
        self._details.configure(
            text=(
                "USER_REVIEW | 已知模块缓存, 但保留价值取决于用户\n"
                f"发行版: {inventory.distribution} ({state})\n"
                f"Go: {inventory.version_text}\n"
                f"GOMODCACHE: {inventory.module_cache_path}"
            )
        )
        self._status.set("已确认 Go module cache. 未自动执行, 需要你明确确认是否清空.")
        self._set_busy(False)

    def _handle_clean(self, event: _CleanEvent) -> None:
        if event.error is not None or event.result is None:
            self._status.set(f"WSL Go 模块缓存维护停止: {event.error or 'unknown error'}")
            self._set_busy(False)
            return
        self._go_inventory = event.result.after
        output = event.result.output or "go clean -modcache returned success"
        self._status.set(f"Go 官方 module-cache clean 完成: {output}")
        self._set_busy(False)


def open_wsl_go_mod_cache_dialog(parent: tk.Tk | tk.Toplevel) -> None:
    _WslGoModCacheDialog(parent).show()


__all__ = ["open_wsl_go_mod_cache_dialog"]
