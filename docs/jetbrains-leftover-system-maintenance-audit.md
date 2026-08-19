# JetBrains expired IDE system-directory audit

Last audited: 2026-08-19

## Scope

This audit narrows the existing JetBrains storage rules to one source-backed lifecycle that is stronger than generic cache-name reasoning: JetBrains' own automatic cleanup of old IDE version storage.

It does **not** grant delete authority to an arbitrary `%LOCALAPPDATA%\JetBrains` child, to a currently used IDE system directory, or to configuration/plugins just because a product version looks old.

## Primary JetBrains sources

Current product documentation:

- Directories used by the IDE: https://www.jetbrains.com/help/idea/directories-used-by-the-ide-to-store-settings-caches-plugins-and-logs.html
- Local History: https://www.jetbrains.com/help/idea/local-history.html
- Invalidate caches: https://www.jetbrains.com/help/idea/invalidate-caches.html

Current IntelliJ Platform source audited at commit `50b850e16e71e38ee24bd10ad8a7bb1170261352`:

- `platform/platform-impl/update-checker/src/com/intellij/openapi/updateSettings/impl/UpdateCheckerProjectActivity.kt`
- `platform/platform-impl/initial-config-import/src/com/intellij/ide/OldDirectoryCleaner.java`
- `platform/platform-impl/initial-config-import/src/com/intellij/openapi/application/ConfigImportHelper.java`
- `platform/platform-impl/bootstrap/src/com/intellij/platform/ide/bootstrap/ApplicationLoader.kt`
- `platform/core-impl/src/com/intellij/openapi/application/ex/ApplicationEx.java`

## Vendor lifecycle established by documentation

JetBrains documents a distinct directory set per major IDE version. Its current documentation says:

- the **system directory** contains caches and Local History;
- the **configuration directory** contains user settings;
- the **plugins directory** contains user-installed plugins;
- logs have a separate documented path, which is inside the modern Windows default system tree;
- old-version **caches and logs** that have not been updated in the last **180 days** are automatically deleted;
- configuration and plugins remain unless the user removes them manually;
- `Help | Delete Leftover IDE Directories…` is a separate user-directed action for versions the user no longer plans to use.

This is materially stronger evidence than a directory named `cache`: JetBrains defines an old-version shelf life and owns an automatic deletion path.

## What the current source actually does

The current source makes the boundary more precise.

### Automatic lane

`UpdateCheckerProjectActivity.kt` defines `OLD_DIRECTORIES_SHELF_LIFE_DAYS = 180` and constructs an `OldDirectoryCleaner(expireAfter)`.

When `OldDirectoryCleaner` receives a nonzero cutoff, it asks `ConfigDirsSearchResult.findRelatedDirectories(config, true)` for the automatic-clean set. `ConfigImportHelper.getRelatedDirectories(..., forAutoClean=true)`:

- does **not** add the old configuration directory;
- does **not** add the old plugins directory;
- adds the default system directory when it exists;
- adds the default logs directory only when it is outside that system directory.

`OldDirectoryCleaner` walks every returned directory, records the latest modification time of every visited directory/file, and only retains the group when that maximum time is at or before the 180-day cutoff. The automatic path then recursively deletes those returned directories.

For modern Windows defaults, the logs directory is under the versioned system directory, so the source-backed automatic object is the exact default `%LOCALAPPDATA%\JetBrains\<product><version>` system tree.

### Manual leftover-directory lane is broader

`DeleteOldAppDirectoriesAction` creates `OldDirectoryCleaner(0)`. In that mode `getRelatedDirectories(..., false)` also includes the configuration directory and an external plugins directory where applicable. The dialog lets the user select versions and defaults the selection according to whether a matching installation is still visible.

DevClean deliberately does **not** reproduce that broader manual operation. Configuration/plugins remain persistent user state in this product.

### Installed-product signal

The platform writes a `.home` locator into the system directory. `OldDirectoryCleaner` reads it, opens the installation's `product-info.json`, and compares `dataDirectoryName` with the version selector to determine whether that version still corresponds to an installation. A missing `product-info.json` next to an existing home is conservatively treated as installed because it may be a self-built IDE.

JetBrains' automatic 180-day code does not use `isInstalled` as a deletion veto. DevClean intentionally adds a stricter veto: if the exact `.home`/`product-info.json` evidence still identifies an existing installation, the old system tree is reported but not executable.

## Product decision

**Exact default, uninstalled JetBrains version system tree whose entire source-backed tree has been untouched for at least 180 days = DETERMINISTIC_CANDIDATE.**

The authority comes from JetBrains' current automatic lifecycle, not from age alone. All of the following are required together:

1. the selector is one of the audited JetBrains IntelliJ-platform products and matches a versioned directory shape;
2. an exact matching default configuration root exists under `%APPDATA%\JetBrains`;
3. the exact default system root exists under `%LOCALAPPDATA%\JetBrains`;
4. both roots are ordinary, non-reparse directories on local fixed storage with stable identities;
5. every entry in the system tree is included in a non-following read-only walk and the maximum last-write time is at least 180 days old;
6. `.home` does not identify a still-existing matching installation and install state is not ambiguous;
7. the complete identity/statistics are revalidated before mutation;
8. no JetBrains IDE process is running or process state is uncertain.

The 180-day rule is therefore **not** a generic age rule. A random 180-day-old directory gets no authority from this audit.

## Local History nuance

The current JetBrains documentation says the system directory contains Local History, which is user-recovery state. For current/recent versions, DevClean's existing generic JetBrains policy continues to protect the mixed system root and treats Local History separately.

The reason the narrow expired-version lane can remove the whole system tree is that current JetBrains source itself selects and recursively deletes the complete old default system directory after its 180-day all-entry cutoff. DevClean does not generalize that lifecycle to a recent/current version and is stricter than JetBrains by protecting any version that still maps to an existing installation.

## Mutation boundary

There is no documented external CLI for the IDE's internal old-directory cleaner. For the narrow proven filesystem lifecycle DevClean uses its existing handle-bound exact-directory purge rather than a raw `shutil.rmtree` fallback:

- capture exact config/system directory identities;
- reject root symlink/junction/reparse or path redirection;
- require local fixed storage;
- scan without traversing reparse children;
- re-run the complete age/install/identity/statistics proof before deletion;
- refresh JetBrains process state immediately before the expensive revalidation and again before mutation;
- bind mutation to the exact verified system parent and exact system-root identity;
- remove reparse children as links, never traverse them;
- require the exact system root to be absent before reporting success.

Configuration and plugins are never included in this purge.

## Deliberate exclusions

- no deletion of `%APPDATA%\JetBrains\<version>` configuration directories;
- no user-plugin deletion;
- no broad `%LOCALAPPDATA%\JetBrains` deletion;
- no custom `idea.system.path` / `idea.log.path` cleanup in this lane;
- no Android Studio inheritance from JetBrains rules;
- no current/recent whole-system deletion;
- no manual `Delete Leftover IDE Directories` emulation that would include configuration/plugins;
- no deletion based only on folder name, version number, size, or age;
- no AI decision and no arbitrary recursive-delete fallback.

## Revisit conditions

A custom-path version of this lane would need source-backed discovery of the **effective old-version** `idea.system.path` / `idea.log.path`, not merely the current running IDE's overrides, plus the same complete 180-day tree proof and exact local mutation boundary.

A broader manual leftover-version lane that includes configuration or plugins would remain USER_REVIEW and would need explicit per-version disclosure of those persistent objects rather than inheriting authority from this automatic system-directory lifecycle.
