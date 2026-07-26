"""Local application paths.

Everything this program writes lives beside the executable, not in ``AppData``.
A cleanup tool that scatters its own state into a hidden per-user directory
becomes exactly the thing it exists to remove: a file nobody can trace back to
its owner and nobody dares delete.  Beside the exe, deleting the folder deletes
the program and everything it ever wrote, with nothing left behind.

The directory is intentionally local and must never be placed on a network
share.  Tests override it with ``DEVCLEAN_DATA_DIR``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _program_directory() -> Path | None:
    """Return the folder holding the running executable, if this is the exe.

    ``sys.executable`` is the real ``.exe`` path even for a one-file build;
    ``sys._MEIPASS`` is the temporary unpack directory and would be wrong, since
    it disappears when the process exits.
    """

    if not getattr(sys, "frozen", False):
        return None
    try:
        return Path(sys.executable).resolve().parent
    except OSError:
        return None


def data_dir() -> Path:
    """Return DevClean's local data directory without creating it."""

    override = os.environ.get("DEVCLEAN_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()

    beside_exe = _program_directory()
    if beside_exe is not None:
        return beside_exe / "DevClean-data"

    # Running from source: keep it out of the repository but still findable.
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        return Path(local_appdata) / "DevClean"

    return Path.home() / ".local" / "share" / "DevClean"


__all__ = [
    "data_dir",
]

