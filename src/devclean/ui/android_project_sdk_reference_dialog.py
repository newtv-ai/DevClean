"""Read-only Android Gradle project -> installed SDK package reference explainer."""

# ruff: noqa: E501, RUF001

from __future__ import annotations

import queue
import threading
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, ttk

from devclean.core.android_project_sdk_references import (
    AndroidProjectReferenceScan,
    scan_android_project_sdk_references,
)
from devclean.core.android_sdk_package_maintenance import AndroidSdkInventory
from devclean.core.android_sdk_package_maintenance import (
    inventory_android_sdk_packages as inventory_sdk_packages,
)


@dataclass(frozen=True, slots=True)
class _ReportEvent:
    scan: AndroidProjectReferenceScan
    sdk_inventory: AndroidSdkInventory


class _AndroidProjectSdkReferenceDialog:
    def __init__(self, parent: tk.Tk | tk.Toplevel) -> None:
        self._parent = parent
        self._window = tk.Toplevel(parent)
        self._window.title("Android 项目 SDK 显式引用检查")
        self._window.geometry("1180x720")
        self._window.minsize(980, 600)
        self._events: queue.Queue[_ReportEvent | Exception] = queue.Queue()
        self._busy = False
        self._selected_root: Path | None = None
        self._status = tk.StringVar(value="请选择一个 Android/Gradle 项目根或模块目录。")
        self._project = tk.StringVar(value="尚未选择项目。")

        root = ttk.Frame(self._window, padding=12)
        root.pack(fill=tk.BOTH, expand=True)
        ttk.Label(root, text="Android 项目 SDK 显式引用检查", font=("Segoe UI", 13, "bold")).pack(
            anchor=tk.W
        )
        ttk.Label(
            root,
            text=(
                "这个工具只读取你明确选择的本地 Gradle 项目，不执行 Gradle，也不修改项目或 Android SDK。"
                "它只把能够从 build.gradle(.kts) / settings.gradle(.kts) 中静态证明的字面量版本映射到 sdkmanager package。"
            ),
            wraplength=1130,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(4, 4))
        ttk.Label(
            root,
            text=(
                "重要：没有显示引用绝不等于“未使用”。Gradle 脚本是可执行逻辑；版本可能来自变量、convention plugin、version catalog、included build、"
                "Android Gradle Plugin 默认值或其他动态配置。这里的结果只能增加“这个 package 明确被项目引用”的正证据，永远不能产生卸载权限。"
            ),
            wraplength=1130,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(0, 8))
        ttk.Label(
            root,
            text=(
                "当前正证据：compileSdk → platforms;android-N；显式 buildToolsVersion → build-tools;VERSION；"
                "显式 ndkVersion → ndk;VERSION；externalNativeBuild.cmake 的显式 version → cmake;VERSION。"
                "新的 Android settings plugin 顶层 compileSdk release(N) 也会识别；模块级显式值与 settings 值同时保留为保护性证据。"
            ),
            wraplength=1130,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(0, 8))

        project_row = ttk.Frame(root)
        project_row.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(project_row, textvariable=self._project, wraplength=900, justify=tk.LEFT).pack(
            side=tk.LEFT, fill=tk.X, expand=True
        )
        self._choose_button = ttk.Button(
            project_row, text="选择项目目录…", command=self._choose_project
        )
        self._choose_button.pack(side=tk.RIGHT)

        columns = ("kind", "value", "package", "installed", "file", "line")
        self._tree = ttk.Treeview(root, columns=columns, show="headings")
        for column, title, width in (
            ("kind", "显式声明", 160),
            ("value", "字面量", 130),
            ("package", "对应 sdkmanager Package", 260),
            ("installed", "当前已安装匹配", 290),
            ("file", "来源文件", 250),
            ("line", "行", 55),
        ):
            self._tree.heading(column, text=title)
            self._tree.column(column, width=width, anchor=tk.W)
        self._tree.pack(fill=tk.BOTH, expand=True)

        self._warnings = tk.Text(root, height=6, wrap="word", state=tk.DISABLED)
        self._warnings.pack(fill=tk.X, pady=(8, 0))
        ttk.Label(root, textvariable=self._status, wraplength=1130, justify=tk.LEFT).pack(
            anchor=tk.W, pady=(8, 0)
        )

        footer = ttk.Frame(root)
        footer.pack(fill=tk.X, pady=(10, 0))
        self._rescan_button = ttk.Button(
            footer,
            text="重新检查",
            command=self._start_scan,
            state=tk.DISABLED,
        )
        self._rescan_button.pack(side=tk.RIGHT, padx=(8, 0))
        ttk.Button(footer, text="关闭", command=self._window.destroy).pack(side=tk.RIGHT)

        self._window.after(100, self._poll)

    def show(self) -> None:
        self._window.transient(self._parent)
        self._window.grab_set()
        self._window.focus_set()

    def _choose_project(self) -> None:
        if self._busy:
            return
        selected = filedialog.askdirectory(
            parent=self._window,
            title="选择 Android/Gradle 项目根或模块目录",
            mustexist=True,
        )
        if not selected:
            return
        self._selected_root = Path(selected)
        self._project.set(f"所选项目：{self._selected_root}")
        self._start_scan()

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._choose_button.configure(state=tk.DISABLED if busy else tk.NORMAL)
        self._rescan_button.configure(
            state=tk.NORMAL if not busy and self._selected_root is not None else tk.DISABLED
        )

    def _start_scan(self) -> None:
        project_root = self._selected_root
        if self._busy or project_root is None:
            return
        self._set_busy(True)
        self._status.set(
            "正在静态读取项目 build/settings 脚本，并通过 sdkmanager 核对当前已安装 package…"
        )

        def work() -> None:
            try:
                scan = scan_android_project_sdk_references(project_root)
                sdk_inventory = inventory_sdk_packages()
            except Exception as error:
                self._events.put(error)
            else:
                self._events.put(_ReportEvent(scan, sdk_inventory))

        threading.Thread(
            target=work,
            name="DevClean-Android-project-SDK-reference-scan",
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
            self._clear_tree()
            self._set_warnings((str(event),))
            self._status.set(f"项目 SDK 引用检查失败：{event}")
        else:
            self._render(event)
        self._set_busy(False)
        self._window.after(100, self._poll)

    def _render(self, event: _ReportEvent) -> None:
        self._clear_tree()
        installed: dict[str, list[str]] = {}
        for root in event.sdk_inventory.roots:
            for package in root.packages:
                installed.setdefault(package.package_id.casefold(), []).append(
                    f"{package.version} @ {root.sdk_root} ({_format_bytes(package.logical_bytes)})"
                )

        for index, reference in enumerate(event.scan.references):
            matches = installed.get(reference.package_id.casefold(), [])
            installed_text = (
                "；".join(matches) if matches else "未在当前已识别 SDK 中发现精确 package"
            )
            try:
                relative = reference.source_file.relative_to(reference.project_root)
                source = str(relative)
            except ValueError:
                source = str(reference.source_file)
            self._tree.insert(
                "",
                tk.END,
                iid=f"ref-{index}",
                values=(
                    reference.kind,
                    reference.raw_value,
                    reference.package_id,
                    installed_text,
                    source,
                    reference.line_number,
                ),
            )

        general = [
            "静态扫描只提供正证据；未出现的 package 永远不能标记为 unused。",
            "未显式声明 buildToolsVersion 时，AGP 可以选择默认 Build Tools；未显式声明 ndkVersion 时，AGP 也可能选择兼容的默认 NDK。",
            "Gradle 变量、插件、version catalog、included build、脚本 apply、生成配置或其他动态逻辑可能决定最终版本；DevClean 不执行 Gradle 来猜测它们。",
        ]
        self._set_warnings(tuple(general) + event.scan.warnings)
        self._status.set(
            f"已稳定读取 {event.scan.files_scanned} 个 build/settings 文件；"
            f"找到 {len(event.scan.references)} 条可静态证明的 SDK package 正引用。"
            "这些引用用于解释/保护性审查，不会自动卸载任何 package。"
        )

    def _clear_tree(self) -> None:
        for item in self._tree.get_children():
            self._tree.delete(item)

    def _set_warnings(self, warnings: tuple[str, ...]) -> None:
        self._warnings.configure(state=tk.NORMAL)
        self._warnings.delete("1.0", tk.END)
        self._warnings.insert("1.0", "\n".join(f"• {warning}" for warning in warnings))
        self._warnings.configure(state=tk.DISABLED)


def _format_bytes(value: int) -> str:
    amount = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value:,} B"


def open_android_project_sdk_reference_dialog(parent: tk.Tk | tk.Toplevel) -> None:
    _AndroidProjectSdkReferenceDialog(parent).show()


__all__ = ["open_android_project_sdk_reference_dialog"]
