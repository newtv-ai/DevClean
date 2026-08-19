# WSL root-filesystem mutation scope

## Problem

An authoritative tool path inside WSL is not automatically a local-disk path.
Developer tools can redirect caches or stores onto separately mounted Windows,
network, removable, or other filesystems.

DevClean is a local storage maintenance product. Therefore `pip cache dir`,
`uv cache dir`, `pnpm store path`, or another vendor-owned path proves semantic
ownership but does not by itself prove that DevClean is allowed to mutate the
underlying storage.

## Conservative first boundary

For destructive WSL maintenance, DevClean initially permits only an existing
absolute non-root path whose POSIX device identity matches the selected
WSL distribution's `/` filesystem.

The proof is obtained inside the exact selected distribution with argv-only:

```text
stat -L -c %d -- /
stat -L -c %d -- <exact-vendor-path>
```

The target is executable only when both commands return one valid decimal device
identifier and those identifiers are equal.

`-L` deliberately follows a symlink so a cache symlinked onto a different mount
cannot inherit mutation authority from the symlink's directory entry.

## Why this is intentionally narrow

This rule can reject legitimate caches located on `/mnt/c`, another Windows
volume, or another separately mounted local filesystem. That is acceptable for
the first WSL mutation boundary: false negatives cost cleanup coverage, while a
false positive can turn a local-disk cleanup feature into mutation of storage
whose locality and lifecycle DevClean has not proved.

A future source-backed mount classifier may safely widen this boundary. Until
then, other mounts remain visible/reportable but non-executable.

## Failure behavior

Mutation stops safely if:

- `stat` is unavailable;
- either path cannot be stat'ed;
- output is missing, ambiguous, or non-numeric;
- the target is relative or `/`;
- the target device differs from the root filesystem device.

There is no shell fallback, no `/proc` parser fallback, no host path guessing,
and no raw Windows-side deletion fallback.

## Current application

This boundary is applied immediately before vendor mutation in the merged WSL
pip and uv lanes. It is also a prerequisite for subsequent WSL pnpm and Go
mutation lanes before they may merge.
