"""Small JSON editor for DevClean's three public rule files."""

# Chinese UI prose uses fullwidth punctuation.
# ruff: noqa: RUF001

from __future__ import annotations

import os
import tkinter as tk
from collections.abc import Callable
from tkinter import messagebox, ttk

from devclean.core.user_rules import (
    DELETE_RULES_NAME,
    KEEP_RULES_NAME,
    SCAN_RULES_NAME,
    RuleConfigError,
    UserRules,
    default_backup_path,
    parse_rule_documents,
    read_rule_documents,
    render_rule_documents,
    restore_default_rules,
    rules_dir,
    save_rules,
)


class RuleEditor:
    def __init__(
        self,
        parent: tk.Tk | tk.Toplevel,
        rules: UserRules,
        on_saved: Callable[[UserRules], None],
        raw_documents: tuple[str, str, str] | None = None,
    ) -> None:
        self._on_saved = on_saved
        self._window = tk.Toplevel(parent)
        self._window.title("DevClean 规则设置")
        self._window.geometry("900x650")
        self._window.minsize(700, 480)
        self._window.transient(parent)

        hint = (
            "DELETE/KEEP 的 rules 保存后立即应用；扫描范围、阈值和分类表"
            "在下次扫描时生效。"
            "保留规则优先于删除规则。match 支持 exact_path、path_prefix、"
            "path_glob、filename_glob、path_regex、filename_regex。"
            "正则忽略大小写，path_regex 中路径分隔符写成 /。"
            "group 和 classification_groups 用于人工分组。"
            "文件内的 _ai_editing_contract 是给 AI 的统一格式约束。"
        )
        ttk.Label(
            self._window,
            text=hint,
            wraplength=850,
            padding=(12, 10),
        ).pack(fill=tk.X)

        notebook = ttk.Notebook(self._window)
        notebook.pack(fill=tk.BOTH, expand=True, padx=12)
        self._editors: list[tk.Text] = []
        for name in (SCAN_RULES_NAME, DELETE_RULES_NAME, KEEP_RULES_NAME):
            frame = ttk.Frame(notebook)
            notebook.add(frame, text=name)
            frame.rowconfigure(0, weight=1)
            frame.columnconfigure(0, weight=1)
            editor = tk.Text(
                frame,
                wrap=tk.NONE,
                undo=True,
                font=("Consolas", 10),
                padx=8,
                pady=8,
            )
            editor.grid(row=0, column=0, sticky="nsew")
            vertical = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=editor.yview)
            horizontal = ttk.Scrollbar(frame, orient=tk.HORIZONTAL, command=editor.xview)
            editor.configure(
                yscrollcommand=vertical.set,
                xscrollcommand=horizontal.set,
            )
            vertical.grid(row=0, column=1, sticky="ns")
            horizontal.grid(row=1, column=0, sticky="ew")
            self._editors.append(editor)

        actions = ttk.Frame(self._window, padding=12)
        actions.pack(fill=tk.X)
        ttk.Button(actions, text="保存并应用", command=self._save).pack(side=tk.RIGHT)
        ttk.Button(actions, text="从磁盘重新载入", command=self._reload).pack(
            side=tk.RIGHT, padx=(0, 8)
        )
        ttk.Button(actions, text="打开规则文件夹", command=self._open_folder).pack(side=tk.LEFT)
        ttk.Button(
            actions,
            text="恢复默认配置",
            command=self._restore_defaults,
        ).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(actions, text="关闭", command=self._window.destroy).pack(
            side=tk.LEFT, padx=(8, 0)
        )
        if raw_documents is None:
            self._fill(rules)
        else:
            self._fill_texts(raw_documents)

    def _fill(self, rules: UserRules) -> None:
        self._fill_texts(render_rule_documents(rules))

    def _fill_texts(self, documents: tuple[str, str, str]) -> None:
        for editor, rendered in zip(self._editors, documents, strict=True):
            editor.delete("1.0", tk.END)
            editor.insert("1.0", rendered)
            editor.edit_modified(False)

    def _save(self) -> None:
        try:
            scan_text, delete_text, keep_text = (
                editor.get("1.0", "end-1c") for editor in self._editors
            )
            rules = parse_rule_documents(scan_text, delete_text, keep_text)
            save_rules(rules)
        except (OSError, RuleConfigError, UnicodeError) as error:
            messagebox.showerror("规则没有保存", str(error), parent=self._window)
            return
        self._fill(rules)
        self._on_saved(rules)
        messagebox.showinfo(
            "DevClean",
            "路径规则已应用；扫描范围、阈值和分类表在下次扫描时生效。",
            parent=self._window,
        )

    def _reload(self) -> None:
        try:
            documents = read_rule_documents(errors="replace")
        except (OSError, RuleConfigError, UnicodeError) as error:
            messagebox.showerror("规则载入失败", str(error), parent=self._window)
            return
        self._fill_texts(documents)
        try:
            parse_rule_documents(*documents)
        except RuleConfigError as error:
            messagebox.showerror(
                "规则内容有误",
                f"{error}\n\n磁盘原文已经载入，可以直接在这里修正。",
                parent=self._window,
            )

    def _restore_defaults(self) -> None:
        if not messagebox.askyesno(
            "恢复默认配置",
            "这会用随程序提供的默认版本替换当前三份规则。继续吗？",
            parent=self._window,
        ):
            return
        try:
            rules = restore_default_rules()
        except (OSError, RuleConfigError, UnicodeError) as error:
            messagebox.showerror("恢复失败", str(error), parent=self._window)
            return
        self._fill(rules)
        self._on_saved(rules)
        messagebox.showinfo(
            "默认配置已恢复",
            f"三份规则已恢复。\n默认备份档：{default_backup_path()}",
            parent=self._window,
        )

    def _open_folder(self) -> None:
        try:
            rules_dir().mkdir(parents=True, exist_ok=True)
            os.startfile(str(rules_dir()))
        except (AttributeError, OSError) as error:
            messagebox.showerror("无法打开规则文件夹", str(error), parent=self._window)


def open_rule_editor(
    parent: tk.Tk | tk.Toplevel,
    rules: UserRules,
    on_saved: Callable[[UserRules], None],
    raw_documents: tuple[str, str, str] | None = None,
) -> RuleEditor:
    return RuleEditor(parent, rules, on_saved, raw_documents)


__all__ = ["RuleEditor", "open_rule_editor"]
