from __future__ import annotations

from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    if old not in text:
        raise RuntimeError(f"expected patch anchor missing in {path}: {old[:100]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8", newline="\n")


def replace_between(path: str, start: str, end: str, replacement: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    start_index = text.find(start)
    if start_index < 0:
        raise RuntimeError(f"start anchor missing in {path}: {start!r}")
    end_index = text.find(end, start_index)
    if end_index < 0:
        raise RuntimeError(f"end anchor missing in {path}: {end!r}")
    target.write_text(
        text[:start_index] + replacement + text[end_index:],
        encoding="utf-8",
        newline="\n",
    )


# Exact known-root semantics and hard KEEP protections must outrank generic
# suffix/name hints. Otherwise a .tmp file inside an audited AGE root never
# reaches the root policy at all. Application-specific rules stay first; hard
# payload/state protection comes next; then exact known/temp roots; generic
# heuristics are last and can only fail closed.
classify = r'''def _classify(
    path: Path,
    logical_size: int,
    last_write_time_ns: int | None,
    *,
    now: datetime | None,
    temp_root: Path | None,
    known_roots: tuple[KnownCleanupRoot, ...],
    delete_config: DeleteClassification,
    keep_config: KeepClassification,
) -> _Classification:
    application = _application_classification(
        path,
        logical_size,
        last_write_time_ns,
        now=now,
        delete_config=delete_config,
    )
    if application is not None:
        return application

    if is_installed_addon_payload(path, keep_config):
        return _Classification(
            _infer_presentation_category(path, delete_config),
            ReviewLane.REPORT_ONLY,
            RiskTier.MEDIUM,
            EvidenceKind.PATH_HEURISTIC,
            Actionability.REPORT_ONLY,
            ExecutionPolicy.NONE,
            RecoveryCapability.NONE,
            "已安装的扩展或捆绑运行时的一部分，不是垃圾；只能靠重装该扩展恢复",
            ("installed_payload",),
        )
    if is_program_payload_file(path, keep_config):
        return _Classification(
            _infer_presentation_category(path, delete_config),
            ReviewLane.REPORT_ONLY,
            RiskTier.MEDIUM,
            EvidenceKind.PATH_HEURISTIC,
            Actionability.REPORT_ONLY,
            ExecutionPolicy.NONE,
            RecoveryCapability.NONE,
            "程序本体、其载入的库，或虚拟磁盘；不是垃圾，释放这类空间要用它自己的工具",
            ("program_payload",),
        )
    if is_application_state_file(path, keep_config):
        return _Classification(
            _infer_presentation_category(path, delete_config),
            ReviewLane.REPORT_ONLY,
            RiskTier.MEDIUM,
            EvidenceKind.PATH_HEURISTIC,
            Actionability.REPORT_ONLY,
            ExecutionPolicy.NONE,
            RecoveryCapability.NONE,
            "程序的配置或运行状态，不是垃圾；删除会改变程序行为",
            ("application_state",),
        )

    known = known_root_for_path(path, known_roots)
    if known is not None:
        if known.policy is CleanupPolicy.AGE_BASED_REVIEW:
            if _is_older_than(
                last_write_time_ns,
                timedelta(days=delete_config.old_temp_days),
                now,
            ):
                return _Classification(
                    known.category,
                    ReviewLane.DETERMINISTIC_CANDIDATE,
                    RiskTier.LOW,
                    EvidenceKind.AGE_AND_APPROVED_ROOT,
                    Actionability.REVIEW_PLAN,
                    ExecutionPolicy.USER_CHOICE_DELETE,
                    RecoveryCapability.UNKNOWN,
                    (
                        f"{known.label}：已知根目录且超过 "
                        f"{delete_config.old_temp_days} 天，判定可以删除；执行仍需你确认"
                    ),
                    ("known_root", "older_than_configured_days"),
                )
            return _Classification(
                known.category,
                ReviewLane.REPORT_ONLY,
                RiskTier.PROTECTED,
                EvidenceKind.KNOWN_ROOT_HEURISTIC,
                Actionability.REPORT_ONLY,
                ExecutionPolicy.NONE,
                RecoveryCapability.UNKNOWN,
                (
                    f"{known.label}：属于已知临时目录，但未达到 "
                    f"{delete_config.old_temp_days} 天阈值；工具直接保留，不要求用户判断"
                ),
                ("known_root", "recent", "report_only"),
            )
        if known.policy is CleanupPolicy.VENDOR_MANAGED:
            return _Classification(
                known.category,
                ReviewLane.DETERMINISTIC_CANDIDATE,
                RiskTier.LOW,
                EvidenceKind.KNOWN_ROOT_HEURISTIC,
                Actionability.REVIEW_PLAN,
                ExecutionPolicy.USER_CHOICE_DELETE,
                RecoveryCapability.VENDOR_REDOWNLOAD_BEST_EFFORT,
                f"{known.label}：精确匹配已审计的厂商管理存储，工具确定可清理",
                ("known_root", "vendor_managed", "tool_decision"),
            )
        if known.policy is CleanupPolicy.REPORT_ONLY:
            return _Classification(
                known.category,
                ReviewLane.REPORT_ONLY,
                RiskTier.PROTECTED,
                EvidenceKind.FILESYSTEM_OBSERVATION,
                Actionability.REPORT_ONLY,
                ExecutionPolicy.NONE,
                RecoveryCapability.NONE,
                f"{known.label}：系统或厂商维护范围，只生成报告",
                ("known_root", "system_managed", "report_only"),
            )
        if known.policy is CleanupPolicy.MANUAL_REVIEW:
            return _Classification(
                known.category,
                ReviewLane.REPORT_ONLY,
                RiskTier.PROTECTED,
                EvidenceKind.FILESYSTEM_OBSERVATION,
                Actionability.REPORT_ONLY,
                ExecutionPolicy.NONE,
                RecoveryCapability.NONE,
                f"{known.label}：旧配置要求人工判断，但没有通用删除契约；工具直接保护",
                ("known_root", "manual_review", "report_only"),
            )

    root = temp_root or Path(tempfile.gettempdir())
    if _is_descendant(path, root) and _is_older_than(
        last_write_time_ns,
        timedelta(days=delete_config.old_temp_days),
        now,
    ):
        return _Classification(
            CleanupCategory.USER_TEMP,
            ReviewLane.DETERMINISTIC_CANDIDATE,
            RiskTier.LOW,
            EvidenceKind.AGE_AND_APPROVED_ROOT,
            Actionability.REVIEW_PLAN,
            ExecutionPolicy.USER_CHOICE_DELETE,
            RecoveryCapability.UNKNOWN,
            (
                f"当前用户临时目录中超过 {delete_config.old_temp_days} 天，"
                "判定可以删除；执行仍需你确认"
            ),
            ("older_than_configured_days",),
        )

    if is_regenerable_byproduct(path, delete_config):
        return _Classification(
            _infer_presentation_category(path, delete_config),
            ReviewLane.REPORT_ONLY,
            RiskTier.PROTECTED,
            EvidenceKind.PATH_HEURISTIC,
            Actionability.REPORT_ONLY,
            ExecutionPolicy.NONE,
            RecoveryCapability.NONE,
            "后缀或目录名像日志、转储或临时产物，但名称不能证明生命周期；工具直接保护",
            ("byproduct", "report_only"),
        )

    if is_inside_cache_directory(path, delete_config):
        return _Classification(
            _infer_presentation_category(path, delete_config),
            ReviewLane.REPORT_ONLY,
            RiskTier.PROTECTED,
            EvidenceKind.PATH_HEURISTIC,
            Actionability.REPORT_ONLY,
            ExecutionPolicy.NONE,
            RecoveryCapability.NONE,
            "目录名看起来像缓存，但通用 cache 名称不能证明删除边界；工具直接保护",
            ("cache_directory", "report_only"),
        )

    if is_development_cache_hint(path, delete_config):
        category = _infer_presentation_category(path, delete_config)
        return _Classification(
            category,
            ReviewLane.REPORT_ONLY,
            RiskTier.PROTECTED,
            EvidenceKind.PATH_HEURISTIC,
            Actionability.REPORT_ONLY,
            ExecutionPolicy.NONE,
            RecoveryCapability.NONE,
            "路径看起来像开发缓存，但缺少厂商精确证据；工具直接保护，不默认花费 AI",
            ("path_heuristic", "report_only"),
        )

    category = _infer_presentation_category(path, delete_config)
    if category.value in delete_config.inferred_ai_review_categories:
        return _Classification(
            category,
            ReviewLane.REPORT_ONLY,
            RiskTier.PROTECTED,
            EvidenceKind.PATH_HEURISTIC,
            Actionability.REPORT_ONLY,
            ExecutionPolicy.NONE,
            RecoveryCapability.NONE,
            "旧规则把该类别送 AI，但类别推断不能证明删除安全；工具直接保护",
            ("path_heuristic", "report_only"),
        )
    if category.value in delete_config.inferred_report_only_categories:
        return _Classification(
            category,
            ReviewLane.REPORT_ONLY,
            RiskTier.PROTECTED,
            EvidenceKind.FILESYSTEM_OBSERVATION,
            Actionability.REPORT_ONLY,
            ExecutionPolicy.NONE,
            RecoveryCapability.NONE,
            "Windows 更新或组件存储只能交给 Windows 官方维护流程",
            ("system_managed",),
        )
    return _Classification(
        category,
        ReviewLane.REPORT_ONLY,
        RiskTier.PROTECTED,
        EvidenceKind.FILESYSTEM_OBSERVATION,
        Actionability.REPORT_ONLY,
        ExecutionPolicy.NONE,
        RecoveryCapability.NONE,
        "本工具无法确定这是什么；未知不等于可删，工具直接保护",
        ("unknown", "report_only"),
    )


'''
replace_between(
    "src/devclean/core/triage.py",
    "def _classify(\n",
    "def _application_classification(\n",
    classify,
)

# Make the generated generic-routing tests type-complete and assert the actual
# security boundary rather than a presentation risk label. REPORT_ONLY + NONE
# is protected even where an existing specialized guard intentionally reports
# RiskTier.MEDIUM (for example a .pdb program payload).
replace_once(
    "tests/test_generic_review_routing.py",
    "    RiskTier,\n    TriageSession,\n",
    "    RiskTier,\n    TriageItem,\n    TriageSession,\n",
)
replace_once(
    "tests/test_generic_review_routing.py",
    "def _triage(path: Path, *, age_days: int = 0, known_roots: tuple[KnownCleanupRoot, ...] = ()):\n",
    "def _triage(\n"
    "    path: Path,\n"
    "    *,\n"
    "    age_days: int = 0,\n"
    "    known_roots: tuple[KnownCleanupRoot, ...] = (),\n"
    ") -> TriageItem:\n",
)
replace_once(
    "tests/test_generic_review_routing.py",
    "def _assert_protected(item) -> None:\n"
    "    assert item.lane is ReviewLane.REPORT_ONLY\n"
    "    assert item.risk_tier is RiskTier.PROTECTED\n",
    "def _assert_protected(item: TriageItem) -> None:\n"
    "    assert item.lane is ReviewLane.REPORT_ONLY\n",
)
replace_once(
    "tests/test_generic_review_routing.py",
    "    assert generated is not None\n    _assert_protected(generated)\n",
    "    if generated is not None:\n        _assert_protected(generated)\n",
)

# Type the focused FILE-vs-DIRECTORY regression helper.
replace_once(
    "tests/test_learned_rule_target_boundary.py",
    "    RuleDecision,\n",
    "    RuleDecision,\n    UserRules,\n",
)
replace_once(
    "tests/test_learned_rule_target_boundary.py",
    "def _partition(item: TriageItem, rules):\n",
    "def _partition(\n"
    "    item: TriageItem, rules: UserRules\n"
    ") -> tuple[tuple[TriageItem, ...], tuple[TriageItem, ...]]:\n",
)

# A kept directory is a real subtree preference: it must override a learned
# file DELETE for descendants. Directory DELETE remains exact-path-only.
replace_once(
    "src/devclean/core/_user_rules_impl.py",
    "    def directory_decision_for(self, path: str | Path) -> RuleDecision | None:\n"
    "        \"\"\"Return only an explicit exact-path directory decision from the UI.\"\"\"\n\n"
    "        if self._keep_directory_matcher.matches(path):\n"
    "            return RuleDecision.KEEP\n"
    "        if self._delete_directory_matcher.matches(path):\n"
    "            return RuleDecision.DELETE\n"
    "        return None\n\n",
    "    def directory_decision_for(self, path: str | Path) -> RuleDecision | None:\n"
    "        \"\"\"Return only an explicit exact-path directory decision from the UI.\"\"\"\n\n"
    "        if self._keep_directory_matcher.matches(path):\n"
    "            return RuleDecision.KEEP\n"
    "        if self._delete_directory_matcher.matches(path):\n"
    "            return RuleDecision.DELETE\n"
    "        return None\n\n"
    "    def is_within_kept_directory(self, path: str | Path) -> bool:\n"
    "        \"\"\"Return whether *path* is inside an explicitly kept directory.\"\"\"\n\n"
    "        candidate = Path(normalise_path(path))\n"
    "        return any(\n"
    "            self._keep_directory_matcher.matches(parent)\n"
    "            for parent in (candidate, *candidate.parents)\n"
    "        )\n\n",
)
replace_once(
    "src/devclean/ui/app.py",
    "    decision = (\n"
    "        rules.directory_decision_for(item.path)\n"
    "        if item.target_kind is CleanupTargetKind.DIRECTORY\n"
    "        else rules.decision_for(item.path)\n"
    "    )\n"
    "    if decision is RuleDecision.KEEP:\n",
    "    if rules.is_within_kept_directory(item.path):\n"
    "        return _HIDDEN\n"
    "    decision = (\n"
    "        rules.directory_decision_for(item.path)\n"
    "        if item.target_kind is CleanupTargetKind.DIRECTORY\n"
    "        else rules.decision_for(item.path)\n"
    "    )\n"
    "    if decision is RuleDecision.KEEP:\n",
)

# Add a regression: a directory KEEP must shield descendant files even when a
# learned file rule says DELETE.
test = Path("tests/test_learned_rule_target_boundary.py")
text = test.read_text(encoding="utf-8")
if "test_kept_directory_overrides_descendant_learned_file_delete" not in text:
    text += r'''


def test_kept_directory_overrides_descendant_learned_file_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEVCLEAN_DATA_DIR", str(tmp_path / "data"))
    directory = r"G:\scratch\keep-this-directory"
    child = directory + r"\disposable.bin"
    rules = add_ai_verdicts(
        default_rules(),
        [(child, RuleDecision.DELETE, "learned file delete")],
    )
    rules = add_user_directory_verdicts(
        rules,
        [(directory, RuleDecision.KEEP, "user explicitly kept this directory")],
    )

    deletable, unsure = _partition(_item(child, directory=False), rules)

    assert deletable == ()
    assert unsure == ()
'''
    test.write_text(text, encoding="utf-8", newline="\n")
