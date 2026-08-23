# pnpm store generic-scan pruning audit — 2026-08

## Finding

The pnpm source/authority re-audit in `pnpm-cache-state-prune-reaudit.md` remains
correct: the content-addressable store is not a raw filesystem TOOL tree;
maintenance is delegated to the source-bounded `pnpm store prune` capability.
DLX and metadata retention remain USER-owned, the exact `pnpm-state.json` object
remains deterministic TOOL state, and broader pnpm cache/state/home/global data
remains protected.

A separate product-scan regression was found after that authority work.
Packaged `scan-rules.json` classifies the known pnpm store root as
`PNPM_STORE / REPORT_ONLY`. Normal application discovery also reports pnpm scan
roots, and the catalog intentionally refreshes a same-path packaged root with
application-aware metadata using `replace_existing=True`. The report-only
metadata mapper did not have a pnpm-store case, so an exact store root could be
replaced with the generic fallback category `IDE_CACHE`.

The normal product scanner prunes unquantified provider stores from generic
per-file traversal by category. `PNPM_STORE` is one of those explicit prune
categories; `IDE_CACHE` is not. The category loss therefore defeated an existing
performance boundary and allowed the generic scanner to enumerate pnpm store
internals even though no generic file rule is supposed to acquire mutation
authority there.

This is a classification/pruning bug, not new cleanup authority.

## Correction

`cleanup_catalog._report_only_root_metadata()` now recognizes only the exact
source-audited `pnpm-store` application rule and preserves the
`CleanupCategory.PNPM_STORE` category when application discovery refreshes that
root.

No other pnpm root receives that category from this correction:

- pnpm cache remains traversable because DLX and metadata mirrors contain
  USER_REVIEW objects whose retention must stay visible;
- pnpm state remains traversable because exact `pnpm-state.json` is a TOOL
  object while surrounding state is protected;
- `PNPM_HOME`, global installs, global bin, and other persistent/mixed roots are
  not converted into provider-store prune roots;
- raw pnpm store deletion remains forbidden;
- `pnpm store prune` authority and its execution hardening are unchanged;
- store occupancy is still not advertised as exact pre-clean reclaim for the
  partial vendor GC operation.

## Why pruning the store is valid

The scanner's `skip_paths` is a traversal boundary, not deletion authority. A
path in that set is skipped before generic directory preparation/descent, so
DevClean does not pay to enumerate a provider-owned tree whose children cannot
make the generic cleanup decision more precise.

The exact store is still represented by its provider semantics and can be
handled only by the separately audited vendor-maintenance lane. Skipping its
children therefore removes redundant generic work without hiding a legitimate
file-level TOOL/USER decision.

## Regression coverage

The focused regression recreates the original metadata collision:

1. begin with a packaged-style exact store root already tagged `PNPM_STORE`;
2. let application discovery report the same store plus pnpm cache, state, and
   home roots;
3. exercise the real `replace_existing=True` catalog path;
4. require the store to remain `PNPM_STORE / REPORT_ONLY`;
5. require cache/state/home not to become `PNPM_STORE`;
6. feed the resulting roots to the product's real generic-provider skip helper;
7. require only the exact store to be pruned from generic traversal.

## Merge gate

Merge only from the exact final PR head after the normal DevClean gate is green:
lock/dependency checks, Ruff, strict mypy, full pytest/current workflow, Windows
EXE build/upload, and CodeQL.
