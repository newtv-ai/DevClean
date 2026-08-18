# Bazel workspace maintenance audit

Audited: 2026-08-18

## Product conclusion

Bazel can consume many gigabytes, but its output state is scoped to a repository/workspace and may be redirected with startup options. DevClean must therefore be project-aware instead of deleting directories named `bazel-out` or `_bazel_*`.

The first Bazel maintenance lane requires the user to select a repository root, then uses Bazel itself to confirm both the workspace and its exact `output_base`.

Two vendor-owned operations are exposed with different product decisions:

| Operation | DevClean lane | Default recommendation | Effect |
| --- | --- | --- | --- |
| `bazel clean` | deterministic local cleanup | when output base >= 2 GiB | clears action cache / execroot build outputs |
| `bazel clean --expunge` | user review | never | removes the entire output base, including external artifacts and temp state |

Neither operation needs AI. The difference is user intent and rebuild/download cost, not technical uncertainty.

## Why direct filesystem deletion is rejected

Current Bazel documentation says the output base contains all scratch and build output for a user/workspace combination. Bazel supports `--output_base` and `--output_user_root`, so hard-coded paths are not authoritative.

On Windows the default output root is based on `%HOME%`, then `%USERPROFILE%`, and Bazel stores per-user build state below `_bazel_$USER`. The output-base directory itself is derived from a hash of the workspace path. That layout is useful for visibility but not enough to establish mutation authority over an arbitrary directory.

DevClean therefore does not recursively delete default Bazel roots, output-user roots, `bazel-out` links, or hash-looking output-base directories.

## Ordinary clean: deterministic lane

Bazel documents `bazel clean` as clearing the on-disk action cache and removing the `execroot`, which contains build outputs and convenience-link targets. The user manual describes `clean` as a disk-space reclamation operation for workspace build state.

This is rebuildable Bazel-generated state, so AI adds no value. DevClean uses 2 GiB only as a benefit threshold for recommending the action; a smaller output base is still understood and vendor-cleanable.

## Expunge: user-review lane

Bazel documents `bazel clean --expunge` as removing the entire `output_base` and stopping the Bazel server. External repository artifacts are also removed, so subsequent work may require substantial refetching and rebuilding.

This remains technically safe through Bazel's own command, but whether the extra disk win is worth the future download/build cost is user intent. DevClean therefore never preselects or silently upgrades ordinary clean to expunge. The UI requires an explicit confirmation.

## Execution contract

Before either mutation DevClean:

1. requires a repository boundary marker: `MODULE.bazel`, `REPO.bazel`, `WORKSPACE.bazel`, or `WORKSPACE`;
2. runs the configured Bazel executable inside the selected directory;
3. requires `bazel info workspace` to exactly equal the selected resolved path;
4. obtains the exact `bazel info output_base` path from the same Bazel executable;
5. inventories that exact output base read-only;
6. refuses if another Bazel/Bazelisk client process is already active;
7. invokes only `bazel clean` or, after explicit user review, `bazel clean --expunge`;
8. measures before/after bytes and propagates vendor errors;
9. never falls back to raw recursive deletion.

The long-lived Bazel server is not treated as an automatic blocker because `bazel clean` is itself a Bazel client operation and `--expunge` is documented to stop that server.

## Disk-cache boundary

Bazel also supports a separate local `--disk_cache` with experimental max-size/max-age garbage-collection controls. That storage can be redirected and is not necessarily the workspace output base.

This PR deliberately does not parse arbitrary `.bazelrc` files or delete disk-cache directories. A later audit can add disk-cache maintenance only if DevClean can resolve the effective cache path and preserve Bazel's own GC semantics without broad filesystem guessing.

## Sources

- Bazel, **Output Directory Layout** (current documentation): output root/output user root/output base layout, Windows defaults, per-workspace output base, `--output_base`, `--output_user_root`, and `bazel clean` behavior.
- Bazel, **Commands and Options / User Manual**: workspace-scoped `clean`, `--expunge`, server shutdown, and disk-space reclamation semantics.
- Bazel, **Command-Line Reference**: `--expunge` behavior and current experimental disk-cache GC size/age controls.
