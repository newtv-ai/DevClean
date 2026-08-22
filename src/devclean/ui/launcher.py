"""Desktop launcher for the single DevClean cleanup workflow.

The product has one scan surface. Application/package/vendor rules are consumed
by the scan engine; users are not expected to open a separate maintenance menu
and inspect every tool one by one.
"""

from __future__ import annotations

import sys
import tkinter as tk
from collections.abc import Sequence

from devclean.ui.product_app import ProductDevCleanWindow


def main(argv: Sequence[str] | None = None) -> int:
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


__all__ = ["main"]
