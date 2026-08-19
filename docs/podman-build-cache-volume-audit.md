# Podman build-cache and volume audit

Last updated: 2026-08-19

## Product conclusions

This audit closes the two remaining high-value Podman storage questions after the merged exact stopped-container and exact image lanes.

### Persistent build cache: REPORT_ONLY / executable lane deferred

Podman now documents `podman image prune --build-cache` as removing persistent build cache created for Containerfile/Buildah `--mount=type=cache`. That cache is reproducible acceleration state, so its *semantic value* is cache-like. However, the current CLI does not expose that maintenance as one isolated build-cache-only lifecycle operation.

`podman image prune` is itself an image-prune command: without `--all` it removes dangling images, and `--build-cache` is an additional option on that command. DevClean therefore cannot invoke `podman image prune --build-cache` while claiming that only persistent build cache will be touched. The already-merged exact-image lane deliberately requires one reviewed full image ID and forbids broad image-prune authority, so the build-cache option cannot bypass that boundary.

`podman system prune` is even broader. Current Podman documentation states that the default operation removes stopped containers, unused networks, dangling images and dangling build cache. `--all` widens image behavior; `--volumes` widens into persistent volume data; `--build` removes leftover build containers and Podman explicitly warns that it is unsafe while builds are in progress and can interfere with active builds.

DevClean therefore records Podman persistent build cache as **REPORT_ONLY for now**. It may show vendor `system df` accounting as explanatory evidence, but it does not expose `image prune --build-cache`, `system prune`, `system prune --build`, or a raw storage-directory workaround.

### Volumes: REPORT_ONLY / persistent data

All Podman volumes remain **REPORT_ONLY**, including anonymous volumes that are currently unused.

Current Podman versions distinguish anonymous and named volumes and now expose a useful `podman volume prune --dry-run` preview. By default `volume prune` considers only anonymous unused volumes; `--all` widens to named unused volumes. These are useful lifecycle facts for inventory, but they do not make the contents disposable.

An anonymous volume is still a persistent filesystem object. It may contain application databases, generated state, credentials, caches the application itself expects to retain, or files copied into the volume from an image. "Anonymous", "unused", "dangling", old, or large therefore describes current topology/benefit, not deletion safety.

Podman also documents that volume data can persist even when container/volume metadata is transient or lost. That is an especially strong reason not to equate absence of a current container reference with absence of user data.

The correct DevClean policy is the same conservative semantic split already used for Docker volumes: inventory and explain exact current vendor state, but do not grant generic volume-prune or volume-rm authority.

## Primary vendor contracts

Current Podman documentation establishes:

- `podman image prune` removes dangling images by default; `--all` widens to all unused images.
- `podman image prune --build-cache` additionally removes persistent build cache created for `--mount=type=cache`.
- the image-prune CLI currently provides no documented build-cache-only dry-run/manifest that proves the image set will remain untouched.
- `podman system prune` is intentionally broad across containers, networks, images and build cache, with optional volumes.
- `podman system prune --build` removes leftover build containers and is explicitly documented as unsafe while builds are in progress.
- `podman volume ls --format json` can expose exact volume metadata including anonymous/name/driver/scope/mountpoint and current dangling/reference state.
- `podman volume inspect` provides exact per-volume metadata such as anonymous status, driver, labels, mount count, mountpoint, options and scope.
- `podman volume prune --dry-run` previews vendor prune candidates.
- current `podman volume prune` defaults to anonymous unused volumes; `--all` includes named unused volumes.
- Podman transient-store documentation explicitly notes that volume data on disk persists across reboot even if metadata is not persisted.
- `podman system df` is vendor logical accounting; image reclaimable estimates may be inaccurate because shared layers can make apparent reclaim exceed actual prune reclaim.

Primary sources:

- https://docs.podman.io/en/stable/markdown/podman-image-prune.1.html
- https://docs.podman.io/en/latest/markdown/podman-system-prune.1.html
- https://docs.podman.io/en/latest/markdown/podman-volume-prune.1.html
- https://docs.podman.io/en/latest/markdown/podman-volume-ls.1.html
- https://docs.podman.io/en/latest/markdown/podman-volume-inspect.1.html
- https://docs.podman.io/en/latest/markdown/podman-system-df.1.html
- https://docs.podman.io/en/latest/markdown/podman.1.html

## Why `podman image prune --build-cache` is not executable yet

The vendor has identified a legitimate cache class, but DevClean requires two separate proofs before mutation:

1. **semantic authority** — is the data reproducible cache? For persistent `--mount=type=cache`, yes;
2. **mutation-scope authority** — can the selected vendor operation be proven to touch only that audited class? With the current documented Podman CLI, not yet.

The command surface combines cache removal with image-prune semantics. DevClean must not loosen the exact image lifecycle merely because an additional cache flag exists.

A future executable lane becomes plausible if Podman exposes either:

- a dedicated build-cache-only command/API whose destructive scope excludes images/containers/volumes/networks; or
- a complete machine-readable dry-run/destructive manifest for `--build-cache` proving every affected object before mutation, with a way to assert zero image removals and then revalidate the same scope immediately before execution.

A future implementation should also refuse while Podman/Buildah builds are active or build-process state is uncertain.

## Why `podman system prune --build` is not a substitute

`--build` refers to build containers left from builds, not a narrow cache-only maintenance boundary. Podman itself warns that it is unsafe when builds are in progress. The surrounding `system prune` operation still owns broad stopped-container/network/image/build-cache lifecycle.

DevClean therefore does not parse its warning text, pre-answer its confirmation prompt, or use filters in an attempt to manufacture a narrower contract than the vendor documents.

## Volume inventory may improve without mutation

A future UI-only Podman volume inventory can safely add explanatory value while preserving REPORT_ONLY:

- exact volume name;
- anonymous vs named;
- driver and scope;
- current container reference/dangling state;
- creation time and labels;
- mount count;
- vendor `volume prune --dry-run` candidacy as a separate explanatory field;
- explicit warning that prune candidacy is **not** a DevClean deletion recommendation.

No age/size threshold, AI judgment, absence of references, anonymous status or dry-run candidacy can turn a volume into an automatic cleanup candidate.

## Deliberate exclusions

This audit grants no authority to:

- `podman image prune --build-cache`;
- `podman image prune` or `podman image prune --all` as a generic shortcut;
- `podman system prune` in any mode;
- `podman system prune --build`;
- `podman volume prune`, including anonymous-only default behavior;
- `podman volume rm` for generic cleanup;
- raw Podman/containers-storage directories;
- WSL/Hyper-V machine disk deletion or compaction;
- AI interpretation of cache/volume names to create command authority.

## Current Podman storage matrix

| Podman object | DevClean conclusion |
| --- | --- |
| Exact stopped standalone container | **USER_REVIEW**, implemented; exact reviewed machine + container identity; no force/volumes |
| Exact ordinary writable unreferenced leaf image | **USER_REVIEW**, implemented; full ID + ordinary/external reference proof + `--no-prune` |
| Manifest list / image index | protected; separate manifest lifecycle |
| Read-only image store image | protected |
| Persistent `--mount=type=cache` build cache | **REPORT_ONLY**, semantic cache but current prune command is not isolated from image-prune scope |
| Leftover build containers / `system prune --build` | **REPORT_ONLY**, broad and vendor-warned unsafe during builds |
| Anonymous or named volumes | **REPORT_ONLY**, persistent data even when unused/dangling |
| Podman machine / VM disk | protected lifecycle; no raw deletion/compaction |

## Next high-value queue after Podman

With Podman containers/images implemented and build-cache/volumes explicitly closed, the next useful work should return to sources with a stronger narrow vendor lifecycle rather than trying to force Podman prune commands into DevClean's authority model. Good candidates remain Android AVD/system-image correlation, Docker unified accounting/UI, Windows diagnostics exact sub-sources, and individually audited high-impact `%USERPROFILE%\.cache` applications.
