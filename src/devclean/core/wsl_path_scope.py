"""Conservative filesystem-scope proof for destructive WSL maintenance."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath

from devclean.core.wsl_exec import run_wsl_exec


@dataclass(frozen=True, slots=True)
class WslRootFilesystemProof:
    distribution: str
    path: str
    root_device: int
    target_device: int


def require_wsl_root_filesystem_path(
    distribution: str,
    path: str,
    environment: Mapping[str, str] | None = None,
) -> WslRootFilesystemProof:
    """Require an existing path to reside on the distro's `/` filesystem.

    WSL tool caches can be redirected to mounted Windows, network, removable,
    or otherwise externally managed filesystems. DevClean is a local storage
    workbench, so an exact vendor path is not by itself sufficient mutation
    authority. The first WSL mutation lanes deliberately accept only paths that
    share the POSIX device identity of the selected distribution's root
    filesystem. Other mounts remain visible but non-executable.
    """

    normalized = _validated_path(path)
    root_device = _stat_device(distribution, "/", environment)
    target_device = _stat_device(distribution, normalized, environment)
    if target_device != root_device:
        raise RuntimeError(
            "WSL target is on a mounted filesystem outside the distribution root; "
            "DevClean keeps this target report-only"
        )
    return WslRootFilesystemProof(
        distribution=distribution,
        path=normalized,
        root_device=root_device,
        target_device=target_device,
    )


def _validated_path(path: str) -> str:
    value = path.strip()
    if not value or "\x00" in value:
        raise ValueError("WSL path is invalid")
    candidate = PurePosixPath(value)
    if not candidate.is_absolute() or str(candidate) == "/":
        raise ValueError("WSL mutation path must be an absolute non-root POSIX path")
    return str(candidate)


def _stat_device(
    distribution: str,
    path: str,
    environment: Mapping[str, str] | None,
) -> int:
    try:
        result = run_wsl_exec(
            distribution,
            "stat",
            ("-L", "-c", "%d", "--", path),
            environment,
            timeout=30,
        )
    except RuntimeError as error:
        raise RuntimeError(
            "Cannot prove WSL filesystem scope with stat; mutation stopped safely"
        ) from error
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        raise RuntimeError("WSL stat did not return one device identity")
    try:
        value = int(lines[0], 10)
    except ValueError as error:
        raise RuntimeError("WSL stat returned an invalid device identity") from error
    if value < 0:
        raise RuntimeError("WSL stat returned an invalid device identity")
    return value


__all__ = ["WslRootFilesystemProof", "require_wsl_root_filesystem_path"]
