# Docker unified maintenance UI

Audited/implemented: 2026-08-20

## Goal

Unify the already-audited Docker storage lanes into one product surface without introducing a broad `docker system prune` authority or weakening any existing decision class.

The unified UI is an **aggregation layer**, not a new cleanup policy.

## Preserved semantic lanes

- classic builder cache: vendor `docker builder prune` with a minimum 24-hour retention; UI uses 168 hours by default;
- Buildx cache: exact proven-local builder, aged reclaimable records only, no `--all`;
- images: exact image ID, no container references, at most one tag, USER_REVIEW, `image rm --no-prune`, no force;
- stopped containers: exact container ID, USER_REVIEW, no force, volumes preserved;
- volumes: REPORT_ONLY persistent data, including currently-unreferenced volumes;
- Docker Desktop data disk / raw WSL/VHD storage: protected mixed state;
- no network/system prune lane is added.

## Endpoint binding improvement

The unified UI resolves the user's effective Docker target once for review, then pins subsequent reads and mutations to the exact resulting daemon endpoint through `DOCKER_HOST` while masking inherited `DOCKER_CONTEXT`.

This is deliberately stronger than following a mutable default context name. A user changing the default Docker context after reviewing the screen must not redirect the action to another daemon.

The reviewed context name/source remain visible as explanatory metadata, but destructive authority is bound to the exact local endpoint.

## Accounting model

The Overview tab shows Docker's own `docker system df --format json` rows. These values are evidence only:

- image sizes can share layers;
- BuildKit cache can share content;
- "Reclaimable" is Docker's logical estimate;
- Docker Desktop stores Linux data inside its managed VM/WSL storage;
- deleting logical objects does not imply equal immediate shrinkage of the Windows-side virtual disk.

The UI therefore never labels Docker logical size or reclaimable bytes as guaranteed Windows physical free-space recovery.

## UI structure

One dialog contains separate tabs for:

1. read-only Docker storage overview;
2. classic and Buildx build cache;
3. images;
4. containers;
5. read-only volumes.

Each destructive action still uses its existing exact lane and asks for its own confirmation. A single "clean everything" control is intentionally absent.

## Safety invariants

- only local Docker endpoints are executable;
- the endpoint shown to the user is the endpoint used for destructive actions;
- no action follows a changed default context;
- image/container identity is freshly revalidated by the underlying exact lane;
- Buildx builder identity, node endpoints and aged-cache accounting are freshly compared to the reviewed snapshot;
- volumes never receive mutation authority from "unused" status;
- no `docker system prune`, `image prune`, `container prune`, `volume prune`, `--volumes`, image `--force`, or Buildx `--all` shortcut is introduced;
- logical Docker accounting is not converted into a physical reclaim promise.

## Validation

Normal DevClean validation remains mandatory on the final head: lock/dependency checks, Ruff, strict mypy, full pytest/current workflow, Windows EXE artifact and CodeQL.
