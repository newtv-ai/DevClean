# pip cache maintenance audit

Audited: 2026-08-18  
Quantification and execution re-audited: 2026-08-23

## Product conclusion

pip's cache is known local performance data, so DevClean does not spend AI on it.
The safe mutation boundary remains the vendor command, not recursive filesystem
deletion.

The second-pass audit separates two different proofs that the earlier product
surface had conflated:

1. **cleanup authority** — current pip source still proves that `pip cache purge`
   is the supported operation for removing pip cache items, so the operation
   remains deterministic vendor maintenance;
2. **pre-clean reclaim amount** — the recursive size of the whole configured
   cache root is **not** the exact byte set that current `pip cache purge` will
   remove, especially when a custom root contains neighboring non-pip files.

DevClean therefore keeps the lower-level deterministic pip purge capability but
no longer places the whole root size in the byte-counted `可以删除` product list.
The known `PIP_CACHE` root is still excluded from generic per-file traversal, so
correcting the displayed amount does not turn a known provider cache back into a
slow AI/file-classification problem.

## Current upstream snapshot

The source behavior was rechecked against pip main commit:

`6d309205789d368b2749d93fd6193d584811efd5`

Relevant current source:

- `src/pip/_internal/commands/cache.py`

Current `CacheCommand.purge_cache()` delegates to removal with the wildcard
pattern. That operation removes pip's current wheel files and HTTP cache files,
cleans the corresponding empty/internal cache directories, and removes the old
`selfcheck.json` file. It does **not** recursively delete every arbitrary file
under `options.cache_dir`.

This distinction is important because pip also documents the on-disk cache
layout as an implementation detail. DevClean should neither raw-delete those
internals nor assume that every byte co-located beneath a custom cache root is a
pip cache item.

## Why whole-root occupancy is not a reclaim promise

The earlier integration measured the selected cache root recursively and used
that number as the pre-clean `可清理` amount. That can overstate the official
purge result:

- a custom `PIP_CACHE_DIR` can contain neighboring files that pip does not own;
- current purge targets pip's cache-item model rather than recursively removing
  the configured root;
- `pip cache info` is human-formatted output, not a stable exact-byte destructive
  manifest;
- `pip cache list --format abspath` covers locally built wheels, not the full HTTP
  cache set that purge also removes.

There is therefore no current public, mutation-free pip interface that gives
DevClean a complete exact byte manifest for `cache purge` without relying on
pip's documented-unstable internal layout.

The root's recursive size remains useful as **occupancy/reporting metadata**, but
it is not used as a recommendation threshold or promised reclaim amount.

## Why raw deletion authority remains removed

pip explicitly documents the exact filesystem structure of its cache contents as
an implementation detail that may change between pip versions. It also provides
`pip cache dir`, `pip cache info`, and `pip cache purge` as the supported
cache-management surface.

The previous DevClean rule gave the default `%LocalAppData%\pip\Cache` directory
whole-tree raw deletion authority. That remains too broad. Both default and
custom pip roots stay REPORT_ONLY in generic filesystem semantics; actual
mutation is delegated only to pip.

## Hardened execution contract

Before purge DevClean now:

1. re-resolves the audited default/custom pip cache roots and requires the
   selected path to match one exactly;
2. refuses while pip activity is detected;
3. requires the selected cache root to be a local-fixed, non-reparse/non-cloud
   directory with stable volume/file identity;
4. sets `PIP_CACHE_DIR` to that exact identity-bound root;
5. resolves one candidate pip command to an exact executable and requires that
   executable to be local-fixed, non-reparse/non-cloud, and identity-stable;
6. records one exact `pip --version` result to bind the selected pip environment
   as well as the launcher/interpreter file;
7. asks that exact command to report `pip cache dir` and requires the result to
   equal the reviewed cache root;
8. immediately before mutation rechecks pip process state, command executable
   identity, cache-root identity, the same `pip --version` identity, and the same
   vendor-reported cache root;
9. runs only `<same exact prefix> cache purge` with the same pinned environment;
10. after vendor success requires both the command executable and preserved cache
    root to retain their reviewed filesystem identities;
11. measures actual logical before/after bytes only after execution, propagates
    vendor failure, and never falls back to raw deletion.

For `python -m pip` or `py -m pip`, binding both the executable identity and the
exact `pip --version` result prevents a changed interpreter/pip environment from
silently inheriting the reviewed action.

## Recovery / tradeoff

`pip cache purge` clears pip's HTTP and wheel cache items. It does not uninstall
already installed packages or intentionally remove unrelated files merely
because they share a custom cache root. Later installs can require downloads
again and locally built wheels may need rebuilding.

The operation remains technically deterministic; the current product limitation
is only that DevClean does not yet have an honest exact pre-clean byte figure for
that operation. A future single-surface representation may expose deterministic
vendor actions whose reclaim amount is explicitly unknown, or pip may add a
stable machine-readable dry-run/manifest interface. Either would allow the action
to return to the normal product list without inventing bytes.

## Sources

Pinned source snapshot:

- https://github.com/pypa/pip/blob/6d309205789d368b2749d93fd6193d584811efd5/src/pip/_internal/commands/cache.py

pip documentation remains authoritative for `cache dir`, `cache info`,
`cache list`, `cache remove`, `cache purge`, Windows default cache location, and
the warning that the internal cache layout is an implementation detail.

## Validation gate

The final PR head must pass lock/dependency checks, Ruff, strict mypy, full
pytest/current workflow, Windows EXE build/upload, and CodeQL before merge.
