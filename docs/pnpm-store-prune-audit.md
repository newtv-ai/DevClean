# pnpm store prune audit

Audited: 2026-08-18

## Product conclusion

`pnpm store prune` belongs in DevClean's deterministic local-cleanup lane. It does not need AI and it does not need the user to understand pnpm's content-addressable store internals.

The reason is stronger than a generic cache heuristic: pnpm itself identifies packages that are no longer referenced by any project on the system and removes only those packages. The pnpm documentation explicitly says the operation is not harmful and has no side effects on projects. If a removed package is needed by a future installation, pnpm downloads it again.

DevClean therefore treats this as vendor-owned garbage collection rather than raw filesystem cleanup.

## Benefit policy

A discovered store at or above 1 GiB is selected by default. Smaller stores remain understood and can still be pruned manually.

The 1 GiB threshold is only a benefit threshold. It is not a safety threshold. pnpm recommends running store prune occasionally, but not too frequently, because packages that are currently unreferenced can become useful again after switching branches or restoring older dependencies, which would cause a re-download.

## Execution contract

Before any mutation DevClean:

1. collapses a versioned active path such as `store/v11` to its configured store root;
2. re-runs DevClean's pnpm root discovery and requires the selected root to be one of the current audited store roots;
3. refuses while pnpm is active;
4. invokes pnpm with the exact selected `--store-dir` and asks `pnpm store path --silent` to confirm the active store;
5. requires the vendor-reported store to resolve back to the same selected root;
6. only then runs `pnpm --store-dir <root> store prune`;
7. reports vendor errors and never falls back to deleting store files directly.

The raw pnpm store remains protected by the generic application cleanup rules. Only the vendor garbage collector has mutation authority.

## Multiple stores

pnpm can maintain stores on different disks. DevClean inventories each discovered store independently and validates the selected store again at execution time. It never assumes one hard-coded user-level store is authoritative for every machine.

## Sources

- pnpm documentation, **pnpm store**: `store prune` removes unreferenced packages; unreferenced means packages not used by any projects on the system; the operation is documented as non-harmful with no project side effects; future installs re-download removed packages; pnpm recommends occasional rather than overly frequent pruning; `store path` returns the active store path.
- pnpm documentation, **Store & Lockfile Settings — storeDir**: documents the store location setting, per-disk store behavior, and the role of the store as pnpm's package storage/trust domain.
