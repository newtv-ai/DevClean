# SCons project-clean scope audit

Audited: 2026-08-19

## Product conclusion

**Audit complete; generic executable lane deferred.**

SCons has stronger built-target semantics than a handwritten Makefile: `scons -c` removes built targets known to SCons, `NoClean()` can protect selected targets, and `Clean(target, extra)` lets projects associate additional files with cleanup.

However, the SCons build description itself is executable Python. Current SCons source literally reads SConstruct/SConscript source and executes it with Python `exec(compile(...))`. Therefore DevClean cannot even evaluate a project's effective clean graph as a harmless inventory step: project code runs before the clean plan exists.

In addition, SCons explicitly lets project code widen cleanup through `Clean()` to files that are not ordinary targets. Those paths can be project-defined and are not guaranteed to stay beneath one conventional build directory.

This is a known execution-authority problem, **not AI uncertainty**. Generic SCons cleanup remains **REPORT_ONLY / deferred**.

## Primary vendor contracts

Current SCons documentation and source establish the relevant behavior:

- `scons -c` / clean mode removes built targets;
- `NoClean()` marks targets that should not be removed by clean;
- `Clean(target, files)` associates additional project-selected files/directories with cleanup, including files SCons does not otherwise know are targets;
- SConstruct/SConscript files are SCons configuration programs;
- current SCons implementation reads SConscript source and executes it with Python `exec(compile(scriptdata, scriptname, 'exec'), ...)`;
- SConscript files can invoke other SConscript files and import/use arbitrary Python-visible logic as part of graph construction.

Primary sources:

- https://github.com/SCons/scons/blob/master/doc/user/file-removal.xml
- https://github.com/SCons/scons/blob/master/doc/user/simple.xml
- https://github.com/SCons/scons/blob/master/SCons/Script/SConscript.py
- https://github.com/SCons/scons/blob/master/SCons/Environment.py
- https://github.com/SCons/scons/blob/master/SCons/Script/Main.py

## Why `SConstruct` does not create read-only cleanup authority

A SCons project commonly begins with an `SConstruct` file and may delegate to one or many `SConscript` files.

Unlike a static manifest format, these files are Python programs. Current SCons source explicitly compiles and executes their contents while reading the project.

That means project evaluation may legally:

- read or write arbitrary files;
- inspect or mutate environment/state;
- import Python modules;
- run subprocesses;
- access network or external services through Python/project code;
- choose different targets and cleanup declarations dynamically;
- recursively evaluate additional SConscript files.

DevClean's execution standard requires scope discovery to be safe before mutation authority is granted. Running arbitrary project Python to discover what clean would do violates that boundary.

Therefore DevClean must not run SCons against an untrusted/arbitrary project merely to inventory cleanup candidates.

## `Clean()` explicitly widens the destructive set

SCons's own file-removal documentation describes `Clean(target, extra_file)` for files that are **not normal target files** but should be removed when the associated target is cleaned.

This is an intentional supported extension point, not an edge case.

The complete effective clean set can therefore contain:

- ordinary built targets;
- project-declared additional files;
- project-declared additional directories;
- paths computed dynamically by Python configuration logic.

A conventional output directory or the location of `SConstruct` is not a complete destructive boundary.

## `NoClean()` proves cleanup policy is project-defined

`NoClean()` lets project code exempt targets from normal `-c` behavior.

This reinforces the same product conclusion: SCons owns a cleanup mechanism, but the project owns meaningful parts of its effective lifecycle policy. DevClean cannot substitute a generic directory purge and claim equivalence with SCons clean.

## Why dry-run does not solve inventory safety

Even if SCons supports options that suppress execution of build actions, that does not make project evaluation read-only.

The SConstruct/SConscript Python still has to be read and executed to construct the build graph, discover targets, process `Clean()`/`NoClean()` calls, and determine what the requested operation means.

Project Python can have arbitrary side effects during this evaluation phase. Therefore no SCons dry-run/command-printing mode can be treated as a generic safe cleanup-manifest probe unless SCons provides a separate non-executing declarative interface.

DevClean must not execute SCons project code merely to obtain a preview and then parse human output into deletion authority.

## Why static parsing is not an acceptable substitute

DevClean must not parse SConstruct/SConscript text as though it were a declarative configuration language.

The effective project is Python and can depend on:

- imports and helper modules;
- functions/classes;
- arbitrary control flow;
- filesystem/environment/system inspection;
- command-line variables/options;
- generated configuration;
- nested SConscript execution;
- arbitrary computed paths passed to builders, `Clean()`, or `NoClean()`.

A partial parser would create false authority, while a complete Python evaluator is exactly the arbitrary-code execution that the inventory boundary forbids.

## Build-directory names do not provide a fallback

Blocking generic `scons -c` does not authorize recursive deletion of directories named `build`, `out`, `variant`, or similar.

SCons supports variant directories and flexible target placement, and project Python can define arbitrary target/output locations. Cleanup extras registered through `Clean()` further break any assumption that one directory contains the complete lifecycle.

Directory-name heuristics therefore remain non-authoritative.

## Target-scoped clean is not a generic safe lane

SCons supports selecting particular targets, but target selection does not solve the fundamental problem:

1. the project Python must still execute to construct the graph and resolve that target;
2. `Clean()` may associate extra paths with the target;
3. project code may compute target and cleanup paths dynamically;
4. evaluating the project can have arbitrary side effects before DevClean has proved any path boundary.

An exact target name is not an exact filesystem deletion authorization.

## Higher-level generated/lifecycle boundaries remain separate

If another audited system owns a stronger lifecycle boundary around SCons-generated state, that higher-level source must establish the authority.

DevClean must not use SCons as an escape hatch around a safer source-specific decision, and it must not infer that all SCons projects share one conventional output layout.

## Revisit condition

A future generic SCons executable lane would require a supported **non-executing** interface that exposes the complete effective clean plan without running project Python.

At minimum DevClean would need to:

1. identify the exact SCons version and exact project/configuration identity;
2. obtain all ordinary clean targets plus every `Clean()`-registered extra and every `NoClean()` exclusion;
3. resolve every affected path without executing SConstruct/SConscript Python;
4. include variant/nested-project semantics and target-scoped clean expansion;
5. reject unresolved/dynamic paths;
6. independently prove every destructive path belongs to approved local storage;
7. re-obtain an identical complete plan immediately before mutation;
8. invoke only a vendor operation whose actual behavior is constrained to that proved plan;
9. verify postconditions without treating logical target size as guaranteed physical reclaim.

If graph/clean discovery inherently requires arbitrary Python execution, generic execution should remain deferred.

## Deliberate exclusions

This audit grants no authority to:

- invoke `scons -c` generically;
- execute SConstruct/SConscript Python to discover cleanup scope;
- treat SCons dry-run output as a safe complete manifest;
- parse SConstruct/SConscript as a declarative cleanup specification;
- infer cleanup authority from `SConstruct`, `SConscript`, variant/build directory names, or target names;
- ignore `Clean()` extras or `NoClean()` exclusions;
- recursively delete conventional build/variant directories as a substitute;
- use AI to interpret arbitrary Python/project cleanup logic and thereby grant execution authority.

## Product status

**SCons generic project clean = REPORT_ONLY / audited / executable action deferred.**

The correct DevClean behavior is to avoid executing project Python merely to discover a destructive scope. A narrower future lane needs an independent, non-executing complete clean-plan interface or a stronger higher-level lifecycle contract.

Normal DevClean validation still applies to this documentation-only safety decision: lock/dependency checks, Ruff, strict mypy, full pytest/current workflow, Windows EXE artifact, and CodeQL must all remain green before merge.
