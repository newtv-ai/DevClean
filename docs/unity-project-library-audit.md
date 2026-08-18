# Unity project Library maintenance audit

Audited: 2026-08-18

## Product conclusion

Unity must not be represented by one broad "Unity cache" rule. Its large disk consumers have different semantics and therefore different DevClean decisions.

This first Unity lane covers only a selected project's direct `Library` directory.

Current Unity 6 documentation describes `Library` as the project's internal imported representation of source assets and explicitly says that, when the project is not open in Unity, the folder can be safely deleted because Unity regenerates its data the next time the project opens.

That makes the technical meaning deterministic, so AI adds no value. However, deleting the entire `Library` can force a large project to re-import assets and rebuild generated state. The operation is therefore **USER_REVIEW**, never an automatic/default cleanup.

## Unity storage must stay split by semantics

The Unity audit is intentionally decomposed into separate sources:

| Storage source | Current conclusion | Why it is separate |
| --- | --- | --- |
| Project `Library` | USER_REVIEW | Fully regenerable when project is closed, but full re-import/rebuild cost can be large |
| Package Manager global cache | separate follow-up audit | Current Unity 6 has its own `db` size/eviction policy, configurable paths, and offline/download value |
| Asset Store package cache | separate follow-up audit | Separate downloaded-package store, user may value keeping large paid/offline assets locally |
| GI Cache | protected from raw deletion pending separate maintenance lane | Unity says clearing should be a last resort and manual deletion while the Editor is running is unsafe |
| `Assets`, `Packages`, `ProjectSettings`, `UserSettings` | protected | Project source, dependency intent, settings, or user state rather than disposable cache |

DevClean must not merge these roots merely because they are all associated with Unity.

## Why project Library is technically regenerable

Unity 6's **Importing assets** documentation says imported internal asset data is stored in the project's `Library` folder. It describes the folder as cache-like and states that if the project is not open in Unity, `Library` can be safely deleted because Unity can regenerate its data from project source/settings when the project is next opened.

Unity's asset metadata documentation separately confirms that important asset identity/import settings live with source assets in `.meta` files, while imported game-ready data is updated in `Library`.

This establishes a strong semantic boundary: `Library` is generated state, while `Assets` and their `.meta` files are not.

## Why this is USER_REVIEW instead of deterministic default cleanup

Safety and value are different questions.

Deleting `Library` is technically understood and supported by Unity's documented model, but the next project open can require a complete re-import and rebuild. For large projects that cost can be substantial in CPU time and developer waiting time.

DevClean therefore:

- never preselects this operation;
- never asks AI to decide whether the rebuild cost is worthwhile;
- uses 5 GiB only as a benefit heuristic to say that the item may be worth reviewing;
- requires explicit user confirmation immediately before deletion.

A `Library` smaller than 5 GiB remains technically understood; the threshold is not a safety boundary.

## Execution contract

Before mutation DevClean:

1. requires the selected directory to contain `Assets` and `ProjectSettings/ProjectVersion.txt`;
2. resolves that directory as the exact project root;
3. grants deletion authority only to the direct child `<project>/Library`;
4. rejects a `Library` entry that is a file, symbolic link, or Windows junction;
5. refuses while any Unity Editor process is running on Windows;
6. revalidates the project boundary and exact `Library` entry immediately before mutation;
7. captures stable Windows file identities for both the project boundary and `Library`, then uses DevClean's handle-bound exact-directory purge rather than pathname-based recursive deletion;
8. keeps the verified boundary handle open during traversal and never descends through reparse points;
9. requires a completed purge with the exact `Library` root absent before reporting success;
10. records before/after/reclaimed bytes;
11. never searches for other folders named `Library` and never expands authority to neighboring project directories.

The all-Editor process guard is deliberately conservative. Unity only requires the target project to be closed, but DevClean does not try to infer a possibly incomplete project-path mapping from process command lines before a destructive operation.

## Explicit non-targets

This action does not delete or edit:

- `Assets` or `.meta` files;
- `Packages` or package manifests/lock data;
- `ProjectSettings`;
- `UserSettings`;
- project `Temp`/`Logs` as a side effect;
- Package Manager global cache;
- Asset Store cache;
- GI Cache;
- Unity Hub installs, editor installs, modules, templates, or downloads.

Those require independent evidence and independent product decisions.

## Sources

- Unity 6 User Manual, **Importing assets**: project folder structure; imported internal data lives in `Library`; a closed project's `Library` can be safely deleted and regenerated.
- Unity 6 User Manual, **Asset metadata**: `.meta` files carry asset identity and import settings beside source assets; imported representations are refreshed in `Library`.
- Unity 6 User Manual, **Customize the global cache**: Package Manager global cache is independently configurable and has its own current `db` size/eviction semantics.
- Unity 6 User Manual, **Customize the Asset Store cache location**: Asset Store packages use a separate cache root.
- Unity Manual, **Global Illumination (GI) cache / Preferences**: GI Cache is a separate shared cache; Unity warns against manual deletion while the Editor is running and describes clearing it as a last-resort operation.
