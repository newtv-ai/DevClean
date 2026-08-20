# DevClean storage-source audit status

Last updated: 2026-08-20

This is the durable handoff for DevClean's storage-source audit. Future work should continue from current `main`, this file, `docs/review-lane-policy.md`, and the relevant source-specific audit rather than reconstructing policy from chat history or stale stacked PRs.

## Product decision model

Every source ends in one of four outcomes:

- **DETERMINISTIC_CANDIDATE** — vendor/source semantics establish disposable or reproducible state and DevClean can prove one narrow mutation boundary.
- **USER_REVIEW** — technical meaning is known, but retention value, rebuild/download cost, compatibility, offline usefulness, rollback value, or unique user state depends on the user.
- **AI_REVIEW** — source/local evidence is genuinely insufficient to identify meaning. AI is residual ambiguity handling only, never a substitute for vendor research.
- **REPORT_ONLY / protected / vendor-managed** — installed, shared, persistent, destructive-lifecycle, or insufficiently provable state with no safe generic mutation authority.

The order is deliberate: **vendor/source facts -> deterministic local semantics -> user intent -> AI only for residual ambiguity**.

Names, age and size are benefit heuristics only. They never create deletion authority.

## Execution standards

A source is not executable merely because DevClean found a familiar path or command. A mutation lane should establish, as applicable:

1. current primary vendor documentation/source for identity and lifecycle;
2. exact source-backed object/root/ID discovery rather than directory-name guessing;
3. separation from neighboring storage with different semantics;
4. vendor CLI/API preference over raw deletion;
5. local/shared/remote authority appropriate to a local-disk product;
6. stable object identity and symlink/junction/reparse protection when direct filesystem mutation is unavoidable;
7. active-process/concurrency guards when use changes safety;
8. fresh revalidation immediately before mutation;
9. bounded exact command arguments with no hidden widening flags;
10. explicit user review for USER_REVIEW and destructive lifecycle operations;
11. postcondition and before/after evidence without overstating physical reclaim;
12. regression tests plus lock/dependency checks, Ruff, strict mypy, full pytest/current workflow, Windows EXE artifact and CodeQL before merge.

A clean CI run on a stale stacked base is not enough. Positive audit and implementation stay in the **same PR**. When an old stacked PR predates meaningful `main` changes, rebuild the work from current `main`; do not merge the obsolete tree merely because its old CI was green.

## Completed source-aware maintenance lanes

| Source | Current product action |
| --- | --- |
| Codex history/storage | application-specific exact maintenance; persistent history/state separated from disposable data |
| Claude Code storage | application-specific maintenance with protected persistent state |
| Cursor / VS Code storage | application-aware maintenance rather than broad editor-directory deletion |
| JetBrains old default system trees | source-backed 180-day old-version lifecycle; exact uninstalled 2020.1+ default system tree deterministic candidate, config/plugins protected |
| NuGet local resources | official `dotnet nuget locals`; HTTP/temp/plugin caches deterministic, global packages USER_REVIEW |
| pip cache | vendor-supported cache inventory/purge with authoritative cache-path validation |
| npm cache | exact configured root; `npm cache verify` deterministic vendor GC, whole package cache USER_REVIEW, exact npx entry USER_REVIEW, TUF protected |
| Cypress binary cache | exact selected CLI + vendor-reported root; `cypress cache prune` USER_REVIEW with future-root fail-closed guard; whole `cache clear` not exposed |
| pnpm store | vendor garbage collection rather than whole-store deletion |
| uv cache | vendor garbage collection |
| Go caches | vendor build-cache deterministic lane and USER_REVIEW module-cache lane |
| Conda caches | vendor cleanup with packages/environments semantically separated |
| Conan 2 cache | `conan cache clean`; source/build/download/temp cleanup without deleting recipe/package artifacts/config/remotes |
| Git object storage | `git maintenance run --auto` behind exact repository/object-store/alternate-storage checks |
| Git LFS | USER_REVIEW vendor prune with remote verification; no force |
| Hugging Face Hub cache | exact cached repo USER_REVIEW; globally-unique full revision USER_REVIEW; vendor prune USER_REVIEW after exact dry-run; Xet/assets/HF_HOME protected |
| PyTorch Hub storage | read-only explicit-root inventory only; checkpoints/repo-like dirs/top-level zip REPORT_ONLY, trusted_list protected; no delete API wrapped |
| Unreal Engine DDC | project/vendor-aware DDC maintenance; no raw recursive Zen/custom DDC deletion |
| Bazel workspace output | exact workspace/output-base discovery; ordinary clean deterministic, expunge USER_REVIEW |
| Cargo workspace target | exact `cargo metadata` target discovery; full vendor clean USER_REVIEW |
| Meson configured build tree | exact source/build binding via Meson introspection; whole verified build tree USER_REVIEW with handle-bound deletion |
| Unity project `Library` | USER_REVIEW at exact project boundary with Editor/process/identity guards |
| Unity Asset Store packages | USER_REVIEW per exact `.unitypackage`; no whole-cache deletion |
| Unity UPM legacy packages | USER_REVIEW only for deprecated subtree; current registry DB protected |
| Ollama models | USER_REVIEW per exact vendor model identity/API; raw model store protected |
| Android SDK package maintenance | exact `sdkmanager` package USER_REVIEW; whole SDK protected; system images additionally protected by strict AVD `image.sysdir.1/2` correlation |
| Android project SDK reference explainer | read-only positive literal `compileSdk`/Build Tools/NDK/CMake package correlation for one selected Gradle project; absence never means unused and Gradle is never executed |
| Android SDK installer temp | exact source-backed SDK `temp` subtree only, with SDK-writer guards |
| Android AVD storage | persistent virtual-device state protected; only narrow uncoupled temporary `cache.img` rule exists in generic scanner |
| Docker classic builder cache | vendor builder prune on source-verified local daemon only |
| Docker Buildx cache | exact local builder identity, conservative aged reclaimable prune, never `--all` |
| Docker images | exact USER_REVIEW; container references/multi-tag cases protected; full ID + no force/no parent prune |
| Docker stopped containers | exact USER_REVIEW; no force, volumes preserved |
| Docker volumes | exact read-only inventory; all volumes REPORT_ONLY persistent data |
| Docker unified maintenance UI | one endpoint-pinned Overview/Build Cache/Images/Containers/Volumes surface; existing decision classes preserved and no `system prune` |
| Podman stopped containers | exact USER_REVIEW on one reviewed local managed Windows machine connection; positive terminal-state whitelist; no force/volumes |
| Podman images | exact ordinary writable unreferenced leaf image USER_REVIEW; ordinary + Buildah/CRI-O external reference proof; no parent prune/force |
| Windows component store | DISM inventory; manual `StartComponentCleanup` USER_REVIEW only when fresh DISM recommends it; `/ResetBase` excluded |
| Previous Windows installation | exact `Windows.old` USER_REVIEW through `cleanmgr /AUTOCLEAN`; rollback/personal-file warning; no raw system-folder delete |
| Windows Recycle Bin | exact per-drive Shell API USER_REVIEW; no raw `$Recycle.Bin` and never all-drive widening |
| Delivery Optimization cache | exact FileId vendor maintenance; expired unpinned `Caching` item deterministic candidate, retained unpinned `Caching` item USER_REVIEW, pinned/active/unknown protected |
| Windows crash dumps | exact CrashControl large/small, LiveKernelReports root/component, and WER LocalDumps `.dmp` files USER_REVIEW with handle-bound exact deletion; WER queue/archive report stores REPORT_ONLY |
| Task Manager live-kernel dumps | exact current-user Known Folder-derived `LiveKernelDumps` direct `.dmp` files USER_REVIEW; mixed `%LOCALAPPDATA%\Temp` user-mode dumps excluded |
| WSL distribution inventory | exact registered/running distro inventory only; distro/rootfs/VHD lifecycle protected |
| WSL pip cache | exact distro + vendor `pip cache` behind root-filesystem locality proof |
| WSL uv cache | exact distro + vendor `uv cache prune` behind root-filesystem locality proof |
| WSL pnpm store | exact distro/store + vendor `pnpm store prune` behind root-filesystem locality proof |
| WSL Go build cache | exact distro/GOCACHE + constrained vendor clean; deterministic candidate |
| WSL Go module cache | exact distro/GOMODCACHE + constrained vendor clean; USER_REVIEW |

## Established container boundaries

### Docker

Docker mutation first proves the effective context/host targets the local machine. Remote SSH/TCP/ambiguous contexts are non-executable and DevClean never switches context automatically. Build cache, images, containers and volumes remain separate semantic lanes; there is no generic `docker system prune` button.

The unified Docker surface resolves the user's effective target once for review, then pins subsequent reads and mutations to the exact reviewed daemon endpoint through `DOCKER_HOST` while masking inherited `DOCKER_CONTEXT`. A later change to the user's default context therefore cannot redirect an already-reviewed action. Context name/source remain explanatory UI metadata; mutation authority is bound to the exact local endpoint.

Docker `system df`, image size and BuildKit cache accounting remain logical vendor evidence. Shared layers and Docker Desktop's VM/WSL storage prevent DevClean from promising equal immediate Windows physical free-space recovery.

### Podman on Windows

Podman mutation is pinned to one exact reviewed Podman-managed WSL/Hyper-V machine connection. The connection must be loopback, `IsMachine=true`, map exactly to one managed machine, and remain unchanged from review through postcondition.

- exact stopped standalone container: USER_REVIEW;
- exact ordinary writable leaf image with complete ordinary/external container-reference proof: USER_REVIEW;
- manifest lists/read-only image stores: protected;
- persistent `--mount=type=cache` build cache: REPORT_ONLY because current `podman image prune --build-cache` is not isolated from image-prune scope;
- leftover build containers / `podman system prune --build`: REPORT_ONLY;
- anonymous/named volumes: REPORT_ONLY persistent data;
- Podman machine and WSL/Hyper-V disk: protected lifecycle.

Logical container/image accounting never becomes a Windows VHD physical-reclaim promise.

## Established WSL boundary

A WSL distribution is persistent state. Its Linux filesystem/VHD is not a cache object.

The shared executable boundary pins one exact registered distro and argv-only `--exec`, with no shell strings or automatic root/sudo/su escalation. Vendor-owned cache paths must also pass that distro's root-filesystem device-identity proof. Paths redirected to `/mnt/c`, another mount, network/removable storage or otherwise non-rootfs storage are reportable but non-executable.

`wsl --unregister`, raw `ext4.vhdx` deletion/truncation, sparse conversion with unsafe overrides, and unproven physical VHD compaction remain outside DevClean cleanup authority. Linux logical space released is not equivalent to Windows host VHD shrink.

## Project build-system audit sequence

Project outputs are treated as **build-system authority problems**, not as generic `build` / `out` / `target` / `bin` / `obj` folder rules.

| Build system | Current conclusion |
| --- | --- |
| Bazel | executable vendor-owned lane; ordinary clean deterministic, expunge USER_REVIEW |
| Cargo workspace `target_directory` | metadata-backed USER_REVIEW vendor clean |
| Cargo `build.build-dir` | audit complete; execution deferred pending stable effective-path/vendor interface |
| Maven | generic clean deferred; inherited/configured filesets widen destructive scope |
| Gradle | generic clean deferred; task graph/Delete/custom actions widen scope |
| CMake | generic clean deferred; supported project/generator configuration can widen clean scope |
| .NET / MSBuild | generic clean deferred; evaluated/imported target graph and Before/AfterTargets can widen clean scope |
| Meson | exact configured whole build-tree USER_REVIEW implemented; `meson compile --clean` separately unaudited backend behavior |
| Ninja standalone | generic clean deferred; Cleaner paths are not directory-bounded and no complete machine-readable destructive manifest exists |
| GNU Make | generic clean deferred; `clean` is arbitrary project-defined recipe execution and dry-run is not a guaranteed read-only scope probe |
| GNU Automake | clean family deferred; file variables and `*-local` recipes can widen behavior |
| SCons | generic clean deferred; clean discovery executes project Python and `Clean()` adds project-defined paths |

A lower-level backend never inherits broader authority than the higher-level generator that produced it.

## Completed audits intentionally kept non-executable

| Source | Current conclusion | Revisit trigger |
| --- | --- | --- |
| Unity GI Cache | Unity-managed/protected | documented exact external maintenance API/CLI |
| Bazel `--disk_cache` | shared/configurable vendor-managed | stable effective-path + installed GC/control interface |
| Maven/Gradle/CMake/.NET/Ninja/Make/Automake/SCons generic project clean | destructive scope not completely provable without project-defined execution/expansion | stable complete non-executing destructive manifest/model |
| Cargo `build.build-dir` | intermediate state but effective-path discovery insufficient | stable metadata/config query or narrow vendor clean action |
| Ollama raw model store | user-selected content with shared blobs/manifests | exact per-model vendor action only |
| Hugging Face Xet/assets/HF_HOME/token | Xet/assets are separate vendor/application caches while HF_HOME mixes authentication and cache state; raw deletion protected | dedicated exact vendor lifecycle/API per cache class; never whole-HF_HOME deletion |
| PyTorch Hub repo/checkpoint/temp objects | current source has no public complete cache inventory/remove API; repo directory encoding is not reliably reversible; checkpoints lack durable URL/provenance metadata; trusted_list is persistent trust state | public exact inventory + remove/prune API, or durable source/hash provenance with equally strong mutation scope |
| Cypress whole-cache clear / unknown future root objects | audited source clears the entire cache root while prune treats `bundles` and `sessions` as separate non-binary entries; unknown future root shapes fail closed | source contract that separates binary clear from neighboring state, or a later audit explicitly covering all whole-root side effects |
| Yarn Classic / modern machine cache | generic raw whole-tree authority removed: Classic vendor clean widens from reported version dir to base root; modern clean is project/config-sensitive and mirror cleanup triggers plugin hooks | dedicated generation-specific exact lifecycle with bounded effective root, no project/plugin widening, fresh revalidation and USER_REVIEW |
| Bun machine/global module cache | generic raw whole-tree authority removed: current vendor `pm cache rm` also clears Bunx temp state, and the cache may contain `globalStore` content backing project symlinks | dedicated exact USER_REVIEW vendor lane with one bound Bun CLI/root, pinned `BUN_INSTALL_CACHE_DIR`, Bunx side-effect review, reinstall warning and fresh revalidation |
| Maven local repository | remote cache mixed with unique local artifacts | vendor semantics separating safely reclaimable remote content |
| Cargo global registry/git | shared vendor-managed download/source state | stable vendor prune/GC interface |
| WSL distro VHD/rootfs | persistent Linux filesystem/user state | no generic delete lane |
| WSL sparse conversion | current safety contract insufficient | stable safe per-distro operation without unsafe bypass |
| WSL physical VHD compaction | exact distro-to-VHD/offline vendor procedure unproven across variants | stable source-backed vendor procedure + verifiable pre/post state |
| Podman persistent build cache | reproducible cache but current `image prune --build-cache` also owns image-prune scope | dedicated cache-only API/command or complete machine-readable no-image destructive manifest |
| Podman volumes | persistent data even when anonymous/unused | no generic cleanup lane; exact app/user lifecycle only |
| Storage Sense / generic Disk Cleanup profiles | broad categories with mixed user/system semantics and extensible handlers | stable exact one-shot category/object manifest/API |
| Downloads | protected user content | explicit user-content workflow, not generic cleanup |
| WER queue/archive report stores | exact per-report metadata exists but documented purge is whole-store | supported exact per-report delete operation or equally bounded vendor mutation surface |
| Task Manager live user-mode dumps | documented location is mixed `%LOCALAPPDATA%\Temp`; `.dmp` suffix does not prove Task Manager ownership | source-backed exact identity or dedicated per-tool manifest/root |
| Windows Setup/Panther/Rollback/MoSetup/CBS/DISM/SetupDiag diagnostics | source-backed troubleshooting evidence but no generic per-object expiration/deletion contract | Microsoft exact cleanup API/command, per-object expiration signal, or explicit normal deletion lifecycle |

## Cross-cutting protections

- Broad roots never inherit delete authority from names such as `cache`, `tmp`, `temp`, `build`, `target`, `bin`, `obj`, `.cache`, `_bazel_*`, `$Recycle.Bin`, `Windows.old`, `WinSxS` or `ext4.vhdx`.
- User-downloaded models/packages/images/volumes are not silently converted into cache because they are large or redownloadable.
- Vendor-owned GC/lifecycle is preferred over DevClean inventing a second retention/LRU model.
- Raw shared stores/databases/VHDs remain protected when the vendor exposes a safer object-level operation.
- AI never creates command authority or supplies arbitrary destructive commands.
- Physical reclaimed bytes are not equated with logical package/model/image/cache size when layers, hardlinks, shared blobs, VHDs, vendor accounting or concurrent reacquisition make them different.
- A project-provided executable recipe/script does not become safe merely because the target is named `clean`.

## Current high-value queue

The broad Windows diagnostics, Podman/Android package work, Docker unification, Android project-reference explanation, exact Hugging Face Hub object maintenance, PyTorch Hub read-only audit, npm vendor maintenance, Cypress binary-cache lifecycle, Yarn correction, and Bun cache-authority correction are substantially closed. Prefer sources where vendor semantics can still provide a narrow object lifecycle or materially improve explanation without inventing deletion authority.

1. **High-impact application caches**
   - continue auditing known applications individually; npm/Cypress/Hugging Face are vendor-aware, while PyTorch Hub, Yarn and Bun are explicitly protected where a bounded lifecycle is unavailable.
2. **Additional local-model products**
   - models remain user-selected content; exact vendor model actions only, with offline/rebuild value treated as USER_REVIEW.
3. **Remaining broad TOOL/cache rules**
   - continue source-first re-audits of any existing rule that can still grant whole-tree or age/size-driven mutation; keep package-manager generations and project/offline state semantically separate.
4. **Narrow application-specific Windows diagnostics**
   - revisit only when Microsoft or the owning application exposes an exact lifecycle/API; broad `Logs`, `Prefetch`, `CbsTemp`, WER or servicing/setup roots remain protected.

## Explicit anti-goals

DevClean must not pursue coverage percentage by adding rules for every recognizable folder. It must not:

- delete a directory because its name looks temporary;
- use AI to replace source research;
- treat user content as cache because it can be downloaded/rebuilt;
- raw-delete vendor stores/databases/VHDs that own lifecycle/GC;
- expose broad `prune all` operations without semantic separation;
- parse one configuration file and pretend to reproduce a richer precedence/inheritance system;
- silently escalate privilege;
- build shell commands from scanned/user/model text;
- execute project code merely to discover whether cleanup is safe;
- claim physical reclaim from logical size when the storage model cannot prove it.

## Continuation protocol

When a new session continues this project:

1. read this document, `docs/review-lane-policy.md`, and the relevant source audit;
2. inspect current `main`, active PRs and recent merge history before relying on an old branch;
3. pick the first unfinished lane with meaningful disk value and a plausible narrow authority boundary;
4. read current primary vendor documentation/source;
5. determine the semantic lane before coding;
6. if positive, keep audit + implementation in the **same PR** from current `main`;
7. if negative, record REPORT_ONLY/protected plus a concrete revisit trigger instead of adding a weak fallback;
8. do not merge until lock/dependency checks, Ruff, strict mypy, full tests, Windows EXE artifact and CodeQL are green on the final head.

A well-supported decision to **not delete** is a successful audit outcome. Safety boundaries are product functionality, not missing features.
