# AGE_BASED_REVIEW lifecycle re-audit

Audited: 2026-08-20

## Source conclusion

Microsoft documents Storage Sense temporary-file cleanup as removal of the user's temporary files that **aren't in use**. Its separately configurable day thresholds apply to Downloads, Recycle Bin and cloud-content dehydration; Microsoft does not document a generic rule that an arbitrary file under `%TEMP%`, `%SYSTEMROOT%\Temp`, `%SYSTEMROOT%\SystemTemp` or `%LOCALAPPDATA%\CrashDumps` becomes safe to raw-delete after one day of mtime age.

DevClean therefore treats age and mtime as benefit/observation evidence only. They no longer create mutation authority.

## Product correction

- packaged `user-temp`, `windows-temp` and `user-crash-dumps` roots are REPORT_ONLY discovery anchors;
- old sidecars that still say `AGE_BASED_REVIEW` fail closed at runtime regardless of age;
- the implicit current-user temp-root fallback is REPORT_ONLY even for very old files;
- an old child directory under a legacy age root no longer becomes a whole-tree candidate;
- the unreachable `AGED_TEMP_ITEM` triage branch is also protected as a defense-in-depth ceiling;
- separately source-audited application rules still run before generic root policy and are unchanged;
- exact learned/common **file** knowledge may still supplement a generic temp-file uncertainty when it passes the existing file-only authority boundary, but no learned rule gains directory authority.

## Why not change 1 day to 7/30 days?

A larger number would still confuse age with lifecycle. A stale mtime does not prove that a file is closed, unused, reconstructable, unreferenced, or outside an application's recovery/rollback state. Where Windows or an application exposes an exact maintenance operation, DevClean should use that source-owned lane instead of inventing a raw-age contract.

## Windows crash dumps

The generic `%LOCALAPPDATA%\CrashDumps` scan root is now discovery-only. DevClean's dedicated Windows crash-dump inventory remains the correct lane: exact dump objects have known diagnostic meaning and are USER_REVIEW rather than being auto-authorized by age. WER queue/archive stores remain protected.
