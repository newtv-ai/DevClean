# Gradle project-clean audit

Audited: 2026-08-18

## Product conclusion

Gradle project build output is separate from Gradle User Home caches. The built-in Base Plugin defines `clean` as a `Delete` task that removes the project's configured `layout.buildDirectory`, and the conventional default is `build/`.

That does **not** justify a generic DevClean rule that deletes directories named `build`, nor does it justify blindly invoking `gradlew clean` in every selected project.

Gradle build logic is executable and extensible: `Delete` tasks can be configured with additional targets, task authors can add execution actions, and multi-project builds can expose multiple `clean` tasks. The current decision is therefore **project-clean semantics understood, executable DevClean lane deferred until the effective destructive task scope can be proven before invocation**.

This is not AI work. The uncertainty is execution authority, not file classification.

## Why generic `build/` detection is rejected

Gradle documents `build/` as the conventional project output directory, but `layout.buildDirectory` is configurable. Plugins and project scripts can also create outputs elsewhere.

A directory merely named `build` is therefore not authoritative. Conversely, a custom build directory can be legitimate generated output even when its name is unrelated to `build`.

DevClean must obtain effective build/task information from Gradle rather than infer it from directory names.

## Why `gradlew clean` cannot be treated as a fixed delete contract

With the Base Plugin, the standard `clean` task is of type `Delete` and deletes `layout.buildDirectory`. Gradle's public `Delete` API also allows additional targets through `delete(...)`, exposes a resolved `targetFiles` collection, and allows symlink-following behavior to be configured.

Gradle tasks are configurable build logic. Build authors/plugins can configure named tasks and add custom task actions. Therefore the presence of a task named `clean` does not prove that its complete destructive behavior is limited to the conventional build directory.

A safe DevClean action must not assume that the task name itself is a capability token.

## Multi-project boundary

Gradle builds can contain subprojects, each with its own project directory, build directory, plugins, and tasks. Command-line task selection can execute tasks in project-specific contexts.

A root-level cleanup flow therefore needs to know the exact set of projects/tasks that will run and the exact destructive targets for each. Deleting only the root `build/` would be incomplete; invoking an uninspected reactor-style clean could be broader than the user-visible manifest.

## Promising inspection primitive

The public `Delete` task API exposes `getTargetFiles()`, documented as the resolved set of files the task will delete. This makes Gradle more promising than a pure text-based build-script parser: a future DevClean inspection plugin/init script could configure the build normally and ask each exact `Delete` clean task for its resolved targets.

However, `targetFiles` alone does not prove the absence of additional custom task actions or other tasks wired into the requested clean execution graph. Before exposing an executable lane, DevClean needs a fail-closed way to prove that the full task graph's destructive scope matches the presented manifest.

## Current DevClean behavior

DevClean intentionally does not:

- mark arbitrary `build` directories as deterministic cleanup;
- invoke `gradlew clean` solely because wrapper/build files are present;
- assume every task named `clean` is the untouched Base Plugin task;
- assume a root project's build directory covers all subprojects;
- raw-delete Gradle User Home caches as a substitute for project cleanup;
- treat an unproven clean-task scope as an AI classification problem.

Gradle User Home and project-local cache cleanup remain governed by Gradle's own automatic cache lifecycle and the existing DevClean vendor-managed policy.

## Revisit criteria

An executable Gradle project-clean lane can be added when DevClean can use a supported Gradle integration to produce a complete, stable pre-execution manifest that proves:

1. the exact build root and project graph;
2. every clean task that will execute;
3. every resolved destructive target;
4. no additional custom action can delete outside that manifest;
5. all targets satisfy DevClean's local-fixed/reparse/shared-storage boundaries.

If that proof can be established, the operation should still be `USER_REVIEW`: generated build output can include final archives/binaries/reports that are rebuildable but may have current user value.

## Primary sources

- Gradle current User Manual, **The Base Plugin**: standard `clean` is a `Delete` task that deletes `layout.buildDirectory`.
  - https://docs.gradle.org/current/userguide/base_plugin.html
- Gradle current API, **Delete**: additional delete targets, resolved `targetFiles`, and symlink behavior.
  - https://docs.gradle.org/current/javadoc/org/gradle/api/tasks/Delete.html
- Gradle current User Manual, **Working With Files**: arbitrary `Delete` tasks can target configured files/directories.
  - https://docs.gradle.org/current/userguide/working_with_files.html
- Gradle current User Manual, **Understanding/Writing Tasks**: tasks are configurable/extensible build logic and can have custom execution actions.
  - https://docs.gradle.org/current/userguide/more_about_tasks.html
- Gradle current User Manual, **Gradle-managed Directories and Caches**: Gradle automatically manages/cleans User Home and project-local caches independently of project build output.
  - https://docs.gradle.org/current/userguide/directory_layout.html
