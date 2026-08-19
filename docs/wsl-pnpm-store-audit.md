# WSL pnpm store authority audit

## Decision

`pnpm store prune` inside one exact WSL distribution is a
**DETERMINISTIC_CANDIDATE** vendor garbage-collection operation.

The pnpm store remains pnpm-owned. DevClean never grants raw deletion authority
to the store directory or its internal package layout.

## Source-backed semantics

Current pnpm documentation states that:

- `pnpm store path` returns the active store directory;
- `pnpm store prune` removes packages that are not referenced by any projects on
  the system;
- running `pnpm store prune` is not harmful and has no side effects on projects;
- if a future install needs a removed package, pnpm downloads it again;
- pnpm recommends running prune occasionally, but not too frequently, because
  branch changes or older dependency sets can make previously unused packages
  useful again;
- when the global virtual store is enabled, the same vendor operation also
  garbage-collects its unused links using pnpm's own project registration data.

Primary sources:

- https://pnpm.io/cli/store
- https://pnpm.io/settings

The settings reference also makes clear that pnpm configuration can affect
`storeDir`, including project/global configuration. Therefore DevClean asks pnpm
for the active path instead of guessing a Linux default.

## Versioned active path versus configured store-dir

`pnpm store path` may report a versioned active store such as
`/home/user/.local/share/pnpm/store/v11`, while the `--store-dir` option is scoped
to the containing store directory.

DevClean therefore records both:

- the exact active store path reported by pnpm;
- the corresponding store-dir obtained only by stripping a final `v<digits>`
  component when present.

Immediately before mutation DevClean runs a scoped
`pnpm --store-dir <store-dir> store path --silent` and requires pnpm to resolve
back to the same exact active store path. If it does not, pruning stops.

## Execution contract

DevClean must:

1. require an exact distribution returned by WSL inventory;
2. run only the code-defined `pnpm` executable through the WSL argv boundary;
3. ask pnpm for `--version` and `store path --silent`;
4. require the store path to be one absolute, non-root POSIX path;
5. bind distribution, pnpm version, exact active store path, and store-dir as the
   mutation identity;
6. repeat the full inventory immediately before mutation;
7. fail closed unless the distro can provide a process snapshot and no pnpm
   operation is visible;
8. re-confirm the exact active path under the pinned `--store-dir`;
9. execute only `pnpm --store-dir <store-dir> store prune`;
10. re-inventory afterward and refuse to claim a confirmed result if the
    identity changed.

## Deliberate non-features

DevClean does **not**:

- recursively inspect pnpm store internals to decide which packages are unused;
- delete the store directory or a versioned `v*` subtree directly;
- remove PNPM_HOME, global installs, global bins, state, or arbitrary cache roots
  through this lane;
- decide package reachability itself;
- run a shell, `rm`, `find`, or Windows-side deletion as fallback;
- auto-run prune based on guessed directory size;
- use AI to decide whether pnpm's own unreferenced-store GC is safe;
- claim that WSL logical space released equals Windows VHD file shrinkage.

## Product behavior

The user selects one registered WSL distribution and explicitly starts the
operation. If the distro is stopped, probing pnpm warns that WSL command
execution may start it.

This lane remains deterministic because pnpm itself owns project registration,
reference detection, and removal. The explicit user action preserves the
product's normal mutation boundary and reflects pnpm's advice not to prune too
frequently.
