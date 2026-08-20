"""Android SDK exact package maintenance with AVD system-image correlation."""

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
    AndroidSdkRootInventory,
    AndroidSdkUninstallResult,
    inventory_android_sdk_packages,
    uninstall_android_sdk_package,
)


@dataclass(frozen=True, slots=True)
class _InventoryEvent:
    inventory: AndroidSdkInventory


@dataclass(frozen=True, slots=True)
class _UninstallEvent:
    result: AndroidSdkUninstallResult | None
    error: str | None = None


class _AndroidSdkPackageMaintenanceDialog:
    def __init__(self, parent: tk.Tk | tk.Toplevel) -> None:
        self._parent = parent
        self._window = tk.Toplevel(parent)
        self._window.title("Android SDK Package 维护")
        self._window.geometry("1200x750")
        self._window.minsize(980, 620)
        self._events: queue.Queue[_InventoryEvent | _UninstallEvent | Exception] = queue.Queue()
        self._inventory: AndroidSdkInventory | None = None
        self._rows: dict[str, tuple[AndroidSdkPackageEntry, AndroidSdkRootInventory]] = {}
        self._busy = False
        self._status = tk.StringVar(value="尚未通过 sdkmanager 检查已安装 package。")

        root = ttk.Frame(self._window, padding=12)
        root.pack(fill=tk.BOTH, expand=True)
        ttk.Label(root, text="Android SDK Package 维护", font=("Segoe UI", 13, "bold")).pack(
            anchor=tk.W
        )
        ttk.Label(
            root,
            text=(
                "Android SDK 是已安装开发工具，不是整棵缓存。DevClean 只使用每个 SDK 自己的 "
                "sdkmanager 列出并卸载一个你明确选择的精确 package。平台、Build Tools、NDK、"
                "CMake 和 system image 即使很旧或很大，也不会自动删除。"
            ),
            wraplength=1160,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(4, 5))
        ttk.Label(
            root,
            text=(
                "System image 额外关联现有 AVD 的 config.ini：只要某个 AVD 的 image.sysdir.1/2 "
                "指向该 package，就直接保护；如果 AVD 引用证明不完整，所有 system image package "
                "也会 fail closed。未被 AVD 引用的 system image 仍然只是 USER_REVIEW，不是自动清理。"
            ),
            wraplength=1160,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(0, 9))

        self._roots_label = ttk.Label(root, text="尚未检查。", wraplength=1160, justify=tk.LEFT)
        self._roots_label.pack(anchor=tk.W, pady=(0, 8))

        columns = ("size", "version", "avds", "status", "description", "location", "root")
        self._tree = ttk.Treeview(root, columns=columns, show="tree headings", selectmode="browse")
        self._tree.heading("#0", text="Package ID")
        self._tree.heading("size", text="占用")
        self._tree.heading("version", text="版本")
        self._tree.heading("avds", text="AVD 引用")
        self._tree.heading("status", text="判定")
        self._tree.heading("description", text="说明")
        self._tree.heading("location", text="Location")
        self._tree.heading("root", text="SDK 根")
        self._tree.column("#0", width=260, stretch=True)
        self._tree.column("size", width=90, anchor=tk.E, stretch=False)
        self._tree.column("version", width=95, stretch=False)
        self._tree.column("avds", width=150, stretch=True)
        self._tree.column("status", width=210, stretch=True)
        self._tree.column("description", width=230, stretch=True)
        self._tree.column("location", width=220, stretch=True)
        self._tree.column("root", width=230, stretch=True)
        self._tree.pack(fill=tk.BOTH, expand=True)
        self._tree.bind("<<TreeviewSelect>>", lambda event: self._update_button())

        ttk.Label(root, textvariable=self._status, wraplength=1160, justify=tk.LEFT).pack(
            anchor=tk.W, pady=(8, 0)
        )
        ttk.Label(
            root,
            text=(
                "执行前会再次确认 SDK 根、sdkmanager 文件身份、package ID/版本/Location/目录身份和 AVD 引用，"
                "并再次确认 Android Studio、Gradle/sdkmanager、ADB、Emulator/QEMU 没有在使用 SDK。"
                "真正执行的命令只有 sdkmanager --uninstall <精确 package-id> --sdk_root=<精确 SDK 根>。"
            ),
            wraplength=1160,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(5, 0))

        footer = ttk.Frame(root)
        footer.pack(fill=tk.X, pady=(10, 0))
        self._refresh_button = ttk.Button(footer, text="检查/刷新", command=self._start_inventory)
        self._refresh_button.pack(side=tk.RIGHT, padx=(8, 0))
        self._uninstall_button = ttk.Button(
            footer,
            text="卸载选中的精确 Package…",
            command=self._confirm_uninstall,
            state=tk.DISABLED,
        )
        self._uninstall_button.pack(side=tk.RIGHT, padx=(8, 0))
        ttk.Button(footer, text="关闭", command=self._window.destroy).pack(side=tk.RIGHT)

        self._window.after(100, self._poll)

    def show(self) -> None:
        self._window.transient(self._parent)
        self._window.grab_set()
        self._window.focus_set()
        self._start_inventory()

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._refresh_button.configure(state=tk.DISABLED if busy else tk.NORMAL)
        self._update_button()

    def _selected(self) -> tuple[AndroidSdkPackageEntry, AndroidSdkRootInventory] | None:
        selected = self._tree.selection()
        if len(selected) != 1:
            return None
        return self._rows.get(selected[0])

    def _update_button(self) -> None:
        selected = self._selected()
        enabled = not self._busy and selected is not None and selected[0].deletion_supported
        self._uninstall_button.configure(state=tk.NORMAL if enabled else tk.DISABLED)

    def _start_inventory(self) -> None:
        if self._busy:
            return
        self._set_busy(True)
        self._status.set("正在运行 sdkmanager --list，并核对 AVD system-image 引用…")

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

    def _confirm_uninstall(self) -> None:
        if self._busy:
            return
        selected = self._selected()
        if selected is None:
            return
        package, root = selected
        if not package.deletion_supported:
            return
        avd_text = ", ".join(package.avd_names) if package.avd_names else "无静态 AVD 引用"
        if not messagebox.askyesno(
            "确认卸载 Android SDK Package",
            (
                "将通过这个 SDK 自己的 sdkmanager 卸载一个精确 package。\n\n"
                f"Package：{package.package_id}\n"
                f"版本：{package.version}\n"
                f"SDK：{package.sdk_root}\n"
                f"Location：{package.location}\n"
                f"当前目录占用：{_format_bytes(package.logical_bytes)}\n"
                f"AVD：{avd_text}\n\n"
                "这个 package 仍可能被项目、CI、旧分支或离线工作流需要。卸载后再次使用通常需要重新下载。"
                "DevClean 不会直接递归删除 SDK 目录，也不会批量推断其他 package。\n\n确定继续吗？"
            ),
            icon=messagebox.WARNING,
            parent=self._window,
        ):
            return
        self._set_busy(True)
        self._status.set(
            "正在重新验证 SDK、package 和 AVD 引用，然后调用精确 sdkmanager --uninstall…"
        )

        def work() -> None:
            try:
                result = uninstall_android_sdk_package(package, root)
            except Exception as error:
                self._events.put(_UninstallEvent(None, str(error)))
            else:
                self._events.put(_UninstallEvent(result))

        threading.Thread(
            target=work,
            name="DevClean-Android-SDK-package-uninstall",
            daemon=True,
        ).start()

    def _poll(self) -> None:
        if not self._window.winfo_exists():
            return
        try:
            event = self._events.get_nowait()
        except queue.Empty:
            self._window.after(100, self._poll)
            return

        if isinstance(event, Exception):
            self._inventory = None
            self._clear_tree()
            self._roots_label.configure(text="")
            self._status.set(f"Android SDK package 检查失败：{event}")
            self._set_busy(False)
        elif isinstance(event, _InventoryEvent):
            self._inventory = event.inventory
            self._render(event.inventory)
            self._set_busy(False)
        else:
            if event.error is not None:
                self._status.set(f"Android SDK package 卸载未完成：{event.error}")
                self._set_busy(False)
            elif event.result is not None:
                result = event.result
                self._status.set(
                    f"已卸载 {result.package_id}；对应 package 路径实测减少约 "
                    f"{_format_bytes(result.reclaimed_bytes)}。正在重新统计…"
                )
                self._set_busy(False)
                self._start_inventory()
        self._window.after(100, self._poll)

    def _clear_tree(self) -> None:
        for item in self._tree.get_children():
            self._tree.delete(item)
        self._rows.clear()

    def _render(self, inventory: AndroidSdkInventory) -> None:
        self._clear_tree()
        root_lines: list[str] = []
        for root in inventory.roots:
            if root.error:
                state = f"只报告：{root.error}"
            else:
                executable = sum(package.deletion_supported for package in root.packages)
                state = (
                    f"{len(root.packages)} 个 installed package / "
                    f"{_format_bytes(root.package_bytes)} / {executable} 个 USER_REVIEW 可执行"
                )
            root_lines.append(f"{root.sdk_root} — {state}")
            for package in root.packages:
                item_id = f"package-{len(self._rows)}"
                self._rows[item_id] = (package, root)
                avds = ", ".join(package.avd_names) if package.avd_names else "—"
                status = "USER_REVIEW" if package.deletion_supported else package.protected_reason
                self._tree.insert(
                    "",
                    tk.END,
                    iid=item_id,
                    text=package.package_id,
                    values=(
                        _format_bytes(package.logical_bytes),
                        package.version,
                        avds,
                        status,
                        package.description,
                        package.location,
                        str(package.sdk_root),
                    ),
                )
        proof = (
            f"AVD 引用证明完整：检查到 {len(inventory.avd_references)} 个 AVD。"
            if inventory.avd_reference_proof_complete
            else "AVD 引用证明不完整；所有 system-image package 已保护。原因："
            + inventory.avd_reference_proof_reason
        )
        self._roots_label.configure(
            text=("\n".join(root_lines) if root_lines else "没有找到来源可确认的 Android SDK 根。")
            + "\n"
            + proof
        )
        count = sum(len(root.packages) for root in inventory.roots)
        executable = sum(
            package.deletion_supported for root in inventory.roots for package in root.packages
        )
        self._status.set(
            f"共识别 {count} 个 installed package；{executable} 个可以在再次验证后由你单独确认卸载。"
        )


def open_android_sdk_package_maintenance_dialog(parent: tk.Tk | tk.Toplevel) -> None:
    _AndroidSdkPackageMaintenanceDialog(parent).show()


def _format_bytes(value: int) -> str:
    amount = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value:,} B"


__all__ = ["open_android_sdk_package_maintenance_dialog"]
