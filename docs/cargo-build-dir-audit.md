# Cargo separate build-directory audit

Audited: 2026-08-18

## Product conclusion

Current Cargo distinguishes the stable target directory from a newer separately configurable **build directory** used for intermediate build artifacts.

Cargo's current build-cache documentation describes `build.build-dir` / `CARGO_BUILD_BUILD_DIR` as the directory where intermediate build artifacts are stored. Its default is expressed relative to the workspace/target layout, and Cargo owns cleanup of that intermediate state.

This source is semantically attractive for DevClean because it is more cache-like than the full `target_directory`: final user-facing binaries/docs/package outputs are not the purpose of the separate build directory.

However, DevClean does **not** add an executable cleanup lane yet. The current stable `cargo metadata --format-version 1` response exposes `target_directory` but not the effective `build.build-dir`, while the documented `cargo config get` command that can inspect effective Cargo configuration remains an unstable/nightly command requiring `-Z unstable-options`.

The current decision is therefore **Cargo-managed / audit complete, executable lane deferred until stable effective-path discovery exists**. This is not AI work.

## Why this stays separate from the merged Cargo target lane

DevClean's existing Cargo project maintenance resolves the exact `workspace_root` and `target_directory` from stable `cargo metadata --no-deps`, then exposes full `cargo clean` only as USER_REVIEW because the target tree can contain both intermediate state and final user-facing artifacts.

The separate build directory has a different semantic role: Cargo documents it specifically as intermediate build artifacts. Mixing it back into the full target decision would lose that distinction.

A future build-directory lane should therefore be audited independently and should not infer its location from the already resolved `target_directory` when the user has configured `build.build-dir` elsewhere.

## Configuration surface

Current Cargo configuration documents:

- `[build] build-dir = "..."`;
- environment override `CARGO_BUILD_BUILD_DIR`;
- path templates/relative-path behavior defined by Cargo configuration;
- configuration discovery from project/workspace and Cargo home locations.

That means an apparent directory such as `target`, `target/build`, or a guessed sibling is not authoritative.

DevClean must resolve the **effective** value after Cargo's own configuration precedence rather than parse one nearby `.cargo/config.toml` and assume it wins.

## Why `cargo config get` is not used in a production cleanup path

Cargo documents `cargo config get` as a way to display effective configuration values. That would be a natural source for `build.build-dir`.

But the current command remains unstable and requires nightly Cargo plus `-Z unstable-options`. DevClean is a Windows developer-tool cleanup utility intended to work with ordinary stable toolchains; requiring or silently switching users to nightly merely to discover cleanup authority is not acceptable.

DevClean also does not implement an independent Cargo-config evaluator. Reproducing Cargo's hierarchical config discovery, environment overrides, path/template expansion, and precedence rules would create a second configuration engine whose disagreement with Cargo could redirect a destructive action.

## Why raw directory-name deletion is rejected

The separate build directory may be redirected outside the workspace or target tree. A directory that looks like Cargo intermediate state can also be shared or intentionally retained for build acceleration.

DevClean therefore does not:

- search for directories named `build` and attribute them to Cargo;
- assume `<workspace>/target` or `<workspace>/target/build` is the effective build directory;
- read only the nearest `.cargo/config.toml` and turn its text into delete authority;
- delete a path from `CARGO_BUILD_BUILD_DIR` without confirming Cargo's complete effective configuration;
- reuse the full-target `cargo clean` action and claim it is a narrow intermediate-cache cleanup.

## Vendor lifecycle boundary

Cargo's build-cache documentation describes the build directory as Cargo-managed intermediate state and says Cargo automatically removes relevant intermediate artifacts as part of its build/cache lifecycle.

That makes a future vendor-owned cleanup preferable to DevClean implementing its own age/LRU policy over the directory contents. If Cargo later exposes a stable command or metadata field that reports the effective build directory and a narrow supported clean operation for that state, DevClean should use those interfaces.

## Revisit criteria

Add an executable lane only when stable Cargo provides at least one of:

1. an additional stable `cargo metadata` field for the effective build directory;
2. stable `cargo config get` (or equivalent) that resolves `build.build-dir` under Cargo's own precedence rules; or
3. a supported narrow Cargo command/API for cleaning only the intermediate build directory.

At that point DevClean should still enforce local-fixed/shared-storage boundaries and revalidate the effective path immediately before mutation.

## Primary sources

- Cargo Book, **Build Cache**: current target/build-directory roles, final vs intermediate artifacts, and `build.build-dir` semantics.
  - https://doc.rust-lang.org/cargo/reference/build-cache.html
- Cargo Book, **Configuration**: current `build.build-dir`, `CARGO_BUILD_BUILD_DIR`, configuration hierarchy and value resolution.
  - https://doc.rust-lang.org/cargo/reference/config.html
- Cargo Book, **cargo config**: `cargo config get` behavior and its current unstable/nightly requirement.
  - https://doc.rust-lang.org/cargo/commands/cargo-config.html
