# Synthetic pnpm store fixtures

The default adapter intentionally has no pnpm CLI transcript: it never invokes `store path`,
`store status`, `store prune`, Corepack, or a shim. Tests create conventional `v10`/`v11` roots
and an old synthetic root, then exercise only DevClean's metadata scanner. This boundary reflects
pnpm 10/11 upstream source reviewed 2026-07-11.
