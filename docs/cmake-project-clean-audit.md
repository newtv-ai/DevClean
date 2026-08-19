# CMake project-clean audit

Audited: 2026-08-19

## Product conclusion

CMake project build output is technically understood, but the destructive scope
of the generated build system's `clean` target is **project- and
generator-configurable**.

CMake documents `cmake --build <dir> --target clean` as the portable way to ask
the generated native build system to run its `clean` target. That does **not**
mean DevClean can safely treat the command as equivalent to "remove only files
inside this build directory".

CMake exposes supported mechanisms that deliberately add files and directories
to the global clean target, and Makefile generators also remove files marked
`GENERATED`. Custom-command outputs and byproducts participate in generator
clean semantics as well.

The current decision is therefore:

**audit complete; generic executable CMake project-clean lane deferred until
DevClean can prove the complete effective clean scope before invocation.**

This is an execution-authority problem, not an AI-classification problem.

## Why a directory named `build` is not authority

`build`, `cmake-build-*`, `out`, and similar names are conventions, not CMake
identity.

CMake supports arbitrary source and binary directories through `cmake -S` and
`cmake -B`. A binary tree also depends on its configured generator and cache.
DevClean must therefore never raw-delete a directory merely because its name
looks like a conventional CMake build directory.

A future lane would first need to prove that the selected directory is the exact
configured binary tree for the intended project, rather than infer that fact
from its basename.

## Vendor clean is real, but its scope can be widened

The CMake command-line documentation says that clean-only operation can be
requested with:

```text
cmake --build <dir> --target clean
```

The user-interaction guide describes `clean` as deleting built object files and
other output files. However, CMake also exposes explicit configuration surfaces
that alter what clean removes.

### `ADDITIONAL_CLEAN_FILES`

CMake provides both directory and target `ADDITIONAL_CLEAN_FILES` properties.
Their documented purpose is to add files or directories to the global clean
target.

For Ninja and Makefile generators:

- entries may be files **or directories**;
- relative paths are interpreted relative to the current binary directory;
- generator expressions are allowed;
- these entries are removed as part of global clean.

The documentation does not constrain the property to a literal `build`
subdirectory. An absolute path can therefore describe a clean target outside the
selected binary tree, and a relative path can contain parent traversal after
normal path resolution.

Consequently, proving that the selected binary directory itself is safe does not
prove that all paths reachable through its generated `clean` target are safe.

### Generated files and custom-command byproducts

CMake's `GENERATED` source-file property documentation states that Makefile
generators remove `GENERATED` files during `make clean`.

Files become generated through supported CMake mechanisms including
`add_custom_command()` outputs, `BYPRODUCTS`, custom targets, and CMake AUTOGEN
features. The clean set is therefore derived from project build metadata, not
merely from the physical contents of one conventional output folder.

`CLEAN_NO_CUSTOM` further demonstrates that custom-command clean behavior is a
first-class generator policy: for Makefile generators it can suppress removal of
custom-command outputs for a directory. Other generators have different clean
implementations.

## Generator differences matter

CMake is an abstraction over native build systems. `cmake --build` delegates to
the configured generator's build tool.

The exact implementation of `clean` differs across Ninja, Makefile, Visual
Studio, Xcode, and other generators. Some CMake clean-related properties are
explicitly documented as applying only to Ninja and/or Makefile generators.

DevClean therefore must not derive one universal deletion manifest from the
behavior of a single generator and apply it to every CMake binary tree.

## Why vendor invocation alone is not enough for DevClean

Using the vendor command is preferable to raw deletion when its scope is known,
but vendor ownership does not automatically grant DevClean destructive
authority over every path the project asked that command to clean.

A CMake project is allowed to declare generated outputs or additional clean
files according to its own build logic. Those declarations may be perfectly
valid for the project author while still exceeding the storage boundary a
DevClean user thought they selected.

Therefore DevClean currently cannot safely reduce the decision to:

1. find `CMakeCache.txt`;
2. confirm the build directory is local;
3. run `cmake --build <dir> --target clean`.

Step 3 can legally have a broader deletion set than step 2 proves.

## Direct binary-tree deletion is not promoted as a shortcut

A tempting alternative is to validate an out-of-source CMake binary tree and
remove the entire tree with DevClean's exact filesystem deletion layer.

This audit does not promote that approach yet because a configured binary tree
may contain outputs with user value, package artifacts, generated code intended
to be retained, or files placed there by other tools. It also would not emulate
CMake's own generator-specific clean semantics.

If a future direct-tree lane is added, it should be a separate **USER_REVIEW**
operation with strong source/binary-tree identity checks and should not be
presented as equivalent to CMake's `clean` target.

## Current DevClean behavior

DevClean intentionally does not:

- classify arbitrary directories named `build`, `out`, or `cmake-build-*` as
  disposable by name;
- run `cmake --build <dir> --target clean` merely because a CMake binary tree is
  detected;
- assume global clean is confined to the selected binary directory;
- ignore `ADDITIONAL_CLEAN_FILES`, generated outputs, custom-command outputs, or
  generator-specific clean behavior;
- raw-delete a CMake binary tree and claim that this is equivalent to vendor
  clean;
- use AI to decide a known but insufficiently bounded CMake clean scope.

## Revisit criteria for an executable lane

A generic executable CMake project-clean action can be reconsidered when
DevClean can obtain a stable, complete pre-mutation manifest of the effective
clean scope for the exact configured generator.

A suitable mechanism would need to:

1. prove the exact source and binary-tree identity;
2. identify the configured generator and native build tool;
3. enumerate every file/directory the clean target may remove, including
   project-added clean files and generated/custom-command outputs;
4. normalize every target and reject paths outside approved local boundaries;
5. revalidate the build identity and manifest immediately before mutation; and
6. invoke a vendor-supported action whose deletion scope is constrained to the
   reviewed manifest, or otherwise prove that the generated clean target cannot
   exceed it.

Until such a complete manifest is available, deferring generic execution is
safer than exposing a deceptively narrow `cmake --build ... --target clean`
button.

## Primary sources

- CMake `cmake(1)`, **Build a Project**: `--target` and clean-only invocation.
  - https://cmake.org/cmake/help/latest/manual/cmake.1.html
- CMake **User Interaction Guide**: meaning of the generated `clean` target.
  - https://cmake.org/cmake/help/latest/guide/user-interaction/index.html
- CMake target property **ADDITIONAL_CLEAN_FILES**.
  - https://cmake.org/cmake/help/latest/prop_tgt/ADDITIONAL_CLEAN_FILES.html
- CMake directory property **ADDITIONAL_CLEAN_FILES**.
  - https://cmake.org/cmake/help/latest/prop_dir/ADDITIONAL_CLEAN_FILES.html
- CMake source-file property **GENERATED**.
  - https://cmake.org/cmake/help/latest/prop_sf/GENERATED.html
- CMake directory property **CLEAN_NO_CUSTOM**.
  - https://cmake.org/cmake/help/latest/prop_dir/CLEAN_NO_CUSTOM.html
- CMake **CMP0058**: Ninja custom-command byproducts and generated build outputs.
  - https://cmake.org/cmake/help/latest/policy/CMP0058.html
