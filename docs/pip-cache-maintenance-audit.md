# pip cache maintenance audit

Audited: 2026-08-18

## Product conclusion

pip's cache is known local performance data, so DevClean does not spend AI on it. The safe mutation boundary is the vendor command, not recursive filesystem deletion.

DevClean inventories both the documented Windows default cache and an effective custom cache. A cache at or above 512 MiB is preselected because the disk benefit is likely material; smaller caches remain understood and selectable, but keeping them may save network traffic and wheel rebuild time.

## Why raw deletion authority was removed

pip explicitly documents the exact filesystem structure of its cache contents as an implementation detail that may change between pip versions. It also provides `pip cache dir`, `pip cache info`, and `pip cache purge` as the supported cache-management surface.

The previous DevClean rule gave the default `%LocalAppData%\pip\Cache` directory whole-tree raw deletion authority. That was unnecessarily broad. This audit removes generic file/tree deletion authority from both default and custom pip caches. They remain visible to scanning but are REPORT_ONLY there; the maintenance action uses pip itself.

## Execution contract

Before purge, DevClean:

1. re-resolves the audited pip cache roots;
2. requires the selected path to match one of those roots exactly;
3. refuses while a pip process is active;
4. sets `PIP_CACHE_DIR` to the exact selected root;
5. asks a candidate pip command to report `pip cache dir` and requires that report to match the target;
6. runs `pip cache purge` with that same validated command;
7. reports vendor errors and never falls back to raw deletion.

This also makes a custom cache safely manageable without assuming its internal directory layout or deleting unrelated paths by name.

## Recovery / tradeoff

`pip cache purge` clears pip's HTTP and wheel caches. It does not uninstall already installed packages. Later installs can require downloads again, and locally built wheels may need rebuilding. This is why DevClean uses a benefit threshold for default selection rather than treating every non-empty cache as worth purging immediately.

## Sources

- pip documentation, **pip cache**: documents `cache dir`, `cache info`, `cache remove`, and `cache purge`; purge removes all cache items.
- pip documentation, **Caching**: documents HTTP and locally built wheel caches, the Windows default cache location, the cache layout as an implementation detail, and the network/build performance value of retaining cache data.
