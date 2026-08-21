# Execution safety second-pass re-audit

## Scope

This pass re-audits the shared Windows exact-mutation boundary after the rule-authority phases: handle identity, reparse/junction confinement, hard-link preservation, and concurrent rename/replacement behavior.

## Finding

The selected root was pinned and observed reparse entries were deleted as links, but a nested ordinary directory could be replaced by a junction after pathname classification and before later traversal. A subsequent pathname `scandir()` could therefore resolve outside the approved tree before a child handle was checked.

## Correction

The already-pinned selected root is reused. Every nested directory is opened with `OPEN_REPARSE_POINT` immediately before traversal, must remain ordinary/non-Cloud/in-boundary at its expected final path, every leaf handle is checked against the approved root before delete disposition, and nested directories are revalidated again before their own deletion. Existing exact-file snapshot, link-count, share-mode, root pinning, and postcondition guards are retained.

## Acceptance gate

Merge only from the exact final head after lock/dependency checks, Ruff, strict mypy, full pytest/current CI, Windows EXE artifact, and CodeQL are green.
