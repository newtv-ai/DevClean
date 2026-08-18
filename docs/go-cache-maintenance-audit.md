# Go cache maintenance audit

Audited: 2026-08-18

## Product conclusion

Go's build cache and module cache are both vendor-managed cache locations, but they should not receive the same product decision.

| Resource | DevClean lane | Default selection | Execution |
| --- | --- | --- | --- |
| Go build cache (`GOCACHE`) | deterministic candidate | when at least 1 GiB | `go clean -cache` |
| Go module cache (`GOMODCACHE`) | user review | never | `go clean -modcache` after explicit choice |

Neither case needs AI. The distinction is user intent, not technical uncertainty.

## Build cache: deterministic

The Go command documents `go clean -cache` as removing the entire Go build cache. The build cache contains compiled build/test artifacts rather than source-of-truth project data. Clearing it makes later builds compile again, so DevClean uses 1 GiB as a benefit threshold before selecting it by default.

The threshold is not a safety boundary: smaller build caches are still known and vendor-cleanable, but often more useful to keep than to reclaim immediately.

## Module cache: user review

The Go module cache stores downloaded module files and unpacked source for versioned dependencies. It is shared by multiple projects on the machine, has no automatic maximum-size eviction, and ordinary module-aware commands download missing modules as needed.

Go explicitly provides `go clean -modcache` as the supported way to remove that cache, including read-only module trees. This makes deletion technically understood and supported, but whether the downloaded dependency set remains valuable for offline work, old projects, private modules, or network savings is personal intent. DevClean therefore never preselects the module cache and does not send it to AI by default.

## Execution contract

Before either clean operation DevClean:

1. re-resolves the exact audited Go cache roots;
2. requires the selected path to match the correct cache kind exactly;
3. refuses while Go/gopls activity is detected;
4. sets the selected `GOCACHE` or `GOMODCACHE` in the command environment;
5. asks the same configured Go executable to report `go env GOCACHE` or `go env GOMODCACHE`;
6. requires the vendor-reported path to match the selected root;
7. only then runs `go clean -cache` or `go clean -modcache`;
8. reports vendor failures and never falls back to raw directory deletion.

## Sources

- Go command documentation, **go clean**: `-cache` removes the entire Go build cache; `-modcache` removes the entire module download cache including unpacked source code of versioned dependencies.
- Go Modules Reference, **Module cache**: the module cache stores downloaded modules, defaults to `$GOPATH/pkg/mod`, may be shared by multiple projects, has no maximum size, and is not automatically emptied; missing modules are downloaded as needed; `go clean -modcache` is the supported cache-removal command.
