# Meson configured build-directory audit

Audited: 2026-08-19

## Product conclusion

Meson is materially different from CMake, Maven, Gradle, and generic MSBuild clean lanes. Out-of-source builds are a core Meson design constraint: the source directory and build directory are distinct, build-generated state is placed in the build tree, and Meson's own documentation explicitly describes deleting the build subdirectory as the complete way to clean a project.

That gives DevClean a plausible narrow mutation boundary that does **not** depend on a directory merely being named `build`, `out`, or `builddir`.

The first DevClean lane should classify deletion of one **exact, already-configured Meson build directory** as **USER_REVIEW**:

- never selected by default;
- never sent to AI by default;
- only available after Meson-backed source/build identity checks;
- only on local fixed storage with the normal reparse/identity protections;
- delete exactly the verified build-directory tree and nothing else;
- never infer authority from a directory name or from finding `meson.build` nearby.

The technical lifecycle is understood, but a configured build tree can contain compiled binaries, test artifacts, logs, downloaded/build-time intermediates, and an expensive configuration/build state that the user may prefer to keep. Rebuildability therefore does not make full-tree removal universally beneficial.

## Primary vendor contract

Current Meson documentation establishes all of the following:

- Meson requires source and build directories to be different;
- out-of-source builds are a core design goal;
- build-generated files are placed in a separate build directory;
- the build tree is self-contained with respect to generated build state;
- Meson explicitly documents complete cleanup as deleting the build subdirectory;
- custom target declared outputs cannot contain path separators and are placed in the corresponding build directory;
- generator outputs are placed in build-private directories;
- a configured project exposes machine-readable introspection data under `meson-info`, and `meson introspect --buildsystem-files <builddir>` lists the Meson build files used by that configured project.

Primary sources:

- https://mesonbuild.com/Using-multiple-build-directories.html
- https://mesonbuild.com/Commands.html
- https://mesonbuild.com/IDE-integration.html
- https://mesonbuild.com/Reference-manual_functions_custom_target.html
- https://mesonbuild.com/Generating-sources.html
- https://mesonbuild.com/FAQ.html
- https://github.com/mesonbuild/meson/blob/master/mesonbuild/mcompile.py

## Why a generic directory rule is still prohibited

Meson's lifecycle contract does not authorize DevClean to search for and remove arbitrary directories named:

- `build`;
- `builddir`;
- `out`;
- `_build`;
- `meson-build`;
- or any other common build-output name.

A directory name does not prove that Meson configured it, which source tree it belongs to, whether the user placed persistent files there, or whether a reparse point redirects it elsewhere.

The authority must come from one exact configured Meson build tree and a freshly revalidated source/build binding.

## Why full build-tree removal is USER_REVIEW

Meson explicitly treats the build tree as regenerable build state, so this is not an AI ambiguity problem. However, full removal can discard current binaries, test/debug artifacts, logs, configuration state, and the time already spent configuring or compiling a large project.

That is the same product distinction DevClean already applies to other understood but potentially valuable generated state: technical identity is known, while the value of retaining it is user-specific.

Size and age may be used only to explain whether review is worthwhile. They never create deletion authority and must never auto-select this lane.

## Source/build identity requirements

A future executable lane should require the user to select or otherwise establish both an exact Meson source root and an exact configured build directory.

Before offering mutation DevClean should, at minimum:

1. require the exact source root to contain the top-level `meson.build`;
2. require the exact build root to contain Meson's configured-build marker `meson-private/coredata.dat`;
3. invoke the configured Meson executable against the exact build root using a read-only introspection operation such as `meson introspect --buildsystem-files <builddir>`;
4. require the returned configured build-system file set to include the exact selected `<source-root>/meson.build`;
5. reject ambiguous source/build bindings rather than guessing from nearest-parent directory names;
6. bind the source root and build root to stable filesystem identities;
7. require both roots to be on approved local fixed storage;
8. reject build-root symlinks, junctions, mount-point redirection, or other reparse indirection;
9. require the build root to be distinct from the source root and **not an ancestor of the source root**, so removing the build tree can never contain and erase the source tree itself;
10. re-run the Meson introspection and identity checks immediately before mutation.

A build directory may legitimately be a child of the source tree or live elsewhere on local fixed storage. DevClean therefore should not require a hard-coded `<source>/build` layout.

## Execution direction

For the initial full-tree lane, Meson's own documented lifecycle contract makes exact build-directory deletion the intended operation. This is not a fallback invented by DevClean after a vendor command failed.

If implemented, DevClean should use its existing handle-bound exact-directory purge semantics rather than an unguarded path-based recursive delete:

1. inventory one exact verified build root;
2. refuse while Meson or the active backend/compiler toolchain is using the build;
3. repeat source/build identity, local-storage, and reparse checks immediately before mutation;
4. keep the verified boundary handle/identity fixed throughout deletion;
5. never traverse linked/reparse children outside that boundary;
6. require the exact build root to be absent after success;
7. report observed before/after evidence without claiming more physical reclaim than can be proven.

The user must explicitly confirm that the entire configured Meson build tree will be discarded and must be recreated before the project can be built from that configuration again.

## Why `meson compile --clean` is not the first authority surface

Meson exposes `meson compile --clean`, but current Meson source shows that this command is a backend dispatcher rather than one uniform cleanup implementation:

- Ninja backend: invokes the backend `clean` target;
- Visual Studio backend: invokes MSBuild `Clean`;
- Xcode backend: invokes Xcode clean behavior.

DevClean does not need to rely on those differing backend clean contracts for the first Meson lane because Meson itself documents the stronger and simpler full-build-tree lifecycle boundary.

A future narrower "preserve configuration, remove compiled outputs" lane may audit `meson compile --clean` separately. It should not inherit authority from this full-tree audit without proving the effective backend-specific scope and any generated-project integrity assumptions.

## Why `meson setup --wipe` is not a cleanup substitute

`meson setup --wipe` removes prior build state and then configures the project again. Reconfiguration evaluates project configuration and Meson explicitly allows build definitions to run external commands during configuration.

DevClean therefore should not use `--wipe` merely to reclaim disk space or to regenerate backend files before deletion. It performs more than cleanup and can execute project-defined configuration behavior.

## Deliberate exclusions

This audit grants no authority to:

- delete directories because their names look like Meson/build output;
- delete a source tree or any directory that contains the source root;
- delete installed files under Meson's installation prefix;
- delete source-side `subprojects`, wraps, project data, or version-controlled files;
- invoke `meson setup --wipe` as a cleanup shortcut;
- treat `meson compile --clean` as already audited across every backend;
- follow symlinks/junctions/reparse points out of the verified build tree;
- mutate remote, removable, shared, or otherwise unapproved storage;
- use AI to guess whether an arbitrary directory is a Meson build tree or to invent a destructive command.

## Revisit / implementation condition

This audit is positive for a narrow USER_REVIEW implementation, provided the implementation can establish the source/build identity and handle-bound local deletion boundary described above.

Before merge, the implementation must include regression tests for at least:

- exact configured-build detection;
- source/build binding success and mismatch refusal;
- build-root-as-source-ancestor refusal;
- symlink/junction/reparse refusal;
- local-fixed-volume enforcement;
- identity change between inventory and mutation;
- active build-tool refusal;
- no deletion outside the exact verified build root;
- postcondition verification;
- no generic build-directory-name authority.

Normal DevClean validation remains mandatory: Ruff, strict mypy, full pytest, Windows EXE build/artifact, and CodeQL must all be green before merge.
