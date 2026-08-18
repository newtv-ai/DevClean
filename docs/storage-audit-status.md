# DevClean storage-source audit status

Last updated: 2026-08-18

This document is the durable handoff for DevClean's storage-source audit. It exists so a future review can continue from repository state instead of reconstructing decisions from chat history.

## Decision model

Every storage source should end in one of these product outcomes:

- **DETERMINISTIC_CANDIDATE** — DevClean has a narrow, source-backed reason the data is disposable/reproducible and can recommend it locally.
- **USER_REVIEW** — the technical meaning is known, but value/rebuild/download/compatibility cost depends on the user. Never default-select; AI is unnecessary by default.
- **AI_REVIEW** — local/source evidence is genuinely insufficient to identify the item or decide whether removal is appropriate.
- **REPORT_ONLY / vendor-managed / protected** — known installed/shared/persistent state, or vendor-owned lifecycle, for which DevClean has no generic delete authority.

The order is deliberate: **vendor/source facts first -> deterministic local decision -> user intent -> AI only for residual ambiguity**.

## Audit/implementation standards

A storage source is not considered complete merely because a likely directory was found. Every executable lane should establish, as applicable:

1. primary vendor documentation for identity/lifecycle;
2. exact source-backed root/path discovery rather than filename/directory-name guessing;
3. separation from neighboring storage with different semantics;
4. vendor CLI/API preference over direct filesystem deletion;
5. local-fixed/shared-storage boundary for DevClean-owned destructive mutations;
6. symlink/junction/reparse refusal and stable identity checks where direct mutation is unavoidable;
7. process/in-use guards when concurrent use changes safety;
8. fresh revalidation immediately before mutation;
9. postcondition and before/after/reclaimed-byte evidence;
10. regression tests plus Ruff, strict mypy, full pytest, Windows EXE artifact, and CodeQL before merge.

Size/age thresholds are benefit heuristics only. They never create deletion authority.

## Completed functional maintenance lanes

These sources have dedicated source-aware maintenance rather than broad raw-delete rules:

| Source | Product decision / action |
| --- | --- |
| Codex history/storage | exact application-specific maintenance; persistent state kept separate from disposable data |
| Claude Code storage | exact application-specific maintenance with protected persistent state |
| Cursor storage | application-aware maintenance rather than generic editor-cache deletion |
| VS Code storage | application-aware maintenance rather than broad IDE directory deletion |
| NuGet local resources | official `dotnet nuget locals`; HTTP/temp/plugin caches deterministic, global packages USER_REVIEW |
| pip cache | vendor-supported cache maintenance |
| pnpm store | vendor garbage collection rather than whole-store deletion |
| uv cache | vendor garbage collection |
| Go caches | vendor commands for build/module/test cache semantics |
| Conda caches | vendor cleanup with packages/environments kept semantically separate |
| Conan 2 cache | `conan cache clean`; source/build/download/temp cleanup without deleting recipes/package artifacts/config/remotes |
| Unreal Engine DDC | project/vendor-aware DDC cleanup; no raw recursive delete of Zen/custom DDC trees |
| Bazel workspace output | exact workspace/output-base resolution through Bazel; ordinary clean vs user-confirmed expunge separated |
| Cargo workspace target | exact `cargo metadata` workspace/target discovery; full `cargo clean` is USER_REVIEW and vendor-owned |
| Unity project `Library` | USER_REVIEW; exact project boundary, closed Editor, handle-bound direct mutation |
| Unity Asset Store packages | USER_REVIEW per exact `.unitypackage`; no whole-cache delete |
| Unity UPM legacy `packages` | USER_REVIEW only for the deprecated subtree; current registry `db` remains Unity-managed |
| Ollama models | USER_REVIEW per exact vendor model identity/API; raw model store remains protected |
| Android SDK packages | USER_REVIEW per exact `sdkmanager` package identity; whole SDK remains protected |

## Completed audits that intentionally do not expose a cleanup button

These are not missing features. The audit concluded that a generic executable lane would weaken safety or duplicate a vendor lifecycle.

| Source | Current conclusion | Revisit trigger |
| --- | --- | --- |
| Unity GI Cache | Unity-managed/protected; automatic size/LRU behavior and vendor Clean Cache; full clear is last-resort | documented external Unity CLI/API for exact GI-cache maintenance |
| Bazel `--disk_cache` | Bazel-managed/shared; Bazel 7.4+ owns size/age GC; effective path may be shared/redirected | stable effective-path + installed vendor GC interface |
| Maven project clean | executable lane deferred because `maven-clean-plugin` can add/inherit arbitrary filesets beyond `target` | complete effective destructive manifest before invocation |
| Gradle project clean | executable lane deferred because `clean`/`Delete` tasks are extensible and multi-project task graphs can widen scope | supported integration proving complete task graph/targets/actions |
| Cargo separate `build.build-dir` | Cargo-managed intermediate state; stable metadata does not expose effective path and `cargo config get` is currently unstable/nightly | stable metadata/config query or narrow vendor clean action |
| Ollama raw model store | downloaded user content with shared blobs/manifests | never raw-delete; use per-model vendor action only |
| Maven local repository | mixed downloaded cache and unique locally installed/deployed artifacts | vendor semantics that can distinguish/reclaim only disposable remote artifacts |
| Cargo global registry/git state | vendor-managed/shared download/source state, not a generic cache tree | stable vendor prune/GC interface with exact semantics |

## Cross-cutting protections already established

- Broad stock roots do not inherit delete authority from names such as `cache`, `temp`, `build`, `target`, `.cache`, or `_bazel_*`.
- Windows exact cleanup uses handle-bound identity checks and refuses reparse traversal.
- Application whole-tree cleanup re-establishes current semantic eligibility immediately before mutation.
- Direct Unity mutation lanes require local fixed storage; shared/remote/removable/reparse-redirected sources remain inspectable but non-executable.
- User-owned downloaded models/packages are not silently converted into cache merely because they are large.
- Vendor-owned GC/lifecycle is preferred over DevClean inventing a second age/LRU implementation.

## Remaining high-value audit queue

The following areas still deserve dedicated source audits or narrower implementations. Priority is based on typical developer disk impact and the likelihood of obtaining a safe vendor-owned action.

### Priority A — high value and plausible safe action

1. **Windows-supported cleanup flows**
   - Windows Update download/servicing leftovers: replace the current broad/manual root with an OS-supported/service-aware action if Microsoft exposes a stable one; never raw-delete `SoftwareDistribution\Download` as routine cleanup.
   - `Windows.old`: preserve rollback semantics; use Windows-supported removal only after explicit USER_REVIEW.
   - component/store cleanup: audit DISM/Storage Sense/Disk Cleanup scopes separately; do not conflate WinSxS servicing with ordinary files.

2. **Android/Gradle ecosystem follow-ups**
   - AVD/system-image relationship: package uninstall exists, but DevClean can improve explanations by correlating installed system images with local AVD usage without granting automatic delete authority.
   - Gradle project output: revisit only when the full effective clean task scope can be proven.

3. **Container tooling**
   - Docker/Podman: split dangling build cache, stopped containers, unused images, volumes, and builder caches. Prefer itemized/vendor prune APIs; never expose one broad destructive prune as a default action.

### Priority B — large developer caches but shared/configurable semantics

4. **JetBrains family exact children**
   - split indexes/caches/logs/system state by product/version instead of treating a broad IDE working root as one cache;
   - prefer IDE/vendor cache invalidation or documented storage semantics where available.

5. **Maven/Gradle project outputs**
   - Maven: complete effective clean-plugin scope remains the blocker;
   - Gradle: complete task-graph/destructive-target proof remains the blocker.

6. **Bazel disk cache**
   - already semantically audited; revisit only when Bazel can report effective cache configuration and expose stable installed GC/control.

### Priority C — broad roots previously downgraded and worth splitting

7. **Windows diagnostics/maintenance roots**
   - WER/crash dumps: user diagnostic value means USER_REVIEW unless a narrower source-backed aging policy is justified;
   - Prefetch/Logs/CbsTemp: keep broad roots non-deterministic; audit exact sub-sources rather than promoting the parent.

8. **Generic `%USERPROFILE%\.cache` descendants**
   - audit high-impact known applications one descendant at a time;
   - never restore delete authority to the whole generic parent.

9. **LM Studio / other local-model products**
   - full model directories remain user-selected/downloaded content;
   - add per-model vendor actions only where an exact supported model-manager API/CLI exists.

## Explicit non-goals

DevClean should not pursue coverage percentage by adding rules for every recognizable folder. The following are anti-goals:

- deleting a directory because its name looks temporary;
- using AI to compensate for missing source research;
- treating user content as cache because it can be downloaded again;
- raw-deleting vendor databases/stores that already own GC;
- broad `prune all` operations without a user-visible destructive manifest;
- parsing only one configuration file when the tool has richer precedence/inheritance semantics;
- claiming reclaimed space from logical package/model size when shared blobs/hardlinks make the physical result different.

## How to continue this audit

When starting a new session, use this document plus `docs/review-lane-policy.md` and the source-specific audit documents as the handoff. Pick the first unfinished source with a plausible safe action, read current primary vendor documentation, and either:

- implement the narrow vendor-owned/user-review lane with tests and full CI; or
- document why execution authority is not yet safe, including a concrete revisit condition.

A well-supported decision to **not delete** is a completed audit outcome, not a failure to add a feature.
