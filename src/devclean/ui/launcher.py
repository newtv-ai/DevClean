"""Desktop launcher that adds application-specific user-decision tools."""

from __future__ import annotations

import sys
import tkinter as tk
from collections.abc import Sequence

from devclean.ui.android_project_sdk_reference_dialog import (
    open_android_project_sdk_reference_dialog,
)
from devclean.ui.android_sdk_package_maintenance_dialog import (
    open_android_sdk_package_maintenance_dialog,
)
from devclean.ui.app import DevCleanWindow
from devclean.ui.bazel_maintenance_dialog import open_bazel_maintenance_dialog
from devclean.ui.cargo_project_maintenance_dialog import open_cargo_project_maintenance_dialog
from devclean.ui.claude_maintenance_dialog import open_claude_maintenance_dialog
from devclean.ui.codex_history_dialog import open_codex_history_dialog
from devclean.ui.conan_maintenance_dialog import open_conan_maintenance_dialog
from devclean.ui.conda_maintenance_dialog import open_conda_maintenance_dialog
from devclean.ui.cursor_maintenance_dialog import open_cursor_maintenance_dialog
from devclean.ui.delivery_optimization_cache_maintenance_dialog import (
    open_delivery_optimization_cache_maintenance_dialog,
)
from devclean.ui.docker_unified_maintenance_dialog import open_docker_unified_maintenance_dialog
from devclean.ui.git_repository_maintenance_dialog import (
    open_git_repository_maintenance_dialog,
)
from devclean.ui.go_maintenance_dialog import open_go_maintenance_dialog
from devclean.ui.jetbrains_leftover_maintenance_dialog import (
    open_jetbrains_leftover_maintenance_dialog,
)
from devclean.ui.meson_project_maintenance_dialog import open_meson_project_maintenance_dialog
from devclean.ui.nuget_maintenance_dialog import open_nuget_maintenance_dialog
from devclean.ui.ollama_model_maintenance_dialog import open_ollama_model_maintenance_dialog
from devclean.ui.pip_maintenance_dialog import open_pip_maintenance_dialog
from devclean.ui.pnpm_maintenance_dialog import open_pnpm_maintenance_dialog
from devclean.ui.podman_container_maintenance_dialog import (
    open_podman_container_maintenance_dialog,
)
from devclean.ui.podman_image_maintenance_dialog import open_podman_image_maintenance_dialog
from devclean.ui.task_manager_live_dump_maintenance_dialog import (
    open_task_manager_live_dump_maintenance_dialog,
)
from devclean.ui.unity_asset_store_maintenance_dialog import (
    open_unity_asset_store_maintenance_dialog,
)
from devclean.ui.unity_project_maintenance_dialog import open_unity_project_maintenance_dialog
from devclean.ui.unity_upm_maintenance_dialog import open_unity_upm_maintenance_dialog
from devclean.ui.unreal_maintenance_dialog import open_unreal_maintenance_dialog
from devclean.ui.uv_maintenance_dialog import open_uv_maintenance_dialog
from devclean.ui.vcpkg_maintenance_dialog import open_vcpkg_maintenance_dialog
from devclean.ui.vscode_maintenance_dialog import open_vscode_maintenance_dialog
from devclean.ui.windows_component_store_maintenance_dialog import (
    open_windows_component_store_maintenance_dialog,
)
from devclean.ui.windows_crash_dump_maintenance_dialog import (
    open_windows_crash_dump_maintenance_dialog,
)
from devclean.ui.windows_previous_install_maintenance_dialog import (
    open_windows_previous_install_maintenance_dialog,
)
from devclean.ui.windows_recycle_bin_maintenance_dialog import (
    open_windows_recycle_bin_maintenance_dialog,
)
from devclean.ui.wsl_go_build_cache_dialog import open_wsl_go_build_cache_dialog
from devclean.ui.wsl_go_mod_cache_dialog import open_wsl_go_mod_cache_dialog
from devclean.ui.wsl_inventory_dialog import open_wsl_inventory_dialog
from devclean.ui.wsl_pip_maintenance_dialog import open_wsl_pip_maintenance_dialog
from devclean.ui.wsl_pnpm_maintenance_dialog import open_wsl_pnpm_maintenance_dialog
from devclean.ui.wsl_uv_maintenance_dialog import open_wsl_uv_maintenance_dialog


def _install_tools_menu(root: tk.Tk) -> None:
    menu = tk.Menu(root)
    tools = tk.Menu(menu, tearoff=False)
    tools.add_command(
        label="Codex 历史管理…",
        command=lambda: open_codex_history_dialog(root),
    )
    tools.add_command(
        label="Claude Code 存储维护…",
        command=lambda: open_claude_maintenance_dialog(root),
    )
    tools.add_command(
        label="Cursor 存储维护…",
        command=lambda: open_cursor_maintenance_dialog(root),
    )
    tools.add_command(
        label="VS Code 存储维护…",
        command=lambda: open_vscode_maintenance_dialog(root),
    )
    tools.add_command(
        label="JetBrains 过期版本存储维护…",
        command=lambda: open_jetbrains_leftover_maintenance_dialog(root),
    )
    tools.add_separator()
    tools.add_command(
        label="NuGet 缓存维护…",
        command=lambda: open_nuget_maintenance_dialog(root),
    )
    tools.add_command(
        label="pip 缓存维护…",
        command=lambda: open_pip_maintenance_dialog(root),
    )
    tools.add_command(
        label="pnpm Store 垃圾收集…",
        command=lambda: open_pnpm_maintenance_dialog(root),
    )
    tools.add_command(
        label="uv 缓存垃圾收集…",
        command=lambda: open_uv_maintenance_dialog(root),
    )
    tools.add_command(
        label="Go 缓存维护…",
        command=lambda: open_go_maintenance_dialog(root),
    )
    tools.add_command(
        label="Conda 安全缓存维护…",
        command=lambda: open_conda_maintenance_dialog(root),
    )
    tools.add_command(
        label="Conan 2 安全缓存维护…",
        command=lambda: open_conan_maintenance_dialog(root),
    )
    tools.add_command(
        label="vcpkg 存储维护…",
        command=lambda: open_vcpkg_maintenance_dialog(root),
    )
    tools.add_command(
        label="Git / Git LFS 存储维护…",
        command=lambda: open_git_repository_maintenance_dialog(root),
    )
    tools.add_command(
        label="Ollama 本机模型维护…",
        command=lambda: open_ollama_model_maintenance_dialog(root),
    )
    tools.add_command(
        label="Docker 本机存储维护…",
        command=lambda: open_docker_unified_maintenance_dialog(root),
    )
    tools.add_command(
        label="Unreal DDC 安全维护…",
        command=lambda: open_unreal_maintenance_dialog(root),
    )
    tools.add_command(
        label="Bazel 工作区维护…",
        command=lambda: open_bazel_maintenance_dialog(root),
    )
    tools.add_command(
        label="Cargo 工作区 target 维护…",
        command=lambda: open_cargo_project_maintenance_dialog(root),
    )
    tools.add_command(
        label="Meson 构建目录维护…",
        command=lambda: open_meson_project_maintenance_dialog(root),
    )
    tools.add_command(
        label="Android SDK Package 维护…",
        command=lambda: open_android_sdk_package_maintenance_dialog(root),
    )
    tools.add_command(
        label="Android 项目 SDK 显式引用检查…",
        command=lambda: open_android_project_sdk_reference_dialog(root),
    )
    tools.add_command(
        label="Podman 已停止容器维护…",
        command=lambda: open_podman_container_maintenance_dialog(root),
    )
    tools.add_command(
        label="Podman 镜像维护…",
        command=lambda: open_podman_image_maintenance_dialog(root),
    )
    tools.add_command(
        label="Unity 项目 Library 维护…",
        command=lambda: open_unity_project_maintenance_dialog(root),
    )
    tools.add_command(
        label="Unity Asset Store 缓存维护…",
        command=lambda: open_unity_asset_store_maintenance_dialog(root),
    )
    tools.add_command(
        label="Unity UPM 全局缓存维护…",
        command=lambda: open_unity_upm_maintenance_dialog(root),
    )
    tools.add_separator()
    tools.add_command(
        label="Windows 组件存储维护…",
        command=lambda: open_windows_component_store_maintenance_dialog(root),
    )
    tools.add_command(
        label="以前的 Windows 安装维护…",
        command=lambda: open_windows_previous_install_maintenance_dialog(root),
    )
    tools.add_command(
        label="Windows 回收站维护…",
        command=lambda: open_windows_recycle_bin_maintenance_dialog(root),
    )
    tools.add_command(
        label="Delivery Optimization 缓存维护…",
        command=lambda: open_delivery_optimization_cache_maintenance_dialog(root),
    )
    tools.add_command(
        label="Windows 崩溃转储维护…",
        command=lambda: open_windows_crash_dump_maintenance_dialog(root),
    )
    tools.add_command(
        label="Task Manager 实时内核转储维护…",
        command=lambda: open_task_manager_live_dump_maintenance_dialog(root),
    )
    tools.add_separator()
    tools.add_command(
        label="WSL 发行版存储概览…",
        command=lambda: open_wsl_inventory_dialog(root),
    )
    tools.add_command(
        label="WSL pip 缓存维护…",
        command=lambda: open_wsl_pip_maintenance_dialog(root),
    )
    tools.add_command(
        label="WSL uv 缓存维护…",
        command=lambda: open_wsl_uv_maintenance_dialog(root),
    )
    tools.add_command(
        label="WSL pnpm Store 维护…",
        command=lambda: open_wsl_pnpm_maintenance_dialog(root),
    )
    tools.add_command(
        label="WSL Go 构建缓存维护…",
        command=lambda: open_wsl_go_build_cache_dialog(root),
    )
    tools.add_command(
        label="WSL Go 模块缓存维护…",
        command=lambda: open_wsl_go_mod_cache_dialog(root),
    )
    menu.add_cascade(label="工具", menu=tools)
    root.configure(menu=menu)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    if arguments == ("--smoke",):
        return 0
    if arguments == ("--ui-smoke",):
        root = tk.Tk()
        root.withdraw()
        DevCleanWindow(root)
        _install_tools_menu(root)
        root.update_idletasks()
        root.destroy()
        return 0
    root = tk.Tk()
    DevCleanWindow(root)
    _install_tools_menu(root)
    root.mainloop()
    return 0


__all__ = ["main"]
