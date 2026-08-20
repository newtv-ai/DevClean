"""Read-only PyTorch Hub storage inventory with no deletion authority."""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from devclean.platform.windows.filesystem import (
    FILE_ATTRIBUTE_OFFLINE,
    FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS,
    FILE_ATTRIBUTE_RECALL_ON_OPEN,
    FILE_ATTRIBUTE_REPARSE_POINT,
)

_MAX_SCAN_ENTRIES = 200_000
_BOUNDARY_ATTRIBUTES = (
    FILE_ATTRIBUTE_REPARSE_POINT
    | FILE_ATTRIBUTE_OFFLINE
    | FILE_ATTRIBUTE_RECALL_ON_OPEN
    | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
)


class TorchHubEntryKind(StrEnum):
    CHECKPOINTS = "checkpoints"
    TRUST_STATE = "trust_state"
    REPOSITORY_OR_UNKNOWN = "repository_or_unknown"
    DOWNLOAD_TEMP_OR_UNKNOWN = "download_temp_or_unknown"
    OTHER = "other"


class TorchHubDecision(StrEnum):
    REPORT_ONLY = "REPORT_ONLY"
    KEEP_PROTECTED = "KEEP_PROTECTED"


@dataclass(frozen=True, slots=True)
class TorchHubRootCandidate:
    path: Path
    source: str
    note: str


@dataclass(frozen=True, slots=True)
class TorchHubEntry:
    path: Path
    name: str
    kind: TorchHubEntryKind
    decision: TorchHubDecision
    logical_bytes: int
    file_count: int
    boundary_skipped: bool
    reason: str


@dataclass(frozen=True, slots=True)
class TorchHubInventory:
    root: Path
    exists: bool
    scannable: bool
    entries: tuple[TorchHubEntry, ...]
    warnings: tuple[str, ...]

    @property
    def total_logical_bytes(self) -> int:
        return sum(item.logical_bytes for item in self.entries)

    @property
    def total_file_count(self) -> int:
        return sum(item.file_count for item in self.entries)


@dataclass(slots=True)
class _ScanBudget:
    entries_seen: int = 0
    exhausted: bool = False


def default_torch_hub_root(
    environment: Mapping[str, str] | None = None,
) -> TorchHubRootCandidate:
    """Return PyTorch's environment/default Hub candidate without importing torch.

    ``torch.hub.set_dir()`` is process-local runtime state and cannot be observed by
    another process. The UI therefore labels this as a candidate and lets the user
    explicitly select a custom Hub root for read-only inspection.
    """

    env = _casefold_env(environment)
    override = env.get("devclean_torch_hub_root")
    if override:
        return TorchHubRootCandidate(
            _expand_user_path(override, env),
            "DEVCLEAN_TORCH_HUB_ROOT",
            "DevClean 显式只读检查根目录。",
        )

    torch_home = env.get("torch_home")
    if torch_home:
        return TorchHubRootCandidate(
            _expand_user_path(torch_home, env) / "hub",
            "TORCH_HOME",
            "PyTorch 默认使用 TORCH_HOME/hub；运行中的 Python 仍可能通过 torch.hub.set_dir() 改写。",
        )

    xdg_cache_home = env.get("xdg_cache_home")
    if xdg_cache_home:
        return TorchHubRootCandidate(
            _expand_user_path(xdg_cache_home, env) / "torch" / "hub",
            "XDG_CACHE_HOME",
            "PyTorch 默认使用 XDG_CACHE_HOME/torch/hub；运行时 set_dir() 仍可能改写。",
        )

    return TorchHubRootCandidate(
        _expand_user_path("~/.cache", env) / "torch" / "hub",
        "default",
        "PyTorch 源码默认 ~/.cache/torch/hub；运行时 torch.hub.set_dir() 无法由 DevClean 外部推断。",
    )


def inventory_torch_hub(root: str | os.PathLike[str]) -> TorchHubInventory:
    """Inspect one explicitly resolved Hub root without following reparse boundaries."""

    path = Path(root).expanduser()
    if not path.is_absolute():
        raise ValueError("Torch Hub 检查根目录必须是绝对路径")

    warnings: list[str] = []
    try:
        root_stat = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return TorchHubInventory(path, False, False, (), ("Torch Hub 根目录不存在。",))
    except OSError as error:
        raise RuntimeError(f"无法读取 Torch Hub 根目录: {error}") from error

    if not stat.S_ISDIR(root_stat.st_mode):
        raise ValueError("Torch Hub 检查根目录不是目录")
    if _is_boundary_stat(root_stat):
        return TorchHubInventory(
            path,
            True,
            False,
            (),
            ("Torch Hub 根目录本身是 symlink/junction/reparse/cloud 边界；只读扫描拒绝穿越。",),
        )

    budget = _ScanBudget()
    entries: list[TorchHubEntry] = []
    try:
        children = sorted(path.iterdir(), key=lambda item: item.name.casefold())
    except OSError as error:
        raise RuntimeError(f"无法枚举 Torch Hub 根目录: {error}") from error

    for child in children:
        if budget.exhausted:
            break
        entry = _inspect_top_level(child, budget, warnings)
        if entry is not None:
            entries.append(entry)

    if budget.exhausted:
        warnings.append(
            f"扫描达到 {_MAX_SCAN_ENTRIES} 个文件系统对象上限；结果仅用于说明，不代表完整清单。"
        )

    return TorchHubInventory(path, True, True, tuple(entries), tuple(dict.fromkeys(warnings)))


def _inspect_top_level(
    path: Path,
    budget: _ScanBudget,
    warnings: list[str],
) -> TorchHubEntry | None:
    try:
        result = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        warnings.append(f"扫描期间对象消失: {path}")
        return None
    except OSError as error:
        warnings.append(f"无法读取 {path}: {error}")
        return None

    boundary = _is_boundary_stat(result)
    if boundary:
        warnings.append(f"跳过 reparse/symlink/cloud 边界: {path}")
        logical_bytes = 0
        file_count = 0
    else:
        logical_bytes, file_count = _measure_path(path, result, budget, warnings)

    name = path.name
    lowered = name.casefold()
    is_directory = stat.S_ISDIR(result.st_mode)

    if lowered == "trusted_list" and not is_directory:
        return TorchHubEntry(
            path,
            name,
            TorchHubEntryKind.TRUST_STATE,
            TorchHubDecision.KEEP_PROTECTED,
            logical_bytes,
            file_count,
            boundary,
            "PyTorch 用 trusted_list 保存信任状态；它不是下载缓存，DevClean 不修改。",
        )
    if lowered == "checkpoints" and is_directory:
        return TorchHubEntry(
            path,
            name,
            TorchHubEntryKind.CHECKPOINTS,
            TorchHubDecision.REPORT_ONLY,
            logical_bytes,
            file_count,
            boundary,
            "load_state_dict_from_url 把权重放在 checkpoints，但没有 URL/来源清单且允许自定义 file_name；无法证明任一文件可安全重建。",
        )
    if is_directory:
        return TorchHubEntry(
            path,
            name,
            TorchHubEntryKind.REPOSITORY_OR_UNKNOWN,
            TorchHubDecision.REPORT_ONLY,
            logical_bytes,
            file_count,
            boundary,
            "Torch Hub 的 repo 目录名由 owner/repo/ref 下划线拼接且 ref 的斜杠也改成下划线；名称不可可靠反解，目录也可能是用户自建内容。",
        )
    if lowered.endswith(".zip"):
        return TorchHubEntry(
            path,
            name,
            TorchHubEntryKind.DOWNLOAD_TEMP_OR_UNKNOWN,
            TorchHubDecision.REPORT_ONLY,
            logical_bytes,
            file_count,
            boundary,
            "Torch Hub 下载 repo 时会短暂使用顶层 <normalized-ref>.zip，但没有持久 provenance；遗留 zip 不能仅凭后缀自动删除。",
        )
    return TorchHubEntry(
        path,
        name,
        TorchHubEntryKind.OTHER,
        TorchHubDecision.REPORT_ONLY,
        logical_bytes,
        file_count,
        boundary,
        "当前 PyTorch Hub 公共契约没有为该顶层对象提供可验证的清理生命周期。",
    )


def _measure_path(
    path: Path,
    initial_stat: os.stat_result,
    budget: _ScanBudget,
    warnings: list[str],
) -> tuple[int, int]:
    stack: list[tuple[Path, os.stat_result]] = [(path, initial_stat)]
    logical_bytes = 0
    file_count = 0

    while stack:
        current, current_stat = stack.pop()
        budget.entries_seen += 1
        if budget.entries_seen > _MAX_SCAN_ENTRIES:
            budget.exhausted = True
            break
        if _is_boundary_stat(current_stat):
            warnings.append(f"跳过 reparse/symlink/cloud 边界: {current}")
            continue
        if stat.S_ISREG(current_stat.st_mode):
            logical_bytes += max(0, int(current_stat.st_size))
            file_count += 1
            continue
        if not stat.S_ISDIR(current_stat.st_mode):
            continue
        try:
            children = list(current.iterdir())
        except OSError as error:
            warnings.append(f"无法枚举 {current}: {error}")
            continue
        for child in children:
            try:
                child_stat = os.stat(child, follow_symlinks=False)
            except FileNotFoundError:
                warnings.append(f"扫描期间对象消失: {child}")
            except OSError as error:
                warnings.append(f"无法读取 {child}: {error}")
            else:
                stack.append((child, child_stat))

    return logical_bytes, file_count


def _is_boundary_stat(result: os.stat_result) -> bool:
    if stat.S_ISLNK(result.st_mode):
        return True
    attributes = int(getattr(result, "st_file_attributes", 0) or 0)
    return bool(attributes & _BOUNDARY_ATTRIBUTES)


def _expand_user_path(value: str, env: Mapping[str, str]) -> Path:
    if value == "~" or value.startswith(("~/", "~\\")):
        home = env.get("userprofile") or env.get("home")
        if home:
            suffix = value[2:] if len(value) > 1 else ""
            return Path(home) / suffix if suffix else Path(home)
    return Path(os.path.expanduser(value))


def _casefold_env(environment: Mapping[str, str] | None) -> dict[str, str]:
    source = os.environ if environment is None else environment
    return {str(key).casefold(): str(value) for key, value in source.items() if value}


__all__ = [
    "TorchHubDecision",
    "TorchHubEntry",
    "TorchHubEntryKind",
    "TorchHubInventory",
    "TorchHubRootCandidate",
    "default_torch_hub_root",
    "inventory_torch_hub",
]
