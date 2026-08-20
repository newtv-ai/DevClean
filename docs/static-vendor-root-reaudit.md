# Static VENDOR_MANAGED root re-audit

Audited: 2026-08-20

## Finding

The packaged scan config still contained 19 `VENDOR_MANAGED` root entries. Most application modules already replaced exact traversal anchors with `REPORT_ONLY` and then re-added narrower audited TOOL roots with an attached `ApplicationCleanupRule`. However, a static configured root that was not replaced could still inherit generic deterministic file authority solely from its path label. An old sidecar could preserve the same fallback after packaged defaults changed.

That is not an acceptable authority boundary: scan configuration proves where to look, not that every current or future object under the path follows one disposable lifecycle.

## Correction

- packaged known roots no longer carry static `VENDOR_MANAGED`; they are `REPORT_ONLY` discovery anchors;
- runtime treats legacy/static `VENDOR_MANAGED` roots without an attached audited application rule as protected, so old sidecars fail closed;
- individual files never inherit authority from a vendor root: file mutation still requires the application classifier or a separately confirmed file-level rule;
- deterministic vendor whole-tree authority requires an attached `ApplicationCleanupRule` whose owner is TOOL and whose audited policy explicitly allows whole-tree cleanup;
- the post-scan directory capability boundary independently re-checks the same provenance before producing a cleanup capability;
- the fresh whole-tree policy layer refuses a legacy static vendor root rather than falling back to generic configured authority.

More-specific application rules are unchanged. `discover_known_cleanup_roots()` may still create runtime `VENDOR_MANAGED` roots, but only from audited application code that attaches the exact TOOL rule.

## Verification invariant

The packaged scan configuration contains zero static `VENDOR_MANAGED` roots after this migration. Any runtime vendor-managed root must therefore be created by application-aware discovery and carry the audited rule that grants its exact authority.

## Consequence

This removes a cross-cutting bypass before the family-by-family source audit: a newly added or stale static path cannot make unknown descendants deletable merely by being called a vendor cache. Package managers, browsers, model stores, IDEs and developer tools must earn mutation authority through their dedicated application lifecycle model.
