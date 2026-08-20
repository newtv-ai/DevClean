from __future__ import annotations

from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    if old not in text:
        raise RuntimeError(f"expected patch anchor missing in {path}: {old[:120]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8", newline="\n")


# Static scan configuration is discovery/presentation only.  Whole-tree vendor
# authority is created only by application audit code that attaches the exact
# TOOL rule to the discovered root at runtime.
scan_path = Path("src/devclean/config/scan-rules.json")
scan_text = scan_path.read_text(encoding="utf-8")
static_vendor_count = scan_text.count('"policy": "VENDOR_MANAGED"')
if static_vendor_count < 1:
    raise RuntimeError("expected packaged VENDOR_MANAGED roots")
scan_path.write_text(
    scan_text.replace('"policy": "VENDOR_MANAGED"', '"policy": "REPORT_ONLY"'),
    encoding="utf-8",
    newline="\n",
)

# Presentation/runtime classification fails closed for old sidecars that still
# carry VENDOR_MANAGED.  The attached application rule is the provenance token:
# it must still be a TOOL whole-tree rule, not merely a configured path label.
replace_once(
    "src/devclean/core/triage.py",
    '''from devclean.core.application_cleanup import (\n    PolicyAction,\n    application_display_name,\n    evaluate_application_path,\n)\n''',
    '''from devclean.core.application_cleanup import (\n    DecisionOwner,\n    PolicyAction,\n    application_display_name,\n    evaluate_application_path,\n)\n''',
)
replace_once(
    "src/devclean/core/triage.py",
    '''class TriageSession:\n''',
    '''def _has_audited_vendor_authority(root: KnownCleanupRoot) -> bool:\n    """Return whether a vendor root carries its exact audited mutation rule."""\n\n    rule = root.application_rule\n    return (\n        root.policy is CleanupPolicy.VENDOR_MANAGED\n        and rule is not None\n        and rule.owner is DecisionOwner.TOOL\n        and rule.allow_whole_tree\n    )\n\n\nclass TriageSession:\n''',
)
replace_once(
    "src/devclean/core/triage.py",
    '''        recovery = (\n            RecoveryCapability.VENDOR_REDOWNLOAD_BEST_EFFORT\n            if known.policy is CleanupPolicy.VENDOR_MANAGED\n            else RecoveryCapability.UNKNOWN\n        )\n        if known.policy is CleanupPolicy.VENDOR_MANAGED:\n''',
    '''        audited_vendor = _has_audited_vendor_authority(known)\n        recovery = (\n            RecoveryCapability.VENDOR_REDOWNLOAD_BEST_EFFORT\n            if audited_vendor\n            else RecoveryCapability.UNKNOWN\n        )\n        if audited_vendor:\n''',
)
replace_once(
    "src/devclean/core/triage.py",
    '''        if known.policy is CleanupPolicy.VENDOR_MANAGED:\n            return _Classification(\n                known.category,\n                ReviewLane.DETERMINISTIC_CANDIDATE,\n                RiskTier.LOW,\n                EvidenceKind.KNOWN_ROOT_HEURISTIC,\n                Actionability.REVIEW_PLAN,\n                ExecutionPolicy.USER_CHOICE_DELETE,\n                RecoveryCapability.VENDOR_REDOWNLOAD_BEST_EFFORT,\n                f"{known.label}：精确匹配已审计的厂商管理存储，工具确定可清理",\n                ("known_root", "vendor_managed", "tool_decision"),\n            )\n''',
    '''        if known.policy is CleanupPolicy.VENDOR_MANAGED:\n            if _has_audited_vendor_authority(known):\n                return _Classification(\n                    known.category,\n                    ReviewLane.DETERMINISTIC_CANDIDATE,\n                    RiskTier.LOW,\n                    EvidenceKind.KNOWN_ROOT_HEURISTIC,\n                    Actionability.REVIEW_PLAN,\n                    ExecutionPolicy.USER_CHOICE_DELETE,\n                    RecoveryCapability.VENDOR_REDOWNLOAD_BEST_EFFORT,\n                    f"{known.label}：携带精确的应用 TOOL 生命周期规则，工具确定可清理",\n                    ("known_root", "vendor_managed", "audited_application_rule", "tool_decision"),\n                )\n            return _Classification(\n                known.category,\n                ReviewLane.REPORT_ONLY,\n                RiskTier.PROTECTED,\n                EvidenceKind.FILESYSTEM_OBSERVATION,\n                Actionability.REPORT_ONLY,\n                ExecutionPolicy.NONE,\n                RecoveryCapability.NONE,\n                f"{known.label}：旧/静态配置仅声明厂商目录，没有附带可验证的应用删除契约；工具直接保护",\n                ("known_root", "vendor_managed", "missing_application_rule", "report_only"),\n            )\n''',
)

# Execution independently re-derives the same provenance rule.  A forged or old
# UI item cannot turn a static REPORT_ONLY/VENDOR path into a directory purge.
replace_once(
    "src/devclean/core/postscan_cleanup.py",
    "from devclean.core.application_cleanup import process_guard_allows\n",
    "from devclean.core.application_cleanup import DecisionOwner, process_guard_allows\n",
)
replace_once(
    "src/devclean/core/postscan_cleanup.py",
    '''from devclean.core.cleanup_catalog import (\n    CleanupCategory,\n    KnownCleanupRoot,\n    known_root_for_path,\n)\n''',
    '''from devclean.core.cleanup_catalog import (\n    CleanupCategory,\n    CleanupPolicy,\n    KnownCleanupRoot,\n    known_root_for_path,\n)\n''',
)
replace_once(
    "src/devclean/core/postscan_cleanup.py",
    '''    if scope is DirectoryScope.NOT_ELIGIBLE:\n        raise CleanupRefusal(\n            "whole-directory cleanup is limited to recognised cache roots and "\n            "deterministically regenerable tool directories"\n        )\n    return scope\n''',
    '''    if scope is DirectoryScope.NOT_ELIGIBLE:\n        raise CleanupRefusal(\n            "whole-directory cleanup is limited to recognised cache roots and "\n            "deterministically regenerable tool directories"\n        )\n    if scope is DirectoryScope.KNOWN_CACHE_ROOT:\n        known = known_root_for_path(path, known_roots)\n        rule = known.application_rule if known is not None else None\n        if not (\n            known is not None\n            and known.policy is CleanupPolicy.VENDOR_MANAGED\n            and rule is not None\n            and rule.owner is DecisionOwner.TOOL\n            and rule.allow_whole_tree\n        ):\n            raise CleanupRefusal(\n                "whole-directory vendor cleanup requires an attached audited application TOOL rule"\n            )\n    return scope\n''',
)

# The fresh whole-tree gate also refuses a legacy static VENDOR root instead of
# treating it as a generic configured-policy fallback.
replace_once(
    "src/devclean/core/whole_tree_policy.py",
    "from devclean.core.cleanup_catalog import KnownCleanupRoot, known_root_for_path\n",
    "from devclean.core.cleanup_catalog import CleanupPolicy, KnownCleanupRoot, known_root_for_path\n",
)
replace_once(
    "src/devclean/core/whole_tree_policy.py",
    '''    rule = known.application_rule\n    if rule is None:\n        return None\n''',
    '''    rule = known.application_rule\n    if rule is None:\n        if known.policy is CleanupPolicy.VENDOR_MANAGED:\n            raise WholeTreePolicyRefusal(\n                "static vendor-managed roots do not carry whole-tree mutation authority"\n            )\n        return None\n''',
)

Path("tests/test_static_vendor_root_fallback.py").write_text(
    r'''from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from devclean.core.application_cleanup import CLAUDE_RULES
from devclean.core.cleanup_catalog import CleanupCategory, CleanupPolicy, KnownCleanupRoot
from devclean.core.postscan_cleanup import CleanupRefusal, _require_directory_scope
from devclean.core.triage import (
    Actionability,
    CleanupTargetKind,
    ExecutionPolicy,
    ReviewLane,
    RiskTier,
    triage_directory,
    triage_file,
)
from devclean.core.user_rules import default_rules
from devclean.scanner.filesystem import ScanRecord, ScanRecordKind

_NOW = datetime(2026, 8, 20, tzinfo=UTC)


def _record(path: Path, *, directory: bool, root: Path | None = None) -> ScanRecord:
    return ScanRecord(
        root=str(root or path.parent),
        path=str(path),
        kind=ScanRecordKind.DIRECTORY if directory else ScanRecordKind.FILE,
        depth=1,
        logical_size=0 if directory else 4096,
        allocated_size=0 if directory else 4096,
        raw_allocated_size=0 if directory else 4096,
        volume_serial=1,
        file_id=("2" if directory else "1") * 32,
        file_id_kind="file_id_128",
        link_count=1,
        attributes=0x10 if directory else 0,
        creation_time_ns=1,
        last_write_time_ns=int(_NOW.timestamp() * 1_000_000_000),
    )


def _assert_protected(item) -> None:
    assert item is not None
    assert item.lane is ReviewLane.REPORT_ONLY
    assert item.risk_tier is RiskTier.PROTECTED
    assert item.actionability is Actionability.REPORT_ONLY
    assert item.execution_policy is ExecutionPolicy.NONE


def test_packaged_scan_roots_are_discovery_only_not_static_vendor_authority() -> None:
    rules = default_rules()
    assert not any(
        CleanupPolicy(root.policy) is CleanupPolicy.VENDOR_MANAGED
        for root in rules.scan.known_cleanup_roots
    )


def test_legacy_static_vendor_file_fails_closed(tmp_path: Path) -> None:
    rules = default_rules()
    root = tmp_path / "legacy-vendor-root"
    root.mkdir()
    known = KnownCleanupRoot(
        path=root,
        category=CleanupCategory.OTHER,
        policy=CleanupPolicy.VENDOR_MANAGED,
        label="Legacy vendor root",
    )
    item = triage_file(
        _record(root / "opaque.blob", directory=False),
        known_roots=(known,),
        delete_config=rules.delete.classification,
        keep_config=rules.keep.classification,
        now=_NOW,
        temp_root=tmp_path / "different-temp",
    )
    _assert_protected(item)
    assert "missing_application_rule" in item.tags


def test_legacy_static_vendor_directory_fails_closed_and_execution_refuses(tmp_path: Path) -> None:
    rules = default_rules()
    root = tmp_path / "legacy-vendor-root"
    root.mkdir()
    known = KnownCleanupRoot(
        path=root,
        category=CleanupCategory.OTHER,
        policy=CleanupPolicy.VENDOR_MANAGED,
        label="Legacy vendor root",
        delete_root_itself=True,
    )
    item = triage_directory(
        _record(root, directory=True, root=root.parent),
        known_roots=(known,),
        delete_config=rules.delete.classification,
        keep_config=rules.keep.classification,
    )
    _assert_protected(item)
    assert item.target_kind is CleanupTargetKind.DIRECTORY

    with pytest.raises(CleanupRefusal, match="attached audited application TOOL rule"):
        _require_directory_scope(
            root,
            (known,),
            rules.delete.classification,
            rules.keep.classification,
        )


def test_attached_audited_tool_rule_retains_vendor_directory_authority(tmp_path: Path) -> None:
    rules = default_rules()
    root = tmp_path / "audited-vendor-root"
    root.mkdir()
    application_rule = next(
        rule
        for rule in CLAUDE_RULES
        if rule.allow_whole_tree and rule.owner.value == "TOOL"
    )
    known = KnownCleanupRoot(
        path=root,
        category=CleanupCategory.OTHER,
        policy=CleanupPolicy.VENDOR_MANAGED,
        label="Audited vendor root",
        delete_root_itself=True,
        application_rule=application_rule,
    )
    item = triage_directory(
        _record(root, directory=True, root=root.parent),
        known_roots=(known,),
        delete_config=rules.delete.classification,
        keep_config=rules.keep.classification,
    )
    assert item is not None
    assert item.lane is ReviewLane.DETERMINISTIC_CANDIDATE
    assert item.actionability is Actionability.REVIEW_PLAN
    assert item.execution_policy is ExecutionPolicy.USER_CHOICE_DELETE
    assert _require_directory_scope(
        root,
        (known,),
        rules.delete.classification,
        rules.keep.classification,
    ).value == "KNOWN_CACHE_ROOT"
''',
    encoding="utf-8",
    newline="\n",
)

Path("docs/static-vendor-root-reaudit.md").write_text(
    f'''# Static VENDOR_MANAGED root re-audit\n\nAudited: 2026-08-20\n\n## Finding\n\nThe packaged scan config still contained {static_vendor_count} `VENDOR_MANAGED` root entries. Most application modules already replaced exact traversal anchors with `REPORT_ONLY` and then re-added narrower audited TOOL roots with an attached `ApplicationCleanupRule`. However, a static configured root that was not replaced could still inherit generic deterministic file authority solely from its path label. An old sidecar could preserve the same fallback after packaged defaults changed.\n\nThat is not an acceptable authority boundary: scan configuration proves where to look, not that every current or future object under the path follows one disposable lifecycle.\n\n## Correction\n\n- packaged known roots no longer carry static `VENDOR_MANAGED`; they are `REPORT_ONLY` discovery anchors;\n- runtime treats legacy/static `VENDOR_MANAGED` roots without an attached audited application rule as protected, so old sidecars fail closed;\n- deterministic vendor whole-tree authority requires an attached `ApplicationCleanupRule` whose owner is TOOL and whose audited policy explicitly allows whole-tree cleanup;\n- the post-scan directory capability boundary independently re-checks the same provenance before producing a cleanup capability;\n- the fresh whole-tree policy layer refuses a legacy static vendor root rather than falling back to generic configured authority.\n\nMore-specific application rules are unchanged. `discover_known_cleanup_roots()` may still create runtime `VENDOR_MANAGED` roots, but only from audited application code that attaches the exact TOOL rule.\n\n## Consequence\n\nThis removes a cross-cutting bypass before the family-by-family source audit: a newly added or stale static path cannot make unknown descendants deletable merely by being called a vendor cache. Package managers, browsers, model stores, IDEs and developer tools must earn mutation authority through their dedicated application lifecycle model.\n''',
    encoding="utf-8",
    newline="\n",
)

replace_once(
    "docs/full-rule-reaudit-2026-08.md",
    "| Static VENDOR_MANAGED root fallback | ⏳ next | verify every root cannot bypass richer app/vendor semantics |",
    "| Static VENDOR_MANAGED root fallback | ✅ phase 3 | static roots are discovery-only; deterministic vendor authority requires an attached audited TOOL whole-tree rule |",
)
replace_once(
    "docs/full-rule-reaudit-2026-08.md",
    "| AGE_BASED_REVIEW temp lifecycle | ⏳ next | re-check exact Windows/temp semantics and age threshold |",
    "| AGE_BASED_REVIEW temp lifecycle | ⏳ next | Microsoft Storage Sense semantics do not justify raw one-day mtime authority; rework this next |",
)
replace_once(
    "docs/storage-audit-status.md",
    "Continue with static VENDOR_MANAGED roots, AGE_BASED_REVIEW lifecycle, selective file-default restoration, then application modules one by one.",
    "Static VENDOR_MANAGED fallback is now closed: static scan roots are discovery-only and runtime vendor authority requires an attached audited application TOOL whole-tree rule. Continue with AGE_BASED_REVIEW lifecycle, selective file-default restoration, then application modules one by one.",
)
