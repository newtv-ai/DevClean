"""Programs and services using a path right now, checked just before deletion.

A scan can be old by the time the user cleans: a program may have started, or
a service may have been installed, that now keeps its data where the scan saw
only leftovers. Before deleting, DevClean therefore reads the running
processes (one query) and the registered services (registry) once per cleanup
run, and refuses a target that one of them runs from or names in its command
line, environment or parameters.

Windows hides the command line of some processes (elevated programs, other
users' programs, protected processes). Such a process is judged by the
evidence that remains, target by target, instead of blocking everything:

* it is accounted for when trusted evidence says what it is: its program lies
  in the Windows folder and is not a host that runs other programs' scripts,
  its program is a registered service's program, or it runs in session 0
  (where only Windows and services start programs) under the name of a
  registered service's program or a Windows program. A name alone never
  vouches for a process;
* otherwise it is refused only targets related to it: its program file and
  folder, a file with its name or a folder holding a program of that name
  above the target, a path carrying its distinctive name, or (for Windows
  Installer) an ``.msi`` package;
* an unrelated target is still deleted when a scan rule proved it is junk; an
  item the AI or the user approved from the uncertain list is refused with a
  reason naming the hidden programs, since nothing shows they leave it alone.

When the process or service list cannot be read at all, every target is
refused: there is no evidence to judge anything by.

The same process list also feeds the audited applications' discovery (for
example a browser started with ``--user-data-dir``), so a scan or a cleanup
lists processes once instead of once per application.
"""

# Chinese UI-facing reason text uses fullwidth punctuation.
# ruff: noqa: RUF001

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from devclean.core._application_cleanup_impl import forget_run_cached, run_cached
from devclean.core.installer_files import broad_folders, is_within, system_folders
from devclean.platform.windows.command_paths import (
    command_paths,
    normalize_path,
    program_and_paths,
)
from devclean.platform.windows.processes import ProcessInfo, running_processes
from devclean.platform.windows.registry import ServiceEntry, service_entries

LIVE_CHECK_UNAVAILABLE_REFUSAL = "running programs and services could not be checked"
LIVE_OWNER_REFUSAL = "a running program or service uses this location"
HIDDEN_RELATED_REFUSAL = "a running program with a hidden command line may use this location"
HIDDEN_UNVERIFIED_REFUSAL = "programs with hidden command lines run; a reviewed item stays"

# Kernel pseudo-processes that have no program file or command line.
_PSEUDO_PROCESSES = frozenset(
    {"system idle process", "idle", "system", "registry", "memory compression", "secure system"}
)
# Windows programs that run scripts, packages or commands named on their command line.
_HOSTS = frozenset(
    {
        "cmd.exe",
        "powershell.exe",
        "powershell_ise.exe",
        "pwsh.exe",
        "wscript.exe",
        "cscript.exe",
        "mshta.exe",
        "rundll32.exe",
        "regsvr32.exe",
        "msiexec.exe",
        "wsl.exe",
        "bash.exe",
    }
)
# Names too common to tie a path to one program.
_GENERIC_STEMS = frozenset(
    {
        "python", "pythonw", "py", "java", "javaw", "node", "dotnet", "code", "cmd",
        "powershell", "pwsh", "conhost", "wscript", "cscript", "mshta", "rundll32",
        "regsvr32", "msiexec", "svchost", "dllhost", "explorer", "setup", "install",
        "installer", "update", "updater", "uninstall", "launcher", "app", "main",
        "service", "svc", "host", "helper", "agent", "server", "client", "daemon",
        "worker", "run", "start", "tool", "tools", "bin", "system",
    }
)  # fmt: skip
_INSTALLER_PACKAGES = (".msi", ".msp")
# Entries walked to look for a hidden program inside a directory about to be deleted.
_TREE_LIMIT = 100_000


@dataclass(frozen=True, slots=True)
class HiddenProcess:
    """A process whose command line is hidden and that nothing trusted vouches for."""

    name: str
    program: str | None
    folder: str | None
    stem: str | None


@dataclass(frozen=True, slots=True)
class LiveState:
    """Processes and services at the start of this run, or why they are unknown."""

    processes: tuple[ProcessInfo, ...]
    services: tuple[ServiceEntry, ...]
    problem: str | None
    hidden: tuple[HiddenProcess, ...] = ()


@dataclass(frozen=True, slots=True)
class _OwnerIndex:
    files: dict[str, str]
    folders: tuple[tuple[str, str], ...]


def list_processes() -> tuple[ProcessInfo, ...]:
    return running_processes()


def list_services() -> tuple[ServiceEntry, ...]:
    return service_entries() if os.name == "nt" else ()


def live_state() -> LiveState:
    """The processes and services of this scan or cleanup run (read once per run)."""

    return run_cached("live_state", _read_live_state)


def shared_running_processes() -> tuple[ProcessInfo, ...]:
    """The run's process list for application discovery; empty when unreadable."""

    return live_state().processes


def forget_live_state() -> None:
    """Ask again next time outside a run (an application's refresh); a run keeps its list."""

    forget_run_cached(
        "live_state",
        "live_owner_index",
        "live_program_files",
        "live_programs_inside",
        "live_shared_folders",
    )


def live_owner_refusal(
    path: Path,
    *,
    directory: bool = False,
    rule_proven: bool = False,
) -> str | None:
    """Why *path* must not be deleted now because something running may use it, or ``None``.

    A directory is also refused when something running uses a path inside it.
    *rule_proven* says a scan rule, not an AI or user review, made it deletable.
    """

    state = live_state()
    if state.problem is not None:
        return f"{LIVE_CHECK_UNAVAILABLE_REFUSAL}: {state.problem}"
    target = normalize_path(str(path))
    if target is None:
        return f"{LIVE_CHECK_UNAVAILABLE_REFUSAL}: not an absolute path"
    index = run_cached("live_owner_index", _build_owner_index)
    owner = index.files.get(target) or next(
        (name for folder, name in index.folders if is_within(target, folder)), None
    )
    if owner is None and directory:
        owner = next(
            (
                name
                for used, name in (*index.files.items(), *index.folders)
                if used == target or is_within(used, target)
            ),
            None,
        )
    if owner is not None:
        return f"{LIVE_OWNER_REFUSAL}: {owner}"
    for process in state.hidden:
        why = _relation(process, target, directory=directory)
        if why is not None:
            return f"{HIDDEN_RELATED_REFUSAL}: {process.name}: {why}"
    unplaced = frozenset(process.name.casefold() for process in state.hidden if not process.program)
    if directory and unplaced:
        inside = _programs_inside(target, unplaced)
        if inside is None:
            names = "、".join(sorted({p.name for p in state.hidden if not p.program}))
            return (
                f"{HIDDEN_RELATED_REFUSAL}: {names}: 这个文件夹太大或有子文件夹无法读取，"
                "不能确认里面没有它的程序"
            )
        if inside:
            name, relative = inside
            return f"{HIDDEN_RELATED_REFUSAL}: {name}: 这个文件夹里有同名程序（{relative}）"
    if state.hidden and not rule_proven:
        names = "、".join(dict.fromkeys(process.name for process in state.hidden))
        return f"{HIDDEN_UNVERIFIED_REFUSAL}: {names}"
    return None


def _read_live_state() -> LiveState:
    try:
        processes = list_processes()
        services = list_services()
    except (OSError, ValueError) as error:
        return LiveState((), (), str(error) or type(error).__name__)
    service_programs = frozenset(
        path for service in services for path in command_paths(service.image_path)
    )
    service_names = frozenset(os.path.basename(path).casefold() for path in service_programs)
    hidden = tuple(
        _hidden(process)
        for process in processes
        if not process.command_line
        and not _vouched_for(process, service_programs, service_names)
    )
    return LiveState(processes, services, None, hidden)


def _vouched_for(
    process: ProcessInfo,
    service_programs: frozenset[str],
    service_names: frozenset[str],
) -> bool:
    """Whether trusted evidence accounts for a process whose command line is hidden.

    Path evidence: a non-host program in the Windows folder, or a registered
    service's program (its configuration is read from the registry). Session
    evidence: session 0, where only Windows and services start programs, with
    the name of a service's program or of a Windows program. A matching name in
    a user session proves nothing: any program can be called svchost.exe.
    """

    name = process.name.casefold()
    if process.session_id == 0 and name in _PSEUDO_PROCESSES:
        return True
    if name in _HOSTS:
        return False
    program = normalize_path(process.executable) if process.executable else None
    if program is not None:
        return program in service_programs or any(
            is_within(program, root) for root in system_folders()
        )
    if process.session_id != 0:
        return False
    return name in service_names or _windows_program_named(process.name)


def _windows_program_named(name: str) -> bool:
    windows = os.environ.get("SYSTEMROOT") or os.environ.get("WINDIR")
    if not windows:
        return False
    return any(
        os.path.isfile(os.path.join(windows, folder, name))
        for folder in ("System32", "", "SysWOW64")
    )


def _hidden(process: ProcessInfo) -> HiddenProcess:
    program = normalize_path(process.executable) if process.executable else None
    folder = os.path.dirname(program) if program is not None else None
    if folder is not None and not _usable(folder):
        folder = None  # a Windows or shared folder says nothing about this program
    stem = os.path.splitext(process.name)[0].casefold()
    usable_stem = stem if len(stem) >= 4 and stem not in _GENERIC_STEMS else None
    return HiddenProcess(process.name, program, folder, usable_stem)


def _relation(process: HiddenProcess, target: str, *, directory: bool) -> str | None:
    """How *target* relates to a hidden process, in the user's terms, or ``None``."""

    if process.program is not None:
        if target == process.program or (directory and is_within(process.program, target)):
            return "这就是它的程序文件或所在文件夹"
        if process.folder is not None and (
            target == process.folder or is_within(target, process.folder)
        ):
            return "它位于该程序的程序文件夹中"
    else:
        name = process.name.casefold()
        if os.path.basename(target).casefold() == name:
            return "它可能就是这个正在运行的程序"
        start = target if directory else os.path.dirname(target)
        for folder in _ancestors(start):
            if _program_file_exists(folder, process.name):
                return f"同一文件夹（{folder}）里有同名程序，它可能正从那里运行"
    if process.stem is not None:
        for part in Path(target).parts:
            text = part.casefold()
            tokens = re.split(r"[^0-9a-z]+", text)
            if process.stem in tokens or (len(process.stem) >= 5 and process.stem in text):
                return f"路径中含有它的名称（{part}）"
    if process.name.casefold() == "msiexec.exe" and target.endswith(_INSTALLER_PACKAGES):
        return "Windows Installer 正在运行，可能正在使用这个安装包"
    return None


def _programs_inside(directory: str, names: frozenset[str]) -> tuple[str, str] | None | tuple[()]:
    """Find a program with one of *names* inside *directory* (once per run).

    Returns ``(name, relative path)`` when found, ``()`` when the whole tree was
    read and none is there, and ``None`` when it could not be read completely.
    """

    known: dict[tuple[str, frozenset[str]], tuple[str, str] | None | tuple[()]] = run_cached(
        "live_programs_inside", dict
    )
    key = (directory, names)
    if key in known:
        return known[key]
    result: tuple[str, str] | None | tuple[()] = ()
    seen = 0
    pending = [directory]
    while pending and result == ():
        try:
            with os.scandir(pending.pop()) as entries:
                for entry in entries:
                    seen += 1
                    if seen > _TREE_LIMIT:
                        result = None
                        break
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(entry.path)
                    elif entry.name.casefold() in names:
                        result = (entry.name, os.path.relpath(entry.path, directory))
                        break
        except OSError:
            result = None
    known[key] = result
    return result


def _ancestors(folder: str) -> list[str]:
    """*folder* and its parents, stopping at a shared or Windows folder."""

    found: list[str] = []
    while folder and _usable(folder):
        found.append(folder)
        parent = os.path.dirname(folder)
        if parent == folder:
            break
        folder = parent
    return found


def _program_file_exists(folder: str, name: str) -> bool:
    known: dict[tuple[str, str], bool] = run_cached("live_program_files", dict)
    key = (folder, name.casefold())
    if key not in known:
        known[key] = os.path.isfile(os.path.join(folder, name))
    return known[key]


def _usable(path: str) -> bool:
    """Not a shared folder (profile, Downloads, Program Files...) and not Windows'."""

    broad, system = run_cached("live_shared_folders", lambda: (broad_folders(), system_folders()))
    return path not in broad and not any(path == root or is_within(path, root) for root in system)


def _build_owner_index() -> _OwnerIndex:
    state = live_state()
    files: dict[str, str] = {}
    folders: dict[str, str] = {}
    usable = _usable

    def note(command: str, owner: str, *, program_folder: bool) -> None:
        """A program owns its folder; named folders are owned; other files only themselves."""

        program, named_folders, named_files = program_and_paths(command, usable=usable)
        for folder in named_folders:
            folders.setdefault(folder, owner)
        for path in named_files:
            files.setdefault(path, owner)
        if program is not None:
            files.setdefault(program, owner)
            parent = os.path.dirname(program)
            if program_folder and usable(parent):
                folders.setdefault(parent, owner)

    for process in state.processes:
        owner = f"正在运行的程序 {process.name}"
        if process.executable:
            note(f'"{process.executable}"', owner, program_folder=True)
        note(process.command_line, owner, program_folder=True)
    for service in state.services:
        owner = f"已登记的 Windows 服务 {service.name}"
        note(service.image_path, owner, program_folder=True)
        for setting in service.settings:
            note(setting, owner, program_folder=False)
    return _OwnerIndex(files, tuple(folders.items()))


__all__ = [
    "HIDDEN_RELATED_REFUSAL",
    "HIDDEN_UNVERIFIED_REFUSAL",
    "LIVE_CHECK_UNAVAILABLE_REFUSAL",
    "LIVE_OWNER_REFUSAL",
    "HiddenProcess",
    "LiveState",
    "forget_live_state",
    "list_processes",
    "list_services",
    "live_owner_refusal",
    "live_state",
    "shared_running_processes",
]
