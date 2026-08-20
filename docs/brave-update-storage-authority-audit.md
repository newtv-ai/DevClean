# Brave Update storage authority audit

Audited: 2026-08-20

## Product conclusion

DevClean's former generic raw deletion authority for Brave Update installer staging and the updater diagnostic log is removed.

Current Brave Update storage lanes are:

- `BraveSoftware\Update\Install`: **REPORT_ONLY / protected from generic raw deletion**;
- `BraveSoftware\Update\Log\BraveUpdate.log`: **REPORT_ONLY / vendor-managed diagnostic state**;
- `BraveSoftware\Update\Download`: **REPORT_ONLY / vendor-managed package cache**;
- `BraveSoftware\Update\Offline`: protected updater/offline-install state;
- updater binaries, active versions, registry/configuration and other updater state: protected;
- no Brave Update whole-tree delete root.

This audit does **not** change the separately audited Chromium-derived Brave browser cache/profile rules. It only corrects Brave Update authority.

The important distinction is that current Omaha source does contain vendor lifecycle code for the install working directory and package cache, but DevClean's old generic rule did not reproduce those exact boundaries. A source fact that something is eventually regenerable does not automatically make an environment-derived whole-tree delete rule safe.

## Primary source

Audited against current `brave/omaha` commit:

`a5ea9fa3f39dfcb88ecbdfaeb1afa5d47c6a0a24`

Primary files:

- `omaha/common/omaha_brave_customization_unittest.cc`
- `omaha/common/config_manager.cc`
- `omaha/goopdate/install_manager.cc`
- `omaha/goopdate/package_cache.cc`
- `omaha/base/constants.h`
- `omaha/base/logging.h`
- `omaha/base/logging.cc`
- `doc/ClientLog.md`

The Brave customization tests pin the relevant product paths and names, including:

- `BraveSoftware\Update`;
- `BraveSoftware\Update\Install`;
- `BraveSoftware\Update\Download`;
- `BraveSoftware\Update\Offline`;
- `BraveSoftware\Update\Log`;
- `BraveUpdate.log`.

## Install working directory is vendor-disposable, but the old DevClean rule was not the vendor lifecycle

Current Omaha source gives the install working directory a strong lifecycle signal.

`ConfigManager::GetUserInstallWorkingDir()` derives the user path from `CSIDL_LOCAL_APPDATA` plus `BraveSoftware\Update\Install`.

`ConfigManager::GetMachineInstallWorkingDir()` derives the machine path from `CSIDL_PROGRAM_FILES` through Omaha's own 32-bit path helper plus the same relative suffix.

`InstallManager::InstallManager()` then:

1. resolves exactly one user or machine install working directory;
2. creates that directory if needed;
3. calls `DeleteDirectoryContents(install_working_dir_)` before using it for the new install/update session.

That source behavior proves that **contents surviving from a previous install-manager lifetime are not persistent product state**. It does not prove that DevClean's old implementation was a faithful or sufficiently bounded execution of the same lifecycle.

The old DevClean rule instead:

- built updater roots from environment-derived `LOCALAPPDATA`, both `ProgramFiles` variants and `ProgramData`;
- treated every matching `...\Update\Install` below those roots as equivalent;
- used a DevClean-invented `7 days + 16 MiB` threshold that is not part of Omaha's install-working-directory lifecycle;
- granted `allow_whole_tree=True`, allowing the `Install` directory itself to become a whole-tree delete root even though Omaha creates the directory and clears its **contents**;
- relied on a generic process-name guard rather than a source-bound updater synchronization/revalidation contract.

The `ProgramData` mismatch is especially important. Omaha's file logger uses common application data, but its user/machine install working directories come from Local AppData and Program Files respectively. A generic list containing all of those locations is useful for reporting; it is not proof that each location has every updater subdirectory semantic.

Therefore the current generic application scanner keeps identifying `Install` for explanation but no longer grants raw mutation authority.

## The diagnostic log has its own size lifecycle, not a seven-day expiry

Current Omaha logging source resolves the default file log under common application data plus `BraveSoftware\Update\Log\BraveUpdate.log`.

The log implementation owns size management:

- optimized builds define a default maximum log size of 10 MB;
- the logging configuration can provide `MaxLogFileSize`;
- when the log crosses its configured maximum, Omaha attempts to archive it;
- a stop-gap threshold at ten times the maximum exists to truncate the log and prevent disk overfill when archiving cannot proceed.

`doc/ClientLog.md` describes the same lifecycle: normal archiving around the 10 MB default and a 100 MB stop-gap under that default.

No current source-backed seven-day expiration contract was found for `BraveUpdate.log`.

The former DevClean rule deleted the file after seven idle days once it exceeded 256 KiB. That could discard diagnostic evidence earlier than Omaha's own configured size lifecycle and had no vendor-backed retention basis. The log is therefore now REPORT_ONLY / protected from generic raw deletion.

## Download storage already has a vendor package-cache lifecycle

Omaha's `Download` storage is not an ordinary folder that DevClean should age independently.

`PackageCache` obtains both package-cache expiration and size limits from `ConfigManager`. Its cleanup path inventories cached packages, orders them by creation time, applies the effective size and expiration boundaries, and deletes packages through Omaha's own cache implementation.

`ConfigManager` also exposes user and machine download-storage paths separately. This is another example where the updater itself already owns lifecycle semantics and path selection.

DevClean therefore continues to protect/report `Download` rather than adding a second filename/mtime-based cache GC implementation.

## Offline and updater-version state remain protected

The customization source also defines `Offline` storage separately from `Download` and `Install`. Offline packages can be intentional install input rather than stale cache.

The updater root additionally contains active updater versions, executables and persistent state. Version-like directory names, file age, size or redownloadability do not create deletion authority for them.

There is no whole `BraveSoftware\Update` cleanup lane.

## No learned-rule bypass

Application semantics outrank generic learned filename/path verdicts.

AI or user verdicts must not turn:

- install-working-directory contents;
- `BraveUpdate.log`;
- updater binaries/version state

back into generic deletion rules after this audit. Regression tests preserve that boundary while still allowing the existing audited Brave HTTP-cache lane to behave according to its own policy.

## Revisit condition for a positive install-working-directory lane

A future positive lane may be reasonable because Omaha itself clears the install working-directory contents. It should be implemented as a dedicated source-bound operation, not by restoring the old rule.

At minimum it would need:

1. exact current-user and machine path resolution through Windows APIs equivalent to Omaha's path selection, not inherited environment strings as destructive authority;
2. explicit separation of Local AppData / Program Files install working roots from ProgramData log storage;
3. a **contents-only** mutation contract that preserves the `Install` directory itself;
4. stable local filesystem/root/object identity and reparse/cloud-boundary protection;
5. source-backed updater concurrency protection or an equivalently strong fail-closed process/lock proof;
6. fresh root/object revalidation immediately before mutation;
7. no widening into `Download`, `Offline`, updater versions, configuration or logs;
8. a postcondition that verifies only the reviewed install-working-directory contents were removed;
9. logical-byte reporting only, without promising identical physical reclaim.

Until those conditions are implemented together, Brave Update storage remains visible but non-executable.

## Validation

This PR removes the two generic updater TOOL rules, removes Brave Update whole-tree authority, and adds regression coverage that age, size, AI verdicts and user verdicts cannot restore it. The existing Chromium-derived Brave browser cache policy remains unchanged.

Normal final-head gate remains mandatory: lock/dependency checks, Ruff, strict mypy, full pytest/current workflow, Windows EXE artifact and CodeQL must all be green before merge.
