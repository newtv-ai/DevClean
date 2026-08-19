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
| Go caches | vendor commands for build/module cache semantics with deterministic build-cache and USER_REVIEW module-cache lanes |
| Conda caches | vendor cleanup with packages/environments semantically separated |
| Conan 2 cache | `conan cache clean`; source/build/download/temp cleanup without deleting recipes/package artifacts/config/remotes |
| Git object storage | `git maintenance run --auto` behind exact worktree/object-store/alternate-storage checks |
| Git LFS | USER_REVIEW vendor prune with remote verification and halt-on-unverified behavior; no force |
| Unreal Engine DDC | project/vendor-aware DDC maintenance; no raw recursive deletion of Zen/custom DDC trees |
| Bazel workspace output | exact workspace/output-base discovery; ordinary clean and user-confirmed expunge separated |
| Cargo workspace target | exact `cargo metadata` workspace/target discovery; full vendor clean remains USER_REVIEW |
| Meson configured build tree | exact source/build binding through Meson introspection; whole verified build tree USER_REVIEW with handle-bound deletion |
| Unity project `Library` | USER_REVIEW at exact project boundary with Editor/process/identity guards |
| Unity Asset Store packages | USER_REVIEW per exact `.unitypackage`; no whole-cache deletion |
| Unity UPM legacy packages | USER_REVIEW only for the deprecated subtree; current registry DB remains Unity-managed |
| Ollama models | USER_REVIEW per exact vendor model identity/API; raw model store protected |
| Android SDK packages | USER_REVIEW per exact `sdkmanager` package identity; whole SDK protected |
| Docker classic builder cache | vendor-owned builder prune hardened to source-verified local daemon only |
| Docker Buildx cache | exact local builder identity, aged reclaimable inventory, conservative vendor prune, never `--all` |
| Docker images | exact per-image USER_REVIEW; container references and multi-tag cases protected; exact no-force/no-parent-prune removal |
| Docker stopped containers | exact stopped-container USER_REVIEW; no force and attached volumes preserved |
| Docker volumes | exact read-only inventory only; all volumes REPORT_ONLY because they are persistent data |
| WSL distribution inventory | exact registered/running distro inventory only; distro/rootfs/VHD lifecycle remains protected |
| WSL pip cache | exact distro + vendor `pip cache` operation behind WSL root-filesystem locality proof |
| WSL uv cache | exact distro + vendor `uv cache prune` behind WSL root-filesystem locality proof |
| WSL pnpm store | exact distro/store identity + vendor `pnpm store prune` behind WSL root-filesystem locality proof |
| WSL Go build cache | exact distro/GOCACHE + constrained vendor clean; deterministic candidate |
| WSL Go module cache | exact distro/GOMODCACHE + constrained vendor clean; USER_REVIEW |

## Docker boundary now established

Docker is deliberately split into semantic lanes. DevClean must never collapse them into a generic `docker system prune` button.

### Local daemon authority

All Docker mutation first resolves the effective Docker target through Docker's context/host semantics and proves that the operation targets the local machine. Remote SSH/TCP/ambiguous contexts are inspectable but non-executable. DevClean never switches the user's Docker context automatically.

### Build cache

Classic builder and Buildx cache are generated acceleration state and use Docker/Buildx vendor maintenance. Buildx is scoped to one exact local builder; the implementation verifies builder/driver/node endpoints, counts only vendor-reported reclaimable old records, enforces a retention floor, revalidates before prune, and never adds `--all`.

### Images

Images are not automatically disposable merely because they are dangling, old, or large. The exact image lane is USER_REVIEW. Images referenced by any running or stopped container are non-executable. Multi-tag images remain report-only because DevClean will not force-remove or silently dismantle multiple tags. Eligible explicit removal is pinned to the exact full image ID with `docker image rm --no-prune` and no `--force`.

### Stopped containers

Stopped containers are USER_REVIEW because their writable layers may contain unique state. DevClean removes only one exact freshly revalidated stopped container, never uses force, and never removes attached volumes as a side effect.

### Volumes

Volumes are persistent data. The implemented lane is strictly REPORT_ONLY, including currently-unreferenced volumes. DevClean may explain exact current container references, driver/scope/metadata, but does not infer deletion safety from age, name, size, or lack of references. No `volume rm`, `volume prune`, or system prune authority is granted.

### Follow-up

A future Docker UI/accounting pass may unify the already-audited object lanes into one clearly separated dialog, but it must preserve the semantic divisions above and must not expose a broad system-prune shortcut.

## WSL boundary now established

A WSL distribution is persistent state. Its Linux filesystem/VHD is not a cache object.

The source and implementation sequence now establishes:

- exact distro identity and running state come from WSL itself;
- `wsl --unregister` is destructive lifecycle administration, never cleanup;
- raw `ext4.vhdx` deletion/truncation/path guessing is prohibited;
- logical Linux free space is distinct from physical Windows host VHD size;
- Docker Desktop WSL disks are not targets of the WSL lane;
- the shared WSL execution boundary pins one exact registered distro and argv-only `--exec`, with no shell command strings or automatic root/sudo/su escalation;
- vendor-owned cache paths must separately pass the selected distro root-filesystem device-identity proof before mutation;
- paths redirected to `/mnt/c`, another mount, network/removable storage, or otherwise non-rootfs storage remain reportable but non-executable;
- pip, uv, pnpm, Go build cache and Go module cache have now been implemented one-by-one on that boundary.

### Sparse VHD safety status

Current upstream WSL safety signals are not strong enough for DevClean to expose sparse conversion as a mutation. The lane is REPORT_ONLY. DevClean must never append or normalize away an `--allow-unsafe` override, must not auto-edit experimental sparse settings, and must not fall back to raw VHD/registry/package-path workarounds.

### Physical VHD reclaim

In-distro cleanup can release logical Linux filesystem space without reducing the Windows VHD file by the same amount. DevClean makes no physical-host reclaim promise. A future VHD compaction lane needs an exact distro-to-VHD identity, a supported offline vendor procedure across supported installation/import variants, and verifiable pre/post state before it can be executable.

## Project build-system audit sequence

Project build outputs are now treated as **build-system authority problems**, not as a reason to add generalized `build` / `out` / `target` / `bin` / `obj` directory rules.

Current conclusions:

| Build system | Current conclusion |
| --- | --- |
| Bazel workspace output | executable vendor-owned lane; ordinary clean deterministic, expunge USER_REVIEW |
| Cargo workspace `target_directory` | exact metadata-backed USER_REVIEW vendor clean when target is local and workspace-contained |
| Cargo `build.build-dir` | audit complete; execution deferred pending stable effective-path/vendor interface |
| Maven | audit complete; generic clean deferred because inherited/configured filesets can widen destructive scope |
| Gradle | audit complete; generic clean deferred because task graph/Delete/custom actions can widen destructive scope |
| CMake | audit complete; generic clean deferred because generated clean scope can be widened by supported project configuration and generator behavior |
| .NET / MSBuild | audit complete; generic clean deferred because evaluated/imported targets and Before/AfterTargets can widen destructive scope |
| Meson | exact configured whole build-tree removal implemented as USER_REVIEW; `meson compile --clean` remains separately unaudited backend behavior |
| Ninja standalone | audit complete; generic clean deferred because graph outputs/depfiles/rspfiles/dyndeps are not directory-bounded and no complete machine-readable destructive manifest is exposed |
| GNU Make | audit complete; generic clean deferred because `clean` is arbitrary project-defined recipe execution and dry-run is not a guaranteed read-only scope probe |
| GNU Automake | audit complete; generic clean-family targets deferred because clean-file variables and `*-local` recipes can widen behavior |
| SCons | audit complete; generic clean deferred because graph discovery executes SConstruct/SConscript Python and `Clean()` can add project-defined extra paths |

A backend never inherits broader authority than the higher-level generator that produced it. For example, CMake-generated Ninja/Make cannot use the lower-level backend to bypass the CMake audit result, while Meson uses its separately proved whole configured build-tree lifecycle rather than generic Ninja clean.

## Completed audits intentionally kept non-executable

These are completed safety decisions, not missing coverage:

| Source | Current conclusion | Revisit trigger |
| --- | --- | --- |
| Unity GI Cache | Unity-managed/protected; full clear is vendor/user lifecycle | documented external exact maintenance API/CLI |
| Bazel `--disk_cache` | shared/configurable vendor-managed state | stable effective-path + installed GC/control interface |
| Maven project clean | clean-plugin can widen scope through inherited/configured filesets | complete effective destructive manifest before invocation |
| Gradle project clean | clean/Delete task graph is extensible and can widen targets | supported complete task/action target proof |
| CMake project clean | clean scope is generator/project-extensible and not bounded by a conventional build directory | stable complete exact-generator destructive manifest |
| .NET / MSBuild project clean | evaluated/imported target graph can add arbitrary cleanup behavior | stable complete evaluated Clean destructive model with unresolved execution failing closed |
| Ninja standalone clean | output/depfile/rspfile/dyndep paths are not directory-bounded | complete stable machine-readable Cleaner plan before mutation |
| GNU Make clean | arbitrary recipe execution; even dry-run is not a guaranteed read-only discovery boundary | only a separately audited higher-level generator lifecycle can earn authority |
| GNU Automake clean family | generated defaults can be widened by file variables and arbitrary `*-local` recipes | complete non-executing effective clean model with all extensions bounded |
| SCons project clean | graph/clean discovery executes project Python; `Clean()` widens extra paths | complete non-executing clean-plan interface |
| Cargo `build.build-dir` | intermediate state but stable effective-path discovery is insufficient | stable metadata/config query or narrow vendor clean action |
| Ollama raw model store | downloaded user content with shared blobs/manifests | never raw-delete; use per-model vendor action |
| Maven local repository | mixture of downloaded cache and unique local artifacts | vendor semantics that distinguish safely reclaimable remote content |
| Cargo global registry/git | shared vendor-managed download/source state | stable vendor prune/GC interface |
| WSL distro VHD/rootfs | persistent Linux filesystem and user state | no generic deletion lane |
| WSL sparse conversion | current safety/capability contract not strong enough | stable documented safe per-distro operation without unsafe bypass |
| WSL physical VHD compaction | exact distro-to-VHD/offline vendor procedure not yet proven for supported variants | stable source-backed vendor contract and verifiable pre/post state |

## Cross-cutting protections

- Broad roots never inherit delete authority from names such as `cache`, `tmp`, `temp`, `build`, `target`, `bin`, `obj`, `.cache`, `_bazel_*`, or `ext4.vhdx`.
- User-downloaded models/packages/images/volumes are not silently converted into cache because they are large or redownloadable.
- Vendor-owned GC/lifecycle is preferred over DevClean inventing a second retention/LRU model.
- Raw shared stores/databases/VHDs remain protected when the vendor exposes a safer object-level operation.
- AI never creates command authority, never supplies arbitrary destructive commands, and does not compensate for missing source research.
- Physical reclaimed bytes are not equated with logical object/package/image size when layers, hardlinks, shared blobs, VHDs, or vendor accounting make them different.
- A project-provided executable recipe/script cannot become safe merely because the target is conventionally named `clean`.

## Current high-value queue

The previous Docker/WSL implementation queue is substantially closed. The next work should prioritize sources where the expected disk win is high and Windows/vendor semantics can provide a narrow operation.

### Priority A — Windows-supported cleanup flows

1. **Windows component store / superseded update components**
   - audit `DISM /Online /Cleanup-Image /AnalyzeComponentStore` as read-only vendor inventory;
   - keep scheduled StartComponentCleanup, manual `/StartComponentCleanup`, and `/ResetBase` semantically separate because they have different rollback/uninstall effects;
   - never raw-delete WinSxS or `SoftwareDistribution` as a shortcut.

2. **Previous Windows installation (`Windows.old`)**
   - preserve rollback semantics;
   - use only current supported Windows cleanup surfaces;
   - deletion before the normal retention window is USER_REVIEW, never defaulted from folder size/name.

3. **Windows cleanup surfaces / accounting**
   - audit `cleanmgr`, Storage settings/Storage Sense and supported system cleanup categories without writing registry profiles blindly;
   - separate user-content categories (Downloads/Recycle Bin) from system-generated cleanup state.

### Priority B — developer storage follow-ups

4. **JetBrains exact product/version children**
   - split indexes/caches/logs/system state by product/version;
   - prefer IDE/vendor invalidation and documented storage semantics.

5. **Podman/container alternatives**
   - audit local-machine identity and split image/container/volume/build-cache semantics similarly to Docker;
   - do not copy Docker commands or assumptions blindly.

6. **Android/Gradle follow-ups**
   - correlate AVD usage with installed system images for better USER_REVIEW explanations;
   - Gradle project mutation remains blocked until destructive task scope can be proven.

7. **Docker unified UI/accounting**
   - unify already-audited object lanes without changing their decision class;
   - explain shared-layer accounting without pretending image logical size equals physical reclaim.

### Priority C — narrower sources

8. **Windows diagnostics**
   - WER/crash dumps are USER_REVIEW unless a narrower documented retention lane is justified;
   - Prefetch/Logs/CbsTemp remain broad protected roots until exact sub-sources are audited.

9. **Generic `%USERPROFILE%\.cache` descendants**
   - audit high-impact known applications individually;
   - never restore generic parent delete authority.

10. **Additional local-model products**
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
- execute project code merely to discover whether its cleanup is safe;
- claim reclaimed physical disk space from logical package/model/image size when the storage model does not support that claim.

## Continuation protocol

When a new session continues this project:

1. read this document plus `docs/review-lane-policy.md` and the relevant source-specific audit;
2. inspect current `main` and active PR state before relying on an old branch;
3. pick the first unfinished lane with meaningful disk value and a plausible narrow authority boundary;
4. read current primary vendor documentation/source;
5. determine the semantic lane before coding;
6. if the audit is positive, implement the narrow vendor-owned/USER_REVIEW lane **in the same work branch/PR** rather than creating a second stacked implementation PR;
7. if the audit is negative, record the REPORT_ONLY/protected result with a concrete revisit trigger instead of adding a weak fallback rule;
8. do not merge until lock/dependency checks, Ruff, strict typing, full tests, Windows artifact, and CodeQL are green on the final head.

A well-supported decision to **not delete** is a successful audit outcome. Safety boundaries are product functionality, not missing features.
