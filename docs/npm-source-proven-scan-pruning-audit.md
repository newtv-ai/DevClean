# npm source-proven generic-scan pruning audit — 2026-08

## Finding

The existing npm authority model remains unchanged:

- `_cacache` is npm-owned package cache state and is maintained only through npm's
  supported cache commands;
- `_npx` is npm-owned exec/npx cache state and exact entry removal remains a
  separately reviewed vendor action;
- `_tuf` is npm-managed Sigstore TUF cache state;
- `_logs` is diagnostic history and remains USER_REVIEW;
- unclassified files at the npm cache root remain protected;
- npm global installs, configuration files, trust files and other persistent
  paths remain protected;
- npm exposes no raw whole-tree generic TOOL root.

A normal DevClean scan still recursively enumerated `_cacache`, `_npx` and `_tuf`
even though every child in those exact source-owned subtrees resolves to a
protected vendor-cache rule and generic file traversal cannot create more precise
cleanup authority. Large package caches can contain very many objects, so this is
avoidable scan work.

This is a scan-performance problem, not new cleanup authority.

## Source proof

The npm CLI source re-audited for this correction is commit
`dc43591e6e08e9857c787116b1ed12f074e68c3c` (npm 12.0.2). npm's `cache`
configuration flattener derives exactly:

- `<cache>/_cacache` as the content-addressable package cache;
- `<cache>/_npx` as the npm exec/npx cache;
- `<cache>/_tuf` as the TUF cache.

The npm 11.6.2 command/config behavior used by DevClean's compatibility path was
also rechecked. Multi-key `npm config get` emits `key=value` rows in both audited
versions.

The mutation-boundary audits in `npm-protected-mutation-scope-audit.md` and
`npm-persistent-path-overlap-followup-audit.md` remain authoritative for
persistent path protection. The pruning proof reuses that same live bound-CLI
configuration evidence instead of inventing a second, weaker source model.

## Why the whole npm cache root is not pruned

The npm base cache is intentionally mixed from the generic scanner's point of
view. In particular:

- `_logs` contains user-retention diagnostic data;
- future or unknown files directly under the cache root are protected rather than
  assumed disposable;
- npm path-valued configuration can redirect persistent/security state.

Therefore this correction does **not** add an npm cache category to the product's
whole-root provider skip set and does not classify the cache root as raw TOOL
storage. Only the three exact provider children are eligible, each independently.

## Constant-cost live proof

`npm_generic_scan_skip_paths()` performs no cache inventory and no recursive size
walk. It uses the already-audited primitives to:

1. bind the exact local npm CLI identity;
2. ask that CLI for the effective cache root;
3. bind the cache root as a local fixed, ordinary directory with stable identity;
4. read npm major and the version-appropriate persistent/security boundary keys;
5. require npm to confirm the same pinned cache root;
6. build the protected path set, including normal `_logs`;
7. inspect only the three exact child directory objects;
8. require each child to match its exact npm rule id;
9. reject only the child whose range overlaps a protected path;
10. recheck npm CLI and cache-root identities before returning.

It never invokes `npm cache ls`, `npm cache npx ls`, `npm cache verify`, or any
recursive inventory command. The cost is independent of package-cache file count.

## Filesystem boundary requirement

The generic scanner applies `skip_paths` before it reads metadata for the skipped
object. That is useful for performance, but it means a blindly skipped path would
also suppress the scanner's normal boundary record for a junction, reparse point,
Cloud Files placeholder, or inaccessible/unstable object.

For that reason an npm child is returned as a skip path only after DevClean itself
proves that exact child is an ordinary local fixed directory, not a symlink,
junction, reparse point or cloud placeholder, with stable filesystem identity.

If one child cannot be proven, only that child remains traversable. Proven sibling
children may still be pruned. If the live npm configuration or cache-root proof
fails, the product falls back to the ordinary full generic scan with no npm
pruning.

## Visibility preserved

This correction deliberately keeps visible:

- `<cache>/_logs` and its USER_REVIEW diagnostic files;
- unclassified cache-root state;
- any `_cacache`, `_npx` or `_tuf` object whose local-directory identity cannot be
  proven;
- any provider child containing a redirected protected path;
- cache state when live npm configuration cannot be proven.

The pruning set therefore cannot turn ambiguity or a filesystem boundary into
silence.

## Product wiring

The product scan worker obtains the source-proven npm child paths separately from
quantified vendor inventory. The paths are added to generic traversal exclusions
only when they are reachable from the drives selected for the current scan.

Failure of the npm proof is non-fatal: it contributes no npm skip paths and the
base scanner proceeds normally. No npm row is added to the byte-counted safe list,
no reclaim amount is inferred from cache occupancy, and no vendor mutation is
triggered by this proof.

## Non-goals

This audit does not:

- grant raw filesystem deletion authority to npm cache directories;
- change TOOL/USER/KEEP ownership;
- change `npm cache verify`, `npm cache clean --force`, or exact npx removal;
- treat provider-root occupancy as exact reclaim;
- hide npm diagnostic logs;
- weaken reparse/cloud/inaccessible-path reporting;
- add a second cleanup workflow.

## Regression coverage

Focused tests require:

- only `_cacache`, `_npx` and `_tuf` to be returned by a normal proven layout;
- the proof to use only `config get` / `--version`, with no cache inventory call;
- a protected-path overlap to keep only the affected child visible;
- a simulated boundary/identity failure to keep only the affected child visible;
- incomplete live config to fail closed;
- `_logs` and unclassified root files to remain visible in an actual generic scan;
- product wiring to include reachable source-proven paths;
- product wiring to fall back to an unchanged full scan when the npm proof fails.

## Merge gate

Merge only from the exact final PR head after lock/dependency checks, Ruff, strict
mypy, full pytest/current workflow, Windows EXE build/upload, and CodeQL are all
green.
