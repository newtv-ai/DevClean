# Cargo separate build-directory audit

Audited: 2026-08-18
Freshened onto current main: 2026-08-19

## Product conclusion

Current Cargo distinguishes the stable target directory from a newer separately configurable **build directory** used for intermediate build artifacts.

Cargo's build-cache documentation describes `build.build-dir` / `CARGO_BUILD_BUILD_DIR` as the directory where intermediate build artifacts are stored. Its default is expressed relative to the workspace/target layout, and Cargo owns cleanup of that intermediate state.

This source is semantically attractive for DevClean because it is more cache-like than the full `target_directory`: final user-facing binaries/docs/package outputs are not the purpose of the separate build directory.

However, DevClean does **not** add an executable cleanup lane yet. Stable `cargo metadata --format-version 1` exposes `target_directory` but not the effective `build.build-dir`, while `cargo config get`, the natural effective-configuration query, remains an unstable/nightly interface requiring `-Z unstable-options` in the audited Cargo contract.

The current decision is therefore **Cargo-managed / audit complete, executable lane deferred until stable effective-path discovery exists**. This is not AI work.

## Why this stays separate from the Cargo target lane

DevClean's Cargo project maintenance resolves the exact `workspace_root` and `target_directory` from stable `cargo metadata --no-deps`, then exposes full `cargo clean` only as USER_REVIEW because the target tree can contain both intermediate state and final user-facing artifacts.

The separate build directory has a different semantic role: Cargo documents it specifically as intermediate build artifacts. Mixing it back into the full target decision would lose that distinction.

A future build-directory lane must therefore be audited independently and must not infer its location from the resolved `target_directory` when the user has configured `build.build-dir` elsewhere.

## Configuration surface

The audited Cargo configuration surface includes:

- `[build] build-dir = "..."`;
- environment override `CARGO_BUILD_BUILD_DIR`;
- Cargo-defined relative-path/template behavior;
- hierarchical configuration discovery from project/workspace and Cargo-home locations.

Therefore a directory such as `target`, `target/build`, or a guessed sibling is not authoritative.

DevClean must resolve the **effective** value after Cargo's own configuration precedence rather than parse one nearby `.cargo/config.toml` and assume it wins.

## Why unstable config discovery is not cleanup authority

`cargo config get` is a natural way to display effective Cargo configuration values, including a configured build directory. But the audited command contract remains unstable/nightly-only.

DevClean is intended to work with ordinary stable toolchains. Requiring, installing, or silently switching to nightly merely to discover a cleanup target is not acceptable.

DevClean also does not implement an independent Cargo-configuration evaluator. Reproducing Cargo's hierarchical discovery, environment overrides, path/template expansion, and precedence rules would create a second configuration engine whose disagreement with Cargo could redirect a destructive action.

## Rejected shortcuts

The build directory may be redirected outside the workspace or target tree, shared, or intentionally retained for acceleration. DevClean therefore does not:

- search for directories named `build` and attribute them to Cargo;
- assume `<workspace>/target` or `<workspace>/target/build` is the effective build directory;
- read only the nearest `.cargo/config.toml` and turn its text into delete authority;
- delete a path from `CARGO_BUILD_BUILD_DIR` without confirming Cargo's complete effective configuration;
- reuse full-target `cargo clean` and claim that it is a narrow intermediate-cache operation.

## Vendor lifecycle boundary

Cargo describes this build directory as Cargo-managed intermediate state. A future Cargo-owned cleanup interface is preferable to DevClean implementing its own age/LRU policy over the contents.

If Cargo exposes a stable metadata/config field for the exact effective build directory and/or a narrow supported cleanup operation, DevClean should use those interfaces rather than raw recursive deletion.

## Revisit criteria

Add an executable lane only when stable Cargo provides at least one of:

1. a stable `cargo metadata` field for the effective build directory;
2. stable `cargo config get` or equivalent that resolves `build.build-dir` under Cargo's own precedence rules; or
3. a supported narrow Cargo command/API for cleaning only the intermediate build directory.

Even then DevClean must revalidate the effective path immediately before mutation and keep redirected/shared/non-local storage non-executable unless the source contract proves the operation is safe there.

## Primary sources

- Cargo Book, **Build Cache** — target/build-directory roles and `build.build-dir` semantics.
- Cargo Book, **Configuration** — `build.build-dir`, `CARGO_BUILD_BUILD_DIR`, hierarchy and value resolution.
- Cargo Book, **cargo config** — effective configuration query and its audited unstable/nightly boundary.
