# uv cache prune audit

Audited: 2026-08-18

## Product conclusion

`uv cache prune` is deterministic local cleanup and should not consume AI. Astral explicitly documents that direct cache mutation is never safe, while `uv cache prune` removes unused cache entries and centralized project environments that are recreated as needed. The same documentation says the prune operation is safe to run periodically.

DevClean therefore keeps uv's raw cache protected in generic scanning and grants mutation authority only to the uv command itself.

## Benefit policy

A discovered uv cache at or above 512 MiB is selected by default. Smaller caches remain understood and manually selectable.

The threshold is a benefit threshold, not a safety threshold. uv uses caching aggressively to avoid duplicate downloads and rebuilds, so keeping a small cache is often useful even though pruning it is supported and safe.

## Execution contract

Before running prune, DevClean:

1. re-resolves uv's current audited cache roots and requires an exact selected-root match;
2. refuses if the selected cache no longer exists;
3. refuses while uv/uvx is active;
4. sets `UV_CACHE_DIR` to the exact selected root;
5. asks the selected uv executable to report `uv cache dir` and requires that result to match the target;
6. only then runs `uv cache prune` with the same executable and environment;
7. reports vendor failures and never falls back to direct filesystem deletion.

uv also has its own cache-modification locking. DevClean's process guard is intentionally an additional fail-closed layer rather than a replacement for uv's lock.

## Why prune instead of clean

`uv cache clean` removes all cache entries. `uv cache prune` is narrower: it removes unused entries and centralized project environments that can be recreated. For a general disk cleaner whose default decisions should benefit essentially every user, prune is the better deterministic maintenance operation.

## Sources

- Astral uv documentation, **Caching**: describes aggressive dependency caching, states that direct cache modification is never safe, documents `uv cache prune` as removing unused cache entries and centralized project environments, says those environments are recreated as needed, and says prune is safe to run periodically.
- Astral uv documentation, **Caching — Cache directory**: documents `--cache-dir`, `UV_CACHE_DIR`, project configuration, and the Windows default cache location.
