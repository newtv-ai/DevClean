# GNU Make project-clean scope audit

Audited: 2026-08-19

## Product conclusion

**Audit complete; generic executable lane deferred.**

GNU Make does not define `clean` as a bounded vendor cleanup operation. `clean` is conventionally a phony/action target, but the Makefile author supplies the recipe and GNU Make simply executes that recipe. The recipe may contain arbitrary shell commands, invoke scripts or tools, recurse into other makefiles, remove paths outside the project, or perform non-cleanup side effects.

Therefore a real local `Makefile` plus a target named `clean` does not grant DevClean any generic mutation authority.

This is a known execution-authority problem, **not AI uncertainty**. Generic `make clean` remains **REPORT_ONLY / deferred**.

## Primary vendor contracts

Current GNU Make documentation establishes the important semantics:

- GNU Make explicitly says it does not know what a recipe does; the Makefile author supplies the commands and Make executes them;
- `clean` is commonly an action/phony target rather than a file target;
- examples use `rm`, but that is an example project recipe, not a built-in cleanup implementation;
- standard target conventions describe `clean`, `mostlyclean`, `distclean`, `realclean`, and `clobber`, with the latter group potentially deleting configuration/preparation state beyond ordinary build output;
- `-n` / `--dry-run` normally prints recipes instead of executing them, but GNU Make explicitly documents exceptions where recipes are still executed;
- recursive recipes using the special `$(MAKE)` mechanism are executed even under `-n`, `-q`, or `-t` so sub-makes receive the requested behavior;
- recipes needed to update included makefiles may still execute during `-n` processing;
- the `$(shell ...)` function runs a shell command when the function is expanded while Make evaluates its input.

Primary sources:

- https://www.gnu.org/software/make/manual/html_node/Simple-Makefile.html
- https://www.gnu.org/software/make/manual/html_node/Phony-Targets.html
- https://www.gnu.org/software/make/manual/html_node/Goals.html
- https://www.gnu.org/software/make/manual/html_node/Instead-of-Execution.html
- https://www.gnu.org/software/make/manual/html_node/Recursion.html
- https://www.gnu.org/software/make/manual/html_node/Shell-Function.html

## Why the name `clean` grants no authority

GNU Make's documentation uses `clean` as a conventional action target. A typical example is a phony target whose recipe runs `rm` over selected files.

The key safety point is that GNU Make does not own the semantics of that `rm` command. The project owns the recipe.

A project may legally define, for example, a `clean` target that:

- removes conventional object/output files;
- removes generated source;
- removes files outside the current directory;
- invokes another script whose behavior is not visible in the Makefile line;
- invokes a package manager or custom cleanup executable;
- performs recursive `make` operations in subdirectories;
- modifies caches, shared storage, deployment directories, or persistent project state;
- performs unrelated side effects before or after deletion.

The target name says nothing about the complete destructive scope.

DevClean must not infer authority from:

- a file named `Makefile`, `makefile`, or `GNUmakefile`;
- a `.PHONY: clean` declaration;
- a target literally named `clean`;
- a local-fixed project directory;
- conventional output names such as `obj`, `bin`, `build`, or `out`;
- the fact that GNU documentation recommends a `clean` target for normal package conventions.

## `.PHONY` is scheduling metadata, not a safety property

Declaring `.PHONY: clean` tells Make that `clean` is an action name rather than a file whose timestamp can make the rule up to date.

It does not constrain the recipe, filesystem paths, commands, recursion, privileges, or side effects.

A phony clean target therefore remains arbitrary project-defined execution from DevClean's authorization perspective.

## Why `make -n clean` is not a safe inspection primitive

At first glance GNU Make's dry-run mode appears attractive because it usually prints the recipe without running it. Current GNU documentation explicitly prevents DevClean from treating it as a guaranteed read-only operation.

GNU Make documents that:

1. some recipes are still executed with `-n`;
2. recursive Make recipe lines using `$(MAKE)` are executed even when `-n`, `-q`, or `-t` is set;
3. recipes needed to remake included makefiles are still executed;
4. Makefile evaluation can itself execute shell commands through functions such as `$(shell ...)`.

Therefore **probing the effective clean recipe can execute project-defined code before DevClean has proved any mutation boundary**.

That violates the DevClean requirement that inventory/scope discovery be read-only and safe to perform before authorization.

DevClean must not run `make -n clean` merely to discover what `make clean` might do.

## Parsing Makefiles inside DevClean is not an acceptable substitute

DevClean must not implement a partial Makefile parser and assume that this avoids execution.

The effective behavior can depend on:

- included makefiles;
- conditional logic;
- recursively and simply expanded variables;
- command-line variable overrides;
- environment variables;
- functions including `shell`;
- target-specific/pattern-specific variables;
- implicit and explicit rules;
- recursive sub-makes;
- scripts and programs invoked by recipes.

Reproducing only a subset would create false authority. Reproducing the whole evaluation model would still not prove the side effects of arbitrary external commands.

This is fundamentally different from a vendor GC command whose mutation semantics are owned and bounded by the vendor tool.

## Recursive Make widens the scope further

GNU Make explicitly supports recursive invocations such as `$(MAKE) -C subdir`.

A top-level `clean` may therefore delegate to many nested projects, each with different Makefiles, included configuration, variables, and recipes. The top-level selected directory is not a complete cleanup boundary.

DevClean must not interpret a top-level `Makefile` as proof that all nested clean behavior stays beneath that directory.

## Standard clean-family targets remain separate conventions

GNU Make documents common target names including:

- `clean`;
- `mostlyclean`;
- `distclean`;
- `realclean`;
- `clobber`.

The documentation notes that the latter targets may delete more than ordinary `clean`, including configuration files or links created in preparation for compilation.

DevClean must never normalize these names into one generic "project cleanup" button, and it must not treat broader names as safe merely because they are conventional GNU package targets.

No executable authority is granted to any of them by this audit.

## Raw directory deletion is not an equivalent shortcut

Blocking `make clean` does not justify recursively deleting conventional output directories instead.

A Make project may:

- build directly beside source files;
- write to multiple output roots;
- use arbitrary configured output paths;
- share generated files between targets/projects;
- retain valuable generated artifacts under conventional directory names.

Directory names do not reproduce the project's clean semantics and do not establish lifecycle ownership.

## Generator-specific projects retain their own authority model

Some projects use Makefiles generated by a higher-level build system. DevClean should use that higher-level source/lifecycle contract rather than generic Make.

Examples:

- **CMake Makefile generators** remain governed by the CMake project-clean audit; invoking generated `make clean` cannot bypass CMake's extensible additional-clean semantics.
- **Autotools/Automake** may define conventional clean-family targets but also supports project customization/hooks; it requires its own source audit before any narrower authority could be considered.
- other Makefile generators likewise require their own independently proven lifecycle boundary.

A generated Makefile is an implementation backend, not a universal disk-cleanup authority.

## Revisit condition

There is no plausible generic executable lane while `clean` remains arbitrary project-defined recipe execution.

A future narrower lane would need an **independent higher-level contract** that eliminates arbitrary recipe behavior from the authorization decision. At minimum it would need to prove:

1. exact project/generator identity;
2. the exact generated-state lifecycle owned by that generator;
3. a complete mutation scope that does not depend on executing/evaluating arbitrary Makefile code to discover it;
4. every affected path independently bounded to approved local storage;
5. no unbounded project-defined hooks/scripts/recursive cleanup entering the operation;
6. fresh identity/scope revalidation immediately before mutation;
7. a postcondition that can be checked without assuming logical size equals physical reclaim.

That would be a generator-specific lane, not generic GNU Make authorization.

## Deliberate exclusions

This audit grants no authority to:

- invoke `make clean` generically;
- invoke `make -n clean` as supposedly harmless inventory;
- invoke `mostlyclean`, `distclean`, `realclean`, or `clobber` generically;
- parse project Makefiles into destructive authority;
- execute shell/script recipes to discover what they would delete;
- recursively delete conventional `build`, `obj`, `bin`, or output directories as a substitute;
- assume `.PHONY` means safe;
- use AI to interpret arbitrary cleanup shell commands and thereby grant execution authority.

## Product status

**GNU Make generic clean = REPORT_ONLY / audited / executable action deferred.**

The correct DevClean behavior is to rely on separately audited generator/tool lifecycle boundaries rather than exposing arbitrary Makefile recipes as cleanup operations.

Normal DevClean validation still applies to this documentation-only safety decision: lock/dependency checks, Ruff, strict mypy, full pytest/current workflow, Windows EXE artifact, and CodeQL must all remain green before merge.
