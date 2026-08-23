# JetBrains / Android Studio cache-semantics re-audit — 2026-08

## Why this row was reopened

This is a current-main regression correction, not a request to widen cleanup by
name or age.

The original JetBrains (#21) and Android Studio (#23) source audits established
exact IntelliJ-platform boundaries for generated `index`, `tmp`, and `vcs-log`
subtrees. Later architecture work (#170) made the product invariant explicit:
for an exact source-audited whole-tree TOOL object, age and minimum reclaim size
are benefit/ranking metadata and must not revoke the underlying safe-clean
classification.

The current per-path JetBrains and Android Studio evaluators still returned
`TOOL_KEEP_RECENT`, `TOOL_KEEP_LOW_BENEFIT`, or `TOOL_KEEP_UNKNOWN_USAGE` for
those exact cache classes. That contradicted the current execution model and
could push proven cleanup back into review/non-cleanup lanes.

This re-audit also rechecked the neighboring product `log` root instead of
mechanically granting every old TOOL rule the new semantics. That check found
that current product logs were over-authorized.

## Current upstream evidence

### IntelliJ Platform

- JetBrains "Directories used by the IDE" documents separate config, system,
  plugin, and log directories. The system directory is cache-oriented; the log
  directory contains product logs and thread dumps. It also documents automatic
  deletion of **old-version** cache/log directories after 180 days of no update:
  https://www.jetbrains.com/help/idea/directories-used-by-the-ide-to-store-settings-caches-plugins-and-logs.html
- JetBrains "Invalidate caches" documents cache invalidation/recreation and a
  separate "Clear VCS Log caches and indexes" action. Local History is a
  separate destructive option rather than ordinary cache invalidation:
  https://www.jetbrains.com/help/idea/invalidate-caches.html
- JetBrains Local History documentation treats Local History as recovery data,
  not generic cache:
  https://www.jetbrains.com/help/idea/local-history.html
- Current IntelliJ Platform source defines the old-directory automatic cleanup
  shelf life as 180 days and invokes `OldDirectoryCleaner` for those old data
  directory groups:
  https://github.com/JetBrains/intellij-community/blob/38a8934174f7d14c0c939ae140b63b037a3830e5/platform/platform-impl/update-checker/src/com/intellij/openapi/updateSettings/impl/UpdateCheckerProjectActivity.kt
- `OldDirectoryCleaner` discovers related old product data directories, checks
  identity/install state and latest modification, and deletes only the proven
  old groups. This is not a generic lifecycle for the current active `log`
  directory:
  https://github.com/JetBrains/intellij-community/blob/38a8934174f7d14c0c939ae140b63b037a3830e5/platform/platform-impl/initial-config-import/src/com/intellij/ide/OldDirectoryCleaner.java

### Android Studio

- Current Android Studio troubleshooting documentation defines the Windows
  config, plugin, system and log locations. It does not define an independent
  raw age/size lifecycle authorizing recursive deletion of the current log root:
  https://developer.android.com/studio/troubleshoot

Android Studio remains a Google-owned product profile. It inherits only the
already-audited IntelliJ-platform subtree semantics, not JetBrains old-version
product lifecycle rules.

## Decision table

| Storage class | Decision | Reason |
| --- | --- | --- |
| current `system/index` | TOOL / exact whole-tree | source-audited generated IDE indexes; rebuildable |
| current `system/tmp` | TOOL / exact whole-tree | previously audited exact platform temporary subtree |
| current `system/vcs-log` | TOOL / exact whole-tree | vendor/source identifies VCS Log caches and indexes |
| current product `log` | KEEP / no whole-tree authority | diagnostic product logs/thread dumps; no current-root raw deletion lifecycle established |
| `LocalHistory` | USER | recovery history with user value |
| `jcef_cache` | USER | embedded-browser cache and cookie/state tradeoff |
| VFS `caches` | KEEP | coupled platform state; prior audit intentionally protected it |
| complete system root | KEEP/report-only | mixed generated and persistent state |
| config/plugins | KEEP | authored/user-installed persistent state |

## Action-semantics correction

For the exact `index`, `tmp`, and `vcs-log` TOOL classes:

- IDE running -> `TOOL_KEEP_IN_USE` remains a hard live blocker;
- IDE closed -> `TOOL_DELETE` even when the object is fresh, tiny, or has unknown
  last-use metadata;
- existing idle/size/rebuild-cost fields remain available for ranking and UI
  benefit information.

This is the same safety/benefit separation established in #170. It does not
turn neighboring unknown or persistent state into TOOL data.

## Current-log correction

The former 14-day/minimum-size raw current-log rule is removed as deletion
authority for both JetBrains IDEs and Android Studio.

Even a very old or very large current `idea.log` remains `KEEP_PROTECTED`, and
the current `log` directory no longer appears as an audited whole-tree delete
root. Age and size cannot recreate that authority.

JetBrains old-version data cleanup remains separate: the existing dedicated
old-version maintenance lane follows the vendor's 180-day group lifecycle and
its own product/install/process/identity checks. Android Studio does not inherit
that lane merely because it is IntelliJ-platform based.

## Invariants retained

- exact application/root identity before whole-tree mutation;
- config, plugins, Local History, JCEF state, VFS state and unknown system
  children remain non-TOOL;
- process/concurrency guard remains an execution condition;
- generic names, age, size and apparent regenerability do not create authority;
- no UI-level rebuild-cost override is introduced;
- all final deletion remains subject to the shared execution revalidation and
  filesystem safety checks.
