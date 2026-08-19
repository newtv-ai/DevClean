# WSL pnpm store authority audit

## Decision

`pnpm store prune` inside one exact WSL distribution is a **DETERMINISTIC_CANDIDATE** vendor garbage-collection operation.

The pnpm store remains pnpm-owned. DevClean never grants raw deletion authority to the store directory or its internal package layout.

## Source-backed semantics

Current pnpm documentation states that `pnpm store path` returns the active store directory and `pnpm store prune` removes packages not referenced by projects on the system. A removed package can be downloaded again later. pnpm recommends occasional rather than overly frequent pruning because old branches or dependency sets may need those packages again.

Primary sources:

- https://pnpm.io/cli/store
- https://pnpm.io/settings

## Active path versus store-dir

`pnpm store path` may report a versioned active path such as `/home/user/.local/share/pnpm/store/v11`, while `--store-dir` scopes the containing store directory. DevClean records both and strips only a final `v<digits>` component when deriving the store-dir.

Immediately before mutation DevClean runs `pnpm --store-dir <store-dir> store path --silent` and requires the result to match the same exact active store path.

## Local-storage authority

A vendor-owned path inside WSL does not automatically prove that the underlying storage belongs to the distro. pnpm can be configured onto another mounted filesystem.

Therefore both the exact active store path and the derived store-dir must pass `require_wsl_root_filesystem_path` immediately before mutation. This follows symlinks and requires their POSIX device identity to match the selected distribution's `/` filesystem. A store on `/mnt/c`, a network mount, removable storage, or any other separate mount remains reportable but non-executable.

## Execution contract

DevClean must:

1. require an exact distribution returned by WSL inventory;
2. ask pnpm for `--version` and `store path --silent`;
3. require an absolute, non-root POSIX store path;
4. bind distro, pnpm version, active store path, and store-dir as mutation identity;
5. repeat that inventory immediately before mutation;
6. fail closed unless no pnpm activity is visible;
7. prove both active store and store-dir are on the distro root filesystem;
8. pin `--store-dir` and require scoped `store path --silent` to resolve to the same active store;
9. execute only `pnpm --store-dir <store-dir> store prune`;
10. re-inventory afterward and refuse to claim a confirmed result if identity changed.

## Deliberate non-features

DevClean does not inspect store internals for reachability, raw-delete any store subtree, remove PNPM_HOME/global installs/state, use shell or Windows-side deletion fallbacks, auto-run based on guessed size, use AI for pnpm's own reachability decision, or promise that freed WSL logical bytes equal Windows VHD shrinkage.

## Product behavior

The user selects one registered WSL distribution and explicitly starts the operation. If the distro is stopped, probing pnpm warns that WSL command execution may start it. The operation stays deterministic because pnpm owns project registration and garbage collection, while DevClean independently enforces local-storage scope before granting mutation authority.
