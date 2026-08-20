# Generic review routing re-audit

Audited: 2026-08-20

## Product conclusion

DevClean must not outsource technical uncertainty to a non-expert user or automatically spend paid AI calls on generic unknown paths. Generic path/name heuristics remain useful for explanation, but they do not by themselves create deletion authority.

The generic scanner now uses this order:

1. source/vendor-backed application semantics and an exact local boundary -> deterministic candidate;
2. source-backed exact object whose retention value is genuinely personal -> USER_REVIEW;
3. generic name/suffix/category/unknown semantics -> REPORT_ONLY / protected;
4. AI is optional help for a file the user explicitly chooses from a legitimate review lane.

## Learned/default file knowledge

Cross-machine learned knowledge is intentionally supported for **files**. A confirmed common-software file may ship in DELETE/KEEP defaults or be learned from AI/user review. A file-level DELETE may fill the knowledge gap for a generic REPORT_ONLY file, so that confirmed knowledge remains useful on another machine.

That override is deliberately narrow. It is accepted only for generic file classifications tagged as byproduct/cache/path-heuristic/unknown. It cannot override hard semantic protection such as program payloads, installed add-ons, application state, source-protected application data, Windows/vendor-managed report-only roots, or other protected known-root semantics. KEEP still has priority.

Directories use a separate authority lane. A file rule is never evaluated as a directory rule. A user may explicitly decide an already eligible review directory, but that choice is stored as an exact-path-only directory decision and is never generalized into a prefix/glob/tree rule. Keeping a directory also shields its descendants from learned file DELETE rules. Generic or unknown directories remain protected; source-proven vendor directories keep their audited deterministic policy.

## Generic authority removed

The following evidence no longer produces USER_REVIEW or AI_REVIEW by itself:

- `.log`, `.bak`, `.tmp`, `.dmp`, `.pdb` and other generic byproduct suffixes;
- a parent directory named `cache`, `.cache`, `caches` or similar;
- generic development-cache path hints;
- inferred build-output, installer/download or system-log categories;
- an otherwise unknown file;
- directories merely named `node_modules`, `__pycache__`, `.mypy_cache`, `.pytest_cache`, `.ruff_cache`, `cache` or `.cache`;
- version-looking directories beneath generic `versions`, `application`, `app` or `update` parents;
- legacy `MANUAL_REVIEW` configured roots;
- recent items inside AGE_BASED_REVIEW temp roots that have not reached the current age threshold.

Old sidecar scan rules cannot preserve the former broad USER/AI routes because runtime classification itself fails closed. A learned DELETE can still authorize a matching **file** when its only blocker is generic uncertainty; it cannot turn a directory or a hard-protected file into a cleanup target.

## Broad packaged roots

The packaged scan config moves former `MANUAL_REVIEW` root groups to `REPORT_ONLY`. This does not disable more-specific application rules: application classification runs before generic known-root policy, so a source-audited exact TOOL or USER object can still receive its narrow lane.

## What intentionally remains

- old entries in exact AGE_BASED_REVIEW temp roots remain deterministic after the configured age; their lifecycle will be re-audited separately;
- exact application `USER_DECISION` objects remain USER_REVIEW because the technical meaning is already known and only personal retention value remains;
- exact application TOOL rules and vendor maintenance lanes remain deterministic subject to their identity/concurrency/revalidation guards;
- curated file-level learned/default knowledge remains supported and will be re-audited for selective restoration after the #142 interim neutralization.

## Next phase

Re-audit every static `VENDOR_MANAGED` configured root against the corresponding application matcher/vendor maintenance path. A configured root must not provide raw fallback authority when the richer application model intentionally protects an unrecognized child. Then re-verify AGE_BASED_REVIEW lifecycle and the application modules one by one against current upstream sources.
