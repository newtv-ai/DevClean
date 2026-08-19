# WSL Go build-cache maintenance

## Lane

The WSL Go build cache (`GOCACHE`) is a **DETERMINISTIC_CANDIDATE** when it is
Go's ordinary local on-disk cache. It is reproducible acceleration state, not
project source or installed toolchain state.

The operation remains explicit and low-priority by default because Go already
ages unused cache entries and its documentation says manual cache cleaning is
normally unnecessary.

## Vendor contract

DevClean asks the selected distribution's `go` command for:

```text
go version
go env -json GOCACHE GOCACHEPROG
```

The current Go command documentation states that `GOCACHE` is the build-cache
directory and that `go clean -cache` removes the ordinary Go build cache. It also
states that Go periodically deletes old cache data itself.

Primary sources:

- https://pkg.go.dev/cmd/go
- https://go.dev/src/cmd/go/internal/cache/cache.go
- https://go.dev/src/cmd/go/internal/cache/default.go
- https://go.dev/src/cmd/go/internal/clean/clean.go

## Test and fuzz cache nuance

Go stores successful package test results in the build cache. Therefore a full
`go clean -cache` can discard ordinary cached test-result entries as part of
clearing that build cache, even though DevClean does **not** invoke the separate
`-testcache` action.

Fuzz cache data is different. Go's current cache implementation explicitly says
the `fuzz` subdirectory is **not** removed by `go clean -cache` or normal cache
trim; it is removed by the separate `go clean -fuzzcache` operation. DevClean
does not invoke `-fuzzcache`, preserving the previously audited fuzz-cache user
tradeoff as a separate deferred lane.

## External cache boundary

A non-empty `GOCACHEPROG` makes this lane **REPORT_ONLY**.

Go defines `GOCACHEPROG` as an external cache program. DevClean cannot infer
whether such a backend is local, remote, shared, or managed by another lifecycle,
so it does not attempt to clean it.

## Mutation contract

Immediately before mutation DevClean:

1. re-reads Go version, `GOCACHE`, and `GOCACHEPROG`;
2. requires the full identity to match the user's inspected state;
3. refuses while `go` or `gopls` activity is visible, and fails closed if process
   state cannot be checked;
4. requires the exact `GOCACHE` path to pass the shared WSL root-filesystem
   device-identity proof;
5. runs Go through the non-shell WSL execution boundary with exact, allowlisted
   Linux environment overrides:

```text
env GOCACHE=<exact-path> GOCACHEPROG= GOFLAGS= go clean -cache
```

`GOFLAGS` is explicitly forced empty. The Go command supports default command-line
flags through `GOFLAGS`; allowing inherited clean flags here could unintentionally
widen an audited `-cache` operation to other clean lanes such as `-modcache`.

The environment wrapper validates the nested executable and argv through the
same WSL denylist. Environment-variable names are allowlisted; generic
loader/search-path variables such as `PATH` or `LD_PRELOAD` cannot be supplied
through this helper. `GOCACHEPROG` and `GOFLAGS` are empty-only overrides.

Afterward DevClean re-inventories Go and refuses to claim a confirmed result if
the Go/cache identity changed.

## Deliberate exclusions

This lane does not:

- run bare `go clean` against a source tree;
- use package arguments, `-i`, or `-r`;
- clean `GOMODCACHE`;
- expose separate `-testcache` or `-fuzzcache` actions;
- mutate `GOPATH`, `GOROOT`, installed binaries, or project metadata;
- raw-delete the cache directory;
- clean a non-empty `GOCACHEPROG` backend;
- mutate caches redirected onto `/mnt/c` or another separately mounted
  filesystem;
- promise Windows VHD file shrinkage when Linux logical space is released.

## Product behavior

The UI presents this as deterministic but usually low-benefit maintenance. It is
not sent to AI and is not run automatically merely because the cache exists.
The user still starts the cleanup explicitly.
