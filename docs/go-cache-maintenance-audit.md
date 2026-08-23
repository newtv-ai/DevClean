# Go cache maintenance audit

Audited: 2026-08-18  
Native execution scope re-audited: 2026-08-23

## Product conclusion

Go's build cache and module cache are both vendor-managed cache locations, but
they do not receive the same product decision.

| Resource | DevClean lane | Execution |
| --- | --- | --- |
| ordinary local Go build cache (`GOCACHE`, no `GOCACHEPROG`) | deterministic candidate | exact pinned `go clean` build-cache flag set |
| build cache with non-empty `GOCACHEPROG` | report-only | none |
| Go module cache (`GOMODCACHE`) | user review | exact pinned `go clean` module-cache flag set after explicit choice |

Neither ordinary cache case needs AI. The module-cache distinction is user
intent, not technical uncertainty. An external build-cache program is instead a
mutation-authority boundary: DevClean does not infer the locality or lifecycle
of that external backend.

## Current upstream snapshot

The native execution contract was rechecked against Go master commit:

`c97cfcb37fced87a43a3dbab8983d6f76b8b84d1`

Relevant current source:

- `src/cmd/go/main.go` — command invocation calls `base.SetFromGOFLAGS` before
  parsing the explicit command line;
- `src/cmd/go/internal/base/goflags.go` — matching flags from effective
  `GOFLAGS` are applied to the command's flag set before command-line flags;
- `src/cmd/go/internal/clean/clean.go` — `go clean` has distinct destructive
  booleans for `-i`, `-r`, `-cache`, `-testcache`, `-modcache`, and
  `-fuzzcache`; `-cache` removes ordinary build-cache entries, `-modcache`
  removes the module cache, and `-fuzzcache` is separately destructive;
- `src/cmd/go/internal/cache/default.go` — `GOCACHEPROG` switches normal build
  caching to an external cache program layered with the disk cache, while
  `GOCACHE` remains the local disk cache directory.

This re-audit also reconciles the native Windows lane with the already hardened
WSL Go lanes, which had independently identified the same `GOFLAGS` and
`GOCACHEPROG` boundaries.

## Build cache: deterministic only for the ordinary local backend

The Go command documents `go clean -cache` as removing the Go build cache. The
ordinary local `GOCACHE` stores compiled build/test artifacts rather than
source-of-truth project data. Clearing it causes later builds to compile again.

Size is benefit metadata only. The lower-level inventory still records a 1 GiB
"worthwhile" signal, but size never creates or revokes deletion authority. The
normal rule-driven vendor capability may expose any non-empty source-backed
ordinary build cache as deterministic.

A non-empty `GOCACHEPROG` changes the architecture: Go is using an external
build-cache program in addition to the local disk cache. DevClean does not infer
where that program stores data or what its lifecycle is. In that configuration
the local build-cache row remains visible but is **REPORT_ONLY** and is not
emitted as a deterministic vendor-cleanup candidate.

## Module cache: user review

The Go module cache stores downloaded module files and unpacked source for
versioned dependencies. It is shared by multiple projects on the machine, has no
automatic maximum-size eviction, and ordinary module-aware commands download
missing modules as needed.

Go explicitly provides `go clean -modcache` as the supported way to remove that
cache, including unpacked versioned dependency source. This makes deletion
technically understood and supported, but whether the downloaded dependency set
remains valuable for offline work, old projects, private modules, or network
savings is personal intent. DevClean therefore never promotes the module cache
to automatic cleanup and does not send it to AI by default.

## GOFLAGS widening finding

The old native wrapper invoked only:

- `go clean -cache`; or
- `go clean -modcache`.

That command text was not a complete destructive manifest. Current Go applies
matching effective `GOFLAGS` first and only then parses the explicit command
line. A user or persistent Go configuration could therefore pre-enable another
clean flag before DevClean's selected flag was parsed. Examples include
`-modcache`, `-fuzzcache`, `-i`, and `-r`.

DevClean now pins every destructive clean boolean explicitly on the final command
line, after `GOFLAGS` has been applied:

For build cache:

`go clean -i=false -r=false -cache=true -testcache=false -modcache=false -fuzzcache=false -n=false`

For module cache:

`go clean -i=false -r=false -cache=false -testcache=false -modcache=true -fuzzcache=false -n=false`

The explicit false values are scope constraints, not convenience flags. They
ensure persisted/user `GOFLAGS` cannot broaden the reviewed mutation. `-n=false`
also prevents a persisted dry-run flag from silently turning a requested cleanup
into a no-op.

## Native execution contract

Before either executable clean operation DevClean now:

1. re-resolves the exact audited Go cache roots and requires the selected path to
   match the correct cache kind exactly;
2. keeps a build cache non-executable when effective `GOCACHEPROG` is non-empty;
3. refuses while Go/gopls activity is detected;
4. resolves one exact Go executable and requires it to be a local-fixed,
   non-reparse/non-cloud file with stable volume/file identity;
5. requires the selected cache root to be a local-fixed,
   non-reparse/non-cloud directory with stable volume/file identity;
6. pins the selected `GOCACHE` or `GOMODCACHE` in the process environment;
7. asks that exact executable for structured `go env -json GOCACHE GOCACHEPROG
   GOMODCACHE` and requires the selected root to match; build-cache execution also
   requires effective `GOCACHEPROG` to remain empty;
8. immediately before mutation rechecks Go/gopls activity, the exact Go CLI
   identity, the exact cache-root identity, and the structured vendor
   configuration again;
9. runs only the fully pinned clean flag set for the reviewed cache kind;
10. after the command, requires the Go executable identity to remain unchanged;
11. for `-cache`, requires the preserved `GOCACHE` root to remain the same
    filesystem object; for `-modcache`, an absent root is valid because Go may
    remove the whole module-cache directory, while any surviving root must retain
    the reviewed identity;
12. reports measured logical before/after bytes, propagates vendor failures, and
    never falls back to raw recursive deletion.

## Retained protection

This re-audit grants no raw whole-tree authority to `GOCACHE` or `GOMODCACHE`.
It does not delete project source, `go.mod` / `go.sum` / workspace metadata,
installed Go binaries, the Go SDK/toolchain, arbitrary `GOPATH` content, fuzz
cache, or external-cache-program state.

## Sources

Pinned source snapshot:

- https://github.com/golang/go/blob/c97cfcb37fced87a43a3dbab8983d6f76b8b84d1/src/cmd/go/main.go
- https://github.com/golang/go/blob/c97cfcb37fced87a43a3dbab8983d6f76b8b84d1/src/cmd/go/internal/base/goflags.go
- https://github.com/golang/go/blob/c97cfcb37fced87a43a3dbab8983d6f76b8b84d1/src/cmd/go/internal/clean/clean.go
- https://github.com/golang/go/blob/c97cfcb37fced87a43a3dbab8983d6f76b8b84d1/src/cmd/go/internal/cache/default.go

The Go Modules Reference remains authoritative for the module cache's shared
purpose, lack of automatic maximum-size eviction, and supported
`go clean -modcache` lifecycle.

## Validation gate

The final PR head must pass lock/dependency checks, Ruff, strict mypy, full
pytest/current workflow, Windows EXE build/upload, and CodeQL before merge.
