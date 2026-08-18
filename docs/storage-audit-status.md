# DevClean storage-source audit status

Last updated: 2026-08-19

This document is the durable handoff for DevClean's storage-source audit. It exists so future work continues from source-controlled decisions instead of reconstructing the product boundary from chat history or old stacked pull requests.

## Product decision model

Every storage source must end in one of four outcomes:

- **DETERMINISTIC_CANDIDATE** — source/vendor semantics establish that the state is disposable or reproducible. DevClean may recommend the narrow vendor-owned operation locally.
- **USER_REVIEW** — technical meaning is known, but value, rebuild/download cost, compatibility, offline usefulness, or retained user state depends on the user. Never default-select merely because the item is old/large.
- **AI_REVIEW** — local/source evidence is genuinely insufficient to identify the item or its meaning. AI is a residual ambiguity tool, not a replacement for vendor research.
- **REPORT_ONLY / protected / vendor-managed** — installed, shared, persistent, destructive-lifecycle, or insufficiently provable state for which DevClean has no safe generic mutation authority.

The order is deliberate: **vendor/source facts -> deterministic local semantics -> user intent -> AI only for residual ambiguity**.

Size and age are benefit heuristics only. They never create deletion authority.

## Execution standards

A source is not considered executable merely because a likely directory or CLI command was found. Every mutation lane should establish, as applicable:

1. current primary vendor documentation or vendor source for identity/lifecycle semantics;
2. exact source-backed object/root/path discovery rather than directory-name guessing;
3. separation from neighboring storage with different semantics;
4. vendor CLI/API preference over raw filesystem deletion;
5. local/shared/remote boundary appropriate to a local-disk product;
6. stable object identity and reparse/symlink/junction protection where direct filesystem mutation is unavoidable;
7. active-process/concurrency guards where use changes safety;
8. fresh revalidation immediately before mutation;
9. bounded exact command arguments with no hidden widening flags;
10. postcondition and before/after evidence without overstating physical reclaim;
11. regression tests plus Ruff, strict mypy, full pytest, Windows EXE artifact, and CodeQL before merge.

A clean CI run on an obsolete stacked base is not enough. When dependency branches land and semantic interaction matters, the lane is freshened onto current `main` and the combined repository is validated again.

## Completed source-aware maintenance lanes

These sources have dedicated vendor/application-aware semantics rather than broad raw-delete rules:

| Source | Current product action |
| --- | --- |
| Codex history/storage | exact application-specific maintenance; persistent history/state separated from disposable data |
| Claude Code storage | application-specific maintenance with protected persistent state |
| Cursor / VS Code storage | application-aware maintenance rather than broad editor-directory deletion |
| NuGet local resources | official `dotnet nuget locals`; HTTP/temp/plugin caches deterministic, global packages USER_REVIEW |
| pip cache | vendor-supported cache inventory and purge with authoritative cache-path validation |
| pnpm store | vendor garbage collection rather than whole-store deletion |
| uv cache | vendor garbage collection |
| Go caches | vendor commands for build/module/test-cache semantics |
| Conda caches | vendor cleanup with packages/environments semantically separated |
| Conan 2 cache | `conan cache clean`; source/build/download/temp cleanup without deleting recipes/package artifacts/config/remotes |
| Git object storage | `git maintenance run --auto` behind exact worktree/object-store/alternate-storage checks |
| Git LFS | USER_REVIEW vendor prune with remote verification and halt-on-unverified behavior; no force |
| Unreal Engine DDC | project/vendor-aware DDC maintenance; no raw recursive deletion of Zen/custom DDC trees |
| Bazel workspace output | exact workspace/output-base discovery; ordinary clean and user-confirmed expunge separated |
| Cargo workspace target | exact `cargo metadata` workspace/target discovery; full vendor clean remains USER_REVIEW |
| Unity project `Library` | USER_REVIEW at exact project boundary with Editor/process/identity guards |
| Unity Asset Store packages | USER_REVIEW per exact `.unitypackage`; no whole-cache deletion |
| Unity UPM legacy packages | USER_REVIEW only for the deprecated subtree; current registry DB remains Unity-managed |
| Ollama models | USER_REVIEW per exact vendor model identity/API; raw model store protected |
| Android SDK packages | USER_REVIEW per exact `sdkmanager` package identity; whole SDK protected |
| Docker classic builder cache | vendor-owned builder prune hardened to source-verified local daemon only |
| Docker Buildx cache | exact local builder identity, aged reclaimable inventory, conservative vendor prune, never `--all` |
| Docker images | exact per-image USER_REVIEW; container references and multi-tag cases protected; exact no-force/no-parent-prune removal |

## Docker boundary now established

Docker is deliberately split into semantic lanes. DevClean must never collapse them into a generic `docker system prune` button.

### Local daemon authority

All Docker mutation must first resolve the effective Docker target through Docker's context/host semantics and prove that the operation targets the local machine. Remote SSH/TCP/ambiguous contexts are inspectable but non-executable. DevClean never switches the user's Docker context automatically.

### Build cache

Classic builder and Buildx cache are generated acceleration state and use Docker/Buildx vendor maintenance. Buildx is scoped to one exact local builder; current implementation verifies builder/driver/node endpoints, counts only vendor-reported reclaimable old records, enforces a retention floor, revalidates before prune, and never adds `--all`.

### Images

Images are not automatically disposable merely because they are dangling, old, or large. The exact image lane is USER_REVIEW. Images referenced by any running or stopped container are non-executable. Multi-tag images remain report-only because DevClean will not force-remove or silently dismantle multiple tags. Eligible explicit removal is pinned to the exact full image ID with `docker image rm --no-prune` and no `--force`.

### Stopped containers

Stopped containers are USER_REVIEW because their writable layers may contain unique state. The implementation is being revalidated on the image-enabled main branch. It removes only one exact stopped container, never uses force, and never removes attached volumes as a side effect.

### Volumes

Volumes are persistent data. The initial lane is strictly REPORT_ONLY, including currently-unreferenced volumes. DevClean may explain exact current container references, driver/scope/metadata, but does not infer deletion safety from age, name, size, or lack of references. No `volume rm`, `volume prune`, or system prune authority is granted.

## WSL boundary now established

A WSL distribution is persistent state. Its Linux filesystem/VHD is not a cache object.

The source audit establishes:

- exact distro identity and state must come from WSL itself;
- `wsl --unregister` is destructive lifecycle administration, never cleanup;
- raw `ext4.vhdx` deletion/truncation/path guessing is prohibited;
- logical Linux free space is distinct from physical Windows host VHD size;
- Docker Desktop WSL disks are not targets of the WSL lane;
- in-distro developer cleanup should use each Linux tool's own audited command through an exact WSL execution boundary.

The read-only distro inventory is in validation. It uses WSL's own registered/running queries, exposes no lifecycle mutation, and is wired to a read-only desktop overview.

### Sparse VHD safety status

Current upstream WSL safety signals are not strong enough for DevClean to expose sparse conversion as a mutation. The lane is REPORT_ONLY for now. DevClean must never append or normalize away an `--allow-unsafe` override, must not auto-edit experimental sparse settings, and must not fall back to raw VHD/registry/package-path workarounds.

### WSL tool adapter direction

The next safe WSL expansion is a narrow non-shell adapter:

- exact distro selected with WSL's distribution argument;
- tool and arguments passed as separate argv through WSL exec, not dynamically constructed `sh -c`/`bash -c` text;
- no arbitrary command UI or AI-built commands;
- no automatic root/sudo/su escalation;
- Linux-native authoritative tool paths/configuration revalidated inside the same distro;
- logical Linux bytes reported separately from Windows physical reclaim.

Initial candidates are the already-audited narrow vendor cache operations, beginning with pip, then uv/pnpm/Go one-by-one. Their Windows path/process implementations are not reused blindly; only the semantic vendor contract is reused.

## Completed audits intentionally kept non-executable

These are completed safety decisions, not missing coverage:

| Source | Current conclusion | Revisit trigger |
| --- | --- | --- |
| Unity GI Cache | Unity-managed/protected; full clear is vendor/user lifecycle | documented external exact maintenance API/CLI |
| Bazel `--disk_cache` | shared/configurable vendor-managed state | stable effective-path + installed GC/control interface |
| Maven project clean | clean-plugin can widen scope through inherited/configured filesets | complete effective destructive manifest before invocation |
| Gradle project clean | clean/Delete task graph is extensible and can widen targets | supported complete task/action target proof |
| Cargo `build.build-dir` | intermediate state but stable effective-path discovery is insufficient | stable metadata/config query or narrow vendor clean action |
| Ollama raw model store | downloaded user content with shared blobs/manifests | never raw-delete; use per-model vendor action |
| Maven local repository | mixture of downloaded cache and unique local artifacts | vendor semantics that distinguish safely reclaimable remote content |
| Cargo global registry/git | shared vendor-managed download/source state | stable vendor prune/GC interface |
| WSL distro VHD/rootfs | persistent Linux filesystem and user state | no generic deletion lane |
| WSL sparse conversion | current safety/capability contract not strong enough | stable documented safe per-distro operation without unsafe bypass |
| WSL physical VHD compaction | exact distro-to-VHD/offline vendor procedure not yet proven for supported variants | stable source-backed vendor contract and verifiable pre/post state |

## Cross-cutting protections

- Broad roots never inherit delete authority from names such as `cache`, `tmp`, `temp`, `build`, `target`, `.cache`, `_bazel_*`, or `ext4.vhdx`.
- User-downloaded models/packages/images/volumes are not silently converted into cache because they are large or redownloadable.
- Vendor-owned GC/lifecycle is preferred over DevClean inventing a second retention/LRU model.
- Raw shared stores/databases/VHDs remain protected when the vendor exposes a safer object-level operation.
- AI never creates command authority, never supplies arbitrary destructive commands, and does not compensate for missing source research.
- Physical reclaimed bytes are not equated with logical object/package/image size when layers, hardlinks, shared blobs, VHDs, or vendor accounting make them different.

## Active validation queue

These are the current near-term lanes and should be resolved before broadening scope further:

1. **Stopped Docker containers** — fresh current-main PR; exact stopped container USER_REVIEW, no force, preserve volumes.
2. **Docker volumes** — read-only exact inventory; all volumes REPORT_ONLY.
3. **Docker unified maintenance UI** — after the image/container/volume core lanes are all on `main`, expose Buildx, images, stopped containers, and volumes in one clearly separated Docker dialog without broad prune shortcuts.
4. **WSL read-only inventory UI** — exact registered/running distros, no VHD path or lifecycle mutation.
5. **WSL sparse safety follow-up** — durable REPORT_ONLY block on unsafe conversion behavior.
6. **WSL tool execution adapter audit** — exact distro, argv-only non-shell execution, no arbitrary/root command surface; first implementation candidate pip cache.

## Remaining high-value backlog

After the active queue is closed, prioritize sources where disk value is high and a narrow vendor-owned action is plausible.

### Priority A

1. **Windows-supported cleanup flows**
   - Windows Update/servicing leftovers: use OS/service-aware supported maintenance, never routine raw deletion of `SoftwareDistribution\Download`;
   - `Windows.old`: preserve rollback semantics; only supported removal after explicit USER_REVIEW;
   - component store: audit DISM/Storage Sense/Disk Cleanup scopes separately from ordinary file cleanup.

2. **Docker follow-up UI/accounting**
   - unify already-audited object lanes;
   - explain shared-layer accounting without pretending image logical size equals reclaim;
   - keep networks outside storage cleanup unless a real storage value emerges.

3. **WSL in-distro high-confidence vendor caches**
   - pip first, then uv/pnpm/Go;
   - package managers and project outputs remain separate audits.

### Priority B

4. **JetBrains exact product/version children**
   - split indexes/caches/logs/system state by product/version;
   - prefer IDE/vendor cache invalidation and documented storage semantics.

5. **Podman/container alternatives**
   - audit local-machine identity and split image/container/volume/build-cache semantics similarly to Docker;
   - do not copy Docker commands or assumptions blindly.

6. **Android/Gradle follow-ups**
   - correlate AVD usage with installed system images for better USER_REVIEW explanations;
   - Gradle mutation remains blocked until destructive task scope can be proven.

### Priority C

7. **Windows diagnostics**
   - WER/crash dumps are USER_REVIEW unless a narrower documented retention lane is justified;
   - Prefetch/Logs/CbsTemp remain broad protected roots until exact sub-sources are audited.

8. **Generic `%USERPROFILE%\.cache` descendants**
   - audit high-impact known applications individually;
   - never restore generic parent delete authority.

9. **Additional local-model products**
   - model directories remain user-selected content;
   - add exact per-model vendor actions only where supported APIs/CLIs exist.

## Explicit anti-goals

DevClean should not pursue a coverage percentage by adding rules for every recognizable folder. It must not:

- delete a directory because the name looks temporary;
- use AI to replace source research;
- treat user content as cache because it can be downloaded/rebuilt again;
- raw-delete vendor stores/databases/VHDs that already own lifecycle/GC;
- expose broad `prune all` operations without exact semantic separation;
- parse one configuration file and pretend to reproduce a richer precedence/inheritance system;
- silently escalate privilege;
- build arbitrary shell commands from scanned/user/model text;
- claim reclaimed physical disk space from logical package/model/image size when the storage model does not support that claim.

## Continuation protocol

When a new session continues this project:

1. read this document plus `docs/review-lane-policy.md` and the relevant source-specific audit;
2. inspect current `main` and active PR state before relying on an old branch;
3. pick the first unfinished lane with meaningful disk value and a plausible narrow authority boundary;
4. read current primary vendor documentation/source;
5. either implement the narrow vendor-owned/USER_REVIEW lane with tests and full CI, or record a REPORT_ONLY/protected outcome with a concrete revisit trigger;
6. do not merge until CI, strict typing/lint/tests, Windows artifact, and CodeQL are green.

A well-supported decision to **not delete** is a successful audit outcome. Safety boundaries are product functionality, not missing features.
