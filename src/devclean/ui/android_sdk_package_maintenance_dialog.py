"""Android SDK installed-package maintenance UI."""

# ruff: noqa: RUF001

from __future__ import annotations

import queue
import threading
import tkinter as tk
from dataclasses import dataclass
from tkinter import messagebox, ttk

from devclean.core.android_sdk_package_maintenance import (
    AndroidSdkInventory,
    AndroidSdkPackageEntry,
    AndroidSdkUninstallResult,
    inventory_android_sdk_packages,
    uninstall_android_sdk_package,
)


@dataclass(frozen=True, slots=True)
class _InventoryEvent:
    inventory: AndroidSdkInventory


@dataclass(frozen=True, slots=True)
class _CleanupEvent:
    results: tuple[AndroidSdkUninstallResult, ...]
    error: str | None = None


class _AndroidSdkPackageMaintenanceDialog:
    def __init__(self, parent: tk.Tk | tk.Toplevel) -> None:
        self._parent = parent
        self._window = tk.Toplevel(parent)
        self._window.title("Android SDK Package 维护")
        self._window.geometry("1120x720")
        self._window.minsize(920, 590)
        self._events: queue.Queue[_InventoryEvent | _CleanupEvent | Exception] = queue.Queue()
        self._status = tk.StringVar(value="正在通过 sdkmanager 读取已安装 package…")
        self._inventory: AndroidSdkInventory | None = None
        self._rows: dict[str, AndroidSdkPackageEntry] = {}
        self._busy = False

        container = ttk.Frame(self._window, padding=12)
        container.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            container,
            text="Android SDK 已安装 Package",
            font=("Segoe UI", 13, "bold"),
        ).pack(anchor=tk.W)
        ttk.Label(
            container,
            text=(
                "Android SDK 不是可整棵清理的缓存。DevClean 只展示 sdkmanager 自己列出的"
                "已安装 package，并通过官方 --uninstall 删除你明确选择的 package。"
                "平台、Build Tools、NDK、CMake、system image 都可能仍被项目或 AVD 使用，"
                "所以全部属于“你来决定”，默认不选，也不需要 AI。"
            ),
            wraplength=1080,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(4, 8))

        roots_frame = ttk.LabelFrame(container, text="SDK 根", padding=8)
        roots_frame.pack(fill=tk.X, pady=(0, 8))
        self._roots_label = ttk.Label(
            roots_frame,
            text="尚未完成统计。",
            wraplength=1060,
            justify=tk.LEFT,
        )
        self._roots_label.pack(anchor=tk.W)

        table_frame = ttk.Frame(container)
        table_frame.pack(fill=tk.BOTH, expand=True)
        self._tree = ttk.Treeview(
            table_frame,
            columns=("size", "version", "status", "description", "location", "root"),
            show="tree headings",
            selectmode="extended",
        )
        self._tree.heading("#0", text="Package ID")
        self._tree.heading("size", text="占用")
        self._tree.heading("version", text="版本")
        self._tree.heading("status", text="权限")
        self._tree.heading("description", text="说明")
        self._tree.heading("location", text="Location")
        self._tree.heading("root", text="SDK")
        self._tree.column("#0", width=250, stretch=True)
        self._tree.column("size", width=95, anchor=tk.E, stretch=False)
        self._tree.column("version", width=95, stretch=False)
        self._tree.column("status", width=105, stretch=False)
        self._tree.column("description", width=250, stretch=True)
        self._tree.column("location", width=210, stretch=True)
        self._tree.column("root", width=260, stretch=True)
        scrollbar = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self._tree.yview)
        self._tree.configure(yscrollcommand=scrollbar.set)
        self._tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        ttk.Label(
            container,
            text=(
                "安全边界：卸载前重新运行 sdkmanager --list，要求 package ID、版本和"
                " Location 都没变化；SDK 和 package 必须在本地固定磁盘；Android Studio、"
                "Gradle、ADB、Emulator 等正在使用 SDK 时拒绝执行。"
            ),
            wraplength=1080,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(8, 0))
        ttk.Label(
            container,
            text=(
                "cmdline-tools/tools 被保护，因为它们承载 sdkmanager 自身，DevClean 不允许"
                "执行器卸载自己。其他 package 只通过 sdkmanager --uninstall；不会直接删除"
                " platforms、build-tools、system-images 或 ndk 目录。"
            ),
            wraplength=1080,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(4, 0))
        ttk.Label(
            container,
            textvariable=self._status,
            wraplength=1080,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(6, 0))

        footer = ttk.Frame(container)
        footer.pack(fill=tk.X, pady=(10, 0))
        self._uninstall_button = ttk.Button(
            footer,
            text="卸载选中的 Package…",
            command=self._start_cleanup,
            state=tk.DISABLED,
        )
        self._uninstall_button.pack(side=tk.RIGHT, padx=(8, 0))
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
            and any(
                package.deletion_supported
                for root in self._inventory.roots
                for package in root.packages
            )
        )
        self._uninstall_button.configure(state=tk.NORMAL if enabled else tk.DISABLED)

    def _start_inventory(self) -> None:
        if self._busy:
            return
        self._set_busy(True)
        self._status.set("正在通过每个 SDK 自己的 sdkmanager --list 读取已安装 package…")

        def work() -> None:
            try:
                inventory = inventory_android_sdk_packages()
            except Exception as error:
                self._events.put(error)
            else:
                self._events.put(_InventoryEvent(inventory))

        threading.Thread(
            target=work,
            name="DevClean-Android-SDK-package-inventory",
            daemon=True,
        ).start()

    def _start_cleanup(self) -> None:
        if self._busy or self._inventory is None:
            return
        selected = [
            self._rows[item]
            for item in self._tree.selection()
            if item in self._rows
        ]
        if not selected:
            messagebox.showinfo(
                "Android SDK Package 维护",
                "请先选择一个或多个要卸载的 package。",
                parent=self._window,
            )
            return
        protected = [package for package in selected if not package.deletion_supported]
        if protected:
            details = "\n".join(
                f"• {package.package_id}: {package.protected_reason or '当前不可执行'}"
                for package in protected[:10]
            )
            messagebox.showwarning(
                "选择中包含受保护 Package",
                details,
                parent=self._window,
            )
            return

        total = sum(package.logical_bytes for package in selected)
        names = "\n".join(f"• {package.package_id}" for package in selected[:12])
        if len(selected) > 12:
            names += f"\n…另有 {len(selected) - 12} 个"
        if not messagebox.askyesno(
            "确认卸载 Android SDK Package",
            (
                f"将通过 sdkmanager 卸载 {len(selected)} 个 package：\n\n{names}\n\n"
                f"当前目录占用合计约 {_format_bytes(total)}。\n"
                "这些 package 可能被现有项目、构建脚本或 AVD 依赖；卸载后再次使用通常需要"
                "重新下载。DevClean 不会替你判断项目是否还需要它们。\n\n确定继续吗？"
            ),
            icon=messagebox.WARNING,
            parent=self._window,
        ):
            return

        self._set_busy(True)
        self._status.set(f"正在逐项重新验证并卸载 {len(selected)} 个 Android SDK package…")

        def work(packages: tuple[AndroidSdkPackageEntry, ...] = tuple(selected)) -> None:
            results: list[AndroidSdkUninstallResult] = []
            error_text: str | None = None
            for package in packages:
                try:
                    results.append(
                        uninstall_android_sdk_package(
                            package.sdk_root,
                            package.package_id,
                            expected_version=package.version,
                            expected_location=package.location,
                        )
                    )
                except Exception as error:
                    error_text = str(error)
                    break
            self._events.put(_CleanupEvent(tuple(results), error_text))

        threading.Thread(
            target=work,
            name="DevClean-Android-SDK-package-uninstall",
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
            self._status.set(f"Android SDK package 统计失败：{outcome}")
            self._set_busy(False)
        elif isinstance(outcome, _InventoryEvent):
            self._inventory = outcome.inventory
            self._render(outcome.inventory)
            self._set_busy(False)
        else:
            reclaimed = sum(result.reclaimed_bytes for result in outcome.results)
            if outcome.error is None:
                self._status.set(
                    f"已卸载 {len(outcome.results)} 个 package，实测释放约 "
                    f"{_format_bytes(reclaimed)}；正在重新统计…"
                )
            else:
                self._status.set(
                    f"已卸载 {len(outcome.results)} 个 package并释放约 "
                    f"{_format_bytes(reclaimed)}，随后停止：{outcome.error}；正在重新统计…"
                )
            self._busy = False
            self._start_inventory()
        self._window.after(100, self._poll)

    def _render(self, inventory: AndroidSdkInventory) -> None:
        for item in self._tree.get_children():
            self._tree.delete(item)
        self._rows.clear()

        root_lines: list[str] = []
        for root in inventory.roots:
            state = (
                root.error
                if root.error
                else f"{len(root.packages)} 个 package / {_format_bytes(root.package_bytes)}"
            )
            ownership = "本地固定磁盘" if root.local_fixed else "非本地固定：只报告"
            manager = str(root.sdkmanager) if root.sdkmanager is not None else "未找到"
            root_lines.append(
                f"{root.sdk_root} — {state} — {ownership}\n  sdkmanager: {manager}"
            )
            for package in root.packages:
                item_id = f"package-{len(self._rows)}"
                self._rows[item_id] = package
                status = "你来决定" if package.deletion_supported else "受保护/只报告"
                self._tree.insert(
                    "",
                    tk.END,
                    iid=item_id,
                    text=package.package_id,
                    values=(
                        _format_bytes(package.logical_bytes),
                        package.version,
                        status,
                        package.description,
                        package.location,
                        str(package.sdk_root),
                    ),
                )
        self._roots_label.configure(
            text="\n".join(root_lines) if root_lines else "没有找到来源可确认的 Android SDK 根。"
        )
        package_count = sum(len(root.packages) for root in inventory.roots)
        executable = sum(
            package.deletion_supported
            for root in inventory.roots
            for package in root.packages
        )
        self._status.set(
            f"共识别 {package_count} 个已安装 package，目录占用约 "
            f"{_format_bytes(inventory.package_bytes)}；其中 {executable} 个可由你明确选择卸载。"
        )


def open_android_sdk_package_maintenance_dialog(
    parent: tk.Tk | tk.Toplevel,
) -> None:
    _AndroidSdkPackageMaintenanceDialog(parent).show()


def _format_bytes(value: int) -> str:
    amount = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value:,} B"


__all__ = ["open_android_sdk_package_maintenance_dialog"]
