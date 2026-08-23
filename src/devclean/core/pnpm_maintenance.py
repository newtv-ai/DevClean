"""Read-only pnpm inventory plus source-bounded vendor store garbage collection.

Current ``pnpm store prune`` does more than store GC: it also expires DLX cache
entries and removes orphaned global install directories. DevClean therefore pins
those secondary pnpm roots to an isolated temporary sandbox and verifies that
the selected pnpm binary accepts the pinned configuration before invoking the
vendor prune. The only user-side mutation scope left to the command is the exact
reviewed store root.
"""

from __future__ import annotations

import math
import os
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from devclean.core import _application_cleanup_impl as _impl
from devclean.core.pnpm_cleanup import (
    clear_pnpm_process_cache,
    pnpm_process_running,
    pnpm_roots,
)
from devclean.platform.windows.filesystem import read_file_metadata
from devclean.platform.windows.volumes import is_local_fixed_path

_GIB = 1024**3
_RECOMMEND_BYTES = _GIB
_PINNED_CONFIG_KEYS = frozenset(
    {
        "pnpm_config_cache_dir",
        "pnpm_config_global_dir",
        "pnpm_config_dlx_cache_max_age",
    }
)


@dataclass(frozen=True, slots=True)
class PnpmStoreEntry:
    path: Path
    logical_bytes: int
    exists: bool
    recommended: bool


@dataclass(frozen=True, slots=True)
class PnpmStorageInventory:
    stores: tuple[PnpmStoreEntry, ...]

    @property
    def total_store_bytes(self) -> int:
        return sum(entry.logical_bytes for entry in self.stores)

    @property
    def recommended_bytes(self) -> int:
        return sum(entry.logical_bytes for entry in self.stores if entry.recommended)


@dataclass(frozen=True, slots=True)
class PnpmPruneResult:
    store_path: Path
    before_bytes: int
    after_bytes: int
    command: tuple[str, ...]
    output: str

    @property
    def reclaimed_bytes(self) -> int:
        return max(0, self.before_bytes - self.after_bytes)


def inventory_pnpm_storage(
    environment: Mapping[str, str] | None = None,
) -> PnpmStorageInventory:
    """Inventory every discovered pnpm store without modifying it."""

    entries: list[PnpmStoreEntry] = []
    seen: set[str] = set()
    for raw in pnpm_roots(environment).store_roots:
        path = _store_config_root(Path(str(raw)))
        key = _impl._normalize(path)
        if not key or key in seen:
            continue
        seen.add(key)
        try:
            exists = path.is_dir()
        except OSError:
            exists = False
        size = _directory_bytes(path) if exists else 0
        entries.append(
            PnpmStoreEntry(
                path=path,
                logical_bytes=size,
                exists=exists,
                recommended=size >= _RECOMMEND_BYTES,
            )
        )
    return PnpmStorageInventory(tuple(entries))


def prune_pnpm_store(
    store_path: Path,
    environment: Mapping[str, str] | None = None,
) -> PnpmPruneResult:
    """Run pnpm store GC while sandboxing its current secondary cleanup scopes."""

    clear_pnpm_process_cache()
    root = _store_config_root(store_path)
    target = _impl._normalize(root)
    audited = {
        _impl._normalize(_store_config_root(Path(str(raw))))
        for raw in pnpm_roots(environment).store_roots
    }
    if not target or target not in audited:
        raise ValueError(f"不是当前已审计的 pnpm store 根目录: {root}")
    if not root.is_dir():
        raise FileNotFoundError(f"pnpm store 不存在: {root}")

    reviewed_identity = _store_identity(root)
    _require_process_idle()
    executable = _pnpm_executable(environment)
    scope = (executable, "--store-dir", str(root))

    with tempfile.TemporaryDirectory(prefix="devclean-pnpm-prune-") as temporary:
        sandbox = Path(temporary)
        pinned = _pinned_prune_environment(environment, sandbox)
        _confirm_prune_sandbox(executable, pinned, sandbox)
        _confirm_store_path(scope, pinned, target)

        # Revalidate immediately before the mutation. The command below is fixed
        # and cannot broaden to the real cache/global roots because the same
        # verified pinned environment is reused unchanged.
        _require_process_idle()
        if _store_identity(root) != reviewed_identity:
            raise RuntimeError("pnpm store 身份在执行前发生变化; 请重新检查")
        _confirm_store_path(scope, pinned, target)

        before = _directory_bytes(root)
        command = (*scope, "store", "prune")
        result = _run_pnpm(command, pinned, timeout=600)
        output = _combined_output(result.stdout, result.stderr)
        if result.returncode != 0:
            raise RuntimeError(
                "pnpm store prune 失败 "
                f"(退出码 {result.returncode}): {output or 'no output'}"
            )

        if _store_identity(root) != reviewed_identity:
            raise RuntimeError("pnpm store 根目录身份在 prune 后发生变化; 不报告成功")
        after = _directory_bytes(root)
        return PnpmPruneResult(
            store_path=root,
            before_bytes=before,
            after_bytes=after,
            command=command,
            output=output,
        )


def _pinned_prune_environment(
    environment: Mapping[str, str] | None,
    sandbox: Path,
) -> dict[str, str]:
    env = dict(os.environ)
    if environment is not None:
        env.update(environment)
    for key in tuple(env):
        if key.casefold() in _PINNED_CONFIG_KEYS:
            del env[key]

    # Current pnpm store prune calls cleanExpiredDlxCache and may clean the
    # derived globalPkgDir. Infinity disables DLX expiry; pinning both roots to
    # a private sandbox makes those vendor side effects harmless even if pnpm's
    # internal call order changes within the audited contract.
    env["PNPM_CONFIG_CACHE_DIR"] = str(sandbox / "cache")
    env["PNPM_CONFIG_GLOBAL_DIR"] = str(sandbox / "global")
    env["PNPM_CONFIG_DLX_CACHE_MAX_AGE"] = "Infinity"
    return env


def _confirm_prune_sandbox(
    executable: str,
    environment: Mapping[str, str],
    sandbox: Path,
) -> None:
    expected_paths = {
        "cache-dir": sandbox / "cache",
        "global-dir": sandbox / "global",
    }
    for key, expected in expected_paths.items():
        value = _pnpm_config_get(executable, key, environment)
        if _impl._normalize(value) != _impl._normalize(expected):
            raise RuntimeError(f"pnpm 未确认隔离的 {key}; 已安全停止")

    raw_age = _pnpm_config_get(executable, "dlx-cache-max-age", environment)
    try:
        age = float(raw_age)
    except ValueError as error:
        raise RuntimeError("pnpm 未确认禁用 DLX expiry; 已安全停止") from error
    if not math.isinf(age) or age < 0:
        raise RuntimeError("pnpm 未确认禁用 DLX expiry; 已安全停止")


def _pnpm_config_get(
    executable: str,
    key: str,
    environment: Mapping[str, str],
) -> str:
    result = _run_pnpm(
        (executable, "config", "get", key),
        environment,
        timeout=30,
    )
    output = _combined_output(result.stdout, result.stderr)
    if result.returncode != 0:
        raise RuntimeError(
            f"pnpm config get {key} 失败 "
            f"(退出码 {result.returncode}): {output or 'no output'}"
        )
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError(f"pnpm config get {key} 没有返回值; 已安全停止")
    return lines[-1].strip().strip('"').strip("'")


def _confirm_store_path(
    scope: tuple[str, ...],
    environment: Mapping[str, str],
    target: str,
) -> None:
    command = (*scope, "store", "path", "--silent")
    active = _run_pnpm(command, environment, timeout=60)
    if active.returncode != 0:
        detail = _combined_output(active.stdout, active.stderr)
        raise RuntimeError(
            f"pnpm store path 失败 (退出码 {active.returncode}): "
            f"{detail or 'no output'}"
        )
    active_root = _parse_store_path(active.stdout)
    if active_root is None or _impl._normalize(active_root) != target:
        raise RuntimeError("pnpm 未确认所选 store 路径; 已安全停止")


def _store_identity(root: Path) -> tuple[int, str, str]:
    if not is_local_fixed_path(root):
        raise RuntimeError("pnpm store 不是普通本地固定磁盘路径; 已安全停止")
    try:
        metadata = read_file_metadata(root)
    except OSError as error:
        raise RuntimeError(f"无法读取 pnpm store 身份: {error}") from error
    if not metadata.is_directory:
        raise RuntimeError("pnpm store 不再是目录; 已安全停止")
    if metadata.is_reparse_point or metadata.is_cloud_placeholder:
        raise RuntimeError("pnpm store 是 reparse/cloud 边界; 已安全停止")
    if (
        metadata.volume_serial is None
        or metadata.file_id is None
        or metadata.file_id_kind is None
    ):
        raise RuntimeError("pnpm store 缺少稳定文件系统身份; 已安全停止")
    return (metadata.volume_serial, metadata.file_id, metadata.file_id_kind)


def _require_process_idle() -> None:
    clear_pnpm_process_cache()
    if pnpm_process_running():
        raise RuntimeError("pnpm 正在运行; 请等待当前 pnpm 操作完成后再清理 store")


def _pnpm_executable(environment: Mapping[str, str] | None) -> str:
    env = _casefold_env(environment)
    configured = env.get("devclean_pnpm_exe")
    if configured:
        return configured
    return "pnpm.cmd" if os.name == "nt" else "pnpm"


def _run_pnpm(
    command: tuple[str, ...],
    environment: Mapping[str, str] | None,
    *,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    if environment is not None:
        env.update(environment)
    try:
        return subprocess.run(
            list(command),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"无法执行 pnpm: {error}") from error


def _parse_store_path(stdout: str | None) -> Path | None:
    lines = [line.strip() for line in (stdout or "").splitlines() if line.strip()]
    if not lines:
        return None
    raw = lines[-1].strip().strip('"').strip("'")
    candidate = PureWindowsPath(raw)
    if candidate.is_absolute():
        return _store_config_root(Path(str(candidate)))
    native = Path(raw)
    if native.is_absolute():
        return _store_config_root(Path(os.path.abspath(native)))
    return None


def _store_config_root(path: Path) -> Path:
    """Convert a versioned store path like store/v10 back to store-dir."""

    name = path.name.casefold()
    if len(name) > 1 and name.startswith("v") and name[1:].isdigit():
        return path.parent
    return path


def _directory_bytes(root: Path) -> int:
    total = 0
    try:
        for directory, _subdirs, files in os.walk(root):
            base = Path(directory)
            for name in files:
                try:
                    total += (base / name).stat().st_size
                except OSError:
                    continue
    except OSError:
        return total
    return total


def _combined_output(stdout: str | None, stderr: str | None) -> str:
    return "\n".join(
        chunk.strip() for chunk in (stdout, stderr) if chunk and chunk.strip()
    )


def _casefold_env(environment: Mapping[str, str] | None) -> dict[str, str]:
    source = os.environ if environment is None else environment
    return {key.casefold(): value for key, value in source.items() if value}


__all__ = [
    "PnpmPruneResult",
    "PnpmStorageInventory",
    "PnpmStoreEntry",
    "inventory_pnpm_storage",
    "prune_pnpm_store",
]
