# WSL uv cache authority audit

## Decision

The effective cache reported by `uv cache dir` inside one exact WSL distribution
is a **DETERMINISTIC_CANDIDATE** for uv's own `cache prune` operation.

This lane does not use AI and grants no raw Linux or Windows filesystem deletion
authority.

## Source-backed semantics

Current Astral documentation makes the cache boundary unusually clear:

- direct mutation of uv's cache files/directories is never safe;
- `uv cache prune` removes unused cache entries and centralized project
  environments that are recreated as needed;
- Astral explicitly describes `uv cache prune` as safe to run periodically;
- cache-modifying uv commands wait on uv's own lock while other uv operations
  are using the cache;
- `--force` bypasses that lock and therefore is not a DevClean cleanup option;
- `uv cache prune --ci` is a CI-specific policy and is not the generic local
  maintenance operation;
- `uv cache dir` returns the current cache location, which may be affected by
  `--cache-dir`, `UV_CACHE_DIR`, or uv configuration.

Primary sources:

- https://docs.astral.sh/uv/concepts/cache/
- https://docs.astral.sh/uv/reference/cli/
- https://docs.astral.sh/uv/reference/storage/

The storage reference also separates disposable cache data from uv's persistent
data directory. Managed Python installations live in persistent data (for
example the `python/` data subtree) and are not part of this lane.

## Execution contract

DevClean must:

1. require an exact distribution returned by the existing WSL inventory;
2. run only the code-defined `uv` executable inside that exact distribution;
3. ask uv for `--version` and `cache dir`;
4. require `cache dir` to return one absolute, non-root POSIX path;
5. retain the exact distribution, uv version, and effective cache path as the
   mutation identity;
6. repeat inventory immediately before mutation and refuse if that identity
   changed;
7. pin the freshly verified path with `uv --cache-dir <path> cache prune` so a
   later configuration lookup cannot silently redirect the mutation;
8. preserve uv's own cache lock and never append `--force`;
9. never use the CI-specific `--ci` policy for local maintenance;
10. re-inventory afterward and refuse to claim a confirmed result if uv version
    or effective cache identity changed.

The existing WSL argv-only execution boundary is used directly. No shell command
string is constructed.

## Why no separate process-kill or process-enumeration policy

Unlike many tools, uv explicitly owns concurrency for cache mutation. Its cache
commands block while other uv commands are using the cache and time out rather
than requiring an external process-kill policy. DevClean therefore preserves
that vendor lock instead of trying to race uv with a second process detector.

The WSL subprocess timeout is deliberately longer than uv's documented default
cache-lock wait, and DevClean never bypasses the lock with `--force`.

## Deliberate non-features

DevClean does **not**:

- delete `~/.cache/uv` or any guessed cache directory;
- inspect or mutate uv cache buckets directly;
- run `uv cache clean` as the routine maintenance action;
- run `uv cache prune --ci` outside CI policy;
- run `uv cache prune --force`;
- alter `UV_CACHE_DIR`, uv configuration, the WSL default distribution, or user;
- touch uv's persistent data directory or managed Python installations;
- use `rm`, `find`, a shell, or Windows-side deletion as a fallback;
- claim that uv's reported logical removed size equals Windows host bytes
  reclaimed from a WSL virtual disk.

## Product behavior

The user explicitly selects one registered distribution before DevClean probes
uv. If the distribution is stopped, the UI warns that probing uv may start it.

The UI shows the exact uv version and the cache path uv itself reports. The
operation remains an explicit user action even though its semantics are
deterministic. No AI review is involved.

For WSL 2, releasing files inside the Linux filesystem does not authorize any
VHD mutation or promise equal host-file shrinkage. The separate WSL sparse/VHD
lane remains REPORT_ONLY.
