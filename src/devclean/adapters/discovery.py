"""Fail-closed executable, version, and local-root discovery helpers."""

from __future__ import annotations

import os
import re
import shutil
import unicodedata
from pathlib import Path

from devclean.platform.windows.volumes import is_local_fixed_path

VersionTuple = tuple[int, int, int]
_VERSION = re.compile(r"(?<![0-9])([0-9]+)\.([0-9]+)(?:\.([0-9]+))?(?![0-9])")


def resolve_executable(
    name: str, *, allowed_suffixes: tuple[str, ...] = (".exe",)
) -> Path | None:
    """Resolve a PATH entry once and reject wrappers or redirected/network files."""

    found = shutil.which(name)
    if found is None:
        return None
    try:
        executable = Path(found).resolve(strict=True)
    except OSError:
        return None
    if executable.suffix.lower() not in {suffix.lower() for suffix in allowed_suffixes}:
        return None
    if not executable.is_file() or not is_local_fixed_path(executable):
        return None
    return executable


def parse_version(text: str) -> VersionTuple | None:
    match = _VERSION.search(text.strip())
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3) or 0))


def format_version(version: VersionTuple) -> str:
    return ".".join(str(item) for item in version)


def parse_single_local_path(text: str) -> Path:
    """Parse one absolute fixed-volume path from deterministic UTF-8 output."""

    value = text.strip()
    if not value or "\n" in value or "\r" in value:
        raise ValueError("expected exactly one path line")
    if any(
        ord(character) < 0x20
        or ord(character) == 0x7F
        or unicodedata.category(character) in {"Cf", "Cs"}
        for character in value
    ):
        raise ValueError("path output contains control or format characters")
    path = Path(os.path.expanduser(value))
    if not path.is_absolute() or str(path).startswith((r"\\", "//")):
        raise ValueError("path output is not an absolute local path")
    if not is_local_fixed_path(path):
        raise ValueError("path output is not on an approved fixed local volume")
    return path


__all__ = [
    "VersionTuple",
    "format_version",
    "parse_single_local_path",
    "parse_version",
    "resolve_executable",
]
