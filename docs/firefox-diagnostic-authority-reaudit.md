# Firefox diagnostic authority re-audit — 2026-08

## Current Mozilla evidence

Mozilla Support documents `Crash Reports/pending` as reports that have not been submitted and exposes them through `about:crashes`; current Firefox source documentation likewise describes the pending data directory as holding pending crash reports. These objects are diagnostic evidence, not disposable cache.

Mozilla's current updater troubleshooting documentation explicitly tells administrators to inspect the update binary log, including `%ProgramData%\Mozilla...\updates\*\updates\0\update.log`, when diagnosing update and Maintenance Service failures. No current Mozilla lifecycle establishes DevClean's former 7-day crash-report or 14-day updater-log expiration thresholds.

Primary references:

- https://support.mozilla.org/en-US/kb/mozillacrashreporter
- https://firefox-source-docs.mozilla.org/toolkit/crashreporter/crashreporter/
- https://firefox-source-docs.mozilla.org/toolkit/mozapps/update/docs/Troubleshooting.html
- https://support.mozilla.org/en-US/kb/managing-firefox-updates

## Correction

- `Crash Reports/pending`: KEEP / protected unsubmitted diagnostic evidence.
- Firefox updater `update.log`, elevated/backup/last-update variants: KEEP / protected troubleshooting evidence.
- age, size, process-idle state, learned AI decisions and user decisions cannot restore raw deletion authority.
- crash root no longer contributes an audited whole-tree TOOL root.
- persistent profile state and update payload/state remain protected as before.
- the separately identified Firefox local-profile/cache lanes are unchanged in this PR and remain queued for the rest of the family re-verification.

## Revisit

Only add deletion for these diagnostics if Mozilla publishes an explicit bounded cleanup API/lifecycle or an exact user-requested maintenance operation whose destructive scope can be freshly revalidated.
