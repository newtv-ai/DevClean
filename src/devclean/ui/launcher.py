"""Desktop launcher that adds application-specific user-decision tools."""

from __future__ import annotations

import sys
import tkinter as tk
from collections.abc import Sequence

from devclean.ui.app import DevCleanWindow
from devclean.ui.codex_history_dialog import open_codex_history_dialog


def _install_tools_menu(root: tk.Tk) -> None:
    menu = tk.Menu(root)
    tools = tk.Menu(menu, tearoff=False)
    tools.add_command(
        label="Codex 历史管理…",
        command=lambda: open_codex_history_dialog(root),
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
