# pnpm store prune audit

Audited: 2026-08-18  
Mutation scope re-audited: 2026-08-23

## Product conclusion

`pnpm store prune` belongs in DevClean's deterministic vendor-maintenance lane,
but current pnpm makes the command broader than the original audit recorded.
The store garbage collector itself is still the right authority for deciding
which package-store content is unreferenced; DevClean must not inspect or delete
store internals directly.

However, current pnpm also performs DLX expiry and orphaned global-install
cleanup from the same `store prune` command. The command can therefore remain a
deterministic DevClean action only after those secondary mutation scopes are
explicitly isolated from real user data and the exact pnpm executable is bound
to a stable filesystem identity.

The detailed current-source reconciliation is recorded in
`docs/pnpm-cache-state-prune-reaudit.md`.

## Benefit policy

A discovered store at or above 1 GiB is selected by default. Smaller stores
remain understood and can still be pruned manually.

The 1 GiB threshold is only a benefit threshold. It is not a safety threshold.
pnpm recommends running store prune occasionally, but not too frequently,
because packages that are currently unreferenced can become useful again after
switching branches or restoring older dependencies, which would cause a
re-download.

## Current vendor mutation scope

Current pnpm `storePrune` performs three operations:

1. store-controller prune;
2. expired-DLX cleanup using the effective `cacheDir` and
   `dlxCacheMaxAge`;
3. orphaned global-install-directory cleanup when `globalPkgDir` is present.

The current configuration reader derives `globalPkgDir` from `globalDir`, so the
third step is part of ordinary command scope rather than an exceptional mode.
The original DevClean audit did not account for these two secondary scopes.

That does **not** make raw store deletion preferable. It means the vendor command
must be constrained before DevClean can use it.

## Execution contract

Before any mutation DevClean now:

1. collapses a versioned active path such as `store/v11` to its configured
   store root;
2. re-runs DevClean's pnpm root discovery and requires the selected root to be
   one of the current audited store roots;
3. requires that store to be a normal local-fixed directory with stable
   volume/file identity and rejects reparse/cloud boundaries;
4. resolves one exact pnpm executable, requires a normal local-fixed
   non-reparse/non-cloud file, and captures its stable volume/file identity;
5. refuses while pnpm is active;
6. creates a private temporary sandbox;
7. removes inherited pnpm overrides for the secondary cleanup roots, then pins:
   - `PNPM_CONFIG_CACHE_DIR=<sandbox>/cache`;
   - `PNPM_CONFIG_GLOBAL_DIR=<sandbox>/global`;
   - `PNPM_CONFIG_DLX_CACHE_MAX_AGE=Infinity`;
8. asks that exact pnpm binary to report all three effective values and fails
   closed unless they equal the pins;
9. invokes the same exact binary with the selected `--store-dir` and asks
   `pnpm store path --silent` to confirm the store;
10. immediately before mutation rechecks pnpm process state, pnpm executable
    identity, stable store identity, and vendor-reported store path;
11. runs only `pnpm --store-dir <root> store prune` with the same verified pinned
    environment and exact executable path;
12. requires both executable and store-root identities to remain unchanged
    afterward and reports logical before/after evidence;
13. reports vendor errors and never falls back to deleting store files directly.

Current pnpm parses lowercase/uppercase `pnpm_config_*`/`PNPM_CONFIG_*`
configuration from the environment, `dlx-cache-max-age` is a Number setting,
and the current DLX cleaner explicitly returns without mutation when the
effective value is `Infinity`. Pinning `cache-dir` and `global-dir` into the
private sandbox additionally prevents the same command from touching the user's
real DLX or global-install roots.

The raw pnpm store remains protected by generic application cleanup rules. Only
the source-bounded vendor garbage collector has store mutation authority.

## Multiple stores

pnpm can maintain stores on different disks. DevClean inventories each
discovered store independently and validates the selected store again at
execution time. It never assumes one hard-coded user-level store is authoritative
for every machine.

## Sources

Current implementation is reconciled against pnpm source commit
`ff9ab71d9d181664f2626a22f266172aa883cf9c`, especially:

- `pnpm11/store/commands/src/store/storePrune.ts` — current composite prune
  operation;
- `pnpm11/store/commands/src/store/cleanExpiredDlxCache.ts` — DLX expiry and the
  Infinity no-op;
- `pnpm11/global/packages/src/scanGlobalPackages.ts` — orphaned global install
  directory cleanup;
- `pnpm11/config/reader/src/index.ts` — `globalPkgDir` derivation;
- `pnpm11/config/reader/src/env.ts` and `types.ts` — environment/config parsing.

Pinned source URLs and the broader pnpm cache/state conclusions are in
`docs/pnpm-cache-state-prune-reaudit.md`.

The original pnpm documentation conclusions remain useful for the store-GC
portion: `store prune` removes unreferenced store packages, future installs can
re-download removed packages, occasional pruning is preferred over overly
frequent pruning, and `store path` reports the active store path. Those facts no
longer justify describing the complete command as store-only.
