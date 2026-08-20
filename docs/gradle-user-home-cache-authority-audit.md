# Gradle User Home cache authority audit

Audited: 2026-08-20

## Product conclusion

DevClean's former age/size-driven raw deletion authority for Gradle User Home version caches, local build cache directories and daemon logs is removed.

Current lanes:

- Gradle User Home: **REPORT_ONLY / protected from generic raw deletion**;
- `caches/<version>` and snapshot-version directories: semantically identified for explanation, but protected;
- `caches/build-cache-*`: semantically identified for explanation, but protected;
- `daemon/<version>/daemon-*.out.log`: semantically identified for explanation, but protected;
- wrapper distributions, downloaded dependencies/toolchains, user properties, init scripts and daemon coordination state: protected;
- no generic Gradle whole-tree delete root.

The key correction is that Gradle's cache lifecycle is not equivalent to `directory mtime + fixed 7/14/30 day threshold`. Gradle owns a configurable GC policy and, for version-specific caches, uses source-level usage markers and version relationships that DevClean's previous rule did not reproduce.

## Primary source

Audited against `gradle/gradle` commit:

`242eb931e5faf2e0b29f7189025cd4f2ef7264bf`

Primary files/docs:

- `subprojects/core-api/src/main/java/org/gradle/api/cache/CacheConfigurations.java`
- `subprojects/core/src/main/java/org/gradle/api/internal/cache/DefaultCacheConfigurations.java`
- `subprojects/core/src/main/java/org/gradle/cache/internal/GradleUserHomeCleanupService.java`
- `subprojects/core/src/main/java/org/gradle/cache/internal/VersionSpecificCacheCleanupAction.java`
- `subprojects/core/src/main/java/org/gradle/cache/internal/DaemonLogCleanupAction.java`
- `platforms/core-execution/build-cache-core/src/main/java/org/gradle/caching/local/internal/DirectoryBuildCacheEntryRetention.java`
- `platforms/documentation/docs/src/docs/userguide/reference/plugin-development/init_scripts.adoc`

Source URLs:

- https://github.com/gradle/gradle/blob/242eb931e5faf2e0b29f7189025cd4f2ef7264bf/subprojects/core-api/src/main/java/org/gradle/api/cache/CacheConfigurations.java
- https://github.com/gradle/gradle/blob/242eb931e5faf2e0b29f7189025cd4f2ef7264bf/subprojects/core/src/main/java/org/gradle/api/internal/cache/DefaultCacheConfigurations.java
- https://github.com/gradle/gradle/blob/242eb931e5faf2e0b29f7189025cd4f2ef7264bf/subprojects/core/src/main/java/org/gradle/cache/internal/GradleUserHomeCleanupService.java
- https://github.com/gradle/gradle/blob/242eb931e5faf2e0b29f7189025cd4f2ef7264bf/subprojects/core/src/main/java/org/gradle/cache/internal/VersionSpecificCacheCleanupAction.java
- https://github.com/gradle/gradle/blob/242eb931e5faf2e0b29f7189025cd4f2ef7264bf/subprojects/core/src/main/java/org/gradle/cache/internal/DaemonLogCleanupAction.java
- https://github.com/gradle/gradle/blob/242eb931e5faf2e0b29f7189025cd4f2ef7264bf/platforms/core-execution/build-cache-core/src/main/java/org/gradle/caching/local/internal/DirectoryBuildCacheEntryRetention.java
- https://github.com/gradle/gradle/blob/242eb931e5faf2e0b29f7189025cd4f2ef7264bf/platforms/documentation/docs/src/docs/userguide/reference/plugin-development/init_scripts.adoc

## Version-specific cache lifecycle is marker- and version-aware

`VersionSpecificCacheCleanupAction` does not decide that a `caches/<version>` directory is stale from the directory's own mtime.

Current source uses the marker:

`fileHashes/fileHashes.lock`

A version-specific cache is considered for removal only when the marker exists and its marker timestamp satisfies the configured retention boundary. The action also refuses to remove a cache for the current or a newer Gradle version.

Snapshot handling has another semantic that a generic age rule misses: when multiple snapshots share one base version, Gradle keeps at least one snapshot for that base version even when an older snapshot crosses the snapshot retention threshold.

The cleanup action also owns its own cleanup-frequency marker through the current-version cache's `gc.properties`.

Therefore a raw DevClean rule such as “delete a 31-day-old `caches/9.6.1` directory” does not reproduce the vendor decision model even when the default released-cache retention happens to be 30 days.

## Retention is effective configuration, not a DevClean constant

`CacheConfigurations` exposes separate user-home lifecycle configuration for:

- released wrapper distributions;
- snapshot wrapper distributions;
- downloaded resources;
- resources created by Gradle;
- local build-cache entries;
- daemon logs;
- overall cleanup behavior/frequency.

Current defaults include 30 days for released wrappers/downloaded resources, 7 days for snapshots/created resources/build cache, and 14 days for daemon logs, but those values are defaults rather than immutable deletion contracts.

`DefaultCacheConfigurations` materializes these values as configurable entry-retention properties and also supports a configurable cleanup mode. `DirectoryBuildCacheEntryRetention` explicitly observes whether cleanup is disabled and obtains its expiry timestamp from the effective build-cache configuration.

So DevClean must not turn the vendor defaults into independent fixed `TOOL_DELETE` thresholds.

## Daemon logs are vendor-managed too

`DaemonLogCleanupAction` does implement precise file cleanup for `daemon-*.out.log`, and the current default retention is 14 days. However `GradleUserHomeCleanupService` supplies the daemon-log retention timestamp from effective `CacheConfigurations` rather than hard-coding an unconditional external contract.

The previous DevClean rule encoded 14 days directly and could delete a log even when the user's effective Gradle policy retained it longer or cleanup policy had been intentionally changed.

The correct generic scanner behavior is therefore to identify daemon logs for reporting while leaving mutation to Gradle's lifecycle.

## Why static init-script grep is not proof of effective policy

The former implementation tried to protect a Gradle User Home when text under `GRADLE_USER_HOME/init.d` appeared to configure cache cleanup. That is not a sound proof boundary.

Gradle's own init-script documentation says init scripts are executable code and are discovered from multiple places:

1. command-line `-I` / `--init-script` arguments;
2. `$GRADLE_USER_HOME/init.gradle(.kts)`;
3. matching scripts in `$GRADLE_USER_HOME/init.d/`;
4. matching scripts in `$GRADLE_HOME/init.d/`.

All discovered scripts execute, and init scripts can apply plugins and dependencies. A literal grep of only one directory cannot prove the effective cache policy, cannot follow arbitrary program logic, and cannot account for command-line or installation-level scripts.

DevClean also must not execute Gradle, project settings/build files, or init scripts merely to discover a wider cleanup scope. Project-provided executable logic is not a safe read-only query surface.

The new implementation therefore removes the static “custom policy detected” exception entirely: Gradle User Home is protected regardless of whether a recognizable token appears in an init script.

## Build-cache directory names are not authority

The scanner may still recognize existing `caches/build-cache-*` directories to explain what the user is seeing, but recognition is classification only.

The vendor's build-cache lifecycle obtains retention from effective cache configuration. Gradle build cache location and behavior can also vary by build/configuration context. A directory matching `build-cache-1` therefore does not gain raw whole-tree deletion authority from its name.

Age and size remain useful benefit evidence for a future reviewed maintenance lane, but never create mutation authority by themselves.

## Why generic direct deletion is removed now

The former implementation allowed whole-tree deletion of:

- version-specific `caches/<version>` directories after a fixed 30-day release / 7-day snapshot threshold;
- `caches/build-cache-*` after a fixed 7-day threshold;
- daemon log files after a fixed 14-day threshold.

That fails the current DevClean execution standard because:

- version-specific cache usage is represented by a vendor marker, not generic directory mtime;
- snapshot cleanup includes same-base-version retention semantics;
- cleanup frequency is vendor-owned;
- cache, build-cache and daemon-log retention are configurable;
- effective policy can come from executable init scripts outside the one directory previously grepped;
- executing Gradle/init/project code solely to widen mutation authority is outside the product's safety boundary;
- raw deletion bypasses the vendor lifecycle that already owns these decisions.

The correct interim state is REPORT_ONLY / protected, not a second heuristic GC implementation.

## No learned-rule bypass

Application semantics outrank generic AI/user filename heuristics. A later AI or user verdict for a path under the protected Gradle User Home must not manufacture a generic delete rule for version caches, build cache, or daemon logs.

This PR adds regression coverage for that boundary in addition to the application-policy tests.

## Revisit conditions

A future positive Gradle User Home lane should not restore generic whole-tree age/size deletion. It would require a source-backed vendor operation or non-executing machine-readable plan that preserves effective Gradle lifecycle semantics.

At minimum, a positive lane would need:

1. one exact reviewed Gradle installation/executable and Gradle User Home identity;
2. a complete source-backed way to obtain the effective cleanup decision without executing untrusted project/settings/init code merely for discovery;
3. exact vendor-selected objects or exact vendor cleanup action rather than name-based folder guesses;
4. preservation of version marker, current-version and same-base snapshot semantics;
5. preservation of effective cache/build-cache/daemon-log retention and cleanup-disabled behavior;
6. no hidden widening into wrapper/toolchain/shared dependency/configuration state;
7. active Gradle process/concurrency guards where required;
8. fresh tool/root/config/object revalidation immediately before mutation;
9. explicit USER_REVIEW if the action can remove useful offline/rebuild state or has wider side effects;
10. postcondition and reclaim reporting that do not overstate physical bytes.

Until those conditions are implemented together, Gradle User Home remains visible but non-executable.

## Validation

This PR removes Gradle whole-tree cache authority and adds regression tests that age, size, process state, init-script text, AI verdicts and user verdicts cannot restore raw deletion. Normal final-head gate remains mandatory: lock/dependency checks, Ruff, strict mypy, full pytest/current workflow, Windows EXE artifact and CodeQL must all be green before merge.
