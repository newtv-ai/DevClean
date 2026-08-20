# Cypress binary cache maintenance audit

Audited/implemented: 2026-08-20

## Product conclusion

Cypress exposes a documented binary-cache lifecycle, but its cache is shared across projects. An older cached Cypress version can therefore still be valuable to another project even though it is redownloadable.

DevClean's narrow lanes are:

- `cypress cache prune`: **USER_REVIEW** on one exact reviewed Cypress CLI/cache root;
- `cypress cache clear`: **REPORT_ONLY / deliberately not exposed** in this pass;
- `%APPDATA%\Cypress` application state: protected;
- `CYPRESS_RUN_BINARY`: protected user-selected runtime;
- raw `<cache>/<version>` or whole-cache filesystem deletion: not exposed.

The existing generic scanner remains report-only for Cypress binary cache. The executable lane lives only in the dedicated maintenance tool.

## Primary source

Audited against Cypress repository commit:

`b4ad58f5275ea0233bb114cb66c85f41f5ab6a8b`

Primary files:

- `cli/lib/tasks/cache.ts`
- `cli/lib/tasks/state.ts`
- `cli/lib/cli.ts`
- `packages/cypress-sessions/lib/index.ts`

Source URLs:

- https://github.com/cypress-io/cypress/blob/b4ad58f5275ea0233bb114cb66c85f41f5ab6a8b/cli/lib/tasks/cache.ts
- https://github.com/cypress-io/cypress/blob/b4ad58f5275ea0233bb114cb66c85f41f5ab6a8b/cli/lib/tasks/state.ts
- https://github.com/cypress-io/cypress/blob/b4ad58f5275ea0233bb114cb66c85f41f5ab6a8b/cli/lib/cli.ts
- https://github.com/cypress-io/cypress/blob/b4ad58f5275ea0233bb114cb66c85f41f5ab6a8b/packages/cypress-sessions/lib/index.ts

Current Cypress documentation also exposes `cypress cache list`, `cache path`, `cache clear`, `cache prune`, and `cache list --size`, and documents the Windows default under the user's local application-data Cypress cache unless `CYPRESS_CACHE_FOLDER` redirects it.

## Exact cache-root authority

Cypress source resolves the cache through `state.getCacheDir()`. `CYPRESS_CACHE_FOLDER` can be absolute or relative to the Cypress invocation working directory, and postinstall has additional relative-path handling.

DevClean therefore does not reconstruct mutation authority from `%LOCALAPPDATA%\Cypress\Cache`, npm configuration, or a directory name. For the executable lane it:

1. resolves or lets the user select one exact installed Cypress CLI;
2. binds that CLI file to stable local-fixed filesystem identity;
3. asks that exact CLI for `cypress cache path`;
4. requires an absolute returned path;
5. pins later commands with `CYPRESS_CACHE_FOLDER=<exact-reviewed-root>` while removing inherited alternate Cypress cache variables;
6. asks the same CLI for `cache path` again and requires an exact match;
7. binds an existing cache root to stable local-fixed directory identity.

DevClean never calls `npx cypress`, because doing so could introduce package resolution/download behavior instead of operating on one already reviewed CLI installation.

## Why prune is USER_REVIEW, not deterministic cleanup

Current Cypress source implements `cache prune` by reading the shared cache root and deleting every top-level entry except:

- the CLI package's own `util.pkgVersion()` entry;
- `bundles`;
- the `sessions` directory.

It then calls Cypress's own dead-session-record pruning.

This is a supported vendor lifecycle, but “keep the selected CLI package version” is not equivalent to “all other projects no longer need their Cypress versions.” A separate project can still depend on an older Cypress release and will need to download it again after prune.

That rebuild/download tradeoff is user-specific, so DevClean classifies the operation as **USER_REVIEW**. No old version is preselected or deleted because of age, last-access time, or size.

The dialog explicitly shows:

- the selected CLI package version;
- every recognized semver binary-cache version;
- which version matches the selected CLI package;
- logical bytes per recognized version;
- the exact versions that Cypress prune is expected to remove.

## Source-shape fail-closed guard

Current Cypress source reserves two non-binary top-level cache entries: `bundles` and `sessions`. `cache prune` skips them, but otherwise removes non-current top-level entries without first requiring them to be semver directories.

DevClean adds a stricter compatibility guard. Before offering prune, every other top-level object must be an ordinary local semver directory. If the cache root contains a future/unknown top-level object, a reparse/junction entry, an unstable directory, or another shape not covered by the audited source, prune is disabled.

This guard protects against a future Cypress release adding another cache-root object that the audited `prune` implementation did not know about. The user can refresh after upgrading DevClean to a source audit that understands the new shape.

## Why `cache clear` is not exposed

The public command description sounds like binary-cache clearing, but the audited source implementation is simply:

`fs.remove(state.getCacheDir())`

The same current cache root can also contain `bundles` and `sessions`, and `cache prune` explicitly treats them as external/non-binary entries. Whole-root clear therefore has a broader mutation surface than “remove old binary versions.”

DevClean does not add a `cache clear` button merely because the command exists. A future clear lane would need a product decision that explicitly accepts removing all neighboring Cypress cache-root state and should prove that no additional future root objects have appeared.

## Review/revalidation sequence

Before `cache prune`, DevClean requires:

1. exact CLI identity unchanged;
2. exact package version unchanged;
3. exact cache path and root identity unchanged;
4. exact recognized binary-version identities, logical bytes and file counts unchanged;
5. exact external/unknown top-level name sets unchanged;
6. no unknown top-level object;
7. at least one non-current cached binary version;
8. no active Cypress application or obvious Cypress Node CLI process;
9. a second complete inventory immediately before mutation;
10. a second process-idle check immediately before mutation.

It then runs only:

`<exact-cypress-cli> cache prune`

with the reviewed cache root pinned in the environment.

After success DevClean inventories again, requires the exact CLI/root/package boundaries to remain bound, rejects any newly unknown root object, and requires every reviewed non-current binary version to be absent. It also refuses to report success if another non-current version remains.

## Process and execution boundary

DevClean does not invoke project `package.json` scripts, Cypress configuration files, test specs, `hubconf`-style code, or `npx` package resolution. The selected Cypress CLI is treated as the vendor maintenance executable and is run only with fixed argv created by DevClean.

On Windows, mutation fails closed if an active `Cypress.exe` or an obvious Cypress Node CLI process is visible, or if process state cannot be queried safely.

## Accounting

Logical size is measured only for recognized binary-version directories and traversal does not intentionally follow symlink/junction boundaries. It is explanatory before/after evidence, not a guarantee that Windows physical free space increases by the same number of bytes.

`bundles`, `sessions`, application data, external runtime binaries and unknown root entries are not included in “prune candidate bytes.”

## Deliberate exclusions

No authority is granted to:

- delete a cached version because it is old, large, or apparently unused;
- directly remove `<cache>/<version>` folders;
- recursively delete the Cypress cache root;
- expose `cypress cache clear` in this pass;
- touch `%APPDATA%\Cypress` persistent application state;
- delete `CYPRESS_RUN_BINARY`;
- run `npx cypress` or install Cypress merely to gain cleanup authority;
- continue pruning when the root contains an unknown future object;
- treat the selected CLI package version as proof that all projects use that same version;
- claim logical binary size as guaranteed physical reclaimed space.

## Revisit conditions

Revisit whole-cache clear only if a later Cypress source contract separates binary cache from neighboring root state or DevClean deliberately audits all whole-root side effects. Revisit exact per-version deletion only if Cypress exposes a stable object-level remove operation or equivalent exact machine-readable lifecycle API.

## Validation

Normal DevClean final-head validation remains mandatory: lock/dependency checks, Ruff, strict mypy, full pytest/current workflow, Windows EXE artifact and CodeQL must all be green before merge.
