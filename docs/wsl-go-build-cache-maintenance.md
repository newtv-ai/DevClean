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
directory and that `go clean -cache` removes the entire Go build cache. It also
states that Go periodically deletes old cache data itself.

Primary sources:

- https://pkg.go.dev/cmd/go
- https://go.dev/src/cmd/go/internal/cache/default.go
- https://go.dev/src/cmd/go/internal/clean/clean.go

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
5. runs Go through the non-shell WSL execution boundary with an exact Linux
   environment override:

```text
env GOCACHE=<exact-path> GOCACHEPROG= go clean -cache
```

The environment wrapper validates the nested executable and argv through the
same WSL denylist, so it is not a shell or arbitrary-command escape hatch.

Afterward DevClean re-inventories Go and refuses to claim a confirmed result if
the Go/cache identity changed.

## Deliberate exclusions

This lane does not:

- run bare `go clean` against a source tree;
- use package arguments, `-i`, or `-r`;
- clean `GOMODCACHE`;
- clean test-result or fuzz caches;
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
