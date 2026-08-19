# Windows live-kernel dump maintenance audit

Audited: 2026-08-20

## Product conclusion

System-generated Windows live-kernel dumps are diagnostic evidence, not cache.

The narrow executable lane is **USER_REVIEW** for one exact `.dmp` file that can be proven to live in the current Windows LiveKernelReports lifecycle:

- full live dumps directly under the exact configured LiveKernelReports root;
- component minidumps directly under one immediate component directory beneath that root.

There is no age- or size-based automatic deletion. Microsoft documents throttling and a maximum count for full live reports, but those settings control generation/retention pressure; they do not state that an existing dump becomes diagnostically worthless after a time threshold.

## Microsoft storage contract

Current Microsoft Windows debugger documentation states:

- live dumps are stored by default under `%SystemRoot%\LiveKernelReports`;
- full dumps use `%SystemRoot%\LiveKernelReports\*.dmp`;
- minidumps use `%SystemRoot%\LiveKernelReports\<ComponentName>\*.dmp`;
- component directories are part of the documented layout (examples include NDIS, PDCRevocation, PoW32kWatchdog, USBHUB3 and WATCHDOG).

WER settings document the current redirected root under:

`HKLM\SYSTEM\CurrentControlSet\Control\CrashControl\LiveKernelReports`

with `LiveKernelReportsPath`. Microsoft documents that the redirected value is an NT path such as `\??\D:\LiveDumpsFolder`; the default remains `%SystemRoot%\LiveKernelReports`.

The `FullLiveKernelReports` settings also document:

- `FullLiveReportsMax`, the maximum number of full live dumps that may be on disk;
- system/component throttle thresholds controlling how frequently full live reports can be generated.

Primary sources:

- https://learn.microsoft.com/en-us/windows-hardware/drivers/debugger/kernel-live-dump-code-reference
- https://learn.microsoft.com/en-us/windows/win32/wer/wer-settings
- https://learn.microsoft.com/en-us/windows-hardware/drivers/devtest/dtrace-live-dump

## Exact source boundary

DevClean must not scan every directory named `LiveKernelReports` or every `.dmp` on the machine.

The implementation resolves one current system root as follows:

1. read the 64-bit CrashControl `LiveKernelReports` subkey;
2. if `LiveKernelReportsPath` is absent, use the documented `%SystemRoot%\LiveKernelReports` default;
3. if present, accept only the documented DOS-volume form after the NT `\??\` prefix is removed;
4. reject UNC, device, GlobalRoot, relative, malformed or unsupported path forms rather than guessing.

Within that exact root, DevClean recognizes only the documented layout:

- direct ordinary `.dmp` files at the root;
- direct ordinary `.dmp` files inside one immediate ordinary component directory.

It does not recurse more deeply and does not delete any non-`.dmp` file or directory.

## Mutation requirements

Every candidate remains **USER_REVIEW** because the dump may be the only evidence for an intermittent GPU, storage, USB, networking, power or watchdog failure.

Before mutation DevClean requires:

- current process already elevated; no automatic UAC/runas path;
- exact configured/default live-kernel root still unchanged;
- local fixed storage;
- ordinary non-reparse/non-cloud root and, for component dumps, ordinary immediate component directory;
- stable directory identity;
- ordinary non-reparse/non-cloud `.dmp` file;
- stable file identity and exactly one hardlink;
- fresh inventory immediately before mutation;
- unchanged root/configuration binding, file identity, size and timestamps.

Deletion reuses DevClean's handle-bound exact-file purge. It does not call pathname `DeleteFile`, recursively remove a component directory, alter LiveKernelReports policy, stop WER, or manufacture cleanup commands.

After mutation the reviewed source pathname must be absent before DevClean reports success.

## Why this is not deterministic cleanup

`FullLiveReportsMax` is a vendor retention cap for full live reports, not permission to delete any particular old file early. Throttle windows are generation controls, not file-expiration timestamps. Minidumps are stored per component and can remain useful long after generation.

Therefore:

- age and size only explain storage impact;
- no `7/30/90/180` day rule creates deletion authority;
- no `FullLiveReportsMax` arithmetic is converted into an automatic deletion recommendation;
- no component name is treated as proof that a failure is resolved.

## Deliberate exclusions

This audit grants no authority to:

- delete the LiveKernelReports root or component directories themselves;
- recurse beyond one documented component-directory level;
- delete non-`.dmp` diagnostics/logs beside a dump;
- change `LiveKernelReportsPath`, `FullLiveReportsMax`, throttle values or other WER/CrashControl settings;
- delete arbitrary Task Manager live user-mode dumps from `%TEMP%`, a mixed-content directory;
- infer authority from a folder/file name alone;
- auto-delete based on age/size;
- use AI to decide that diagnostic evidence is no longer useful.

Task Manager's dedicated current-user live-kernel dump directory and other setup/CBS/application diagnostic stores remain separate audits; they do not inherit this authority automatically.

## Validation

Normal DevClean validation is mandatory on the final PR head: lock/dependency checks, Ruff, strict mypy, full pytest/current workflow, Windows EXE artifact and CodeQL must all be green before merge.
