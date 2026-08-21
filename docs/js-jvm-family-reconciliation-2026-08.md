# JS and JVM family second-pass reconciliation — 2026-08

## Scope

This pass closes the queued JavaScript-package-manager/test-browser family and JVM family on the current post-#157 `main`. It is a reconciliation pass, not a new policy rewrite: each named product already has a source-audited storage or maintenance contract with regression coverage, and later corrective PRs removed broad raw deletion where vendor lifecycle could not support it.

## JavaScript family

The family is re-verified as the combination of the following current source-backed lanes and protections:

- npm — #134 replaces broad raw cache deletion with exact npm-owned maintenance: `npm cache verify` for vendor GC, explicit USER_REVIEW for full package-cache clear and exact npx-key removal, while `_tuf`, config, prefix and unknown root state stay protected.
- pnpm — #57 hardens `pnpm store prune` as the vendor garbage collector with exact store re-resolution and process guards; the raw content-addressable/global virtual store remains protected. Earlier #11 provides the storage/root/global-install boundary.
- Yarn — #136 removes generic whole-tree cache deletion for both Classic and modern Yarn because effective cache scope and plugin/project behavior are configuration-sensitive. Machine caches remain REPORT_ONLY/protected.
- Bun — #137 removes generic whole-cache deletion because `bun pm cache rm` has broader side effects and the global store can back installed projects. Machine/project caches remain REPORT_ONLY/protected.
- Cypress — #135 exposes only exact reviewed vendor `cache prune` as USER_REVIEW, keeps `cache clear` report-only, and preserves App Data/runtime state. #42 supplies the underlying shared-cache storage boundary.
- Playwright — #40 keeps the shared browser registry REPORT_ONLY/protected because browser liveness is owned by Playwright client tracking and `uninstall --all` is wider than unattended cleanup.
- Puppeteer — #41 keeps the shared browser cache REPORT_ONLY/protected because `trimCache()` explicitly cannot prove liveness across other Puppeteer versions and `clear` is whole-cache destructive.

Cross-family conclusion: no cache-looking directory name, version folder, age, size, or learned rule is sufficient to create raw JavaScript-family deletion authority. Positive mutation paths are vendor commands whose exact executable/root/scope is revalidated; ambiguous or configuration-sensitive stores remain protected/report-only.

## JVM family

The JVM family is re-verified from the following current source-backed results:

- Gradle — #138 removes generic raw deletion for Gradle User Home version caches, build-cache directories and daemon logs. Current retention is configuration- and Gradle-owned; `gradle_audited_tool_roots()` remains empty and the user home stays REPORT_ONLY/protected.
- Maven — #36 keeps the local repository REPORT_ONLY/protected because it mixes remote cache with locally installed artifacts and Maven explicitly warns against plain filesystem manipulation. #71 separately audits project `clean` scope and defers execution because inherited/plugin `filesets` can widen the destructive manifest beyond conventional `target` paths.

Cross-family conclusion: Gradle and Maven storage remains visible without generic raw recursive deletion. Project/build cleanup cannot be inferred from directory names or partial local configuration; execution requires a complete source-proven mutation scope.

## Regression and merge boundary

The cited implementation PRs already carry product-level regression tests for root discovery, KEEP/REPORT_ONLY projection, whole-tree authority, process/configuration guards, learned-rule ceilings, and vendor-maintenance revalidation. This reconciliation intentionally changes no implementation and adds no duplicate policy tests; the final head must still pass the repository-wide lock/dependency checks, Ruff, strict mypy, full pytest/current CI, Windows EXE artifact build, and CodeQL before merge.

## Revisit triggers

Re-open a family only when vendor documentation/source materially changes the cleanup scope, introduces a bounded machine-readable destructive manifest, removes a currently relied-upon safety warning, or changes an executable/cache-root resolution contract. A new cache-like path or age/size heuristic alone is not a revisit reason.