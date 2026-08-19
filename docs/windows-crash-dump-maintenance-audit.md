# Windows crash-dump and WER diagnostic storage audit

Audited: 2026-08-20

## Product conclusion

Windows diagnostic storage is not one cleanup class.

- **Configured kernel/complete/automatic/active crash-dump file:** exact file is **USER_REVIEW**.
- **Configured small-kernel dump directory:** each direct ordinary `.dmp` file is **USER_REVIEW**.
- **WER LocalDumps user-mode dump folder:** each direct ordinary `.dmp` file in an exact source-backed dump folder is **USER_REVIEW**.
- **WER queued/archive report stores:** **REPORT_ONLY** for mutation. Windows exposes exact read-only report enumeration/metadata, but the documented purge surface is whole-store rather than one reviewed report.
- Broad `Logs`, `CbsTemp`, `Prefetch`, `WER`, `Minidump`, `CrashDumps` or similarly named directories receive no generic delete authority from this audit.

Crash dumps are diagnostic evidence, not cache. Age and size can explain disk impact but never make a dump automatically disposable.

## Primary Microsoft contracts

### Kernel-mode crash dumps

Microsoft documents the active Startup and Recovery configuration under:

`HKLM\SYSTEM\CurrentControlSet\Control\CrashControl`

Relevant values include:

- `CrashDumpEnabled` (`0` none, `1` complete, `2` kernel, `3` small, `7` automatic; `1` plus `FilterPages=1` is active memory dump);
- `DumpFile`, default `%SystemRoot%\Memory.dmp`;
- `MinidumpDir`, default `%SystemRoot%\Minidump`;
- `Overwrite` for the single large dump lifecycle.

Microsoft also documents that kernel/complete dump files default to `%SystemRoot%\Memory.dmp` and are overwritten by a later kernel/complete dump, while small dumps are preserved as distinct date-coded files under `%SystemRoot%\Minidump`.

Primary sources:

- https://learn.microsoft.com/en-us/troubleshoot/windows-server/performance/memory-dump-file-options
- https://learn.microsoft.com/en-us/troubleshoot/windows-client/performance/configure-system-failure-and-recovery-options
- https://learn.microsoft.com/en-us/troubleshoot/windows-client/performance/generate-a-kernel-or-complete-crash-dump
- https://learn.microsoft.com/en-us/troubleshoot/windows-client/performance/read-small-memory-dump-file
- https://learn.microsoft.com/en-us/windows-hardware/drivers/debugger/kernel-memory-dump
- https://learn.microsoft.com/en-us/windows-hardware/drivers/debugger/small-memory-dump

### WER LocalDumps

Microsoft documents opt-in user-mode local dumps under:

`HKLM\SOFTWARE\Microsoft\Windows\Windows Error Reporting\LocalDumps`

and optional per-application subkeys. `DumpFolder` is the storage path; per-application settings override global settings. The documented default is `%LOCALAPPDATA%\CrashDumps`. `DumpCount` is a count-based retention limit, default 10; after the maximum is exceeded the oldest dump is replaced.

These files are deliberately collected for troubleshooting application crashes and can contain valuable process state. A full dump can be very large. DevClean therefore treats an exact file as known diagnostic content whose retention value is a user decision.

Primary sources:

- https://learn.microsoft.com/en-us/windows/win32/wer/collecting-user-mode-dumps
- https://learn.microsoft.com/en-us/windows/win32/wer/wer-settings

### WER report stores

Microsoft exposes `WerStoreOpen` for the machine queue (reports not yet sent) and machine archive (reports already sent), report-key enumeration, report counts/sizes and `WerStoreQueryReportMetadataV2` with per-report identifiers, creation time, size, status and constituent file names.

The current documented purge operation is `WerStorePurge`, a whole-store purge with no report key parameter. DevClean therefore does not turn per-report metadata into unsupported raw filesystem deletion and does not expose the whole-store purge as a cleanup shortcut.

Primary sources:

- https://learn.microsoft.com/en-us/windows/win32/api/werapi/nf-werapi-werstoreopen
- https://learn.microsoft.com/en-us/windows/win32/api/werapi/nf-werapi-werstorequeryreportmetadatav2
- https://learn.microsoft.com/en-us/windows/win32/api/werapi/ns-werapi-wer_report_metadata_v2
- https://learn.microsoft.com/en-us/windows/win32/api/werapi/nf-werapi-werstorepurge

## Executable file boundary

The implemented lane does **not** recursively delete a diagnostic directory.

For kernel diagnostics DevClean reads the current 64-bit `CrashControl` key and resolves only:

- the exact configured/default `DumpFile`;
- direct `.dmp` children of the exact configured/default `MinidumpDir`.

For WER LocalDumps DevClean reads the current 64-bit LocalDumps global/per-application configuration and resolves only:

- a literal absolute local `DumpFolder`; or
- the documented default `%LOCALAPPDATA%\CrashDumps` for the current-user context.

Other environment-variable custom LocalDumps paths are report-only rather than being expanded in the wrong process/account context. A service crash can use a service-specific profile, so DevClean does not pretend the interactive user's environment reproduces every service identity.

Only direct ordinary `.dmp` children are considered under a LocalDumps/Minidump directory. Nested directories are not walked.

## Filesystem mutation requirements

Before one file can be executable, DevClean requires:

1. source-backed configured file/directory identity;
2. local fixed storage;
3. ordinary non-reparse, non-cloud root;
4. stable root volume/file identity;
5. ordinary non-reparse, non-cloud file;
6. stable file identity and single hardlink;
7. explicit USER_REVIEW confirmation;
8. a fresh complete inventory immediately before mutation;
9. unchanged configuration binding, root identity, file identity, size and timestamps;
10. DevClean's handle-bound exact-file purge rather than pathname `DeleteFile`/recursive deletion;
11. the reviewed pathname to be absent after success.

The exact-file handle is opened without writer/delete sharing. A debugger, crash writer or other process that holds incompatible access therefore causes the mutation to fail closed rather than deleting a file that is actively changing.

Kernel/small-kernel dump deletion requires the current DevClean process to already be elevated. DevClean does not invoke `runas`, create a task, alter ACLs or enable backup/restore privileges.

## Why no automatic age rule

Windows gives LocalDumps a count-based retention mechanism and the large kernel dump an overwrite lifecycle, but neither contract says that a particular existing dump becomes worthless after N days. The newest dump can be useless and an old dump can be the only reproduction of a rare failure.

Therefore:

- no 7/30/90/180-day auto-delete rule;
- no minimum-size rule that creates authority;
- no deterministic candidate merely because the file is old or huge.

Logical size is explanatory only. Compression, sparse allocation and filesystem accounting can make immediate physical free-space change differ from the logical file size.

## Deliberate exclusions

This audit grants no authority to:

- purge the complete WER report store;
- raw-delete WER queue/archive report directories or databases;
- recursively delete `%SystemRoot%\Minidump`, `%LOCALAPPDATA%\CrashDumps` or any custom dump root;
- delete nested arbitrary files because they happen to be under a dump root;
- delete non-`.dmp` direct children of dump directories;
- change `CrashDumpEnabled`, `DumpFile`, `MinidumpDir`, `Overwrite`, LocalDumps `DumpCount`, `DumpType`, `DumpFolder` or other diagnostic policy;
- stop WER services/processes;
- take ownership, change ACLs or auto-elevate;
- infer authority from a directory/file name alone;
- use AI to decide that diagnostic evidence is no longer useful.

## Revisit conditions

WER report-store mutation can be revisited if Microsoft exposes a supported exact per-report delete operation, or a complete vendor manifest + mutation interface that can remove only one reviewed report without widening to the queue/archive.

Additional diagnostic sources such as live kernel reports, setup diagnostics, CBS logs and application-specific diagnostic bundles require separate source audits; they do not inherit this crash-dump authority.

## Validation

Normal DevClean validation is mandatory on the final PR head: lock/dependency checks, Ruff, strict mypy, full pytest/current workflow, Windows EXE build/artifact and CodeQL must all be green before merge.
