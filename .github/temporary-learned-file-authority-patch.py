from __future__ import annotations

from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    if old not in text:
        raise RuntimeError(f"expected patch anchor missing in {path}: {old[:100]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8", newline="\n")


# A confirmed learned/default DELETE is legitimate reusable product knowledge for
# FILES. It may fill the knowledge gap left by a generic REPORT_ONLY classifier,
# but it may never override a hard semantic protection or apply to directories.
replace_once(
    "src/devclean/ui/app.py",
    "from dataclasses import dataclass\n",
    "from dataclasses import dataclass, replace\n",
)
replace_once(
    "src/devclean/ui/app.py",
    '''def _configured_delete_eligible(item: TriageItem) -> bool:\n    """Configured DELETE may promote only an item the executor already accepts."""\n\n    return (\n        is_direct_cleanup_eligible(item)\n        or is_user_review_eligible(item)\n        or is_ai_review_eligible(item)\n    )\n\n\n''',
    '''_LEARNED_FILE_OVERRIDE_TAGS = frozenset(\n    {"byproduct", "cache_directory", "path_heuristic", "unknown"}\n)\n\n\ndef _configured_delete_eligible(item: TriageItem) -> bool:\n    """Return whether a configured DELETE can authorize this exact file item."""\n\n    if (\n        is_direct_cleanup_eligible(item)\n        or is_user_review_eligible(item)\n        or is_ai_review_eligible(item)\n    ):\n        return True\n    return (\n        item.target_kind is CleanupTargetKind.FILE\n        and item.lane is ReviewLane.REPORT_ONLY\n        and item.actionability is Actionability.REPORT_ONLY\n        and item.execution_policy is ExecutionPolicy.NONE\n        and bool(_LEARNED_FILE_OVERRIDE_TAGS.intersection(item.tags))\n    )\n\n\ndef _effective_deletable_item(item: TriageItem, rules: UserRules) -> TriageItem:\n    """Materialize file-only learned authority without mutating classifier truth."""\n\n    if (\n        item.target_kind is CleanupTargetKind.FILE\n        and rules.decision_for(item.path) is RuleDecision.DELETE\n        and item.lane is ReviewLane.REPORT_ONLY\n        and _configured_delete_eligible(item)\n    ):\n        return replace(\n            item,\n            lane=ReviewLane.USER_REVIEW,\n            risk_tier=RiskTier.MEDIUM,\n            actionability=Actionability.USER_REVIEW,\n            execution_policy=ExecutionPolicy.USER_CHOICE_DELETE,\n            reason=item.reason + "；命中已确认的文件级 DELETE 默认/学习规则",\n            tags=(*item.tags, "configured_file_delete"),\n        )\n    return item\n\n\n''',
)
replace_once(
    "src/devclean/ui/app.py",
    '''        if bucket == _DELETE_BUCKET:\n            deletable.append(item)\n''',
    '''        if bucket == _DELETE_BUCKET:\n            deletable.append(_effective_deletable_item(item, rules))\n''',
)

# Generic REPORT_ONLY files may be promoted by an explicit learned file rule;
# hard semantic protection remains a ceiling.
replace_once(
    "tests/test_generic_review_routing.py",
    "def test_learned_delete_rule_cannot_promote_generic_protected_path(tmp_path: Path) -> None:\n",
    "def test_learned_delete_rule_can_promote_generic_protected_file(tmp_path: Path) -> None:\n",
)
replace_once(
    "tests/test_generic_review_routing.py",
    '''    deletable, unsure = app._partition_items(session, rules)\n    assert deletable == ()\n    assert unsure == ()\n\n\ndef test_packaged_scan_roots_no_longer_delegate_broad_raw_paths_to_manual_review() -> None:\n''',
    '''    deletable, unsure = app._partition_items(session, rules)\n    assert [candidate.path for candidate in deletable] == [item.path]\n    assert deletable[0].execution_policy is ExecutionPolicy.USER_CHOICE_DELETE\n    assert "configured_file_delete" in deletable[0].tags\n    assert unsure == ()\n\n\ndef test_learned_delete_cannot_override_hard_program_payload(tmp_path: Path) -> None:\n    path = tmp_path / "cache" / "runtime.pdb"\n    path.parent.mkdir()\n    item = _triage(path)\n    assert "program_payload" in item.tags\n\n    base = default_rules()\n    rules = UserRules(\n        scan=base.scan,\n        delete=replace(\n            base.delete,\n            rules=(\n                DecisionRule(\n                    rule_id="unsafe-learned-delete",\n                    group="ai_import",\n                    match=RuleMatch.EXACT_PATH,\n                    value=item.path,\n                    source="AI_IMPORT",\n                    reason="historical verdict must not beat hard protection",\n                ),\n            ),\n        ),\n        keep=base.keep,\n    )\n    session = TriageSession(review_sample_per_category=rules.scan.review_sample_per_category)\n    session.observe_path(item.path, rules)\n    session.add(item)\n\n    deletable, unsure = app._partition_items(session, rules)\n    assert deletable == ()\n    assert unsure == ()\n\n\ndef test_packaged_scan_roots_no_longer_delegate_broad_raw_paths_to_manual_review() -> None:\n''',
)

# The README must distinguish live AI uncertainty from curated/learned file
# knowledge, which the product intentionally supports.
replace_once(
    "README.md",
    "- 仅凭 cache/tmp/build/log 等名称、文件后缀、年龄或大小无法证明安全的对象，以及工具\n"
    "  真正无法识别的对象，默认保护并且不进入删除/AI 队列。AI 不再替产品制造删除权限。\n",
    "- 仅凭 cache/tmp/build/log 等名称、文件后缀、年龄或大小无法证明安全的对象，以及工具\n"
    "  真正无法识别的对象，默认保护并且不进入删除/AI 队列。一次性的未知 AI 猜测不会\n"
    "  给目录或硬保护对象制造删除权限；但经过确认并沉淀的文件级 DELETE/KEEP 规则可以\n"
    "  作为后续默认知识，而且只对文件生效。\n",
)

Path("docs/generic-review-routing-reaudit.md").write_text(
    '''# Generic review routing re-audit\n\nAudited: 2026-08-20\n\n## Product conclusion\n\nDevClean must not outsource technical uncertainty to a non-expert user or automatically spend paid AI calls on generic unknown paths. Generic path/name heuristics remain useful for explanation, but they do not by themselves create deletion authority.\n\nThe generic scanner now uses this order:\n\n1. source/vendor-backed application semantics and an exact local boundary -> deterministic candidate;\n2. source-backed exact object whose retention value is genuinely personal -> USER_REVIEW;\n3. generic name/suffix/category/unknown semantics -> REPORT_ONLY / protected;\n4. AI is optional help for a file the user explicitly chooses from a legitimate review lane.\n\n## Learned/default file knowledge\n\nCross-machine learned knowledge is intentionally supported for **files**. A confirmed common-software file may ship in DELETE/KEEP defaults or be learned from AI/user review. A file-level DELETE may fill the knowledge gap for a generic REPORT_ONLY file, so that confirmed knowledge remains useful on another machine.\n\nThat override is deliberately narrow. It is accepted only for generic file classifications tagged as byproduct/cache/path-heuristic/unknown. It cannot override hard semantic protection such as program payloads, installed add-ons, application state, source-protected application data, Windows/vendor-managed report-only roots, or other protected known-root semantics. KEEP still has priority.\n\nDirectories use a separate authority lane. A file rule is never evaluated as a directory rule. A user may explicitly decide an already eligible review directory, but that choice is stored as an exact-path-only directory decision and is never generalized into a prefix/glob/tree rule. Keeping a directory also shields its descendants from learned file DELETE rules. Generic or unknown directories remain protected; source-proven vendor directories keep their audited deterministic policy.\n\n## Generic authority removed\n\nThe following evidence no longer produces USER_REVIEW or AI_REVIEW by itself:\n\n- `.log`, `.bak`, `.tmp`, `.dmp`, `.pdb` and other generic byproduct suffixes;\n- a parent directory named `cache`, `.cache`, `caches` or similar;\n- generic development-cache path hints;\n- inferred build-output, installer/download or system-log categories;\n- an otherwise unknown file;\n- directories merely named `node_modules`, `__pycache__`, `.mypy_cache`, `.pytest_cache`, `.ruff_cache`, `cache` or `.cache`;\n- version-looking directories beneath generic `versions`, `application`, `app` or `update` parents;\n- legacy `MANUAL_REVIEW` configured roots;\n- recent items inside AGE_BASED_REVIEW temp roots that have not reached the current age threshold.\n\nOld sidecar scan rules cannot preserve the former broad USER/AI routes because runtime classification itself fails closed. A learned DELETE can still authorize a matching **file** when its only blocker is generic uncertainty; it cannot turn a directory or a hard-protected file into a cleanup target.\n\n## Broad packaged roots\n\nThe packaged scan config moves former `MANUAL_REVIEW` root groups to `REPORT_ONLY`. This does not disable more-specific application rules: application classification runs before generic known-root policy, so a source-audited exact TOOL or USER object can still receive its narrow lane.\n\n## What intentionally remains\n\n- old entries in exact AGE_BASED_REVIEW temp roots remain deterministic after the configured age; their lifecycle will be re-audited separately;\n- exact application `USER_DECISION` objects remain USER_REVIEW because the technical meaning is already known and only personal retention value remains;\n- exact application TOOL rules and vendor maintenance lanes remain deterministic subject to their identity/concurrency/revalidation guards;\n- curated file-level learned/default knowledge remains supported and will be re-audited for selective restoration after the #142 interim neutralization.\n\n## Next phase\n\nRe-audit every static `VENDOR_MANAGED` configured root against the corresponding application matcher/vendor maintenance path. A configured root must not provide raw fallback authority when the richer application model intentionally protects an unrecognized child. Then re-verify AGE_BASED_REVIEW lifecycle and the application modules one by one against current upstream sources.\n''',
    encoding="utf-8",
    newline="\n",
)

replace_once(
    "docs/full-rule-reaudit-2026-08.md",
    "| Packaged DELETE/KEEP defaults | ✅ #142 | learned machine decisions removed; neutral defaults + conservative migration |\n",
    "| Packaged DELETE/KEEP defaults | ⚠ #142 interim | backup/migration bug fixed; defaults temporarily neutralized, safe file-level knowledge will be selectively restored |\n",
)
replace_once(
    "docs/full-rule-reaudit-2026-08.md",
    "| Learned-rule portability/generalization | ⏳ queued | re-check generated glob/regex reuse after #142 neutral baseline |\n",
    "| Learned-rule target boundary | ✅ phase 2 | learned/default rules apply to files only; directory choices are exact-path-only and subtree KEEP wins |\n"
    "| Learned-rule portability/default restoration | ⏳ queued | re-audit old packaged file rules, generated glob/regex reuse, then selectively restore safe common-file knowledge |\n",
)
replace_once(
    "docs/full-rule-reaudit-2026-08.md",
    "A reduction in USER_REVIEW or AI_REVIEW counts is accepted only by moving an item either upward to a source-proven exact deterministic lane or downward to protected/report-only. It must never be achieved by treating cache-like names, age, size, redownloadability, or an AI guess as deletion authority.",
    "A reduction in USER_REVIEW or AI_REVIEW counts is accepted by moving an item to a source-proven exact deterministic lane, to protected/report-only, or by applying a separately confirmed reusable **file-level** DELETE/KEEP rule. Cache-like names, age, size, redownloadability, or a one-off AI guess are not authority by themselves; learned file authority never extends to directories or hard semantic protections.",
)

replace_once(
    "docs/storage-audit-status.md",
    "A full rule re-audit is active in `docs/full-rule-reaudit-2026-08.md`. Phase 1 removed machine-specific learned decisions from packaged defaults (#142). Phase 2 removes generic name/suffix/category/unknown USER/AI routing: unproven raw paths are protected instead of outsourcing technical risk to a non-expert user or paid AI. Continue with static VENDOR_MANAGED roots, AGE_BASED_REVIEW lifecycle, then application modules one by one.",
    "A full rule re-audit is active in `docs/full-rule-reaudit-2026-08.md`. #142 fixed default-backup/migration handling but temporarily neutralized packaged learned rules more aggressively than the product policy requires. Phase 2 restores the intended boundary: confirmed learned/default DELETE/KEEP knowledge is reusable for files, never for directories, while generic name/suffix/category/unknown evidence alone routes to protection instead of asking a non-expert user or automatically spending paid AI calls. Continue with static VENDOR_MANAGED roots, AGE_BASED_REVIEW lifecycle, selective file-default restoration, then application modules one by one.",
)
