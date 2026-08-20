from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 1) Packaged scan policy: broad/raw roots that previously delegated expertise
# to the end user become report-only. Exact USER_REVIEW remains available only
# through source-audited application/vendor object lanes.
scan_path = ROOT / "src/devclean/config/scan-rules.json"
scan = json.loads(scan_path.read_text(encoding="utf-8"))
changed_ids: list[str] = []
for root in scan["known_cleanup_roots"]:
    if root["policy"] == "MANUAL_REVIEW":
        root["policy"] = "REPORT_ONLY"
        changed_ids.append(root["id"])
scan_path.write_text(
    json.dumps(scan, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
    newline="\n",
)

# Keep the schema field for compatibility with old sidecars, but new packaged
# defaults no longer nominate generic categories for AI. Runtime code below also
# protects old sidecars that still contain the former list.
delete_path = ROOT / "src/devclean/config/delete-rules.json"
delete = json.loads(delete_path.read_text(encoding="utf-8"))
delete["classification"]["inferred_ai_review_categories"] = []
delete_path.write_text(
    json.dumps(delete, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
    newline="\n",
)

# 2) Runtime classification: path names/suffixes/category guesses no longer
# create USER_REVIEW/AI_REVIEW deletion authority. This is intentionally coded
# in the classifier, not only JSON, so old user sidecars cannot preserve the
# previous risky routing after an executable upgrade.
triage_path = ROOT / "src/devclean/core/triage.py"
text = triage_path.read_text(encoding="utf-8")
replacements = {
'''        else:\n            lane = ReviewLane.USER_REVIEW\n            risk_tier = RiskTier.MEDIUM\n            evidence_kind = EvidenceKind.KNOWN_ROOT_HEURISTIC\n            actionability = Actionability.USER_REVIEW\n            execution_policy = ExecutionPolicy.USER_CHOICE_DELETE\n            reason = f"{known.label}：已识别目录，但是否清理取决于你的使用方式，由你决定"\n            tags = ("whole_directory", "known_cache_root", "user_review")\n''': '''        else:\n            lane = ReviewLane.REPORT_ONLY\n            risk_tier = RiskTier.PROTECTED\n            evidence_kind = EvidenceKind.FILESYSTEM_OBSERVATION\n            actionability = Actionability.REPORT_ONLY\n            execution_policy = ExecutionPolicy.NONE\n            reason = f"{known.label}：没有足够的通用删除契约，工具直接保护，不要求用户猜测"\n            tags = ("whole_directory", "known_cache_root", "report_only")\n''',
'''    elif scope is DirectoryScope.STALE_VERSION:\n        category = CleanupCategory.OTHER\n        recovery = RecoveryCapability.VENDOR_REDOWNLOAD_BEST_EFFORT\n        lane = ReviewLane.USER_REVIEW\n        risk_tier = RiskTier.MEDIUM\n        evidence_kind = EvidenceKind.PATH_HEURISTIC\n        actionability = Actionability.USER_REVIEW\n        execution_policy = ExecutionPolicy.USER_CHOICE_DELETE\n        reason = f"{path.name}：看起来像被更新取代的旧版本目录，但缺少厂商级证据，由你决定"\n        tags = ("whole_directory", "stale_version", "user_review")\n    else:\n        category = CleanupCategory.PROJECT_BUILD_OUTPUT\n        recovery = RecoveryCapability.VENDOR_REDOWNLOAD_BEST_EFFORT\n        lane = ReviewLane.USER_REVIEW\n        risk_tier = RiskTier.MEDIUM\n        evidence_kind = EvidenceKind.PATH_HEURISTIC\n        actionability = Actionability.USER_REVIEW\n        execution_policy = ExecutionPolicy.USER_CHOICE_DELETE\n        reason = f"{path.name}：通常是可重建的工具产物，但项目可能有自定义行为，由你决定"\n        tags = ("whole_directory", "regenerable_tool_output", "user_review")\n''': '''    elif scope is DirectoryScope.STALE_VERSION:\n        category = CleanupCategory.OTHER\n        recovery = RecoveryCapability.NONE\n        lane = ReviewLane.REPORT_ONLY\n        risk_tier = RiskTier.PROTECTED\n        evidence_kind = EvidenceKind.PATH_HEURISTIC\n        actionability = Actionability.REPORT_ONLY\n        execution_policy = ExecutionPolicy.NONE\n        reason = f"{path.name}：名称像旧版本但缺少厂商生命周期证据，工具直接保护"\n        tags = ("whole_directory", "stale_version", "report_only")\n    else:\n        category = CleanupCategory.PROJECT_BUILD_OUTPUT\n        recovery = RecoveryCapability.NONE\n        lane = ReviewLane.REPORT_ONLY\n        risk_tier = RiskTier.PROTECTED\n        evidence_kind = EvidenceKind.PATH_HEURISTIC\n        actionability = Actionability.REPORT_ONLY\n        execution_policy = ExecutionPolicy.NONE\n        reason = f"{path.name}：名称像可重建工具产物，但项目语义未经证明，工具直接保护"\n        tags = ("whole_directory", "regenerable_tool_output", "report_only")\n''',
'''    if is_regenerable_byproduct(path, delete_config):\n        return _Classification(\n            _infer_presentation_category(path, delete_config),\n            ReviewLane.USER_REVIEW,\n            RiskTier.MEDIUM,\n            EvidenceKind.PATH_HEURISTIC,\n            Actionability.USER_REVIEW,\n            ExecutionPolicy.USER_CHOICE_DELETE,\n            RecoveryCapability.VENDOR_REDOWNLOAD_BEST_EFFORT,\n            "看起来是日志、转储或临时产物，但仅凭文件名不能对所有用户保证可删；由你决定",\n            ("byproduct", "user_review"),\n        )\n\n    if is_inside_cache_directory(path, delete_config):\n        return _Classification(\n            _infer_presentation_category(path, delete_config),\n            ReviewLane.USER_REVIEW,\n            RiskTier.MEDIUM,\n            EvidenceKind.PATH_HEURISTIC,\n            Actionability.USER_REVIEW,\n            ExecutionPolicy.USER_CHOICE_DELETE,\n            RecoveryCapability.VENDOR_REDOWNLOAD_BEST_EFFORT,\n            "目录名看起来像缓存，但通用 cache 名称不足以给所有用户授予删除结论；由你决定",\n            ("cache_directory", "user_review"),\n        )\n''': '''    if is_regenerable_byproduct(path, delete_config):\n        return _Classification(\n            _infer_presentation_category(path, delete_config),\n            ReviewLane.REPORT_ONLY,\n            RiskTier.PROTECTED,\n            EvidenceKind.PATH_HEURISTIC,\n            Actionability.REPORT_ONLY,\n            ExecutionPolicy.NONE,\n            RecoveryCapability.NONE,\n            "后缀或目录名像日志、转储或临时产物，但名称不能证明生命周期；工具直接保护",\n            ("byproduct", "report_only"),\n        )\n\n    if is_inside_cache_directory(path, delete_config):\n        return _Classification(\n            _infer_presentation_category(path, delete_config),\n            ReviewLane.REPORT_ONLY,\n            RiskTier.PROTECTED,\n            EvidenceKind.PATH_HEURISTIC,\n            Actionability.REPORT_ONLY,\n            ExecutionPolicy.NONE,\n            RecoveryCapability.NONE,\n            "目录名看起来像缓存，但通用 cache 名称不能证明删除边界；工具直接保护",\n            ("cache_directory", "report_only"),\n        )\n''',
'''            return _Classification(\n                known.category,\n                ReviewLane.USER_REVIEW,\n                RiskTier.MEDIUM,\n                EvidenceKind.KNOWN_ROOT_HEURISTIC,\n                Actionability.USER_REVIEW,\n                ExecutionPolicy.USER_CHOICE_DELETE,\n                RecoveryCapability.UNKNOWN,\n                (\n                    f"{known.label}：属于已知临时目录，但未达到 "\n                    f"{delete_config.old_temp_days} 天阈值；由你决定是否提前清理"\n                ),\n                ("known_root", "recent", "user_review"),\n            )\n''': '''            return _Classification(\n                known.category,\n                ReviewLane.REPORT_ONLY,\n                RiskTier.PROTECTED,\n                EvidenceKind.KNOWN_ROOT_HEURISTIC,\n                Actionability.REPORT_ONLY,\n                ExecutionPolicy.NONE,\n                RecoveryCapability.UNKNOWN,\n                (\n                    f"{known.label}：属于已知临时目录，但未达到 "\n                    f"{delete_config.old_temp_days} 天阈值；工具直接保留，不要求用户判断"\n                ),\n                ("known_root", "recent", "report_only"),\n            )\n''',
'''        if known.policy is CleanupPolicy.MANUAL_REVIEW:\n            return _Classification(\n                known.category,\n                ReviewLane.USER_REVIEW,\n                RiskTier.MEDIUM,\n                EvidenceKind.KNOWN_ROOT_HEURISTIC,\n                Actionability.USER_REVIEW,\n                ExecutionPolicy.USER_CHOICE_DELETE,\n                RecoveryCapability.UNKNOWN,\n                f"{known.label}：已识别但没有通用删除结论，由你决定是否清理",\n                ("known_root", "manual_review", "user_review"),\n            )\n''': '''        if known.policy is CleanupPolicy.MANUAL_REVIEW:\n            return _Classification(\n                known.category,\n                ReviewLane.REPORT_ONLY,\n                RiskTier.PROTECTED,\n                EvidenceKind.FILESYSTEM_OBSERVATION,\n                Actionability.REPORT_ONLY,\n                ExecutionPolicy.NONE,\n                RecoveryCapability.NONE,\n                f"{known.label}：旧配置要求人工判断，但没有通用删除契约；工具直接保护",\n                ("known_root", "manual_review", "report_only"),\n            )\n''',
'''    if is_development_cache_hint(path, delete_config):\n        category = _infer_presentation_category(path, delete_config)\n        return _Classification(\n            category,\n            ReviewLane.AI_REVIEW,\n            RiskTier.HIGH,\n            EvidenceKind.PATH_HEURISTIC,\n            Actionability.AI_REVIEW,\n            ExecutionPolicy.USER_CHOICE_DELETE,\n            RecoveryCapability.UNKNOWN,\n            "路径看起来像开发缓存，但缺少厂商精确证据；不确定，交 AI 判断",\n            ("path_heuristic", "ai_review_required"),\n        )\n''': '''    if is_development_cache_hint(path, delete_config):\n        category = _infer_presentation_category(path, delete_config)\n        return _Classification(\n            category,\n            ReviewLane.REPORT_ONLY,\n            RiskTier.PROTECTED,\n            EvidenceKind.PATH_HEURISTIC,\n            Actionability.REPORT_ONLY,\n            ExecutionPolicy.NONE,\n            RecoveryCapability.NONE,\n            "路径看起来像开发缓存，但缺少厂商精确证据；工具直接保护，不默认花费 AI",\n            ("path_heuristic", "report_only"),\n        )\n''',
'''    if category.value in delete_config.inferred_ai_review_categories:\n        return _Classification(\n            category,\n            ReviewLane.AI_REVIEW,\n            RiskTier.HIGH,\n            EvidenceKind.PATH_HEURISTIC,\n            Actionability.AI_REVIEW,\n            ExecutionPolicy.USER_CHOICE_DELETE,\n            RecoveryCapability.UNKNOWN,\n            "疑似构建产物、安装介质或日志；需 AI 建议与用户最终确认",\n            ("path_heuristic", "ai_review_required"),\n        )\n''': '''    if category.value in delete_config.inferred_ai_review_categories:\n        return _Classification(\n            category,\n            ReviewLane.REPORT_ONLY,\n            RiskTier.PROTECTED,\n            EvidenceKind.PATH_HEURISTIC,\n            Actionability.REPORT_ONLY,\n            ExecutionPolicy.NONE,\n            RecoveryCapability.NONE,\n            "旧规则把该类别送 AI，但类别推断不能证明删除安全；工具直接保护",\n            ("path_heuristic", "report_only"),\n        )\n''',
'''    return _Classification(\n        category,\n        ReviewLane.AI_REVIEW,\n        RiskTier.HIGH,\n        EvidenceKind.FILESYSTEM_OBSERVATION,\n        Actionability.AI_REVIEW,\n        ExecutionPolicy.USER_CHOICE_DELETE,\n        RecoveryCapability.UNKNOWN,\n        "本工具无法确定这是什么；导出给 AI 判断，导回结果后由你确认执行",\n        ("ai_review_required",),\n    )\n''': '''    return _Classification(\n        category,\n        ReviewLane.REPORT_ONLY,\n        RiskTier.PROTECTED,\n        EvidenceKind.FILESYSTEM_OBSERVATION,\n        Actionability.REPORT_ONLY,\n        ExecutionPolicy.NONE,\n        RecoveryCapability.NONE,\n        "本工具无法确定这是什么；未知不等于可删，工具直接保护",\n        ("unknown", "report_only"),\n    )\n''',
}
for old, new in replacements.items():
    if old not in text:
        raise RuntimeError(f"triage block not found:\n{old[:160]}")
    text = text.replace(old, new, 1)
triage_path.write_text(text, encoding="utf-8", newline="\n")

# 3) Product copy: AI is now optional help for the small set of exact
# personal-value USER_REVIEW objects. Unknown/unproven objects are protected.
readme_path = ROOT / "README.md"
readme = readme_path.read_text(encoding="utf-8")
old_readme = '''它按“本地确定优先、用户判断其次、AI 只处理剩余不确定项”的顺序处理扫描结果：\n\n- 工具有经过审计、对所有用户都成立的清理依据时，直接进入“可以删除”列表；\n  默认勾选，但真正执行仍由用户点击清理。\n- 工具能解释这是什么、但是否值得删除取决于个人用途时，进入“需要判断”列表，\n  直接由用户决定，不产生 AI 费用。如果用户自己也拿不准某个文件，可以主动选中\n  后再交给 AI。\n- 工具确实无法从本地规则和路径证据判断的文件，才默认进入 AI 审核集合。导回\n  结论后自动分类；AI 仍不确定的项目继续由用户作最终决定。\n'''
new_readme = '''它按“产品承担技术判断、用户只决定个人取舍、未知默认保护”的顺序处理扫描结果：\n\n- 只有经过源码/厂商语义审计、并且本地边界可以精确证明的对象，才进入“可以删除”\n  列表；默认勾选，但真正执行仍由用户点击清理。\n- 只有技术语义已经明确、但是否保留确实取决于个人用途的对象，才进入“需要判断”\n  列表，例如某些精确识别的可重新下载模型/包。用户无需判断技术上“能不能删”；\n  只需决定自己是否还想保留。拿不准时可以主动选中文件再交给 AI。\n- 仅凭 cache/tmp/build/log 等名称、文件后缀、年龄或大小无法证明安全的对象，以及工具\n  真正无法识别的对象，默认保护并且不进入删除/AI 队列。AI 不再替产品制造删除权限。\n'''
if old_readme not in readme:
    raise RuntimeError("README routing copy did not match")
readme_path.write_text(readme.replace(old_readme, new_readme, 1), encoding="utf-8", newline="\n")

app_path = ROOT / "src/devclean/ui/app.py"
app = app_path.read_text(encoding="utf-8")
copy_replacements = {
'''            title="需要判断：先由你决定，必要时再用 AI",\n            hint=(\n                "能解释但不能对所有用户保证可删的项目，直接由你决定，不花 AI 费用；"\n                "本工具真正认不出的项目会默认交给 AI；你拿不准的文件也可以选中后主动交 AI。"\n                "AI 判断可能不准确且可能产生费用，导出文件包含本机完整路径；"\n                "AI 仍不确定的也回到你这里最终决定。"\n            ),\n''': '''            title="需要你的偏好：仅保留少量明确项目",\n            hint=(\n                "这里只放技术语义已明确、是否保留确实取决于用途的项目；"\n                "无法证明安全的未知项会直接保护，不会默认交给 AI。"\n                "你拿不准某个文件时可主动选中后交 AI；AI 可能判断错误且可能产生费用，"\n                "导出文件包含本机完整路径。"\n            ),\n''',
'''                ("export", "导出给 AI", "Muted", self._export_for_ai),\n''': '''                ("export", "导出给 AI（可选）", "Muted", self._export_for_ai),\n''',
'''                    "没有必须交 AI 的未知文件。若你对某个“你来决定”的文件也拿不准，"\n                    "先选中它，再点“导出给 AI”。",\n''': '''                    "没有默认需要交 AI 的项目。若你对右侧某个确实取决于用途的文件拿不准，"\n                    "先选中它，再点“导出给 AI（可选）”。",\n''',
'''                "请先选中右侧可由你判断的项目；真正未知的项目需要先交 AI。",\n''': '''                "请先选中右侧确实取决于你用途的项目；无法证明安全的未知项会直接保护。",\n''',
}
for old, new in copy_replacements.items():
    if old not in app:
        raise RuntimeError(f"UI copy block not found: {old[:120]}")
    app = app.replace(old, new, 1)
app_path.write_text(app, encoding="utf-8", newline="\n")

# 4) Update broad-root regression expectations.
builtin_path = ROOT / "tests/test_builtin_root_authority.py"
builtin = builtin_path.read_text(encoding="utf-8")
builtin = builtin.replace("is CleanupPolicy.MANUAL_REVIEW", "is CleanupPolicy.REPORT_ONLY")
builtin = builtin.replace(
    "def test_windows_old_requires_user_review_and_has_no_whole_root_authority() -> None:",
    "def test_windows_old_is_report_only_and_has_no_whole_root_authority() -> None:",
)
builtin = builtin.replace(
    "def test_system_crash_dumps_require_user_review() -> None:",
    "def test_system_crash_dumps_are_report_only() -> None:",
)
builtin_path.write_text(builtin, encoding="utf-8", newline="\n")

# 5) Add explicit regression coverage for the new expert-product boundary.
test_path = ROOT / "tests/test_generic_review_routing.py"
test_path.write_text(r'''from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from devclean.core.cleanup_catalog import KnownCleanupRoot
from devclean.core.rule_schema import CleanupCategory, CleanupPolicy
from devclean.core.triage import (
    Actionability,
    CleanupTargetKind,
    ExecutionPolicy,
    ReviewLane,
    RiskTier,
    TriageSession,
    triage_directory,
    triage_file,
)
from devclean.core.user_rules import DecisionRule, RuleMatch, UserRules, default_rules
from devclean.scanner.filesystem import ScanRecord, ScanRecordKind
from devclean.ui import app

_NOW = datetime(2026, 8, 20, tzinfo=UTC)


def _file_record(path: Path, *, age_days: int = 0, size: int = 4096) -> ScanRecord:
    return ScanRecord(
        root=str(path.parent.parent),
        path=str(path),
        kind=ScanRecordKind.FILE,
        depth=2,
        logical_size=size,
        allocated_size=size,
        raw_allocated_size=size,
        volume_serial=1,
        file_id="1" * 32,
        file_id_kind="file_id_128",
        link_count=1,
        attributes=0,
        creation_time_ns=1,
        last_write_time_ns=int((_NOW - timedelta(days=age_days)).timestamp() * 1_000_000_000),
    )


def _directory_record(path: Path, *, root: Path) -> ScanRecord:
    return ScanRecord(
        root=str(root),
        path=str(path),
        kind=ScanRecordKind.DIRECTORY,
        depth=1,
        logical_size=0,
        allocated_size=0,
        raw_allocated_size=0,
        volume_serial=1,
        file_id="2" * 32,
        file_id_kind="file_id_128",
        link_count=1,
        attributes=0x10,
        creation_time_ns=1,
        last_write_time_ns=int(_NOW.timestamp() * 1_000_000_000),
    )


def _triage(path: Path, *, age_days: int = 0, known_roots: tuple[KnownCleanupRoot, ...] = ()):
    rules = default_rules()
    return triage_file(
        _file_record(path, age_days=age_days),
        delete_config=rules.delete.classification,
        keep_config=rules.keep.classification,
        now=_NOW,
        temp_root=path.parents[1] / "different-temp-root",
        known_roots=known_roots,
    )


def _assert_protected(item) -> None:
    assert item.lane is ReviewLane.REPORT_ONLY
    assert item.risk_tier is RiskTier.PROTECTED
    assert item.actionability is Actionability.REPORT_ONLY
    assert item.execution_policy is ExecutionPolicy.NONE
    assert not app.is_direct_cleanup_eligible(item)
    assert not app.is_user_review_eligible(item)
    assert not app.is_ai_review_eligible(item)


def test_generic_filename_cache_and_development_hints_are_protected(tmp_path: Path) -> None:
    cases = (
        tmp_path / "opaque" / "diagnostics.log",
        tmp_path / "opaque" / "library.pdb",
        tmp_path / "cache" / "payload.bin",
        tmp_path / "huggingface" / "opaque.bin",
        tmp_path / "target" / "artifact.blob",
        tmp_path / "Downloads" / "installer.iso",
        tmp_path / "unknown" / "mystery.blob",
    )
    for path in cases:
        path.parent.mkdir(parents=True, exist_ok=True)
        _assert_protected(_triage(path))


def test_recent_age_based_root_is_kept_without_asking_user(tmp_path: Path) -> None:
    root = tmp_path / "known-temp"
    root.mkdir()
    known = KnownCleanupRoot(
        path=root,
        category=CleanupCategory.USER_TEMP,
        policy=CleanupPolicy.AGE_BASED_REVIEW,
        label="Known temp",
    )
    recent = root / "recent.tmp"
    old = root / "old.tmp"

    _assert_protected(_triage(recent, age_days=0, known_roots=(known,)))
    old_item = _triage(old, age_days=3, known_roots=(known,))
    assert old_item.lane is ReviewLane.DETERMINISTIC_CANDIDATE
    assert app.is_direct_cleanup_eligible(old_item)


def test_legacy_manual_review_root_is_protected_even_from_old_sidecar(tmp_path: Path) -> None:
    root = tmp_path / "legacy-manual"
    root.mkdir()
    known = KnownCleanupRoot(
        path=root,
        category=CleanupCategory.OTHER,
        policy=CleanupPolicy.MANUAL_REVIEW,
        label="Legacy manual root",
    )
    _assert_protected(_triage(root / "payload.bin", known_roots=(known,)))


def test_generic_stale_version_and_tool_output_directories_are_not_user_delete_lanes(
    tmp_path: Path,
) -> None:
    rules = default_rules()
    versions = tmp_path / "versions"
    old = versions / "1.0.0"
    current = versions / "2.0.0"
    old.mkdir(parents=True)
    current.mkdir()
    stale = triage_directory(
        _directory_record(old, root=tmp_path),
        delete_config=rules.delete.classification,
        keep_config=rules.keep.classification,
    )
    assert stale is not None
    _assert_protected(stale)

    project = tmp_path / "project"
    node_modules = project / "node_modules"
    node_modules.mkdir(parents=True)
    generated = triage_directory(
        _directory_record(node_modules, root=tmp_path),
        delete_config=rules.delete.classification,
        keep_config=rules.keep.classification,
    )
    assert generated is not None
    _assert_protected(generated)


def test_learned_delete_rule_cannot_promote_generic_protected_path(tmp_path: Path) -> None:
    path = tmp_path / "cache" / "opaque.log"
    path.parent.mkdir()
    item = _triage(path)
    _assert_protected(item)

    base = default_rules()
    rules = UserRules(
        scan=base.scan,
        delete=replace(
            base.delete,
            rules=(
                DecisionRule(
                    rule_id="legacy-delete",
                    group="ai_import",
                    match=RuleMatch.EXACT_PATH,
                    value=item.path,
                    source="AI_IMPORT",
                    reason="old heuristic verdict",
                ),
            ),
        ),
        keep=base.keep,
    )
    session = TriageSession(review_sample_per_category=rules.scan.review_sample_per_category)
    session.observe_path(item.path, rules)
    session.add(item)

    deletable, unsure = app._partition_items(session, rules)
    assert deletable == ()
    assert unsure == ()


def test_packaged_scan_roots_no_longer_delegate_broad_raw_paths_to_manual_review() -> None:
    rules = default_rules()
    assert not any(
        CleanupPolicy(root.policy) is CleanupPolicy.MANUAL_REVIEW
        for root in rules.scan.known_cleanup_roots
    )
    assert rules.delete.classification.inferred_ai_review_categories == frozenset()
''', encoding="utf-8", newline="\n")

# 6) Durable audit notes and complete re-audit tracker.
audit_path = ROOT / "docs/generic-review-routing-reaudit.md"
audit_path.write_text(
    """# Generic review routing re-audit\n\n"
    "Audited: 2026-08-20\n\n"
    "## Product conclusion\n\n"
    "DevClean must not outsource technical uncertainty to a non-expert user or to a paid AI model. "
    "Generic path/name heuristics remain useful for explanation, but they no longer create deletion "
    "authority.\n\n"
    "The generic scanner now uses this order:\n\n"
    "1. source/vendor-backed application semantics and exact local boundary -> deterministic candidate;\n"
    "2. source-backed exact object whose retention value is genuinely personal -> USER_REVIEW;\n"
    "3. generic name/suffix/category/unknown semantics -> REPORT_ONLY / protected;\n"
    "4. AI is optional help only when the user actively selects an already legitimate USER_REVIEW file.\n\n"
    "## Authority removed\n\n"
    "The following evidence no longer produces USER_REVIEW or AI_REVIEW by itself:\n\n"
    "- `.log`, `.bak`, `.tmp`, `.dmp`, `.pdb` and other generic byproduct suffixes;\n"
    "- a parent directory named `cache`, `.cache`, `caches` or similar;\n"
    "- generic development-cache path hints;\n"
    "- inferred build-output, installer/download or system-log categories;\n"
    "- an otherwise unknown file;\n"
    "- directories merely named `node_modules`, `__pycache__`, `.mypy_cache`, `.pytest_cache`, "
    "  `.ruff_cache`, `cache` or `.cache`;\n"
    "- version-looking directories beneath generic `versions`, `application`, `app` or `update` parents;\n"
    "- legacy `MANUAL_REVIEW` configured roots;\n"
    "- recent items inside AGE_BASED_REVIEW temp roots that have not reached the audited age threshold.\n\n"
    "Old sidecar scan rules cannot preserve these former routes: runtime classification itself now "
    "fails closed. Likewise, an old learned DELETE verdict cannot promote a REPORT_ONLY item because "
    "the UI/executor only honors learned deletion inside an already executable lane.\n\n"
    "## Broad packaged roots\n\n"
    f"The packaged scan config moved {len(changed_ids)} formerly `MANUAL_REVIEW` root groups to "
    "`REPORT_ONLY`: " + ", ".join(f"`{item}`" for item in changed_ids) + ".\n\n"
    "This does not disable more-specific application rules. Application classification runs before "
    "generic known-root policy, so a source-audited exact TOOL or USER object can still receive its "
    "narrow lane. Dedicated maintenance dialogs remain the preferred way to handle Windows, package "
    "managers, models, browsers, IDEs and build systems.\n\n"
    "## What intentionally remains\n\n"
    "- old entries in exact AGE_BASED_REVIEW temp roots remain deterministic after the configured age; "
    "  their lifecycle will be re-audited separately;\n"
    "- exact application `USER_DECISION` objects remain USER_REVIEW because the technical meaning is "
    "  already known and only personal retention value remains;\n"
    "- exact application TOOL rules and vendor maintenance lanes remain deterministic subject to their "
    "  existing identity/concurrency/revalidation guards.\n\n"
    "## Next phase\n\n"
    "Re-audit every static `VENDOR_MANAGED` configured root against the corresponding application "
    "matcher/vendor maintenance path. A configured root must not provide raw fallback authority when "
    "the richer application model intentionally protects an unrecognized child. Then re-verify the "
    "application modules one by one against current upstream sources.\n"
    """,
    encoding="utf-8",
    newline="\n",
)

tracker = ROOT / "docs/full-rule-reaudit-2026-08.md"
root_rows = "\n".join(
    f"| `{root['id']}` | `{root['policy']}` | phase 2 generic boundary applied; vendor/source detail still tracked separately |"
    for root in scan["known_cleanup_roots"]
)
tracker.write_text(
    """# Full rule re-audit tracker — 2026-08\n\n"
    "This tracker exists so the requested second-pass audit is genuinely one-by-one rather than a "
    "sequence of ad-hoc fixes. A check means the layer has been re-audited on current main; it does not "
    "mean every neighboring product has already been re-verified.\n\n"
    "## Cross-cutting pipeline\n\n"
    "| Layer | Status | Result |\n| --- | --- | --- |\n"
    "| Packaged DELETE/KEEP defaults | ✅ #142 | learned machine decisions removed; neutral defaults + conservative migration |\n"
    "| Generic file-name/suffix/cache heuristics | ✅ phase 2 | protected/report-only; no USER/AI delete authority |\n"
    "| Generic directory-name/version heuristics | ✅ phase 2 | protected/report-only; no whole-tree USER delete authority |\n"
    "| Generic unknown-file routing | ✅ phase 2 | protected; no default paid AI route |\n"
    "| Legacy MANUAL_REVIEW raw roots | ✅ phase 2 | runtime fails closed to REPORT_ONLY |\n"
    "| Static VENDOR_MANAGED root fallback | ⏳ next | verify every root cannot bypass richer app/vendor semantics |\n"
    "| AGE_BASED_REVIEW temp lifecycle | ⏳ next | re-check exact Windows/temp semantics and age threshold |\n"
    "| Scan exclusions/pruning | ⏳ queued | verify no important audited cache is accidentally skipped and no user data is widened |\n"
    "| Learned-rule portability/generalization | ⏳ queued | re-check generated glob/regex reuse after #142 neutral baseline |\n"
    "| Execution identity/reparse/hardlink/concurrency gates | ⏳ queued | second-pass regression audit; no weakening planned |\n\n"
    "## Packaged known cleanup roots\n\n"
    "| Root id | Current packaged policy | Re-audit state |\n| --- | --- | --- |\n"
    + root_rows
    + "\n\n## Application/source modules\n\n"
    "Re-verify on current primary vendor docs/source in small PRs. Recent audits are evidence, not a "
    "permanent exemption from this second pass.\n\n"
    "| Family | Status |\n| --- | --- |\n"
    "| Chromium browsers: Chrome / Edge / Brave / Vivaldi / Opera | ⏳ queued (Brave/Vivaldi/Opera recent authority corrections already landed) |\n"
    "| Firefox | ⏳ queued |\n"
    "| Electron/editors: VS Code / Cursor / Windsurf / Trae / Claude / Codex | ⏳ queued |\n"
    "| JetBrains / Toolbox / Android Studio | ⏳ queued |\n"
    "| Python: pip / uv / Conda / PyTorch Hub / Hugging Face Hub | ⏳ queued |\n"
    "| JS: npm / pnpm / Yarn / Bun / Cypress / Playwright / Puppeteer | ⏳ queued |\n"
    "| JVM: Gradle / Maven | ⏳ queued |\n"
    "| .NET / NuGet | ⏳ queued |\n"
    "| Go / Cargo / Conan / vcpkg | ⏳ queued |\n"
    "| Docker / Podman / WSL | ⏳ queued |\n"
    "| Android SDK / AVD | ⏳ queued |\n"
    "| Unity / Unreal | ⏳ queued |\n"
    "| Ollama / LM Studio and other local-model products | ⏳ queued |\n"
    "| Windows diagnostics / servicing / Recycle Bin / previous install | ⏳ queued |\n"
    "| Project build systems: Bazel / Cargo / Meson / CMake / MSBuild / Ninja / Make / SCons | ⏳ queued |\n\n"
    "## Acceptance rule for reducing user/AI burden\n\n"
    "A reduction in USER_REVIEW or AI_REVIEW counts is accepted only by moving an item either upward "
    "to a source-proven exact deterministic lane or downward to protected/report-only. It must never be "
    "achieved by treating cache-like names, age, size, redownloadability, or an AI guess as deletion authority.\n",
    encoding="utf-8",
    newline="\n",
)

# 7) Durable handoff explicitly records the newly reopened full audit.
status_path = ROOT / "docs/storage-audit-status.md"
status = status_path.read_text(encoding="utf-8")
needle = "## Current high-value queue\n"
insert = (
    "## 2026-08 second-pass rule re-audit\n\n"
    "A full rule re-audit is active in `docs/full-rule-reaudit-2026-08.md`. Phase 1 removed "
    "machine-specific learned decisions from packaged defaults (#142). Phase 2 removes generic "
    "name/suffix/category/unknown USER/AI routing: unproven raw paths are protected instead of "
    "outsourcing technical risk to a non-expert user or paid AI. Continue with static "
    "VENDOR_MANAGED roots, AGE_BASED_REVIEW lifecycle, then application modules one by one.\n\n"
)
if needle not in status:
    raise RuntimeError("storage audit queue heading not found")
status_path.write_text(status.replace(needle, insert + needle, 1), encoding="utf-8", newline="\n")
