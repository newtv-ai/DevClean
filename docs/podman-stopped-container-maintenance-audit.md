# Podman stopped-container maintenance audit

Last updated: 2026-08-19

## Conclusion

**Exact stopped standalone container removal on one exact Podman-managed Windows machine connection is USER_REVIEW.**

This lane is deliberately narrower than `podman system prune` or `podman container prune`.

Podman on Windows requires a Podman machine. The Windows client therefore talks to a Linux container store through a managed machine connection. DevClean must bind every inventory and mutation to one exact Podman-managed connection and must never assume that an arbitrary configured remote endpoint represents the local Windows machine.

Stopped containers are not automatically disposable. Their writable layers may contain unique user state, so exact removal requires explicit user confirmation. Named/anonymous volumes are separate persistent objects and are preserved.

## Primary vendor contracts

Current Podman documentation establishes:

- Windows requires `podman machine`; supported Windows providers are WSL and Hyper-V.
- `podman machine list --format json` exposes machine identity, running state, default state and VM type.
- `podman system connection list --format json` exposes connection name, URI, `IsMachine`, default state and read/write state.
- rootful and rootless connections for a machine are separate stores and switching rootful changes which containers/images/volumes are visible.
- `podman ps`/`container inspect` expose exact container identity and state.
- `podman rm <container>` removes the specified container; `--force` widens semantics to running/paused/unknown containers and is excluded.
- `--volumes` removes associated anonymous volumes and is excluded.
- `podman system df` is useful only as logical container-store accounting; Podman itself warns image reclaimable accounting can be inaccurate because of shared layers.
- `podman system prune` is broad: stopped containers, networks, dangling images/build cache and optional volumes can be affected, so DevClean does not expose it.

## DevClean authority boundary

A connection is executable only when all of these are true:

1. the current platform is Windows;
2. exactly one Podman system connection is marked default;
3. that connection reports `IsMachine=true`;
4. its URI is an SSH loopback endpoint (`localhost`, `127.0.0.1`, or `::1`);
5. the connection name maps exactly to one Podman-managed machine name (either `<machine>` or Podman's documented `<machine>-root` companion connection);
6. the matched machine reports a supported Windows provider (`wsl` or `hyperv`);
7. all Podman calls are pinned with `--connection <exact-name>`; DevClean never changes the user's default connection.

Anything else is REPORT_ONLY. In particular, arbitrary SSH/TCP endpoints, user-created remote connections, ambiguous defaults and unmatched machine connections never receive local-cleanup authority.

## Container eligibility

One container is eligible for USER_REVIEW only when fresh exact inspection proves:

- full immutable container ID is present;
- `State.Running=false`;
- `State.Paused=false`;
- status is not an active/unknown state;
- the container is not an infra container;
- the container is not attached to a pod;
- exact identity fields used for the user's review (ID, name, image identity, creation time, state, pod binding and volume names) remain unchanged immediately before mutation.

Pod members are protected in this first lane because pod topology is a separate lifecycle object. A future pod-specific audit may add exact pod actions.

## Mutation

The only mutation command is:

```text
podman --connection <exact-machine-connection> rm <full-container-id>
```

DevClean never adds:

- `--force` / `-f`;
- `--volumes` / `-v`;
- `--all`;
- `--latest`;
- prune/system-prune operations;
- shell wrappers or user-supplied extra arguments.

After removal DevClean re-inventories the same exact connection and requires the exact container ID to be absent.

## Accounting boundary

On Windows, Podman container data lives inside a Podman machine. Deleting container state may free logical space inside the Linux machine without shrinking the Windows-side WSL/Hyper-V virtual disk file by the same amount. DevClean therefore shows Podman-reported logical writable/rootfs size and before/after `podman system df` evidence only; it never promises equivalent Windows physical free-space reclaim.

## Explicit anti-goals

- no `podman system prune` shortcut;
- no broad `container prune`;
- no volume pruning/removal;
- no raw Podman machine/WSL/Hyper-V disk deletion or compaction;
- no machine reset/removal;
- no automatic rootful/rootless switching;
- no remote-host cleanup;
- no AI-created Podman command.

## Revisit candidates

Images, build cache, pods and volumes must be audited separately. Volumes should remain persistent data by default. Image/build-cache work also needs explicit handling of shared-layer accounting and Windows machine-disk physical-reclaim semantics.
