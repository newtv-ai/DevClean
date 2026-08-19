# Windows cleanup surfaces and Recycle Bin audit

Last audited: 2026-08-19

## Scope

This audit follows the component-store and previous-installation lanes. It asks whether DevClean can safely automate the remaining broad Windows cleanup surfaces, and whether any narrower object has a source-backed execution boundary.

## Primary Microsoft sources

- Microsoft Support, **Manage drive space with Storage Sense**: https://support.microsoft.com/en-US/Windows/Experience/Storage-FileManagement/manage-drive-space-with-storage-sense
- Microsoft Support, **Free up drive space in Windows**: https://support.microsoft.com/en-US/Windows/Experience/Storage-FileManagement/free-up-drive-space-in-windows
- Microsoft Learn, **Policy CSP - Storage**: https://learn.microsoft.com/en-us/windows/client-management/mdm/policy-csp-storage
- Microsoft Learn, **cleanmgr**: https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/cleanmgr
- Microsoft Learn, **Automating Disk Cleanup tool**: https://learn.microsoft.com/troubleshoot/windows-server/backup-and-storage/automating-disk-cleanup-tool
- Microsoft Learn, **Creating a Disk Cleanup Handler**: https://learn.microsoft.com/en-us/windows/win32/lwef/disk-cleanup
- Microsoft Learn, **SHQueryRecycleBin** and **SHEmptyRecycleBin**: https://learn.microsoft.com/en-us/windows/win32/api/shellapi/nf-shellapi-shqueryrecyclebinw and https://learn.microsoft.com/en-us/windows/win32/api/shellapi/nf-shellapi-shemptyrecyclebinw

## Conclusions

### Storage Sense / Cleanup recommendations

**Vendor-managed / REPORT_ONLY for generic programmatic cleanup.**

Storage Sense is a user/administrator policy and Settings surface. Its documented configuration deliberately mixes categories with different product semantics:

- unused temporary files;
- Recycle Bin items after an age threshold;
- Downloads files after an optional age threshold;
- cloud-backed content dehydration;
- cadence and low-disk behavior.

Downloads are user content. Cloud dehydration changes local availability. Recycle Bin contents are recoverable user data until permanently emptied. These categories cannot inherit one common deletion class merely because Windows presents them on one Storage page.

The current Microsoft documentation exposes Settings/policy configuration, but not a stable one-shot API that gives DevClean an exact complete pre-mutation manifest for the user's current Cleanup recommendations. DevClean therefore does not toggle Storage Sense policy, write its policy/registry values, or attempt to invoke undocumented Settings internals.

### Generic Disk Cleanup / `cleanmgr`

**Generic automation deferred.**

`cleanmgr /sageset:n` creates persisted registry selection state, and `/sagerun:n` runs the selected profile. Microsoft also documents that Disk Cleanup is extensible through registered cleanup handlers: applications can register their own COM handlers and each handler owns its cleanup behavior. A generic profile therefore is not a fixed Microsoft-only path manifest.

DevClean will not:

- manufacture `StateFlags` registry values;
- reuse an unknown existing `sagerun` profile;
- select every handler because its UI label looks disposable;
- use `/LOWDISK` or `/VERYLOWDISK` as an implicit broad-selection shortcut;
- claim that a human-visible cleanmgr category name proves the exact destructive scope of a registered handler.

The separately audited `cleanmgr /AUTOCLEAN` previous-Windows-installation lane remains valid because Microsoft documents that switch specifically for files left after a Windows upgrade and DevClean independently binds it to an exact reviewed `Windows.old` lifecycle object.

### Downloads

**Protected user content.**

Microsoft's own Storage Sense default is never to delete Downloads unless the user explicitly configures a threshold. DevClean does not create a generalized Downloads cleanup rule based on age, extension, size, or Storage Sense availability.

### Recycle Bin

**Positive narrow lane: exact per-drive Recycle Bin emptying = USER_REVIEW.**

This object has a much stronger vendor boundary than generic cleanup categories:

- `SHQueryRecycleBin` returns the size and item count for a specified drive;
- `SHEmptyRecycleBin` empties the Recycle Bin on a specified drive;
- Microsoft explicitly documents that passing an empty/NULL root widens the operation to all drives.

DevClean therefore binds the operation to exactly one current fixed local drive root and never passes NULL/empty scope.

Recycle Bin data remains USER_REVIEW rather than deterministic cleanup because it is intentionally recoverable user content. Emptying it permanently destroys that recovery copy. The UI must require explicit confirmation and never default-select a drive merely because its Recycle Bin is large or old.

## Recycle Bin execution contract

The implementation must:

1. enumerate only Windows-reported fixed local volume roots;
2. query accounting through `SHQueryRecycleBin`, never inspect or delete `$Recycle.Bin` directly;
3. expose one exact drive per action, never an all-drive call;
4. re-query immediately before mutation and refuse if the reviewed count/size changed;
5. call `SHEmptyRecycleBin` with the exact non-empty drive root;
6. suppress only duplicate Shell confirmation/progress UI after DevClean's own explicit irreversible confirmation;
7. re-query after the call and require both item count and reported size to be zero before reporting success;
8. treat reported Recycle Bin size as logical vendor accounting, not guaranteed physical free-space reclaim;
9. never elevate automatically and never mutate Downloads, cloud files, arbitrary temporary folders, cleanmgr profiles, or Storage Sense policy as a side effect.

## Product boundary

This audit does **not** create broad Windows cleanup authority. It deliberately keeps the following separate:

- component store: DISM-backed USER_REVIEW only when DISM recommends cleanup;
- previous Windows installation: exact `Windows.old` USER_REVIEW through audited `/AUTOCLEAN`;
- Recycle Bin: exact per-fixed-drive USER_REVIEW through Shell APIs;
- Downloads: protected user content;
- Storage Sense / Cleanup recommendations: vendor-managed/report-only without a stable exact one-shot interface;
- generic cleanmgr handlers/profiles: execution deferred because the handler set is extensible and profile state is persisted/ambient.

## Revisit conditions

Generic Cleanup recommendations could become executable only if Microsoft exposes a stable supported API that returns the exact current recommendation objects and their destructive semantics before mutation, with independently selectable categories and a bounded exact execution operation.

Generic cleanmgr automation could be reconsidered only if DevClean can prove the exact registered handler identity and behavior for each selected handler without relying on mutable ambient profile state or arbitrary third-party handler execution.
