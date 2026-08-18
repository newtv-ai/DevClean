# Bazel disk-cache audit

Audited: 2026-08-18

## Product conclusion

Bazel's `--disk_cache` is a distinct storage source from the workspace `output_base` already covered by DevClean's Bazel maintenance action.

The current Bazel disk cache is deliberately usable across branches and multiple workspaces/checkouts of the same project. Starting with Bazel 7.4, Bazel also owns an automatic garbage-collection policy for this cache through maximum-size and maximum-age flags.

DevClean therefore does **not** turn a discovered disk-cache directory into a raw-delete target. The current decision is **Bazel-managed / report-only until an effective-cache configuration can be resolved through a stable Bazel interface**.

This is not AI work: the storage semantics are known. The missing piece is safe execution authority over a potentially shared and redirectable cache.

## Separate from `output_base`

The existing Bazel workspace lane resolves `output_base` through `bazel info output_base` and delegates cleanup to `bazel clean` / user-confirmed `bazel clean --expunge`.

`--disk_cache` is different:

- it is configured as a build option rather than being the workspace output base;
- it may point to an arbitrary filesystem path;
- it is specifically useful for sharing build artifacts across multiple workspaces/checkouts;
- a project can enable it in a checked-in `.bazelrc` for multiple developers;
- `--disk_cache` with no explicit value uses `<outputUserRoot>/cache/disk`;
- `--nodisk_cache` disables it.

A directory name such as `cache/disk`, `disk`, or `bazel-cache` is therefore not sufficient evidence that DevClean exclusively owns or may delete that tree.

## Current Bazel-owned garbage collection

Current Bazel documentation says that, starting with Bazel 7.4, disk-cache garbage collection can be configured with:

- `--experimental_disk_cache_gc_max_size` — keep the cache under a configured size;
- `--experimental_disk_cache_gc_max_age` — remove entries older than a configured age;
- `--experimental_disk_cache_gc_idle_delay` — control how long the Bazel server remains idle before background GC starts; the documented default is five minutes.

When positive size/age limits are configured, Bazel performs GC in the background while the server is idle.

This is materially stronger than a generic "cache can be deleted" statement. Bazel itself understands its content-addressed cache layout and owns the lifecycle policy, so DevClean should not maintain a second independent age/size deletion implementation over the raw directory.

## Why DevClean does not parse `.bazelrc` files for deletion authority

The effective `--disk_cache` option can be influenced by Bazel startup/command configuration and rc files. Bazel supports workspace and user rc files, and command-line options can override rc values.

DevClean's earlier Bazel output-base action intentionally avoided arbitrary `.bazelrc` interpretation: it asks Bazel itself for authoritative runtime paths instead.

The current public `bazel info` surface exposes workspace/output paths such as `workspace` and `output_base`, but does not provide an equivalent documented `info disk_cache` key. `canonicalize-flags` canonicalizes a supplied list of options; the current public documentation does not establish it as a stable "print the effective rc-derived disk-cache configuration for this workspace" API.

Until Bazel exposes that effective value through a stable interface DevClean can query, parsing rc text itself would reintroduce exactly the configuration-guessing problem the Bazel workspace audit avoided.

## On-demand GC utility boundary

Bazel's current remote-caching documentation links to a standalone disk-cache GC utility in the Bazel source tree. Its documented invocation is from the Bazel source workspace:

`bazel run //src/tools/diskcache:gc -- --disk_cache=<path> --max_age=... --max_size=...`

That tool demonstrates the intended GC semantics, but it is not documented as an installed `bazel` subcommand available in an arbitrary user's project. DevClean therefore does not assume that a user's Bazel installation carries an externally callable GC command.

If Bazel later publishes this as a stable installed CLI/API, it becomes a strong candidate for a vendor-managed DevClean action.

## Shared-storage boundary

The official remote-caching guide explicitly positions disk cache as useful for sharing artifacts between workspaces. A configured path may also live on shared storage.

Even if DevClean could resolve an exact disk-cache path, a full raw deletion could discard useful cache entries for other workspaces or users. Source identity alone would still not imply exclusive ownership.

A future executable lane therefore needs both:

1. authoritative effective-path discovery through Bazel;
2. vendor-owned GC/removal semantics that are safe for a shared content-addressed cache.

A local-fixed-volume check alone would not solve the cross-workspace ownership problem.

## Current DevClean behavior

DevClean intentionally does not:

- search for directories named `cache/disk` or `bazel-cache` and mark them deletable;
- assume `<outputUserRoot>/cache/disk` is enabled merely because the default path exists;
- raw-delete a custom `--disk_cache` directory;
- parse arbitrary `.bazelrc` files and turn matching strings into mutation authority;
- treat `bazel clean` or `bazel clean --expunge` as disk-cache cleanup;
- silently add GC flags to a user's project configuration;
- delete a shared cache because it exceeds a DevClean size/age threshold.

The existing Bazel workspace/output-base maintenance remains unchanged.

## Revisit criteria

Re-open this audit for an executable disk-cache lane when at least one of these becomes available through a stable supported interface:

- Bazel can report the effective disk-cache path and GC policy for the selected workspace without DevClean parsing rc files; or
- Bazel exposes a supported installed command/API to perform on-demand disk-cache GC against the effective configured cache.

At that point DevClean should still preserve shared-cache semantics and prefer Bazel's own max-size/max-age GC over whole-directory deletion.

## Primary sources

- Bazel, **Remote Caching — Disk cache / Garbage collection**: disk-cache sharing across workspaces, `--disk_cache`, default location, `--nodisk_cache`, and Bazel 7.4+ background GC size/age/idle controls.
  - https://bazel.build/remote/caching
- Bazel, **Command-Line Reference**: current `--disk_cache` and `--experimental_disk_cache_gc_*` flag semantics.
  - https://bazel.build/reference/command-line-reference
- `bazelbuild/bazel`, **src/tools/diskcache**: Bazel source-tree on-demand GC utility and its documented `bazel run //src/tools/diskcache:gc` usage.
  - https://github.com/bazelbuild/bazel/tree/master/src/tools/diskcache
