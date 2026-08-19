# Windows component-store maintenance audit

Audited: 2026-08-19

## Product conclusion

Windows component-store cleanup is a high-value system-maintenance source with a supported vendor operation, but its cleanup modes have materially different rollback semantics and must not be collapsed into one generic "WinSxS cleanup" rule.

The first executable DevClean lane is:

- `DISM /Online /Cleanup-Image /AnalyzeComponentStore` for read-only inventory;
- **USER_REVIEW** for manual `DISM /Online /Cleanup-Image /StartComponentCleanup` only when the fresh DISM report says component-store cleanup is recommended;
- **REPORT_ONLY / deliberately not exposed** for `/ResetBase`;
- zero raw deletion authority over `WinSxS`, `SoftwareDistribution`, CBS storage, package registry state, or component-store files.

Manual `StartComponentCleanup` is USER_REVIEW rather than deterministic/default cleanup because Microsoft documents that, unlike the automatic scheduled maintenance path, it immediately removes previous versions of updated components instead of retaining the normal 30-day grace period. The technical lifecycle is understood, but trading rollback headroom for immediate disk cleanup is a user decision.

`/ResetBase` is intentionally excluded from the executable surface because it goes further: Microsoft explicitly warns that all update packages already installed when it runs can no longer be uninstalled. A disk-cleanup product does not need that irreversible update-lifecycle change when a narrower supported operation exists.

## Primary vendor contracts

Current Microsoft documentation establishes:

- component cleanup is a supported Windows maintenance operation;
- Microsoft warns never to delete files directly from the WinSxS folder because doing so can severely damage Windows and prevent boot/update;
- the automatic `StartComponentCleanup` scheduled task normally waits at least 30 days after an updated component is installed before removing the previous version;
- running `DISM /Online /Cleanup-Image /StartComponentCleanup` manually performs similar cleanup but removes previous versions immediately, without that 30-day grace period and without the scheduled task's one-hour timeout;
- adding `/ResetBase` removes all superseded versions and prevents uninstalling all update packages already installed when the command completes;
- `DISM /Online /Cleanup-Image /AnalyzeComponentStore` produces a component-store report including actual store size, reclaimable-package count and Microsoft's `Component Store Cleanup Recommended` decision;
- DISM's global `/English` option can force command-line output to English, which gives DevClean a fail-closed way to parse only a small set of advisory/report fields without depending on the user's display language;
- DISM servicing requires elevation; DevClean must not silently elevate itself.

Primary sources:

- https://learn.microsoft.com/windows-hardware/manufacture/desktop/clean-up-the-winsxs-folder?view=windows-11
- https://learn.microsoft.com/windows-hardware/manufacture/desktop/determine-the-actual-size-of-the-winsxs-folder?view=windows-11
- https://learn.microsoft.com/windows-hardware/manufacture/desktop/dism-operating-system-package-servicing-command-line-options?view=windows-11
- https://learn.microsoft.com/windows-hardware/manufacture/desktop/dism-global-options-for-command-line-syntax?view=windows-11
- https://learn.microsoft.com/windows-hardware/manufacture/desktop/dism/disminitialize-function?view=windows-11

## Why WinSxS is not a raw cleanup root

The component store is part of Windows servicing state. Explorer-visible size is also misleading because WinSxS uses hard links and shares component files with the rest of Windows.

DevClean therefore must never:

- recursively delete `%SystemRoot%\WinSxS`;
- delete individual WinSxS children based on age/size;
- infer reclaimable bytes from Explorer's directory-size measurement;
- stop Windows Update services and delete `SoftwareDistribution` as a routine cleanup shortcut;
- remove CBS/package registry entries;
- translate a DISM failure into a filesystem fallback.

Only the Windows servicing stack may decide which superseded components can be removed.

## Inventory contract

DevClean uses only the exact Windows DISM executable and the online component-store analysis command:

`dism.exe /Online /English /Cleanup-Image /AnalyzeComponentStore`

The inventory is advisory and read-only. DevClean reads only the fields needed for product explanation and gating:

- DISM tool version;
- image version;
- DISM-reported actual component-store size when parseable;
- number of reclaimable packages when parseable;
- `Component Store Cleanup Recommended : Yes/No`.

The report remains human-oriented text rather than a formal JSON contract, so parsing is deliberately fail-closed:

- `/English` is always requested;
- missing/ambiguous recommendation text means **no executable action**;
- size/package parsing failure does not create a guess or deletion authority;
- raw report text may be shown for diagnosis, but it is not a destructive path manifest.

The cleanup authority comes from the fixed vendor operation, not from interpreting filenames or estimating which component files appear unused.

## Manual `StartComponentCleanup` lane

The manual DISM operation is technically vendor-owned and bounded to the online component store, but it bypasses the automatic maintenance task's 30-day retention grace.

DevClean therefore classifies it as **USER_REVIEW**:

- never preselected by a generic scanner;
- never sent to AI;
- offered only after an exact fresh DISM analysis recommends cleanup;
- explicit warning/confirmation required;
- no `/ResetBase`, no `/SPSuperseded`, no package IDs, no arbitrary servicing arguments;
- no `/Quiet` flag that would hide progress/errors;
- use `/NoRestart` defensively so DevClean never permits a cleanup command to restart the machine automatically;
- re-run analysis immediately before mutation and bind the same DISM/image version plus a still-positive recommendation;
- run one exact synchronous `StartComponentCleanup` command;
- run `AnalyzeComponentStore` again afterward and report the observed vendor state.

DevClean does not claim that the difference between two displayed component-store sizes equals physical free-space gain. The report accounts for shared/hard-linked Windows state, and cleanup may alter servicing storage in ways that do not map 1:1 to free bytes.

## Elevation boundary

Microsoft's servicing interface requires administrator/elevated access. DevClean's existing anti-goal is no silent privilege escalation.

Therefore the lane must:

- detect whether the current DevClean process is elevated;
- remain report/explanation-only when it is not;
- tell the user that DevClean must be restarted explicitly as administrator to use the system-maintenance action;
- never invoke `runas`, ShellExecute elevation, UAC automation, scheduled-task tricks, service creation, or another privilege-escalation helper itself.

User consent to cleanup and operating-system elevation are separate permissions.

## Concurrency and servicing boundary

DevClean must not kill or suspend Windows servicing processes. The vendor servicing stack owns CBS/TrustedInstaller locking and transaction behavior.

Before starting its own DISM mutation DevClean should conservatively refuse if an existing `dism.exe` or `DismHost.exe` activity is visible or if process state cannot be checked. It should not block solely because TrustedInstaller/TiWorker exists, because those are normal servicing components that DISM itself may use.

Any DISM nonzero exit, timeout, servicing conflict, or analysis failure ends the operation with no fallback.

## `/ResetBase` is deliberately excluded

Microsoft documents `/StartComponentCleanup /ResetBase` as a stronger reduction operation and explicitly warns that existing installed update packages cannot be uninstalled after it completes.

That is a supported administrator lifecycle choice, but it is not necessary for DevClean's first component-store cleanup surface. The product has a narrower vendor operation that does not permanently rebase every existing update package.

DevClean therefore:

- may explain `/ResetBase` in the audit/UI help text;
- never constructs or executes it;
- never offers a "deep clean" toggle that silently appends it;
- never treats extra potential disk savings as sufficient reason to destroy update rollback capability.

This lane can be revisited only if a future product requirement explicitly targets update-baseline administration rather than ordinary disk cleanup.

## Postcondition

After a successful `StartComponentCleanup`, DevClean re-runs the same exact `AnalyzeComponentStore` command.

Success means:

- the fixed vendor cleanup command returned zero;
- the same online-image identity can still be analyzed;
- a fresh component-store report is available.

DevClean does **not** require `Cleanup Recommended` to become `No`, because Windows may legitimately continue reporting reclaimable state after one maintenance pass and a servicing issue should be surfaced rather than converted into a false failure or raw workaround.

## Deliberate exclusions

This lane grants no authority to:

- raw-delete WinSxS/component-store files;
- raw-delete Windows Update download/database state;
- stop/rename Windows Update services/folders as routine cleanup;
- invoke `/ResetBase`;
- invoke `/SPSuperseded`;
- remove individual DISM packages;
- repair component-store corruption (`/RestoreHealth`) as a cleanup side effect;
- manipulate CBS registry/package state;
- auto-elevate DevClean;
- infer file-level deletion decisions with AI.

## Implementation requirements

The implementation in this same work branch/PR must include regression coverage for at least:

- exact fixed DISM command construction with `/English`;
- positive/negative/ambiguous analysis parsing;
- non-elevated refusal with no cleanup command;
- cleanup offered only when fresh analysis recommends it;
- DISM/image-version revalidation immediately before mutation;
- existing DISM/DismHost activity refusal and fail-closed process-state errors;
- exact `StartComponentCleanup` command with `/NoRestart` and no `/ResetBase`/`/Quiet`;
- vendor failure/timeout without raw fallback;
- post-cleanup reanalysis;
- no claim that logical/report size delta equals physical reclaim.

Normal DevClean validation remains mandatory before merge: lock/dependency checks, Ruff, strict mypy, full pytest/current workflow, Windows EXE artifact, and CodeQL.
