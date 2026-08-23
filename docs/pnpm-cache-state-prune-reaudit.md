# pnpm cache, state, and prune authority re-audit — 2026-08

## Why this row was reopened

The earlier pnpm audit correctly protected the content-addressable store and
global installations from raw deletion, and later added `pnpm store prune` as a
vendor maintenance action. The second-pass audit found three stale assumptions:

1. DevClean treated the whole `<cache>/dlx` tree as a one-day TOOL root even
   though pnpm expires individual DLX entries using its own configured
   `dlx-cache-max-age` and each entry's `pkg` symlink mtime.
2. DevClean treated top-level `metadata-v*` directories as 14-day TOOL cache.
   Current pnpm 11 instead stores metadata under `vN/metadata*`, supports offline
   resolution from those mirrors, and exposes dedicated `pnpm cache` commands.
3. The existing `pnpm store prune` wrapper described the command as selected
   store-only, but current pnpm also expires DLX entries and cleans orphaned
   global install directories in the same command.

The exact `pnpm-state.json` rule had the opposite problem: source proves it is a
regenerable update-check throttle, but the old 1 MiB benefit gate meant this tiny
file was effectively never a deterministic candidate.

## Audited upstream snapshot

Primary source was rechecked against pnpm commit:

`ff9ab71d9d181664f2626a22f266172aa883cf9c`

Relevant source files:

- `pnpm11/store/commands/src/store/cleanExpiredDlxCache.ts`
- `pnpm11/store/commands/src/store/storePrune.ts`
- `pnpm11/global/packages/src/scanGlobalPackages.ts`
- `pnpm11/config/reader/src/index.ts`
- `pnpm11/config/reader/src/env.ts`
- `pnpm11/config/reader/src/types.ts`
- `pnpm11/core/constants/src/index.ts`
- `pnpm11/resolving/npm-resolver/src/fetchFullMetadataCached.ts`
- `pnpm11/cache/commands/src/cache.cmd.ts`
- `pnpm11/pnpm/src/checkForUpdates.ts`

Pinned source URLs:

- https://github.com/pnpm/pnpm/blob/ff9ab71d9d181664f2626a22f266172aa883cf9c/pnpm11/store/commands/src/store/cleanExpiredDlxCache.ts
- https://github.com/pnpm/pnpm/blob/ff9ab71d9d181664f2626a22f266172aa883cf9c/pnpm11/store/commands/src/store/storePrune.ts
- https://github.com/pnpm/pnpm/blob/ff9ab71d9d181664f2626a22f266172aa883cf9c/pnpm11/global/packages/src/scanGlobalPackages.ts
- https://github.com/pnpm/pnpm/blob/ff9ab71d9d181664f2626a22f266172aa883cf9c/pnpm11/config/reader/src/index.ts
- https://github.com/pnpm/pnpm/blob/ff9ab71d9d181664f2626a22f266172aa883cf9c/pnpm11/config/reader/src/env.ts
- https://github.com/pnpm/pnpm/blob/ff9ab71d9d181664f2626a22f266172aa883cf9c/pnpm11/config/reader/src/types.ts
- https://github.com/pnpm/pnpm/blob/ff9ab71d9d181664f2626a22f266172aa883cf9c/pnpm11/core/constants/src/index.ts
- https://github.com/pnpm/pnpm/blob/ff9ab71d9d181664f2626a22f266172aa883cf9c/pnpm11/resolving/npm-resolver/src/fetchFullMetadataCached.ts
- https://github.com/pnpm/pnpm/blob/ff9ab71d9d181664f2626a22f266172aa883cf9c/pnpm11/cache/commands/src/cache.cmd.ts
- https://github.com/pnpm/pnpm/blob/ff9ab71d9d181664f2626a22f266172aa883cf9c/pnpm11/pnpm/src/checkForUpdates.ts

## DLX cache: USER_REVIEW plus vendor expiry, not raw TOOL tree

Current pnpm's `cleanExpiredDlxCache` targets `<cacheDir>/dlx`, enumerates each
entry, reads the `pkg` symlink with `lstat`, and compares that entry mtime with
the configured `dlxCacheMaxAge`. Only expired entries are recursively removed.
The same function also removes broken/orphan state inside DLX entries.

`dlx-cache-max-age` is a configurable Number. Current default is one day, but
that default is not a universal filesystem lifecycle contract for DevClean.

Deleting the entire DLX root is technically recoverable but discards reusable
execution environments and can require packages to be fetched/installed again.
That full-retention tradeoff belongs to the user. Current generic semantics are:

- `<cache>/dlx/...`: **USER_REVIEW**;
- no raw deterministic whole-tree authority;
- age, size, and directory mtime do not manufacture TOOL authority;
- user-approved raw mutation still requires pnpm to be idle;
- deterministic expiry remains delegated to pnpm's own lifecycle.

## Metadata mirrors: USER_REVIEW and current-layout recognition

Current pnpm 11 constants use:

- `vN/metadata`;
- `vN/metadata-full`;
- `vN/metadata-full-filtered`.

The resolver can satisfy `offline: true` only from the on-disk metadata mirror;
if required metadata is absent it raises the offline-metadata error instead of
contacting the registry. These mirrors are therefore regenerable online but can
have deliberate offline value.

Current pnpm also exposes vendor metadata-cache operations through `pnpm cache`,
including list/view/path/delete. DevClean should not replace that object model
with an invented 14-day recursive deletion rule.

Current generic semantics are therefore:

- current `vN/metadata*` mirrors: **USER_REVIEW**;
- legacy top-level `metadata-v*` mirrors remain recognized as USER for migration
  and older-install compatibility;
- similar names such as `metadata-backup` remain unclassified/KEEP;
- no metadata directory receives generic whole-tree TOOL authority;
- future package-level maintenance should prefer pnpm's own cache commands.

## Exact update-check state: deterministic TOOL

Current `checkForUpdates.ts` stores only update-check throttle state in the exact
`<stateDir>/pnpm-state.json` path. Read failure or absence is tolerated; pnpm can
query again and rewrite the state file. The source-defined check frequency is a
performance/network concern, not a deletion-safety boundary.

Therefore:

- exact `pnpm-state.json`: **DETERMINISTIC_CANDIDATE / TOOL** when pnpm is idle;
- fresh, tiny, old, or unknown-last-use metadata does not revoke that authority;
- active pnpm remains a hard execution blocker;
- the surrounding state directory stays KEEP;
- no directory-level authority is inferred from the exact file rule.

## `pnpm store prune`: bound the real vendor mutation scope

Current `storePrune` performs three operations in order:

1. store-controller prune;
2. `cleanExpiredDlxCache(cacheDir, dlxCacheMaxAge)`;
3. `cleanOrphanedInstallDirs(globalPkgDir)` when `globalPkgDir` is set.

The configuration reader derives `globalPkgDir` from `globalDir` even for normal
configuration. The global cleanup scans that directory and removes directories
that no symlink points to, with a five-minute safety window. This is pnpm-owned
GC, but it disproves the old DevClean claim that `store prune` mutates only the
selected store.

DevClean now narrows the command back to the reviewed user-side scope before
execution:

1. create a private temporary sandbox;
2. remove conflicting pnpm environment overrides for the secondary scopes;
3. pin `PNPM_CONFIG_CACHE_DIR` to `<sandbox>/cache`;
4. pin `PNPM_CONFIG_GLOBAL_DIR` to `<sandbox>/global`;
5. pin `PNPM_CONFIG_DLX_CACHE_MAX_AGE=Infinity`;
6. ask the selected pnpm binary to report all three effective values and fail
   closed unless they exactly match the pins;
7. ask the same command scope to confirm the exact selected store;
8. recheck pnpm process state and stable store filesystem identity;
9. confirm the store again immediately before mutation;
10. run exactly `pnpm --store-dir <reviewed-store> store prune` with the same
    verified pinned environment;
11. require the store root identity to remain unchanged and report logical
    before/after evidence.

Current pnpm's environment parser accepts uppercase `PNPM_CONFIG_*`, parses
`dlx-cache-max-age` as Number, and `Number("Infinity")` yields the Infinity value
that `cleanExpiredDlxCache` explicitly treats as an immediate no-op. Pinning the
cache/global paths is defense in depth: even if another current vendor step
consults those configured locations, it sees only DevClean-owned temporary
state.

The selected store itself must be on a local fixed path, must not be a
reparse/cloud boundary, and must expose a stable volume-scoped file identity.

## Retained protection

No new authority is granted to:

- raw-delete pnpm store internals;
- raw-delete all DLX environments automatically;
- raw-delete metadata mirrors automatically;
- global packages or executable shims;
- `PNPM_HOME` persistent state;
- `pnpm-lock.yaml` or `pnpm-workspace.yaml`;
- unclassified cache/state files;
- infer cleanup from a `cache`, `metadata`, `state`, or version-like name alone.

AI and generic learned DELETE rules must not bypass the new USER/KEEP lanes.

## Validation gate

The final PR head must pass the normal DevClean gate before merge:

- lock/dependency checks;
- Ruff;
- strict mypy;
- full pytest/current workflow;
- Windows EXE build/upload;
- CodeQL.
