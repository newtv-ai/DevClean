# Previous Windows installation (`Windows.old`) maintenance audit

Audited: 2026-08-19

## Product conclusion

Previous Windows installation data is a **USER_REVIEW** lane.

Microsoft documents `Windows.old` as the previous Windows installation retained after an upgrade. It supports the limited-time ability to go back to the previous Windows version, and in some upgrade scenarios it can also temporarily contain personal files that were not migrated into the new installation.

Deleting it is therefore technically understood but never universally beneficial. DevClean must not auto-select it because it is large or old, and AI adds no value to the decision. The user must explicitly decide that rollback and file-recovery value are no longer needed.

## Current Microsoft contracts

Current Microsoft Support and Microsoft Learn documentation establishes:

- in most cases Windows keeps the previous version for about 10 days after an upgrade and then removes it automatically;
- deleting the previous Windows version before then is supported through **Settings > System > Storage > Temporary files / Cleanup recommendations**;
- deleting the previous version removes `Windows.old` and is irreversible for the previous-version rollback path;
- going back requires retaining the contents of `Windows.old` and `$WINDOWS.~BT`;
- `Windows.old` can temporarily contain personal files that a user may still want to recover;
- removal requires administrator privileges;
- the supported `cleanmgr` command exposes `/AUTOCLEAN`, documented as automatically deleting files left behind after upgrading Windows;
- DISM exposes `/Get-OSUninstallWindow` as a read-only query for the configured number of days after upgrade during which an OS uninstall can be initiated.

Primary sources:

- https://support.microsoft.com/windows/deployment/install-upgrade/delete-your-previous-version-of-windows
- https://support.microsoft.com/windows/deployment/install-upgrade/go-back-to-the-previous-version-of-windows
- https://support.microsoft.com/windows/deployment/install-upgrade/retrieve-files-from-the-windows-old-folder-after-a-windows-upgrade
- https://support.microsoft.com/windows/experience/storage-filemanagement/manage-drive-space-with-storage-sense
- https://learn.microsoft.com/windows-server/administration/windows-commands/cleanmgr
- https://learn.microsoft.com/windows-hardware/manufacture/desktop/dism-uninstallos-command-line-options?view=windows-11

## Why DevClean does not delete `Windows.old` directly

A root directory called `Windows.old` is not a generic cache root. It is OS upgrade lifecycle state with special ACLs, rollback semantics and potentially recoverable user files.

DevClean therefore does not take ownership of its internal filesystem layout, does not run `takeown` / `icacls`, and does not recursively remove it with Python, PowerShell, `cmd /c rmdir`, or another raw deletion mechanism.

The mutation authority comes only from Windows' own upgrade-leftover cleanup operation.

## Exact execution lane

The initial executable lane is deliberately narrow:

1. resolve the current `%SystemRoot%` and require it to be an ordinary directory on local fixed storage;
2. look only for the exact `Windows.old` sibling at the current Windows volume root;
3. require that exact `Windows.old` root to be an ordinary non-reparse directory with a stable Windows file identity;
4. inventory `$WINDOWS.~BT` only as related rollback context, never as a raw-delete target;
5. optionally query `DISM /Online /English /Get-OSUninstallWindow` for explanatory context; failure to obtain it does not manufacture or remove authority;
6. resolve the exact `%SystemRoot%\System32\cleanmgr.exe`, require a stable file identity and local fixed storage;
7. require the current DevClean process already to be elevated; never auto-elevate;
8. require explicit USER_REVIEW confirmation explaining rollback loss and possible recoverable personal files;
9. refuse while another Disk Cleanup, DISM, DismHost or Windows Setup host operation is visible; process-query uncertainty fails closed;
10. re-inventory the exact system root, `Windows.old` identity and cleanmgr identity immediately before mutation;
11. invoke only `cleanmgr.exe /AUTOCLEAN` with no registry profile editing, no `sageset`/`sagerun`, and no raw-delete fallback;
12. require the exact `Windows.old` root to be absent before reporting the operation as completed.

The implementation may show a best-effort logical size of `Windows.old` to help the user understand why the operation is worth considering. That number is informational only. It does not create deletion authority and is not promised as equal physical free-space reclaim.

## Why the DISM uninstall-window query is explanatory only

`DISM /Get-OSUninstallWindow` returns the configured uninstall-window value. DevClean must not label that number as "days remaining" and must not infer rollback availability from it alone.

The irreversible product decision is simpler and safer: whenever DevClean offers removal, the warning states that deleting the previous installation removes this retained rollback copy and may remove files that could otherwise be manually recovered.

DevClean does not call `/Remove-OSUninstall`, `/Set-OSUninstallWindow`, or `/Initiate-OSUninstall` as part of disk cleanup.

## Deliberate exclusions

This lane grants no authority to:

- recursively delete `Windows.old` or `$WINDOWS.~BT` itself;
- change ACLs or take ownership of upgrade folders;
- call DISM `/Remove-OSUninstall` as a disk-cleanup substitute;
- modify the OS uninstall window;
- initiate rollback;
- invent or edit `cleanmgr /sageset` registry profiles;
- run broad `/LOWDISK` or `/VERYLOWDISK` cleanup modes;
- remove user Downloads, Recycle Bin contents, or other unrelated cleanup categories;
- run Windows Setup cleanup while setup/servicing activity is already in progress;
- auto-elevate or bypass UAC;
- use AI to decide that rollback data is valueless.

## Decision class

**USER_REVIEW, never default-selected, no AI by default.**

This is a supported Windows lifecycle cleanup with a clear user-value tradeoff: disk space now versus rollback/file-recovery value. That belongs to the user, not a heuristic and not a model.

## Validation

The normal DevClean gate remains mandatory before merge: lock/dependency checks, Ruff, strict mypy, full pytest/current workflow, Windows EXE artifact, and CodeQL must all be green.
