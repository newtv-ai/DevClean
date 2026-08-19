# Meson configured build-directory audit

Audited: 2026-08-19

## Product conclusion

Meson is materially different from CMake, Maven, Gradle, and generic MSBuild clean lanes. Out-of-source builds are a core Meson design constraint: the source directory and build directory are distinct, build-generated state is placed in the build tree, and Meson's own documentation explicitly describes deleting the build subdirectory as the complete way to clean a project.

That gives DevClean a narrow mutation boundary that does **not** depend on a directory merely being named `build`, `out`, or `builddir`.

DevClean classifies deletion of one **exact, already-configured Meson build directory** as **USER_REVIEW**:

- never selected by default;
- never sent to AI by default;
- only available after Meson-backed source/build identity checks;
- only on local fixed storage with the normal reparse/identity protections;
- delete exactly the verified build-directory tree and nothing else;
- never infer authority from a directory name or from finding `meson.build` nearby.

The technical lifecycle is understood, but a configured build tree can contain compiled binaries, test artifacts, logs, downloaded/build-time intermediates, and an expensive configuration/build state that the user may prefer to keep. Rebuildability therefore does not make full-tree removal universally beneficial.

## Implemented lane

The first executable Meson lane is implemented together with this audit rather than as a stacked follow-up branch.

The desktop flow asks for both the source root and configured build root, performs read-only Meson introspection, shows the exact bound directories and current logical size, and enables full build-tree removal only after all execution checks pass. The action always requires an explicit warning/confirmation.

A 2 GiB threshold is only an informational "worth reviewing" heuristic. It never changes USER_REVIEW classification and never creates deletion authority.

## Primary vendor contract

Current Meson documentation establishes all of the following:

- Meson requires source and build directories to be different;
- out-of-source builds are a core design goal;
- build-generated files are placed in a separate build directory;
- the build tree is self-contained with respect to generated build state;
- Meson explicitly documents complete cleanup as deleting the build subdirectory;
- custom target declared outputs cannot contain path separators and are placed in the corresponding build directory;
- generator outputs are placed in build-private directories;
- a configured project exposes machine-readable introspection data under `meson-info`;
- `meson introspect --buildsystem-files <builddir>` returns the Meson build-definition files used by that configured project as absolute paths.

Primary sources:

- https://mesonbuild.com/Using-multiple-build-directories.html
- https://mesonbuild.com/Commands.html
- https://mesonbuild.com/IDE-integration.html
- https://mesonbuild.com/Reference-manual_functions_custom_target.html
- https://mesonbuild.com/Generating-sources.html
- https://mesonbuild.com/FAQ.html
- https://github.com/mesonbuild/meson/blob/master/mesonbuild/mcompile.py
- https://github.com/mesonbuild/meson/blob/master/mesonbuild/mintro.py

## Why a generic directory rule is still prohibited

Meson's lifecycle contract does not authorize DevClean to search for and remove arbitrary directories named:

- `build`;
- `builddir`;
- `out`;
- `_build`;
- `meson-build`;
- or any other common build-output name.

A directory name does not prove that Meson configured it, which source tree it belongs to, whether the user placed persistent files there, or whether a reparse point redirects it elsewhere.

The authority comes from one exact configured Meson build tree and a freshly revalidated source/build binding.

## Why full build-tree removal is USER_REVIEW

Meson explicitly treats the build tree as regenerable build state, so this is not an AI ambiguity problem. However, full removal can discard current binaries, test/debug artifacts, logs, configuration state, and the time already spent configuring or compiling a large project.

That is the same product distinction DevClean already applies to other understood but potentially valuable generated state: technical identity is known, while the value of retaining it is user-specific.

Size and age may be used only to explain whether review is worthwhile. They never create deletion authority and never auto-select this lane.

## Source/build identity contract

Before offering mutation DevClean:

1. requires the exact source root to contain the top-level `meson.build`;
2. requires the exact build root to contain Meson's configured-build marker `meson-private/coredata.dat`;
3. invokes the configured Meson executable for `--version` and `meson introspect --buildsystem-files <builddir>`;
4. requires the returned absolute build-system file set to include the exact selected `<source-root>/meson.build`;
5. requires every canonical Meson build-definition file (`meson.build`, `meson.options`, `meson_options.txt`) returned by that configured build to remain beneath the selected source root, so a nested subproject cannot masquerade as the configured top-level project;
6. binds the source root and build root to stable Windows directory identities before and after introspection;
7. requires source, build, and the exact build-parent mutation boundary to be on approved local fixed storage;
8. rejects source/build symlinks, junctions, reparse roots, and resolved-path redirection;
9. requires the build root to be distinct from the source root and **not an ancestor of the source root**, so removing the build tree can never contain and erase the source tree itself;
10. re-runs Meson version/introspection and identity checks immediately before mutation.

A build directory may legitimately be a child of the source tree or live elsewhere on local fixed storage. DevClean therefore does not require a hard-coded `<source>/build` layout.

The canonical-build-file containment rule intentionally prefers a false negative for unusual redirected source layouts to accidentally binding a configured build tree to the wrong source root.

## Execution contract

For the initial full-tree lane, Meson's own documented lifecycle contract makes exact build-directory deletion the intended operation. This is not a fallback invented by DevClean after a vendor command failed.

At mutation time DevClean:

1. inventories one exact verified build root;
2. refuses while Meson, Ninja/Samu, MSBuild/Visual Studio, or common compiler/linker activity is visible;
3. fails closed if Windows process state cannot be checked;
4. repeats Meson version, build-system file set, source/build identity, local-storage, and reparse checks immediately before mutation;
5. snapshots the exact build-parent directory as the mutation boundary;
6. calls the existing handle-bound `purge_exact_directory_tree` for the exact verified build root;
7. keeps the verified root/boundary identities fixed throughout deletion;
8. never traverses linked/reparse children outside that boundary; reparse children are removed as links;
9. requires the exact build root to be absent after success;
10. reports observed before/after logical bytes without claiming more physical reclaim than can be proven.

The user must explicitly confirm that the entire configured Meson build tree will be discarded and must be recreated before that configuration can be built again.

## Why `meson compile --clean` is not the first authority surface

Meson exposes `meson compile --clean`, but current Meson source shows that this command is a backend dispatcher rather than one uniform cleanup implementation:

- Ninja backend: invokes the backend `clean` target;
- Visual Studio backend: invokes MSBuild `Clean`;
- Xcode backend: invokes Xcode clean behavior.

DevClean does not rely on those differing backend clean contracts for the first Meson lane because Meson itself documents the stronger and simpler full-build-tree lifecycle boundary.

A future narrower "preserve configuration, remove compiled outputs" lane may audit `meson compile --clean` separately. It does not inherit authority from this full-tree audit without proving the effective backend-specific scope and any generated-project integrity assumptions.

## Why `meson setup --wipe` is not a cleanup substitute

`meson setup --wipe` removes prior build state and then configures the project again. Reconfiguration evaluates project configuration and Meson allows build definitions to run external commands during configuration.

DevClean therefore does not use `--wipe` merely to reclaim disk space or to regenerate backend files before deletion. It performs more than cleanup and can execute project-defined configuration behavior.

## Deliberate exclusions

This lane grants no authority to:

- delete directories because their names look like Meson/build output;
- delete a source tree or any directory that contains the source root;
- delete installed files under Meson's installation prefix;
- delete source-side `subprojects`, wraps, project data, or version-controlled files;
- invoke `meson setup --wipe` as a cleanup shortcut;
- treat `meson compile --clean` as already audited across every backend;
- follow symlinks/junctions/reparse points out of the verified build tree;
- mutate remote, removable, shared, or otherwise unapproved storage;
- use AI to guess whether an arbitrary directory is a Meson build tree or to invent a destructive command.

## Regression requirements

The implementation includes regression coverage for:

- exact configured-build marker detection;
- source/build binding success and mismatch refusal;
- nested subproject/top-level source confusion;
- build-root-as-source-ancestor refusal;
- symlink/junction/reparse refusal;
- local-fixed-volume enforcement;
- identity change between inventory and mutation;
- active build-tool refusal;
- no deletion outside the exact verified build root;
- postcondition verification through the exact purge result;
- no generic build-directory-name authority;
- USER_REVIEW remaining USER_REVIEW even when size makes the action worth reviewing.

Normal DevClean validation remains mandatory before merge: Ruff, strict mypy, full pytest, Windows EXE build/artifact, and CodeQL must all be green.
