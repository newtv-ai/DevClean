# Task Manager live-kernel dump maintenance audit

Audited: 2026-08-20

## Product conclusion

A direct `.dmp` file in Task Manager's exact current-user live-kernel dump folder is **USER_REVIEW**.

Task Manager live dumps are deliberately created diagnostic snapshots. They may be the only retained evidence for a kernel/driver/hardware problem. Age and size can explain disk impact but never make one automatically disposable.

The documented live user-mode dump location is deliberately **excluded** from this lane because it is the mixed `%LOCALAPPDATA%\Temp` directory. A `.dmp` extension inside a generic temporary directory does not establish Task Manager ownership.

## Microsoft contract

Current Microsoft Learn documents Task Manager's live memory dump behavior:

- Task Manager can create a live kernel memory dump of the System process without crashing Windows;
- the result is intended for analysis with debugger/symbol/source information;
- after completion Task Manager displays the `.dmp` location and offers **Open File Location**;
- the default live-kernel dump location is:
  `%LocalAppData%\Microsoft\Windows\TaskManager\LiveKernelDumps`;
- live user-mode dumps instead go to `%LOCALAPPDATA%\Temp`;
- Task Manager must run at administrator level to generate the live kernel dump.

Primary source:

- https://learn.microsoft.com/en-us/windows-hardware/drivers/debugger/task-manager-live-dump

## Current-user Local AppData identity

The source documentation names `%LocalAppData%`, but DevClean does **not** trust an inherited `LOCALAPPDATA` environment variable as destructive path authority. A caller can alter process environment before launching DevClean, which could otherwise redirect the apparent default to an unrelated tree containing `.dmp` files.

The implementation resolves the current user's `FOLDERID_LocalAppData` through the Windows Known Folder API `SHGetKnownFolderPath`.

Microsoft documents:

- `FOLDERID_LocalAppData` = `{F1B32785-6FBA-4FCF-9D55-7B8E7F157091}`;
- its default path is `%LOCALAPPDATA%` / `%USERPROFILE%\AppData\Local`;
- new code should use the Known Folder APIs rather than legacy CSIDL lookup;
- `SHGetKnownFolderPath` with a null token requests the current user's folder;
- the returned string must be released with `CoTaskMemFree`.

Primary sources:

- https://learn.microsoft.com/en-us/windows/win32/shell/knownfolderid
- https://learn.microsoft.com/en-us/windows/win32/api/shlobj_core/nf-shlobj_core-shgetknownfolderpath
- https://learn.microsoft.com/en-us/windows/win32/shell/known-folders

Known folders can themselves be redirected, including to non-local storage, so successful Known Folder resolution is **not** sufficient mutation authority. The resulting Task Manager dump root must still pass the normal DevClean local-fixed and filesystem-identity gates.

## Exact filesystem boundary

The only audited root is:

`<current FOLDERID_LocalAppData>\Microsoft\Windows\TaskManager\LiveKernelDumps`

DevClean requires the exact root to be:

- an existing ordinary directory;
- non-reparse and non-cloud-placeholder;
- on local fixed storage;
- identifiable by stable volume + file ID.

Within that root DevClean considers **only direct ordinary `.dmp` children**. It does not recurse into subdirectories and it does not treat neighboring files as disposable.

Each executable file must be:

- ordinary, non-directory;
- non-reparse and non-cloud-placeholder;
- local-fixed;
- stable volume/file identity;
- exactly one hardlink;
- freshly unchanged at mutation time.

## USER_REVIEW and mutation

Generating a Task Manager live kernel dump requires administrator-level Task Manager, but the completed file lives in the current user's local profile. DevClean does not manufacture a separate privilege requirement for deletion: actual file ACLs remain authoritative, and an inaccessible file simply fails closed during exact handle acquisition.

Before deletion DevClean performs a fresh complete inventory from the Known Folder API and requires the reviewed root identity, exact file identity, size and timestamps to remain unchanged.

Deletion reuses DevClean's handle-bound `purge_exact_file` implementation. It does not use pathname `DeleteFile`, recursive deletion, ACL takeover, `runas`, a shell command, or an AI-generated operation. The exact reviewed source pathname must be absent before success is reported.

## Why the user-mode Task Manager dump is excluded

Microsoft documents live user-mode Task Manager dumps under `%LOCALAPPDATA%\Temp`. That directory is mixed state used by many applications and Windows components. The presence of a `.dmp` suffix there does not prove that Task Manager created the file.

Therefore this audit grants **no** authority to:

- scan all `.dmp` files under `%LOCALAPPDATA%\Temp` and label them Task Manager dumps;
- recursively clean Temp;
- infer ownership from filename shape, age or size;
- delete arbitrary user-mode dumps without a separate source-backed identity mechanism.

## Deliberate exclusions

This audit also grants no authority to:

- delete the `LiveKernelDumps` directory itself;
- recurse into nested folders;
- delete non-`.dmp` neighbors;
- use the `LOCALAPPDATA` process environment variable as production mutation authority;
- auto-delete a dump because it is old or large;
- change Task Manager settings;
- delete the broader system `LiveKernelReports` root through this Task Manager lane;
- use AI to decide whether diagnostic evidence is still useful.

The system `CrashControl` / `LiveKernelReports` and WER LocalDumps sources remain separately audited lanes with their own identities and scope.

## Validation

Normal DevClean validation is mandatory on the final PR head: lock/dependency checks, Ruff, strict mypy, full pytest/current workflow, Windows EXE build/artifact and CodeQL must all be green before merge.
