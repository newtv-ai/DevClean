"""Unity Asset Store package-cache review and exact package removal UI."""

# ruff: noqa: RUF001

from __future__ import annotations

import queue
import threading
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from devclean.core.unity_asset_store_maintenance import (
    UnityAssetStoreDeleteResult,
    UnityAssetStoreInventory,
    UnityAssetStorePackage,
    UnityAssetStoreRootOrigin,
    delete_unity_asset_store_package,
    inventory_unity_asset_store,
)


@dataclass(frozen=True, slots=True)
class _InventoryEvent:
    inventory: UnityAssetStoreInventory


@dataclass(frozen=True, slots=True)
class _CleanupEvent:
    results: tuple[UnityAssetStoreDeleteResult, ...]
    error: str | None = None


class _UnityAssetStoreMaintenanceDialog:
    def __init__(self, parent: tk.Tk | tk.Toplevel) -> None:
        self._parent = parent
        self._window = tk.Toplevel(parent)
        self._window.title("Unity Asset Store 资源包缓存维护")
        self._window.geometry("1080x700")
        self._window.minsize(900, 580)
        self._events: queue.Queue[_InventoryEvent | _CleanupEvent | Exception] = queue.Queue()
        self._status = tk.StringVar(value="正在统计 Unity Asset Store 资源包缓存…")
        self._inventory: UnityAssetStoreInventory | None = None
        self._manual_locations: list[Path] = []
        self._row_packages: dict[str, UnityAssetStorePackage] = {}
        self._busy = False

        container = ttk.Frame(self._window, padding=12)
        container.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            container,
            text="Unity Asset Store 资源包缓存",
            font=("Segoe UI", 13, "bold"),
        ).pack(anchor=tk.W)
        ttk.Label(
            container,
            text=(
                "Unity 官方允许从 Asset Store 缓存中逐个删除 .unitypackage，且不会删除已经"
                "导入项目的 Assets。这里因此属于“你来决定”：DevClean 能确定它是可重新获取"
                "的缓存副本，但离线价值、下载成本和资源是否仍在商店可用由你判断；不调用 AI，"
                "也绝不整棵删除缓存目录。"
            ),
            wraplength=1040,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(4, 8))

        toolbar = ttk.Frame(container)
        toolbar.pack(fill=tk.X, pady=(0, 8))
        self._add_button = ttk.Button(
            toolbar,
            text="添加缓存位置…",
            command=self._add_location,
        )
        self._add_button.pack(side=tk.LEFT)
        ttk.Label(
            toolbar,
            text=(
                "若你在 Unity Preferences 里改过 My Assets 缓存位置，可按那里显示的位置添加；"
                "DevClean 不猜未公开的 EditorPrefs 内部键。"
            ),
            wraplength=760,
            justify=tk.LEFT,
        ).pack(side=tk.LEFT, padx=(10, 0))
        self._refresh_button = ttk.Button(
            toolbar,
            text="重新统计",
            command=self._start_inventory,
        )
        self._refresh_button.pack(side=tk.RIGHT)

        roots_frame = ttk.LabelFrame(container, text="已检查的缓存根", padding=8)
        roots_frame.pack(fill=tk.X, pady=(0, 8))
        self._roots_label = ttk.Label(
            roots_frame,
            text="尚未完成统计。",
            wraplength=1020,
            justify=tk.LEFT,
        )
        self._roots_label.pack(anchor=tk.W)

        table_frame = ttk.Frame(container)
        table_frame.pack(fill=tk.BOTH, expand=True)
        self._tree = ttk.Treeview(
            table_frame,
            columns=("size", "publisher", "source", "path"),
            show="headings",
            selectmode="extended",
        )
        self._tree.heading("size", text="大小")
        self._tree.heading("publisher", text="发布者")
        self._tree.heading("source", text="缓存来源")
        self._tree.heading("path", text="相对路径")
        self._tree.column("size", width=100, anchor=tk.E, stretch=False)
        self._tree.column("publisher", width=180, stretch=False)
        self._tree.column("source", width=120, stretch=False)
        self._tree.column("path", width=620, stretch=True)
        scrollbar = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self._tree.yview)
        self._tree.configure(yscrollcommand=scrollbar.set)
        self._tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        ttk.Label(
            container,
            text=(
                "执行边界：只处理你选中的普通 .unitypackage 文件；删除前重新验证缓存根和文件"
                "的 Windows 文件身份，并通过 DevClean 的 handle-bound 精确删除执行。链接、"
                "junction、根外文件、其他扩展名以及 UPM global cache 均不在这条规则中。"
            ),
            wraplength=1040,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(8, 0))
        ttk.Label(
            container,
            textvariable=self._status,
            wraplength=1040,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(6, 0))

        footer = ttk.Frame(container)
        footer.pack(fill=tk.X, pady=(10, 0))
        self._delete_button = ttk.Button(
            footer,
            text="删除选中的缓存包…",
            command=self._start_cleanup,
            state=tk.DISABLED,
        )
        self._delete_button.pack(side=tk.RIGHT, padx=(8, 0))
        ttk.Button(footer, text="关闭", command=self._window.destroy).pack(side=tk.RIGHT)

        self._window.after(100, self._poll)
        self._start_inventory()

    def show(self) -> None:
        self._window.transient(self._parent)
        self._window.grab_set()
        self._window.focus_set()

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        state = tk.DISABLED if busy else tk.NORMAL
        self._refresh_button.configure(state=state)
        self._add_button.configure(state=state)
        self._delete_button.configure(
            state=tk.DISABLED
            if busy or self._inventory is None or not self._inventory.packages
            else tk.NORMAL
        )

    def _add_location(self) -> None:
        if self._busy:
            return
        selected = filedialog.askdirectory(
            parent=self._window,
            title="选择 Unity Asset Store 缓存位置或其父目录",
            mustexist=True,
        )
        if not selected:
            return
        path = Path(selected)
        if path not in self._manual_locations:
            self._manual_locations.append(path)
        self._start_inventory()

    def _start_inventory(self) -> None:
        if self._busy:
            return
        self._set_busy(True)
        self._status.set("正在统计默认、环境变量和已添加的 Asset Store 缓存位置…")
        extra = tuple(self._manual_locations)

        def work() -> None:
            try:
                inventory = inventory_unity_asset_store(extra_locations=extra)
            except Exception as error:
                self._events.put(error)
            else:
                self._events.put(_InventoryEvent(inventory))

        threading.Thread(
            target=work,
            name="DevClean-Unity-asset-store-inventory",
            daemon=True,
        ).start()

    def _start_cleanup(self) -> None:
        if self._busy or self._inventory is None:
            return
        selected = [
            self._row_packages[item_id]
            for item_id in self._tree.selection()
            if item_id in self._row_packages
        ]
        if not selected:
            messagebox.showinfo(
                "Unity Asset Store 缓存维护",
                "请先选择一个或多个要删除的 .unitypackage 缓存副本。",
                parent=self._window,
            )
            return
        total = sum(package.logical_bytes for package in selected)
        if not messagebox.askyesno(
            "确认删除缓存包",
            (
                f"将永久删除 {len(selected)} 个 Asset Store 缓存包，约 {_format_bytes(total)}。\n\n"
                "已导入项目的 Assets 不会因此被删除，但这些本地 .unitypackage 副本将不再可用于"
                "离线重新导入；若资源已下架，也可能无法再次下载。\n\n"
                "DevClean 不会删除整个缓存目录。确定继续吗？"
            ),
            icon=messagebox.WARNING,
            parent=self._window,
        ):
            return

        self._set_busy(True)
        self._status.set(f"正在精确删除 {len(selected)} 个已确认的 .unitypackage…")

        def work(packages: tuple[UnityAssetStorePackage, ...] = tuple(selected)) -> None:
            results: list[UnityAssetStoreDeleteResult] = []
            error_text: str | None = None
            for package in packages:
                try:
                    results.append(
                        delete_unity_asset_store_package(
                            package.cache_root,
                            package.path,
                        )
                    )
                except Exception as error:
                    error_text = str(error)
                    break
            self._events.put(_CleanupEvent(tuple(results), error_text))

        threading.Thread(
            target=work,
            name="DevClean-Unity-asset-store-cleanup",
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
            self._status.set(f"Unity Asset Store 缓存统计失败：{outcome}")
            self._set_busy(False)
        elif isinstance(outcome, _InventoryEvent):
            self._inventory = outcome.inventory
            self._render(outcome.inventory)
            self._set_busy(False)
        else:
            reclaimed = sum(result.reclaimed_bytes for result in outcome.results)
            if outcome.error is None:
                self._status.set(
                    f"已精确删除 {len(outcome.results)} 个缓存包，释放约 "
                    f"{_format_bytes(reclaimed)}；正在重新统计…"
                )
            else:
                self._status.set(
                    f"已删除 {len(outcome.results)} 个缓存包并释放约 "
                    f"{_format_bytes(reclaimed)}，随后停止：{outcome.error}；正在重新统计…"
                )
            self._busy = False
            self._start_inventory()
        self._window.after(100, self._poll)

    def _render(self, inventory: UnityAssetStoreInventory) -> None:
        for item in self._tree.get_children():
            self._tree.delete(item)
        self._row_packages.clear()

        for index, package in enumerate(inventory.packages):
            item_id = f"package-{index}"
            self._row_packages[item_id] = package
            self._tree.insert(
                "",
                tk.END,
                iid=item_id,
                values=(
                    _format_bytes(package.logical_bytes),
                    package.publisher,
                    _origin_label(package.origin),
                    str(package.relative_path),
                ),
            )

        root_lines = []
        for root in inventory.roots:
            state = (
                f"{root.package_count} 个包 / {_format_bytes(root.package_bytes)}"
                if root.exists
                else "不存在"
            )
            root_lines.append(f"{_origin_label(root.origin)}: {root.path} — {state}")
        self._roots_label.configure(
            text="\n".join(root_lines) if root_lines else "未找到可确认的缓存根。"
        )
        self._status.set(
            f"共找到 {len(inventory.packages)} 个 .unitypackage，约 "
            f"{_format_bytes(inventory.package_bytes)}。默认不选择；由你决定是否删除。"
        )


def open_unity_asset_store_maintenance_dialog(
    parent: tk.Tk | tk.Toplevel,
) -> None:
    _UnityAssetStoreMaintenanceDialog(parent).show()


def _origin_label(origin: UnityAssetStoreRootOrigin) -> str:
    return {
        UnityAssetStoreRootOrigin.DEFAULT: "默认位置",
        UnityAssetStoreRootOrigin.ENVIRONMENT: "环境变量",
        UnityAssetStoreRootOrigin.USER_SELECTED: "手动添加",
    }[origin]


def _format_bytes(value: int) -> str:
    amount = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value:,} B"


__all__ = ["open_unity_asset_store_maintenance_dialog"]
