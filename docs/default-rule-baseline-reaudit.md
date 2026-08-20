# Default rule baseline re-audit

"
    "Audited: 2026-08-20

"
    "## Finding

"
    "The packaged `delete-rules.json` and `keep-rules.json` were not neutral product defaults. "
    "They contained machine-specific `AI_IMPORT` decisions learned during development. The build "
    "script embeds those exact JSON files into `DevClean.exe`, and `load_rules()` copies packaged "
    "templates into a new user's `DevClean-data` when the sidecar files are missing. Those learned "
    "decisions therefore had the ability to become product-wide defaults on unrelated machines.

"
    "Examples found during the re-audit included direct DELETE decisions for browser/model assets, "
    "package-cache internals, editor state databases, embedded OCR model weights, debug symbols, "
    "runtime/JDK artifacts and application resources merely because they were thought redownloadable "
    "or regenerable on the development machine. That is incompatible with DevClean's source-first "
    "authority model. A learned judgment is evidence about one observation, not universal delete "
    "authority.

"
    "## Correction

"
    "- Packaged DELETE and KEEP decision arrays are now empty.
"
    "- Audited deterministic semantics remain in product classification/application rules instead.
"
    "- User/AI decisions continue to live only in the user's sidecar after that user actually makes/imports them.
"
    "- Existing installations are migrated conservatively: if the old default-backup ZIP proves an "
    "  active decision was shipped by the old baseline and the entry is still exactly unchanged, it "
    "  is removed. Later user/AI decisions and edited entries are preserved.
"
    "- The visible default-backup ZIP is refreshed best-effort from the current executable, so "
    "  `restore defaults` no longer pins a user to defaults from the first installed version.
"
    "- Missing activity files prefer current packaged templates and use the sidecar ZIP only as an "
    "  availability fallback.

"
    "## Product rule

"
    "No future release may ship `AI_IMPORT` or `USER_DECISION` entries in packaged defaults. If a "
    "decision is reliable for every user, encode it as a source-audited deterministic application or "
    "vendor lifecycle rule with tests. If it is not reliable for every user, it cannot be a product "
    "default.

"
    "This is phase 1 of the 2026-08 full rule re-audit. The next phases re-check scan-root scope, "
    "generic classification heuristics and then each application-specific rule to reduce both "
    "USER_REVIEW and AI_REVIEW without widening destructive authority.
"
    