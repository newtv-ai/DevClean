"""Unreal Engine Derived Data Cache inventory and vendor-owned maintenance.

Unreal's DDC is derived data, but modern local Zen storage can also contain
cooked output. DevClean therefore inventories known DDC/Zen locations but never
recursively deletes them. Maintenance is delegated to Unreal's DDCCleanup
commandlet so the engine and registered cache stores retain ownership of GC.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

_GIB = 1024**3
_RECOMMEND_BYTES = 2 * _GIB


class UnrealStorageKind(StrEnum):
    FILESYSTEM_DDC = "filesystem_ddc"
    ZEN_DATA = "zen_data"
    CONFIGURED_LOCAL = "configured_local"


@dataclass(frozen=True, slots=True)
class UnrealEngineInstall:
    editor_cmd: Path
    engine_root: Path


@dataclass(frozen=True, slots=True)
class UnrealStorageEntry:
    kind: UnrealStorageKind
    path: Path
    logical_bytes: int
    exists: bool
    raw_delete_allowed: bool
    note: str


@dataclass(frozen=True, slots=True)
class UnrealStorageInventory:
    engines: tuple[UnrealEngineInstall, ...]
    stores: tuple[UnrealStorageEntry, ...]

    @property
    def total_known_bytes(self) -> int:
        return sum(entry.logical_bytes for entry in self.stores)

    @property
    def recommended(self) -> bool:
        return self.total_known_bytes >= _RECOMMEND_BYTES and bool(self.engines)


@dataclass(frozen=True, slots=True)
class UnrealDDCCleanupResult:
    editor_cmd: Path
    before_known_bytes: int
    after_known_bytes: int
    command: tuple[str, ...]
    output: str

    @property
    def observed_reclaimed_bytes(self) -> int:
        return max(0, self.before_known_bytes - self.after_known_bytes)


def inventory_unreal_storage(
    environment: Mapping[str, str] | None = None,
) -> UnrealStorageInventory:
    """Inventory known local Unreal DDC/Zen storage without mutating it."""

    env = _casefold_env(environment)
    engines = _discover_engines(environment)
    stores: list[UnrealStorageEntry] = []
    seen: set[str] = set()

    for engine in engines:
        _append_store(
            stores,
            seen,
            UnrealStorageKind.FILESYSTEM_DDC,
            engine.engine_root / "Engine" / "DerivedDataCache",
            note=(
                "Engine-local filesystem DDC; derived data, but mutation stays with "
                "Unreal maintenance rather than raw deletion"
            ),
        )

    localappdata = env.get("localappdata")
    if localappdata:
        _append_store(
            stores,
            seen,
            UnrealStorageKind.ZEN_DATA,
            Path(localappdata) / "UnrealEngine" / "Common" / "Zen" / "Data",
            note=(
                "Zen data can contain local DDC and cooked output; never raw-delete "
                "this directory"
            ),
        )

    zen_override = env.get("ue-zendatapath")
    if zen_override:
        _append_store(
            stores,
            seen,
            UnrealStorageKind.ZEN_DATA,
            Path(zen_override),
            note="Configured Zen data path; may contain DDC and cooked output",
        )

    local_override = env.get("ue-localdatacachepath")
    if local_override and local_override.casefold() != "none":
        _append_store(
            stores,
            seen,
            UnrealStorageKind.CONFIGURED_LOCAL,
            Path(local_override),
            note=(
                "Configured local DDC path; backend type depends on engine/config, "
                "so DevClean grants no raw delete authority"
            ),
        )

    return UnrealStorageInventory(engines=engines, stores=tuple(stores))


def run_unreal_ddc_cleanup(
    editor_cmd: Path,
    environment: Mapping[str, str] | None = None,
) -> UnrealDDCCleanupResult:
    """Run Unreal's DDCCleanup commandlet for one exact discovered engine."""

    inventory = inventory_unreal_storage(environment)
    target = _normalized(editor_cmd)
    selected = next(
        (engine for engine in inventory.engines if _normalized(engine.editor_cmd) == target),
        None,
    )
    if selected is None:
        raise ValueError(f"不是当前已发现的 UnrealEditor-Cmd: {editor_cmd}")
    if unreal_process_running():
        raise RuntimeError("Unreal Editor/构建任务正在运行; 请等待完成后再执行 DDC 维护")

    command = (
        str(selected.editor_cmd),
        "-run=DDCCleanup",
        "-unattended",
        "-NoShaderCompile",
        "-NullRHI",
        "-NoSplash",
        "-stdout",
        "-FullStdOutLogOutput",
    )
    before = inventory.total_known_bytes
    result = _run_unreal(command, environment, timeout=1800)
    after = inventory_unreal_storage(environment).total_known_bytes
    return UnrealDDCCleanupResult(
        editor_cmd=selected.editor_cmd,
        before_known_bytes=before,
        after_known_bytes=after,
        command=command,
        output=(result.stdout or result.stderr).strip(),
    )


def unreal_process_running() -> bool:
    """Fail closed when editor/build processes may be using DDC."""

    if os.name != "nt":
        return False
    script = (
        "$p=Get-Process -ErrorAction SilentlyContinue | Where-Object { "
        "$_.ProcessName -like 'UnrealEditor*' -or "
        "$_.ProcessName -ieq 'ShaderCompileWorker' -or "
        "$_.ProcessName -ieq 'UnrealBuildTool' -or "
        "$_.ProcessName -ieq 'AutomationTool' }; "
        "if ($p) { 'RUNNING' }"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=6,
        )
    except (OSError, subprocess.SubprocessError):
        return True
    return result.returncode != 0 or "RUNNING" in result.stdout


def _discover_engines(
    environment: Mapping[str, str] | None,
) -> tuple[UnrealEngineInstall, ...]:
    env = _casefold_env(environment)
    candidates: list[Path] = []

    explicit_editor = env.get("devclean_unreal_editor_cmd")
    if explicit_editor:
        candidates.append(Path(explicit_editor))

    explicit_roots = env.get("devclean_unreal_engine_roots")
    if explicit_roots:
        for value in explicit_roots.split(os.pathsep):
            value = value.strip().strip('"').strip("'")
            if value:
                candidates.append(_editor_for_engine_root(Path(value)))

    programfiles = env.get("programfiles")
    if programfiles:
        epic = Path(programfiles) / "Epic Games"
        try:
            roots = tuple(epic.glob("UE_*"))
        except OSError:
            roots = ()
        for root in roots:
            candidates.append(_editor_for_engine_root(root))

    if environment is None:
        located = shutil.which(
            "UnrealEditor-Cmd.exe" if os.name == "nt" else "UnrealEditor-Cmd"
        )
        if located:
            candidates.append(Path(located))

    found: list[UnrealEngineInstall] = []
    seen: set[str] = set()
    for editor in candidates:
        try:
            editor = editor.expanduser().resolve(strict=False)
        except OSError:
            continue
        if not editor.is_absolute() or not editor.is_file():
            continue
        engine_root = _engine_root_for_editor(editor)
        if engine_root is None:
            continue
        key = _normalized(editor)
        if key in seen:
            continue
        seen.add(key)
        found.append(UnrealEngineInstall(editor_cmd=editor, engine_root=engine_root))
    return tuple(sorted(found, key=lambda item: str(item.editor_cmd).casefold()))


def _editor_for_engine_root(root: Path) -> Path:
    name = "UnrealEditor-Cmd.exe" if os.name == "nt" else "UnrealEditor-Cmd"
    return root / "Engine" / "Binaries" / "Win64" / name


def _engine_root_for_editor(editor: Path) -> Path | None:
    if len(editor.parts) < 5 or editor.name.casefold() not in {
        "unrealeditor-cmd.exe",
        "unrealeditor-cmd",
    }:
        return None
    if editor.parent.name.casefold() != "win64":
        return None
    if editor.parent.parent.name.casefold() != "binaries":
        return None
    if editor.parent.parent.parent.name.casefold() != "engine":
        return None
    return editor.parent.parent.parent.parent


def _append_store(
    stores: list[UnrealStorageEntry],
    seen: set[str],
    kind: UnrealStorageKind,
    path: Path,
    *,
    note: str,
) -> None:
    try:
        path = path.expanduser().resolve(strict=False)
    except OSError:
        return
    if not path.is_absolute():
        return
    key = _normalized(path)
    if not key or key in seen:
        return
    seen.add(key)
    try:
        exists = path.is_dir()
    except OSError:
        exists = False
    stores.append(
        UnrealStorageEntry(
            kind=kind,
            path=path,
            logical_bytes=_directory_bytes(path) if exists else 0,
            exists=exists,
            raw_delete_allowed=False,
            note=note,
        )
    )


def _run_unreal(
    command: tuple[str, ...],
    environment: Mapping[str, str] | None,
    *,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    if environment is not None:
        env.update(environment)
    try:
        result = subprocess.run(
            list(command),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"无法执行 UnrealEditor-Cmd: {error}") from error
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(
            f"Unreal DDCCleanup 失败 (退出码 {result.returncode}): {detail}"
        )
    return result


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


def _normalized(path: Path) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def _casefold_env(environment: Mapping[str, str] | None) -> dict[str, str]:
    source = os.environ if environment is None else environment
    return {key.casefold(): value for key, value in source.items() if value}


__all__ = [
    "UnrealDDCCleanupResult",
    "UnrealEngineInstall",
    "UnrealStorageEntry",
    "UnrealStorageInventory",
    "UnrealStorageKind",
    "inventory_unreal_storage",
    "run_unreal_ddc_cleanup",
    "unreal_process_running",
]
