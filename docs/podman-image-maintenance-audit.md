# Podman exact image maintenance audit

Last updated: 2026-08-19

## Conclusion

**One exact writable ordinary Podman image can be USER_REVIEW only after DevClean proves that no Podman or external Buildah/CRI-O container references it, that it has no child images, that it is not a manifest list/image index, and that removing its full image ID cannot silently dismantle multiple user-visible tags.**

This is not a dangling-image auto-clean rule. Image age, size, dangling status, and redownloadability never create deletion authority.

## Primary vendor contracts

Current Podman documentation establishes:

- `podman images --all --no-trunc --format json` enumerates locally stored image records and exposes full image IDs, parent IDs, repository tags/digests, read-only status and size fields;
- the `readonly` filter distinguishes images in read-only image stores such as `additionalimagestores`;
- the `manifest` filter identifies locally stored manifest lists/image indexes;
- `podman image inspect` exposes the full immutable image ID, `RepoTags`, `RepoDigests`, `Parent`, `Created`, `Size`, and `ManifestType` for ordinary images;
- Podman and Buildah share image storage, and `podman ps --external` exposes external storage containers from tools such as Buildah/CRI-O that may depend on the same images;
- `podman rmi` / `podman image rm` normally removes the requested image **and dangling parent images**;
- `--no-prune` prevents those parent-image side effects;
- `--force` is much broader: Podman removes containers using the image before removing the image, so DevClean never uses it;
- Podman returns a dedicated failure when an image still has child images or is used by a container;
- `podman system df` is logical store accounting and Podman warns image reclaimable estimates can overstate actual reclaim when layers are shared.

Primary sources:

- https://docs.podman.io/en/latest/markdown/podman-images.1.html
- https://docs.podman.io/en/latest/markdown/podman-image-inspect.1.html
- https://docs.podman.io/en/latest/markdown/podman-ps.1.html
- https://docs.podman.io/en/latest/markdown/podman-rmi.1.html
- https://docs.podman.io/en/latest/markdown/podman-system-df.1.html
- https://docs.podman.io/en/latest/markdown/podman-manifest.1.html

## Exact Windows machine boundary

This lane reuses the already-audited Podman Windows machine identity from the stopped-container lane:

1. exactly one current default Podman system connection;
2. `IsMachine=true`;
3. loopback SSH endpoint only;
4. exact connection-name binding to one Podman-managed machine;
5. Windows provider `wsl` or `hyperv`;
6. every inventory/mutation command pinned with `--connection <exact-name>`;
7. the reviewed connection URI, machine, provider, rootful/rootless mode and resolved executable are carried into mutation and must still match before the reviewed image is looked up.

Changing the user's default Podman connection between review and mutation therefore fails closed instead of redirecting deletion to another rootful/rootless/remote image store.

## Reference proof

Ordinary `podman ps --all` alone is not enough. Podman documents that image storage is shared with Buildah and CRI-O and that external storage containers may depend on the same images.

Before any image is executable DevClean therefore obtains both:

- ordinary Podman container image references;
- `podman ps --all --external` image references.

Every non-empty reported `ImageID` must normalize to one complete 64-hex SHA256 image ID. If an ordinary or external container reports a nontrivial image name but no verifiable full image ID, or if either reference query cannot be completed, **reference proof becomes incomplete and all image mutation is disabled**. DevClean may still show inventory and explain why execution is unavailable.

This is intentionally global fail-closed behavior: an unresolved external container could otherwise be the only thing keeping a selected image in use.

## Image eligibility

One exact image is USER_REVIEW only when all of the following remain true immediately before mutation:

- full image ID is known and exact;
- image is not in Podman's read-only image set;
- image is not a manifest list/image index;
- ordinary Podman container reference set is empty;
- external Buildah/CRI-O container reference set is empty;
- no currently inventoried image reports this image as its parent;
- `RepoTags` has zero or one entry;
- the complete ordinary + external container-reference proof succeeded.

### Why multi-tag images are protected

Removing an image by ID can remove the locally stored image identity and its names. If multiple tags point at one image, the user may value those names independently. DevClean therefore does not use one image-ID cleanup decision to silently dismantle multiple user-visible tags. A future tag-specific lifecycle would need a separate exact `untag` audit.

### Why manifest lists are protected

Podman has a dedicated `podman manifest` lifecycle for manifest lists/image indexes. A generic image cleanup decision should not collapse that higher-level object into an ordinary single-image deletion. Manifest-list IDs are inventoried and explained but not executable in this lane.

### Why read-only images are protected

Podman supports read-only additional image stores. Local visibility therefore does not imply local mutation authority. Any image reported by the vendor `readonly=true` filter remains protected.

## Mutation

The only mutation command is:

```text
podman --connection <exact-reviewed-connection> image rm --no-prune <full-64-hex-image-id>
```

DevClean never adds:

- `--force`;
- `--all`;
- prune/system-prune operations;
- tag/name guessing;
- short IDs;
- user- or AI-supplied extra arguments.

The complete machine target and image identity/tag/parent/child/reference state are freshly re-inventoried before mutation. After the vendor command succeeds, DevClean re-inventories again and requires the exact image ID to be absent.

## Decision class

Eligible images remain **USER_REVIEW**, including untagged/dangling images. Images can represent expensive local builds, offline working sets, private registries, old branches, or reproducibility anchors. Podman's statement that dangling images may serve no active purpose is not enough for DevClean to infer the user's retention value across every local workflow.

Size and age may help sort/explain review value but never auto-select an image.

## Accounting boundary

Podman image layers are shared and Windows Podman storage lives inside a WSL/Hyper-V machine. Image logical size therefore is not equivalent to host physical reclaim. DevClean shows vendor logical size and before/after `podman system df` evidence only and never promises an equal decrease in the Windows-side VM/VHD file.

## Deliberate exclusions

- no `podman image prune`;
- no `podman system prune`;
- no `--force`;
- no manifest-list deletion;
- no read-only additional-store mutation;
- no tag-specific untagging;
- no parent pruning;
- no build-cache cleanup in this lane;
- no Podman volume mutation;
- no WSL/Hyper-V VM disk deletion or compaction;
- no AI-created Podman command.

## Follow-up

Podman build cache and volumes remain separate audits. Volumes are persistent data by default. Build-cache maintenance needs a narrow vendor operation and pre-mutation scope/accounting proof that does not inherit broad image/system-prune side effects.
