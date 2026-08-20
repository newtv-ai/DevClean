# Android project -> SDK package positive-reference audit

Audited/implemented: 2026-08-20

## Product conclusion

Android project/build-file correlation can improve package explanations, but it **cannot prove that an installed SDK package is unused**.

Gradle build scripts are executable/extensible logic. Effective Android SDK versions can come from literal module DSL, the Android settings plugin, variables, convention plugins, version catalogs, included builds, generated logic, environment/properties, or Android Gradle Plugin defaults. DevClean therefore must not run an arbitrary Gradle build merely to manufacture cleanup authority and must not interpret a missing static reference as an uninstall recommendation.

The safe first lane is **read-only positive evidence** from one exact user-selected local Gradle project/module directory:

- literal `compileSdk` / `compileSdkVersion` -> `platforms;android-N`;
- literal `buildToolsVersion` -> `build-tools;VERSION`;
- literal `ndkVersion` -> `ndk;VERSION`;
- literal `externalNativeBuild { cmake { version ... } }` -> `cmake;VERSION`;
- Android settings plugin `compileSdk { version = release(N) ... }` -> `platforms;android-N`.

These mappings may explain that an installed package has an explicit consumer. **They create no new uninstall authority.** Absence of a result is never rendered as "unused", "stale", or "safe to delete".

## Current Android contracts

Current Android documentation establishes:

- `compileSdk` controls the SDK API level used to compile a module;
- the Android settings plugin can define project-wide SDK values in `settings.gradle`, while module-level values can override them;
- Build Tools are required to build Android apps, but modern Android Gradle Plugin versions can choose a default Build Tools version when `buildToolsVersion` is not explicitly set;
- NDK versions are installed side-by-side under the Android SDK, and AGP can choose/download a default compatible NDK when the project does not explicitly set `ndkVersion`;
- `externalNativeBuild.cmake.version` can request a CMake version, while other CMake selection mechanisms also exist;
- `sdkmanager` package IDs use exact SDK-style paths such as `platforms;android-36`, `build-tools;36.0.0`, `ndk;<version>`, and `cmake;<version>`.

Primary sources:

- https://developer.android.com/build
- https://developer.android.com/build/android-settings-plugin
- https://developer.android.com/studio/releases/build-tools
- https://developer.android.com/studio/projects/configure-agp-ndk
- https://developer.android.com/reference/tools/gradle-api/8.3/com/android/build/api/dsl/Cmake
- https://developer.android.com/tools/sdkmanager

## Why this is not an effective Gradle configuration parser

A text match is intentionally narrower than Gradle semantics. Examples that **must not** be guessed include:

```kotlin
compileSdk = libs.versions.compileSdk.get().toInt()
ndkVersion = rootProject.extra["ndkVersion"] as String
buildToolsVersion = versions.androidBuildTools
```

Those values may be fully valid at build time, but static text does not tell DevClean the effective result without evaluating more build logic. Running Gradle is not an acceptable fallback because the repository's Gradle audit already establishes that project configuration/task logic is executable and extensible.

The explainer therefore records only literal values with unambiguous package mappings and explicitly warns that dynamic configuration may be missing from the report.

## Selected-project boundary

The user chooses one project/root/module directory explicitly. DevClean then:

1. requires that root to be an ordinary, non-reparse directory on local fixed storage;
2. requires a direct `settings.gradle(.kts)` or `build.gradle(.kts)` marker;
3. scans only `build.gradle`, `build.gradle.kts`, `settings.gradle`, and `settings.gradle.kts`;
4. does not follow symlink/junction/reparse directories;
5. skips conventional generated/cache trees such as `.gradle`, `build`, `.cxx`, `.externalNativeBuild`, `.git`, `.idea`, `node_modules`, and `out`;
6. limits recursion depth, file count, and individual script size;
7. reads each participating script with stable before/after filesystem identity checks;
8. strips comments before matching so commented examples do not become evidence;
9. never executes Gradle, the wrapper, plugins, shell commands, or project code.

Any skipped/unreadable boundary is shown as a warning. Since absence is never used as deletion authority, an incomplete scan cannot accidentally turn a package into a cleanup candidate.

## Positive mapping details

### Platforms

Literal forms such as:

```kotlin
android { compileSdk = 35 }
```

or legacy Groovy forms such as:

```groovy
android { compileSdkVersion "android-35" }
```

map to `platforms;android-35`.

The settings-plugin form:

```kotlin
android {
    compileSdk {
        version = release(36)
    }
}
```

maps to `platforms;android-36`.

If settings and a module contain different explicit values, DevClean keeps both as positive evidence. It does not attempt precedence evaluation.

### Build Tools

Only an explicit literal `buildToolsVersion` maps to `build-tools;VERSION`. A missing declaration is intentionally silent because AGP can choose a default.

### NDK

Only an explicit literal `ndkVersion` maps to `ndk;VERSION`. `ndkPath` and AGP-default selection are not converted into SDK package IDs by this lane.

### CMake

Only a literal `version` inside an `externalNativeBuild` -> `cmake` block maps to `cmake;VERSION`. Unrelated blocks named `cmake` do not count.

## Relationship to Android SDK package maintenance

The existing Android SDK package maintenance lane remains unchanged:

- every executable package is USER_REVIEW;
- system images retain their strict AVD reference gate;
- sdkmanager remains the only uninstall mechanism;
- whole SDK/package-directory deletion remains prohibited.

This project scanner is an explanatory companion. It shows whether an exact literal project declaration maps to a currently installed package and where that declaration came from. It does not disable or enable the sdkmanager uninstall button in the separate package-maintenance dialog, because the static scan is not a complete effective-project dependency model.

A future deeper integration may use positive references as an additional protection signal across selected project sets, but only if the product can make the scope and incompleteness obvious without implying that unlisted projects/packages are unused.

## Deliberate exclusions

No authority is granted to:

- call a package unused because no literal reference was found;
- execute Gradle or `gradlew` to discover SDK versions;
- evaluate arbitrary Groovy/Kotlin code;
- resolve version catalogs/convention plugins/included builds into deletion authority;
- infer AGP default Build Tools/NDK versions as a complete project dependency model;
- modify build files;
- uninstall an SDK package from the explainer UI;
- convert project age, package age, or package size into a cleanup decision.

## Validation

Normal DevClean validation remains mandatory: lock/dependency checks, Ruff, strict mypy, full pytest/current workflow, Windows EXE artifact and CodeQL must all be green before merge.
