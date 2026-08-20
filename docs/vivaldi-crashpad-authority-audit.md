# Vivaldi Crashpad report authority audit

Audited: 2026-08-20

## Product conclusion

DevClean's former generic raw deletion authority for Vivaldi Crashpad reports is removed.

Current Vivaldi Crashpad lane:

- `%LOCALAPPDATA%\Vivaldi\User Data\Crashpad\reports`: **REPORT_ONLY / protected diagnostic evidence**;
- individual `.dmp` reports remain identifiable for explanation, but age and size do not create deletion authority;
- the `reports` directory is no longer a generic whole-tree delete root;
- Vivaldi's separately audited Chromium-derived browser-cache rules are unchanged.

The correction is deliberate: Vivaldi documents these files as crash logs used for manual crash and bug reporting, while current Crashpad owns report-database cleanup through its own database semantics. DevClean's previous `7 days + 1 MiB` whole-directory rule reproduced neither contract.

## Vivaldi source boundary

Current Vivaldi Browser Help, **Report crashes on Windows**, documents the Windows crash-log location as:

`%UserProfile%\AppData\Local\Vivaldi\User Data\Crashpad\reports`

Source:

- https://help.vivaldi.com/desktop/troubleshoot/reporting-crashes-on-windows/

The current instructions tell users to:

- use `vivaldi:crashes` to find crash logs;
- locate a crash log created around the failure time;
- send an individual crash log manually when desired;
- when Vivaldi cannot start, open the exact `Crashpad\reports` path, find the relevant crash log by date, and attach it to the Vivaldi bug report.

Vivaldi therefore treats these `.dmp` files as troubleshooting evidence with user-visible diagnostic value. Directory identity proves the files' meaning; it does not prove that a seven-day-old report is disposable.

Vivaldi snapshot guidance has also instructed users to attach the most recent `.dmp` from this same location when reporting startup crashes, including standalone installations where the reports live under that installation's `User Data\Crashpad\reports` tree.

## Crashpad owns report-database lifecycle

Audited against current `chromium/crashpad` commit:

`48b459d7aed33d3b47c8c2c3daff12716b95c2d5`

Primary files:

- `handler/prune_crash_reports_thread.cc`
- `client/prune_crash_reports.cc`
- `client/crash_report_database.h`

Current upstream source shows that report cleanup is not modeled as a generic filesystem age rule.

`PruneCrashReportThread` periodically operates on one `CrashReportDatabase`. Its worker:

1. asks the database to clean internal database state older than three days;
2. invokes `PruneCrashReportDatabase()` with a `PruneCondition`.

`PruneCrashReportDatabase()` obtains pending and completed reports from the database, sorts reports by creation time, and removes selected reports through `CrashReportDatabase::DeleteReport(report.uuid)`.

The current upstream **default** report-prune condition is an OR of:

- a database-size condition of 128 MiB;
- an age condition of 365 days.

These upstream defaults are important evidence that Crashpad treats reports as database-owned diagnostic records and that the old DevClean seven-day rule did not match the current generic Crashpad lifecycle.

They are **not** copied into DevClean. DevClean cannot prove from an external directory scan that Vivaldi uses the unmodified upstream default condition in every current build, nor can it safely reproduce Crashpad's report database state merely from file mtimes and pathnames.

## Why the old rule is removed

The former `vivaldi-crashpad-reports` rule granted TOOL authority when a report tree was old enough and large enough:

- fixed DevClean idle threshold: 7 days;
- fixed minimum logical size: 1 MiB;
- process-closed gate;
- `allow_whole_tree=True` on `Crashpad\reports`.

That fails the current DevClean execution standard because:

- Vivaldi documents the files as crash-report evidence that users may need for support and bug reporting;
- no Vivaldi seven-day expiration contract was found;
- current upstream Crashpad manages report objects through a database and exact report UUIDs rather than by raw recursive deletion of the reports tree;
- upstream prune criteria are materially different from DevClean's former threshold and may be application-configured;
- raw directory deletion can bypass database metadata/coordination semantics;
- closing `vivaldi.exe` does not prove that an external Crashpad handler/database writer is inactive or that database state is stable;
- logical report bytes are not by themselves authority to discard diagnostic evidence.

Therefore age, size and apparent browser inactivity remain explanatory facts only.

## No learned-rule bypass

Application semantics outrank generic learned path verdicts.

AI or user verdicts for a path inside Vivaldi `Crashpad\reports` must not manufacture a generic deletion rule after this audit. Regression tests preserve that boundary while retaining the existing learned-rule behavior for audited Vivaldi HTTP cache objects.

## Revisit conditions

A future positive Vivaldi Crashpad lane should not restore the former whole-directory rule.

Reconsider mutation only when one of these can be implemented safely:

1. Vivaldi exposes a documented exact crash-report delete/prune operation with a complete mutation contract; or
2. DevClean can bind one exact Vivaldi/Crashpad report database and implement a dedicated **USER_REVIEW** exact-report operation that preserves database semantics.

At minimum a dedicated exact-report lane would need:

- exact source-backed Vivaldi User Data / Crashpad database identity, including standalone layout handling;
- complete database/report inventory rather than suffix-only `.dmp` matching;
- exact report identity and metadata sufficient to correlate a reviewed item to the database record;
- Crashpad/Vivaldi process and concurrent-writer protection;
- stable local filesystem identities and reparse/cloud-boundary protection;
- fresh inventory and object revalidation immediately before mutation;
- deletion through the owning vendor/database lifecycle where available rather than raw recursive deletion;
- explicit USER_REVIEW because crash evidence may still be valuable even when old;
- a postcondition proving only the reviewed report was removed;
- no claim that logical report size equals immediate physical reclaim.

Until those conditions are implemented together, Vivaldi Crashpad reports remain visible but non-executable.

## Validation

This correction removes Vivaldi Crashpad TOOL/whole-tree authority and adds regressions proving that age, size, AI verdicts and user verdicts cannot restore generic report deletion. Vivaldi's existing Chromium-derived cache policy remains unchanged.

Normal final-head gate remains mandatory: lock/dependency checks, Ruff, strict mypy, full pytest/current workflow, Windows EXE artifact and CodeQL must all be green before merge.
