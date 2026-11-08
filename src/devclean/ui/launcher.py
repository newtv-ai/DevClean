"""Desktop launcher for the single DevClean cleanup workflow.

The product has one scan surface. Application/package/vendor rules are consumed
by the scan engine; users are not expected to open a separate maintenance menu
and inspect every tool one by one.
"""

from __future__ import annotations

import sys
import tkinter as tk
from collections.abc import Sequence

from devclean.platform.windows.subprocess_policy import install_no_console_subprocess_policy
from devclean.ui.product_vendor_app import ProductDevCleanWindow


def main(argv: Sequence[str] | None = None) -> int:
    # Idempotent; the packaged entry point already installed it. A source run
    # of the launcher gets the same absolute-path rule for child programs.
    install_no_console_subprocess_policy()
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    if arguments == ("--smoke",):
        return 0
    if arguments == ("--ui-smoke",):
        root = tk.Tk()
        root.withdraw()
        ProductDevCleanWindow(root)
        root.update_idletasks()
        root.destroy()
        return 0

    root = tk.Tk()
    ProductDevCleanWindow(root)
    root.mainloop()
    return 0


__all__ = ["ProductDevCleanWindow", "main"]
