# Generic review routing re-audit

"
    "Audited: 2026-08-20

"
    "## Product conclusion

"
    "DevClean must not outsource technical uncertainty to a non-expert user or to a paid AI model. "
    "Generic path/name heuristics remain useful for explanation, but they no longer create deletion "
    "authority.

"
    "The generic scanner now uses this order:

"
    "1. source/vendor-backed application semantics and exact local boundary -> deterministic candidate;
"
    "2. source-backed exact object whose retention value is genuinely personal -> USER_REVIEW;
"
    "3. generic name/suffix/category/unknown semantics -> REPORT_ONLY / protected;
"
    "4. AI is optional help only when the user actively selects an already legitimate USER_REVIEW file.

"
    "## Authority removed

"
    "The following evidence no longer produces USER_REVIEW or AI_REVIEW by itself:

"
    "- `.log`, `.bak`, `.tmp`, `.dmp`, `.pdb` and other generic byproduct suffixes;
"
    "- a parent directory named `cache`, `.cache`, `caches` or similar;
"
    "- generic development-cache path hints;
"
    "- inferred build-output, installer/download or system-log categories;
"
    "- an otherwise unknown file;
"
    "- directories merely named `node_modules`, `__pycache__`, `.mypy_cache`, `.pytest_cache`, "
    "  `.ruff_cache`, `cache` or `.cache`;
"
    "- version-looking directories beneath generic `versions`, `application`, `app` or `update` parents;
"
    "- legacy `MANUAL_REVIEW` configured roots;
"
    "- recent items inside AGE_BASED_REVIEW temp roots that have not reached the audited age threshold.

"
    "Old sidecar scan rules cannot preserve these former routes: runtime classification itself now "
    "fails closed. Likewise, an old learned DELETE verdict cannot promote a REPORT_ONLY item because "
    "the UI/executor only honors learned deletion inside an already executable lane.

"
    "## Broad packaged roots

"
    f"The packaged scan config moved {len(changed_ids)} formerly `MANUAL_REVIEW` root groups to "
    "`REPORT_ONLY`: " + ", ".join(f"`{item}`" for item in changed_ids) + ".

"
    "This does not disable more-specific application rules. Application classification runs before "
    "generic known-root policy, so a source-audited exact TOOL or USER object can still receive its "
    "narrow lane. Dedicated maintenance dialogs remain the preferred way to handle Windows, package "
    "managers, models, browsers, IDEs and build systems.

"
    "## What intentionally remains

"
    "- old entries in exact AGE_BASED_REVIEW temp roots remain deterministic after the configured age; "
    "  their lifecycle will be re-audited separately;
"
    "- exact application `USER_DECISION` objects remain USER_REVIEW because the technical meaning is "
    "  already known and only personal retention value remains;
"
    "- exact application TOOL rules and vendor maintenance lanes remain deterministic subject to their "
    "  existing identity/concurrency/revalidation guards.

"
    "## Next phase

"
    "Re-audit every static `VENDOR_MANAGED` configured root against the corresponding application "
    "matcher/vendor maintenance path. A configured root must not provide raw fallback authority when "
    "the richer application model intentionally protects an unrecognized child. Then re-verify the "
    "application modules one by one against current upstream sources.
"
    
## Learned/default knowledge boundary

Cross-machine learned knowledge is supported for **files**. A confirmed common software file may therefore ship in DELETE/KEEP defaults or be learned from AI/user review. Those rules are evaluated only for file observations.

Directories use a separate authority lane. A user may explicitly decide an already eligible review directory, but that choice is stored as an exact-path-only directory decision and is never generalized into a prefix/glob/tree rule. Generic or unknown directories remain protected; source-proven vendor directories keep their audited deterministic policy.
