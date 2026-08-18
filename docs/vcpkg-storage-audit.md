# vcpkg storage audit

Audited: 2026-08-19

## Product conclusion

vcpkg should not be represented by one broad "vcpkg cache" rule. The current Microsoft documentation distinguishes several storage classes with different semantics:

| Storage | DevClean conclusion | Reason |
| --- | --- | --- |
| `<vcpkg-root>/packages` | USER_REVIEW | Microsoft documents it as safe to delete when the user only cares about installed packages, but it is build/package staging rather than a universal background cache |
| `<vcpkg-root>/buildtrees` | USER_REVIEW, high caution | Normally generated build state, but `--editable` deliberately preserves extracted source for port development; those trees may contain user edits |
| `<vcpkg-root>/downloads` | USER_REVIEW | Downloaded source/tool assets are reproducible but have meaningful offline/network value; extracted tools may also be retained by vcpkg's own clean-after-build behavior |
| default local binary cache | USER_REVIEW / no automatic selection | Compressed reusable binary packages; deleting them trades disk space for future rebuild time |
| custom `files` binary cache | REPORT_ONLY unless exclusive local ownership is proven | May be redirected or shared across projects/machines; path knowledge alone does not prove exclusive ownership |
| remote/NuGet/HTTP binary sources | REPORT_ONLY | Not local raw filesystem cleanup targets |
| installed packages / manifests / ports / triplets / configuration | protected | Dependency state, source/configuration, or installed payload rather than disposable cache |

No listed vcpkg storage should consume AI by default: the technical type is known. The remaining decision is user intent or execution authority.

## Primary-source basis

Microsoft's current vcpkg FAQ says that, if the user only cares about installed packages, it is safe to delete `packages`, `buildtrees`, and `downloads` under the vcpkg root. The same documentation recommends `vcpkg install --clean-after-build` to prevent these temporary trees from accumulating during future builds.

The `vcpkg install` reference narrows those semantics further:

- `--clean-buildtrees-after-build` removes buildtree subdirectories while retaining top-level logs;
- `--clean-downloads-after-build` removes top-level unextracted download assets while keeping extracted tools;
- `--clean-packages-after-build` removes package staging after installation;
- `--editable` intentionally preserves extracted buildtree source so the user can modify it during port development.

That last point is decisive for DevClean: a generic whole-`buildtrees` automatic cleanup would be unsafe even though most buildtrees are generated.

## Binary-cache boundary

vcpkg enables a local binary cache by default. On Windows the first valid location is:

1. `%VCPKG_DEFAULT_BINARY_CACHE%` when configured;
2. `%LOCALAPPDATA%\vcpkg\archives`;
3. `%APPDATA%\vcpkg\archives`.

The default cache uses the `files` provider and stores built packages as compressed archives so later installs can restore them rather than rebuilding from source.

`VCPKG_BINARY_SOURCES` and `--binarysource` can add or replace `files`, NuGet, HTTP, and other sources. A custom files provider can be shared, so DevClean must not convert arbitrary configured cache paths into destructive authority merely because vcpkg knows about them.

## Why this is not deterministic default cleanup

The storage is technically understandable, but each deletion has a real user-specific cost:

- deleting downloads can break offline/restricted-network rebuilds until assets are fetched again;
- deleting binary archives increases future compile time;
- deleting buildtrees can destroy editable port-development changes;
- deleting package staging can remove useful diagnostics or inspection state even when installed packages remain intact.

Therefore these are **USER_REVIEW**, not AI and not default-selected deterministic cleanup.

## Execution design for the follow-up implementation

The executable lane must be narrower than the FAQ's conceptual statement:

1. identify the exact vcpkg executable/root rather than searching for directories named `vcpkg`, `packages`, `downloads`, or `buildtrees`;
2. inventory only direct children of that confirmed root;
3. show `packages`, `buildtrees`, and `downloads` separately so the user can make different choices;
4. warn specifically that `buildtrees` may contain `--editable` work;
5. resolve the active default binary-cache location separately and never merge it with the vcpkg root;
6. keep custom/shared binary providers report-only unless exclusive local ownership can be established;
7. require local fixed storage for direct filesystem mutation;
8. refuse while vcpkg/build activity is present;
9. revalidate exact root and target immediately before mutation;
10. use DevClean's handle-bound exact-directory mutation path with no reparse traversal and no raw path-based recursive fallback;
11. never touch installed state, manifests, ports, triplets, integration/configuration, registries, or arbitrary sibling directories;
12. measure actual before/after/reclaimed bytes and never equate total vcpkg-root size with reclaimable bytes.

## Source references

- Microsoft Learn, **vcpkg FAQ — How can I remove temporary files?**
- Microsoft Learn, **vcpkg install** (`--clean-after-build`, `--clean-buildtrees-after-build`, `--clean-downloads-after-build`, `--clean-packages-after-build`, `--editable`)
- Microsoft Learn, **Default local vcpkg binary cache**
- Microsoft Learn, **Binary caching configuration**
