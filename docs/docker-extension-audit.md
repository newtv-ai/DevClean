# Docker maintenance extension audit

Audited: 2026-08-19

## Context

DevClean already has a conservative Docker Desktop lane:

- Docker Desktop WSL/VHDX storage stays KEEP / REPORT_ONLY;
- Docker CLI configuration, contexts, credentials and Desktop settings stay protected;
- the only existing mutation is old classic/current-builder build-cache cleanup through Docker's own `docker builder prune` command;
- no raw Docker Desktop filesystem deletion exists.

This follow-up asks which additional Docker-owned operations can be exposed without turning `docker system prune` into a generic cleanup shortcut.

## Product conclusion

Docker's own documentation distinguishes several object classes with very different persistence semantics. DevClean should preserve that split.

| Docker storage/object | DevClean conclusion | Reason |
| --- | --- | --- |
| Buildx builder cache | DETERMINISTIC vendor maintenance | Buildx owns cache accounting and pruning; cache is generated build acceleration state |
| Dangling image | USER_REVIEW | Docker can identify it precisely and remove it without raw storage access, but an untagged image can still be intentionally retained or used by image ID |
| Other unused image | USER_REVIEW, lower priority | Re-pull/rebuild cost and offline value are user intent; do not equate "not referenced by a container" with worthless |
| Stopped container | USER_REVIEW | Its writable layer still uses disk and Docker can remove it precisely, but that writable layer may contain unique user data/state |
| Unused named/anonymous volume | REPORT_ONLY in the initial extension | Docker explicitly treats volumes as persistent data and warns automatic removal can destroy data; absence of a current container reference does not prove low value |
| Unused network | no cleanup feature | Very small disk value; not worth adding a destructive maintenance surface |
| `docker system prune` | protected / not exposed | Combines multiple semantic classes and can remove stopped containers plus images/networks/build cache in one broad action |
| `docker system prune --volumes` | protected / never exposed | Adds persistent volume deletion to the already broad operation |

Known Docker object semantics do not need AI. The distinction is between vendor-owned deterministic cache maintenance, explicit user intent and protected persistent state.

## Local-daemon boundary

Docker CLI commands do not necessarily target the local machine. Docker contexts, `DOCKER_CONTEXT`, `DOCKER_HOST`, `--context` and `--host` can redirect the same CLI to a remote Engine. Docker also allows Buildx builders to point at Docker contexts or remote/Kubernetes endpoints.

That matters for DevClean: this product is a **local disk maintenance** tool, not remote Docker administration. A vendor command is not sufficient safety evidence if its target daemon can be on another machine.

The existing `docker builder prune` lane therefore needs one additional guard before Docker is extended further:

1. resolve the effective Docker context through Docker's own CLI;
2. inspect the effective Docker endpoint instead of trusting a context name;
3. allow local Windows named-pipe endpoints and equivalent local-only engine sockets;
4. treat `tcp://`, `ssh://`, non-local contexts and ambiguous endpoints as REPORT_ONLY / non-executable;
5. apply the same local-daemon check to the existing `docker builder prune` action and every new Docker mutation;
6. never silently switch the user's context to make an operation executable.

Docker's own context documentation shows local Windows Engine endpoints such as `npipe:////./pipe/docker_engine`, while remote contexts can target TCP or SSH endpoints. Source-backed context identity must therefore precede all destructive Docker maintenance.

## 1. Buildx cache

Current Docker documentation describes `docker buildx prune` as the selected builder's build-cache cleanup command. It supports:

- an explicit `--builder` target;
- age filtering through `until=`;
- cache usage controls such as `--max-used-space`, `--min-free-space` and `--reserved-space`;
- filters for cache record properties such as `inuse`, `shared`, `private` and cache type.

`docker buildx du --format=json` provides newline-delimited vendor inventory for individual cache records, including ID, reclaimable state, shared state, size, type and last-used metadata. The same command accepts `--builder` for an exact builder target.

Docker also documents periodic BuildKit garbage collection and says the default GC behavior is sufficient for most users. That means DevClean should not fight the builder's configured GC policy or blindly clear all records.

### Extension decision

Add a separate **Buildx builder cache** lane that:

1. first confirms the Docker daemon/context is local;
2. enumerates builders through Buildx itself;
3. identifies the selected/target builder by exact Buildx name rather than guessing Docker Desktop files;
4. inspects the builder's driver/nodes and refuses remote, Kubernetes or otherwise non-local builder endpoints;
5. inventories that exact builder with `docker buildx du --builder <name> --format=json`;
6. recommends vendor maintenance only when Buildx itself reports material reclaimable cache;
7. preserves a conservative age window of at least 24 hours;
8. invokes only `docker buildx prune --builder <name> --filter until=<hours>h`;
9. never adds `--all` automatically;
10. never modifies BuildKit GC configuration;
11. refuses while a Docker/BuildKit build is active;
12. re-checks daemon and builder identity immediately before mutation.

This is deterministic because the object class is generated build cache and Docker itself owns the prune semantics. A size/age threshold remains only a benefit threshold.

## 2. Images

Docker documents two relevant concepts:

- default `docker image prune` removes dangling images;
- `docker image prune -a` additionally removes every image not referenced by a container.

A dangling image is untagged and not referenced by a container, but Docker's image listing documentation also shows that such images remain real image IDs. A developer can still deliberately retain or refer to an image by ID.

### Extension decision

Do **not** add a blanket `docker image prune` button as deterministic cleanup.

Instead, a later executable lane may inventory exact images and expose only explicit user-directed removal through Docker's own `docker image rm <ID>` with no force flag.

Requirements:

- first confirm the effective Docker daemon is local;
- image identity comes from Docker daemon output, not Docker Desktop files;
- show repository/tags/digest/image ID/created time/logical size;
- show whether any current container references the image;
- never preselect an image merely because it is dangling or old;
- never use `--force`;
- refresh daemon identity plus image/container references immediately before removal;
- accept that shared layers mean the displayed logical image size is not equal to reclaimable physical bytes;
- report actual daemon-wide storage change after the operation rather than promise the image's logical size will be reclaimed.

This is **USER_REVIEW**, not AI.

## 3. Stopped containers

Docker explicitly states that stopped containers are not removed automatically and that their writable layers continue consuming disk. `docker container prune` removes all stopped containers.

The fact that a container is stopped does not imply that its writable layer is disposable. A developer may intentionally keep a stopped database, test environment or one-off debugging state and restart it later.

### Extension decision

Do **not** expose blanket `docker container prune` as deterministic cleanup.

A later executable lane can offer exact per-container **USER_REVIEW** using `docker container rm <ID>` with all of the following constraints:

- first confirm the effective Docker daemon is local;
- show name, ID, image, status, created time and writable-layer size where Docker reports it;
- only stopped containers are selectable;
- never preselect based on age/size alone;
- never use `--force`;
- never add `--volumes` / `-v`;
- refresh daemon identity and status immediately before mutation and refuse if the container is now running;
- remove exactly the selected container ID;
- leave named and anonymous volumes untouched;
- measure before/after Docker storage rather than treating container logical size as guaranteed reclaimed space.

Again, this is user intent and needs no AI.

## 4. Volumes

Docker's storage documentation treats volumes as persistent data whose lifetime is independent of a container. Docker's pruning guide explicitly explains that volumes are not removed automatically because doing so can destroy data.

`docker volume prune` removes unused local volumes; current Docker defaults to anonymous unused volumes, while `--all` includes named unused volumes. "Unused" only means no container currently references the volume. It does not prove that the data is obsolete.

### Extension decision

Keep unused Docker volumes **REPORT_ONLY** in the first Docker extension.

DevClean may display Docker's own unused-volume inventory and size/accounting when available, but should not surface a one-click prune action yet. A future volume-specific workflow would need a stronger user-facing data review/backup story before destructive authority is justified.

Never expose:

- `docker volume prune --all`;
- `docker system prune --volumes`;
- raw VHDX/volume-directory deletion.

This is known persistent state, not AI ambiguity.

## 5. Networks

Docker can prune unused networks, but the expected local disk benefit is negligible compared with cache/images/containers. Adding another destructive button increases UI and review complexity for little storage value.

DevClean should leave network pruning out of the disk-cleaning product surface.

## 6. Why `docker system prune` remains excluded

Docker documents `docker system prune` as a shortcut that can remove stopped containers, unused networks, dangling or unused images and build cache, with optional volume pruning.

Those object classes do not share one DevClean decision lane:

- build cache is generated acceleration state;
- images are downloadable/buildable artifacts with user-specific reuse value;
- stopped containers can hold unique writable-layer state;
- volumes are persistent data.

A broad system-prune action would erase the semantic separation that DevClean is deliberately building. It therefore remains outside the product even though Docker officially supports it.

## Proposed implementation order

1. **Docker local-daemon guard + existing builder-prune hardening** — close the remote-context authority gap first.
2. **Buildx builder cache maintenance** — deterministic vendor lane, highest-confidence extension.
3. **Exact image inventory/removal** — USER_REVIEW, no force, no blanket prune.
4. **Exact stopped-container inventory/removal** — USER_REVIEW, no force and no volume removal.
5. **Volume inventory only** — REPORT_ONLY.

Each executable operation must continue using Docker CLI/daemon identity rather than raw Docker Desktop storage paths.

## Primary sources

- Docker Docs, **Docker contexts**
- Docker CLI reference, **docker context show**
- Docker CLI reference, **docker context inspect**
- Docker CLI reference, **docker** (daemon socket / host schemes)
- Docker Docs, **Prune unused Docker objects**
- Docker CLI reference, **docker image prune**
- Docker CLI reference, **docker image ls**
- Docker CLI reference, **docker container prune**
- Docker CLI reference, **docker container rm**
- Docker CLI reference, **docker volume prune**
- Docker Docs, **Volumes**
- Docker CLI reference, **docker system prune**
- Docker CLI reference, **docker builder prune**
- Docker CLI reference, **docker buildx du**
- Docker CLI reference, **docker buildx inspect**
- Docker CLI reference, **docker buildx prune**
- Docker CLI reference, **docker buildx**
- Docker Docs, **Build garbage collection**
