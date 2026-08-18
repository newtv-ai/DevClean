# Maven project-clean audit

Audited: 2026-08-18

## Product conclusion

Maven project build output is a different semantic source from the local artifact repository already audited by DevClean.

Maven's Clean Plugin is the vendor-owned mechanism for removing generated project output. Its default targets are the effective project build, output, test-output, and reporting directories. However, the same `clean` goal can be configured with additional `filesets` that delete arbitrary project-relative files/directories, including configuration inherited from a parent POM.

That means DevClean must **not** expose a naive "run `mvn clean`" button merely because a selected directory contains `pom.xml`.

The current decision is **audit complete, executable lane deferred until DevClean can prove the effective clean scope before invocation**. This is not AI work; it is an execution-authority problem.

## Why generic `target` detection is rejected

Maven commonly uses `target`, but the effective build directory is configurable through the project model. The Clean Plugin itself resolves Maven expressions such as:

- `project.build.directory`;
- `project.build.outputDirectory`;
- `project.build.testOutputDirectory`;
- `project.reporting.outputDirectory`.

A directory merely named `target` does not prove that it is the current Maven build output, and a custom build directory may be located elsewhere.

DevClean therefore must not grant deletion authority from the literal name `target`, just as the Cargo and Bazel project audits do not grant authority from conventional output-directory names.

## Why invoking `mvn clean` is broader than the default output directories

The current Maven Clean Plugin documentation says the `clean` goal normally deletes the effective default output directories. It also explicitly supports `filesets`, which are deleted **in addition** to those defaults.

A fileset can name project-relative directories and include/exclude patterns. Maven also documents that filesets can be defined in a parent POM and inherited by a subproject.

Consequently, DevClean cannot infer the destructive scope of `mvn clean` from these facts alone:

- the selected project has a `pom.xml`;
- `project.build.directory` resolves to a safe local path;
- the conventional `target` directory is large;
- the user only intends to reclaim build output.

A project-specific Clean Plugin configuration may legally cause `mvn clean` to remove additional generated trees outside `target`.

## Why command-line overrides do not currently solve this cleanly

The plugin exposes `maven.clean.excludeDefaultDirectories`, which can suppress the default output directories and leave only configured filesets. This is the opposite of the safety property DevClean needs.

The current public plugin documentation does not expose a corresponding stable user property that means "ignore every configured/inherited fileset and clean only these exact default directories".

DevClean therefore does not assume it can neutralize arbitrary clean-plugin filesets from the command line.

## Effective-path inspection is useful but insufficient

Maven's Help Plugin provides `help:evaluate`, including a scripting mode using `-q -DforceStdout`, and can evaluate project expressions such as `project.build.directory`.

This is a promising mechanism for authoritative path discovery. It could let a future DevClean lane resolve the actual default output directories rather than guessing `target`.

But resolving those paths still does not prove the complete scope of the configured `maven-clean-plugin`. Before delegating to `mvn clean`, DevClean would also need to prove that no extra filesets or other clean behavior expands the deletion set beyond the user-visible manifest.

## Direct deletion is not promoted as a shortcut

One alternative would be to ask Maven for `project.build.directory` and then directly remove only that directory using DevClean's handle-bound filesystem mutation layer.

This audit deliberately does not promote that shortcut yet because:

- multi-module reactors can have multiple build directories;
- output/test/report directories may be configured outside the main build directory;
- a build directory can itself be redirected/shared;
- Maven's own clean lifecycle may encode project/plugin behavior that a raw directory delete would bypass;
- build output can contain final packaged artifacts that are rebuildable but may still have user value.

A future direct-mutation lane would need an explicit, source-backed manifest of every exact target and must remain USER_REVIEW rather than silently emulating Maven clean.

## Multi-module boundary

A root Maven project can aggregate modules. Running the clean lifecycle at the reactor root can execute clean across modules, each with its own effective output paths and inherited plugin configuration.

DevClean therefore must not treat one root `target` directory as the complete cleanup scope of a multi-module build, nor invoke reactor clean without presenting the full effective scope to the user.

This makes project-aware Maven cleanup materially different from Cargo's stable `cargo metadata` response, which directly reports an absolute workspace root and target directory for the current workspace.

## Current DevClean behavior

DevClean intentionally does not:

- mark arbitrary `target` directories as deterministic cleanup solely by name;
- run `mvn clean` from a selected directory without auditing effective clean-plugin configuration;
- assume `project.build.directory` is the only path Maven clean may delete;
- parse one local POM while ignoring parent/inherited plugin configuration;
- raw-delete a reactor root's `target` and claim that Maven project cleanup is complete;
- treat unknown Maven clean scope as an AI classification task.

The existing Maven local-repository policy remains separate and unchanged.

## Revisit criteria for an executable lane

A Maven project-clean action can be added once DevClean can obtain a stable, complete destructive manifest before execution. Acceptable approaches include either:

1. a supported Maven/plugin interface that reports the exact effective clean targets/filesets for the selected reactor without performing deletion; or
2. a DevClean implementation that evaluates the complete effective model/plugin configuration, expands all reactor modules and filesets, confines every path to an approved local boundary, presents that manifest to the user, and then invokes a vendor-supported action whose scope is pinned to that manifest.

Until then, deferring execution is safer than exposing a deceptively simple `mvn clean` button.

## Primary sources

- Apache Maven Clean Plugin 4.x, **clean:clean**: default output directories and the additional configurable `filesets` deletion scope.
  - https://maven.apache.org/plugins/maven-clean-plugin-4.x/clean-mojo.html
- Apache Maven Clean Plugin, **Delete Additional Files Not Exposed to Maven**: project-relative filesets and parent-POM inheritance examples.
  - https://maven.apache.org/plugins/maven-clean-plugin-4.x/examples/delete_additional_files.html
- Apache Maven Help Plugin, **help:evaluate**: expression evaluation and `-q -DforceStdout` scripting support.
  - https://maven.apache.org/plugins/maven-help-plugin/evaluate-mojo.html
