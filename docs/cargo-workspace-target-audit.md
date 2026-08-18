# Cargo workspace target-directory audit

Audited: 2026-08-18

## Product conclusion

Cargo project build output is a separate semantic source from Cargo's global registry/git cache.

Cargo explicitly provides `cargo clean` to remove generated artifacts from the effective target directory. DevClean can therefore identify and clean this storage through Cargo itself instead of treating arbitrary directories named `target` as disposable.

However, Cargo's current build-cache documentation distinguishes **final build artifacts** from intermediate artifacts. The target directory can contain binaries, library outputs, generated documentation, timing reports, and `cargo package` output that may still be useful to the user even though they are rebuildable.

Full workspace target cleanup is therefore **USER_REVIEW**:

- never selected by default;
- never sent to AI by default;
- always performed through `cargo clean` rather than raw recursive deletion;
- only executable when the effective target directory is a local-fixed descendant of the selected workspace.

A 2 GiB threshold is only a UI benefit heuristic for whether the item is worth reviewing. It never changes the safety classification.

## Project-aware discovery

A directory name is not authoritative because Cargo allows the target directory to be changed with:

- `--target-dir`;
- `CARGO_TARGET_DIR` / `CARGO_BUILD_TARGET_DIR`;
- the `build.target-dir` Cargo configuration value.

DevClean therefore requires the user to select a directory containing `Cargo.toml`, then invokes:

`cargo metadata --format-version 1 --no-deps --manifest-path <selected>/Cargo.toml`

Cargo's metadata format provides absolute `workspace_root` and `target_directory` values. `--no-deps` limits output to workspace members and does not fetch dependencies.

DevClean requires the reported `workspace_root` to exactly match the selected resolved directory. Selecting a workspace member while Cargo reports another root does not grant cleanup authority.

## Why externally located target directories are report-only

Cargo supports moving the target directory outside the workspace. Such a target can be deliberately shared across workspaces or placed on shared/remote storage.

`cargo clean` with no package/profile restriction deletes the entire target directory. DevClean cannot prove from the path alone that an external target belongs exclusively to the selected workspace.

The first Cargo project lane therefore permits execution only when:

1. both workspace and target are on local fixed storage without reparse redirection;
2. the target directory is a strict descendant of the selected workspace root;
3. the target is not the workspace root itself.

An external/shared target is still inventoried and explained but is non-executable.

## Why full clean is USER_REVIEW

Cargo's build-cache documentation divides generated data into:

- final build artifacts intended for Cargo users, such as compiled binaries, docs, and timing/package outputs;
- intermediate build artifacts used internally to accelerate builds.

A full `cargo clean` intentionally removes the generated target artifacts. Rebuildability makes the technical semantics clear, but it does not make every current artifact valueless.

That is user intent, not AI uncertainty.

## Execution contract

Immediately before mutation DevClean:

1. validates an exact selected workspace with `Cargo.toml`;
2. asks Cargo for `workspace_root` and `target_directory` through `cargo metadata --no-deps`;
3. requires exact workspace-root equality;
4. requires target to remain a strict descendant of the workspace on local fixed storage;
5. refuses while Cargo/rustc/rustup/rust-analyzer activity is present;
6. repeats the Cargo metadata check immediately before mutation;
7. invokes only Cargo's own `clean` command;
8. pins the already verified target explicitly with `--target-dir <exact-target>` so a configuration edit cannot redirect the cleanup between inspection and execution;
9. passes the exact selected `Cargo.toml` through `--manifest-path`;
10. measures before/after bytes and surfaces Cargo errors;
11. never falls back to raw recursive deletion.

## Separate current `build-dir` boundary

Current Cargo also supports a `build.build-dir` / `CARGO_BUILD_BUILD_DIR` location for intermediate artifacts, distinct from the target directory. Its lifecycle and stable discovery surface are not conflated with this first lane.

This audit covers only the stable `target_directory` reported by Cargo metadata and the semantics of `cargo clean` for that target. A separately configured build directory should be audited independently if DevClean can resolve and clean it with a stable Cargo-supported interface.

## Explicit non-targets

This lane does not delete or modify:

- Cargo global registry/git caches under `CARGO_HOME`;
- installed Cargo binaries or credentials/configuration;
- arbitrary directories merely named `target`;
- target directories outside the selected workspace;
- shared/remote/removable/reparse-redirected target storage;
- separately configured `build.build-dir` storage;
- source files, `Cargo.toml`, or `Cargo.lock` through direct filesystem deletion.

## Primary sources

- Cargo Book, **cargo clean**: describes `cargo clean` as removal of generated artifacts, full target deletion with no options, target-dir overrides, and dry-run/profile/package alternatives.
  - https://doc.rust-lang.org/cargo/commands/cargo-clean.html
- Cargo Book, **cargo metadata**: stable format-version 1, absolute `workspace_root` and `target_directory`, and `--no-deps` semantics.
  - https://doc.rust-lang.org/cargo/commands/cargo-metadata.html
- Cargo Book, **Build Cache**: target/build directory roles and the distinction between final user-facing artifacts and intermediate build artifacts.
  - https://doc.rust-lang.org/cargo/reference/build-cache.html
- Cargo Book, **Configuration**: `build.target-dir` and the separate current `build.build-dir` configuration surface.
  - https://doc.rust-lang.org/cargo/reference/config.html
