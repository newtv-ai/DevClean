# Chromium browser family second-pass reconciliation — 2026-08

## Scope

This reconciliation closes the requested current-main second pass for Chrome/Chromium, Microsoft Edge, Brave, Vivaldi and Opera by reviewing the already-landed source-first authority corrections as one family rather than reopening each product in another micro-PR.

## Evidence reviewed

- Chrome / Chromium Updater: PR #151 re-checked the current Chromium Updater functional specification. `crx_cache` remains the narrow source-owned download cache; `updater.log` and `updater.log.old` are protected vendor-rotated diagnostic state rather than DevClean-invented age-based delete lanes.
- Microsoft Edge: PR #152 re-checked current Microsoft troubleshooting guidance. `MicrosoftEdgeUpdate.log`, its rotated backup and `msedge_installer.log` are protected support evidence; Chromium-derived browser cache semantics remain narrow and separate.
- Brave: PR #139 audited Brave/Omaha source and removed broad raw deletion authority from updater Install/log state. Download/Offline/version/persistent updater state remain protected; normal Chromium-derived browser cache rules were not widened.
- Vivaldi: PR #140 re-checked Vivaldi crash-report guidance and upstream Crashpad lifecycle, protecting `Crashpad\reports` diagnostic evidence and removing the generic seven-day whole-tree delete rule while leaving Chromium cache semantics unchanged.
- Opera: PR #141 re-checked Opera cache guidance and removed unsupported whole-tree `System Cache` age/size deletion authority while preserving the existing narrow Chromium-derived cache rules and dedicated explicit disk-cache boundary.

## Family conclusion

Across all five Chromium-family products, current `main` now follows the same authority model:

1. exact browser cache locations that are already source-identified remain narrow application-owned cleanup lanes;
2. profile/site/user state remains protected;
3. updater/install/diagnostic objects do not gain raw deletion authority merely from age, size, cache-like naming or process-idle state;
4. product-specific updater/crash subsystems remain vendor-managed unless a bounded destructive lifecycle is explicitly established;
5. learned user/AI rules cannot override application KEEP boundaries or manufacture whole-tree authority.

No additional implementation change is required by this reconciliation. The recent product-specific corrections already landed on `main`; this PR records the family-level second-pass closure and keeps the regression/authority boundaries established by those PRs.

## Revisit triggers

Re-open this family only when a browser/vendor changes its cache, updater, crash-report or profile lifecycle in a way that creates a new supported destructive API/boundary, or when a regression demonstrates that current application KEEP/TOOL provenance can be bypassed.
