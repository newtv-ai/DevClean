# Bun cache authority audit

Audited: 2026-08-20

## Product conclusion

DevClean's former age/size-driven raw whole-tree deletion authority for Bun's machine package cache is removed.

Current lanes:

- documented/default or `BUN_INSTALL_CACHE_DIR` machine cache: **REPORT_ONLY / protected from generic raw deletion**;
- project-local/custom `.bun/cache`: user/offline state, protected from generic cleanup;
- Bun home, runtime, global installations and project metadata: protected;
- no generic Bun whole-tree delete root.

A future dedicated vendor action is plausible, but current `bun pm cache rm` must be audited as a broader **USER_REVIEW** lifecycle rather than used to justify silent raw folder deletion.

## Primary source

Audited against `oven-sh/bun` commit:

`34cbb9a40b4bd1bd767d134a7065e66c2432a676`

Primary files/docs:

- `docs/pm/cli/pm.mdx`
- `docs/pm/global-cache.mdx`
- `docs/pm/global-store.mdx`
- `src/runtime/cli/package_manager_command.rs`
- `src/install/PackageManager/PackageManagerDirectories.rs`
- `test/cli/install/bun-pm.test.ts`

Source URLs:

- https://github.com/oven-sh/bun/blob/34cbb9a40b4bd1bd767d134a7065e66c2432a676/docs/pm/cli/pm.mdx
- https://github.com/oven-sh/bun/blob/34cbb9a40b4bd1bd767d134a7065e66c2432a676/docs/pm/global-cache.mdx
- https://github.com/oven-sh/bun/blob/34cbb9a40b4bd1bd767d134a7065e66c2432a676/docs/pm/global-store.mdx
- https://github.com/oven-sh/bun/blob/34cbb9a40b4bd1bd767d134a7065e66c2432a676/src/runtime/cli/package_manager_command.rs
- https://github.com/oven-sh/bun/blob/34cbb9a40b4bd1bd767d134a7065e66c2432a676/src/install/PackageManager/PackageManagerDirectories.rs
- https://github.com/oven-sh/bun/blob/34cbb9a40b4bd1bd767d134a7065e66c2432a676/test/cli/install/bun-pm.test.ts

## Cache identity

Bun documents a global package cache under `~/.bun/install/cache` by default. `BUN_INSTALL_CACHE_DIR` can override it, and project Bun configuration can also change package-manager cache behavior.

Current source `fetch_cache_directory_path()` resolves cache paths in this order when package-manager options are available:

1. `BUN_INSTALL_CACHE_DIR`;
2. package-manager option/configured cache directory;
3. `BUN_INSTALL/install/cache`;
4. XDG/HOME default;
5. a project-local fallback.

This already means a generic path convention cannot reproduce the complete effective configuration model.

The former scanner still identifies the documented default and an explicit `BUN_INSTALL_CACHE_DIR` for explanation, but it no longer executes Bun automatically to discover another destructive root. Executing a project-sensitive package-manager command merely to widen scanner delete authority would violate DevClean's current policy.

## Why `bun pm cache rm` is broader than a raw package-cache rule

Current `bun pm cache rm` source does two important things:

1. deletes the resolved Bun install cache tree;
2. scans the platform temporary directory and deletes matching current-user Bunx temporary cache directories (`bunx-<user-id>-*`).

So the vendor lifecycle is not equivalent to “delete exactly the scanner's package-cache folder.” The old DevClean whole-tree rule neither represented nor reviewed the Bunx side effect.

For the deletion branch, current source resolves the target with `fetch_cache_directory_path(process_env, None)`. Passing `BUN_INSTALL_CACHE_DIR` can therefore be a useful future pinning mechanism, but that does not erase the separate Bunx-temp side effect or make automatic cleanup appropriate.

## Global-store hazard

Bun's current global-store documentation is especially important. With `linker = "isolated"` and `globalStore = true`, Bun keeps a global virtual store under the package cache and project `node_modules` can symlink directly into that shared store.

The documentation explicitly says `bun pm cache rm` clears the cache, including the global store, and that the next `bun install` repopulates what the project needs.

Therefore the cache can be more than a passive download optimization. Removing it may leave project symlink targets absent until reinstall. That is a clear USER_REVIEW tradeoff and directly contradicts the old assumption that a 30-day-old, sufficiently large cache can become an automatic TOOL_DELETE candidate.

## Hardlink/accounting nuance

Bun's package cache may use hardlinks when materializing packages into project `node_modules`. Deleting a cache path therefore does not imply that the same number of logical bytes will become free on Windows. Other hardlinks can continue to own the underlying file content.

Any future Bun maintenance UI must report logical cache bytes only as explanatory evidence and must not promise equal physical disk reclaim.

## Why generic direct deletion is removed now

The former rule granted whole-tree raw deletion after a 30-day age threshold and a 64 MiB minimum size. That fails the current execution standard:

- age/size are benefits, not mutation authority;
- vendor lifecycle has an additional Bunx-temp side effect;
- global-store data can be active symlink backing for installed projects;
- effective cache location can be configuration-sensitive;
- raw deletion bypasses the vendor lifecycle and provides no exact postcondition for its wider semantics.

The correct interim state is REPORT_ONLY, not another heuristic fallback.

## Project-local and persistent state

No authority is granted to:

- arbitrary project `.bun/cache` directories;
- `bun.lock`, `bun.lockb`, or `bunfig.toml`;
- `~/.bun/install/global` global packages;
- Bun runtime binaries/shims;
- global store content by directory name;
- Bunx temporary caches by prefix alone;
- another path merely because it contains `bun`, `cache`, or package-like names.

## Revisit conditions

A future positive Bun lane should be a dedicated **USER_REVIEW vendor operation**, not restoration of generic whole-tree deletion. It should require at least:

1. one exact Bun executable with stable local identity;
2. one exact cache root obtained from a source-backed configuration context;
3. `BUN_INSTALL_CACHE_DIR` pinned to that reviewed root for the destructive command;
4. explicit review of the fact that the global store may back installed project symlinks and may require `bun install` afterward;
5. complete handling of the vendor command's Bunx temporary-cache side effect, either by exact source-backed inventory/review or by a source-proven way to prevent scope widening;
6. active-Bun process guards;
7. fresh root/tool/config revalidation immediately before mutation;
8. postcondition that the reviewed package cache lifecycle completed as expected;
9. logical/physical reclaim wording that accounts for hardlinks.

Until those conditions are implemented together, Bun machine cache remains visible but non-executable.

## Validation

This PR removes generic whole-tree Bun authority and adds regression tests that age, size, process state, AI verdicts and user verdicts cannot restore raw cache deletion. Normal final-head gate remains mandatory: lock/dependency checks, Ruff, strict mypy, full pytest/current workflow, Windows EXE artifact and CodeQL must all be green before merge.
