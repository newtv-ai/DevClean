# Firefox storage authority re-audit — 2026-08

## Scope

This pass re-verifies the Firefox Windows storage family against current Mozilla support/source documentation and closes two unsupported diagnostic deletion lanes.

## Current source conclusions

Mozilla documents the Windows Firefox profile as two distinct locations: the Roaming profile contains persistent user data, while the Local profile contains the disk cache and other temporary files. DevClean therefore retains its existing fail-closed persistent-profile boundary and exact Local-profile/cache authority, with Firefox required to be closed for whole-tree cleanup. Custom/co-located profiles never gain whole-profile authority; only exact known cache children can be delegated.

Mozilla's current crash-reporting help states that `Crash Reports\pending` contains unsubmitted crash reports and Firefox can prompt the user to submit recent reports. Mozilla troubleshooting guidance explicitly asks users to submit recent unsubmitted reports when diagnosing crashes. These objects are diagnostic evidence, not source-proven disposable cache. The old `7 days + 1 MiB` raw-delete rule was DevClean-invented and is removed.

Mozilla's current updater troubleshooting documentation explicitly asks users to collect `updates\0\update.log`, `update-elevated.log`, `last-update*.log` and `backup-update*.log` when diagnosing update failures, and describes Firefox moving/rotating those logs as updates are analyzed. It does not define DevClean's old `14 days + 256 KiB` deletion lifecycle. Those logs are therefore protected diagnostic state.

## Product decision

- Roaming profile, profile registry/group state, update payload/state: KEEP / protected.
- Default/MSIX Local profile: existing source-identified temporary/cache TOOL lane retained; no widening.
- Exact `cache2`, `startupCache` and `jumpListCache` children in custom/co-located profiles: existing narrow cache lanes retained.
- `Crash Reports\pending`: KEEP / protected diagnostic evidence.
- Firefox updater logs: KEEP / protected update diagnostics.
- Age, size, process-idle state, AI decisions and user learned rules cannot recreate authority for the protected diagnostics.

## Primary references

- Mozilla Support, “Unsent crash reports in Firefox” (updated 2026-01-15).
- Mozilla Support, “Troubleshoot Firefox crashes (closing or quitting unexpectedly)”.
- Firefox Source Docs, “Update Troubleshooting”, Update Binary Logs.
- Mozilla Support profile guidance distinguishing the Roaming user-data profile from the Local disk-cache/temporary profile.

## Revisit trigger

Only add a destructive diagnostic lane if Mozilla documents a bounded deletion API/lifecycle for the exact crash/update objects. Age, size or apparent regenerability alone are not lifecycle authority.
