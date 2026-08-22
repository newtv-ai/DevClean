"""Searchable home for DevClean's source-audited maintenance tools."""

from __future__ import annotations

import tkinter as tk
from dataclasses import dataclass

_BG = "#F6F1EA"
_SURFACE = "#FFFDFC"
_BORDER = "#E4D9CC"
_TEXT = "#26342F"
_MUTED = "#6F756F"
_PRIMARY = "#B85F43"


@dataclass(frozen=True, slots=True)
class _Tool:
    menu_index: int
    group: str
    label: str


def _read_tools(menu: tk.Menu) -> tuple[_Tool, ...]:
    end = menu.index("end")
    if end is None:
        return ()
    groups = (
        "编辑器与 AI",
        "开发工具、包与模型",
        "Windows 维护",
        "WSL 维护",
    )
    group_index = 0
    tools: list[_Tool] = []
    for index in range(int(end) + 1):
        kind = menu.type(index)
        if kind == "separator":
            group_index = min(group_index + 1, len(groups) - 1)
            continue
        if kind != "command":
            continue
        label = str(menu.entrycget(index, "label")).rstrip("…").strip()
        tools.append(_Tool(index, groups[group_index], label))
    return tuple(tools)


def open_tool_palette(root: tk.Tk, menu: tk.Menu) -> None:
    """Open a warm searchable palette over the existing trusted menu commands."""

    tools = _read_tools(menu)
    window = tk.Toplevel(root)
    window.title("DevClean · 工具中心")
    window.geometry("820x650")
    window.minsize(680, 520)
    window.configure(background=_BG)
    window.transient(root)

    header = tk.Frame(window, background=_SURFACE, padx=24, pady=20)
    header.pack(fill=tk.X, padx=18, pady=(18, 10))
    tk.Label(
        header,
        text="工具中心",
        background=_SURFACE,
        foreground=_TEXT,
        font=("Segoe UI Semibold", 20),
    ).pack(anchor=tk.W)
    tk.Label(
        header,
        text=(
            "主页扫描只负责有明确删除依据的文件。厂商维护、包管理器、模型、"
            "Windows 与 WSL 清理都在这里。"
        ),
        background=_SURFACE,
        foreground=_MUTED,
        font=("Segoe UI", 10),
        wraplength=730,
        justify=tk.LEFT,
    ).pack(anchor=tk.W, pady=(4, 12))

    query = tk.StringVar()
    search_shell = tk.Frame(
        header,
        background="#F1E9DE",
        highlightbackground=_BORDER,
        highlightthickness=1,
        padx=10,
        pady=7,
    )
    search_shell.pack(fill=tk.X)
    tk.Label(
        search_shell,
        text="⌕",
        background="#F1E9DE",
        foreground=_PRIMARY,
        font=("Segoe UI Semibold", 14),
    ).pack(side=tk.LEFT, padx=(2, 8))
    search = tk.Entry(
        search_shell,
        textvariable=query,
        relief=tk.FLAT,
        borderwidth=0,
        background="#F1E9DE",
        foreground=_TEXT,
        insertbackground=_TEXT,
        font=("Segoe UI", 11),
    )
    search.pack(side=tk.LEFT, fill=tk.X, expand=True)

    shell = tk.Frame(window, background=_BG)
    shell.pack(fill=tk.BOTH, expand=True, padx=18, pady=(0, 18))
    canvas = tk.Canvas(
        shell,
        background=_BG,
        highlightthickness=0,
        borderwidth=0,
    )
    scrollbar = tk.Scrollbar(shell, orient=tk.VERTICAL, command=canvas.yview)
    canvas.configure(yscrollcommand=scrollbar.set)
    scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
    canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    body = tk.Frame(canvas, background=_BG)
    body_window = canvas.create_window((0, 0), window=body, anchor="nw")

    def sync_scroll_region(_event: tk.Event | None = None) -> None:
        canvas.configure(scrollregion=canvas.bbox("all"))

    def sync_body_width(event: tk.Event) -> None:
        canvas.itemconfigure(body_window, width=event.width)

    body.bind("<Configure>", sync_scroll_region)
    canvas.bind("<Configure>", sync_body_width)

    def invoke(index: int) -> None:
        menu.invoke(index)

    def rebuild(*_args: object) -> None:
        for child in body.winfo_children():
            child.destroy()
        needle = query.get().strip().casefold()
        visible = tuple(
            tool for tool in tools if not needle or needle in tool.label.casefold()
        )
        if not visible:
            tk.Label(
                body,
                text="没有匹配的工具",
                background=_BG,
                foreground=_MUTED,
                font=("Segoe UI", 11),
            ).pack(anchor=tk.W, padx=8, pady=18)
            return

        ordered_groups = tuple(dict.fromkeys(tool.group for tool in visible))
        for group in ordered_groups:
            tk.Label(
                body,
                text=group,
                background=_BG,
                foreground=_MUTED,
                font=("Segoe UI Semibold", 10),
            ).pack(anchor=tk.W, padx=4, pady=(12, 6))
            card = tk.Frame(
                body,
                background=_SURFACE,
                highlightbackground=_BORDER,
                highlightthickness=1,
                padx=8,
                pady=8,
            )
            card.pack(fill=tk.X, padx=2)
            card.columnconfigure(0, weight=1, uniform="tool")
            card.columnconfigure(1, weight=1, uniform="tool")
            group_tools = tuple(tool for tool in visible if tool.group == group)
            for offset, tool in enumerate(group_tools):
                button = tk.Button(
                    card,
                    text=tool.label,
                    command=lambda index=tool.menu_index: invoke(index),
                    anchor="w",
                    justify=tk.LEFT,
                    relief=tk.FLAT,
                    borderwidth=0,
                    background=_SURFACE,
                    activebackground="#F1E9DE",
                    foreground=_TEXT,
                    activeforeground=_PRIMARY,
                    cursor="hand2",
                    font=("Segoe UI", 10),
                    padx=12,
                    pady=10,
                )
                button.grid(
                    row=offset // 2,
                    column=offset % 2,
                    sticky="ew",
                    padx=3,
                    pady=2,
                )
        sync_scroll_region()

    query.trace_add("write", rebuild)
    rebuild()
    search.focus_set()


def bind_tool_palette(root: tk.Tk, menu: tk.Menu) -> None:
    """Connect the modern home-screen button to the existing tools menu."""

    root.bind(
        "<<DevCleanOpenTools>>",
        lambda _event: open_tool_palette(root, menu),
        add="+",
    )


__all__ = ["bind_tool_palette", "open_tool_palette"]
