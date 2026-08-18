from __future__ import annotations

import re
from pathlib import Path


def sub_once(text: str, pattern: str, replacement: str, *, flags: int = 0) -> str:
    updated, count = re.subn(pattern, replacement, text, count=1, flags=flags)
    if count != 1:
        raise RuntimeError(f"expected one match, got {count}: {pattern[:100]}")
    return updated


def replace_once(text: str, old: str, new: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"expected one exact match, got {count}: {old[:100]!r}")
    return text.replace(old, new, 1)


def patch_triage() -> None:
    path = Path("src/devclean/core/triage.py")
    text = path.read_text(encoding="utf-8")

    text = replace_once(
        text,
        '''class ReviewLane(StrEnum):
    """Human review queues; none implies automatic execution."""

    DETERMINISTIC_CANDIDATE = "DETERMINISTIC_CANDIDATE"
    AI_REVIEW = "AI_REVIEW"
    REPORT_ONLY = "REPORT_ONLY"
''',
        '''class ReviewLane(StrEnum):
    """Local confidence lanes; none implies automatic execution."""

    DETERMINISTIC_CANDIDATE = "DETERMINISTIC_CANDIDATE"
    USER_REVIEW = "USER_REVIEW"
    AI_REVIEW = "AI_REVIEW"
    REPORT_ONLY = "REPORT_ONLY"
''',
    )
    text = replace_once(
        text,
        '''class Actionability(StrEnum):
    """Which post-scan workflow may consider one observation."""

    REVIEW_PLAN = "REVIEW_PLAN"
    AI_REVIEW = "AI_REVIEW"
    REPORT_ONLY = "REPORT_ONLY"
''',
        '''class Actionability(StrEnum):
    """Which post-scan workflow may consider one observation."""

    REVIEW_PLAN = "REVIEW_PLAN"
    USER_REVIEW = "USER_REVIEW"
    AI_REVIEW = "AI_REVIEW"
    REPORT_ONLY = "REPORT_ONLY"
''',
    )

    directory_block = '''    if scope is DirectoryScope.KNOWN_CACHE_ROOT and known is not None:
        category = known.category
        recovery = (
            RecoveryCapability.VENDOR_REDOWNLOAD_BEST_EFFORT
            if known.policy is CleanupPolicy.VENDOR_MANAGED
            else RecoveryCapability.UNKNOWN
        )
        if known.policy is CleanupPolicy.VENDOR_MANAGED:
            lane = ReviewLane.DETERMINISTIC_CANDIDATE
            risk_tier = RiskTier.LOW
            evidence_kind = EvidenceKind.KNOWN_ROOT_HEURISTIC
            actionability = Actionability.REVIEW_PLAN
            execution_policy = ExecutionPolicy.USER_CHOICE_DELETE
            reason = f"{known.label}：精确匹配已审计的厂商管理根目录，可作为单个对象清理"
            tags = ("whole_directory", "known_cache_root", "tool_decision")
        elif known.policy is CleanupPolicy.REPORT_ONLY:
            lane = ReviewLane.REPORT_ONLY
            risk_tier = RiskTier.PROTECTED
            evidence_kind = EvidenceKind.FILESYSTEM_OBSERVATION
            actionability = Actionability.REPORT_ONLY
            execution_policy = ExecutionPolicy.NONE
            reason = f"{known.label}：已知受保护目录，只生成报告"
            tags = ("whole_directory", "known_cache_root", "report_only")
        else:
            lane = ReviewLane.USER_REVIEW
            risk_tier = RiskTier.MEDIUM
            evidence_kind = EvidenceKind.KNOWN_ROOT_HEURISTIC
            actionability = Actionability.USER_REVIEW
            execution_policy = ExecutionPolicy.USER_CHOICE_DELETE
            reason = f"{known.label}：已识别目录，但是否清理取决于你的使用方式，由你决定"
            tags = ("whole_directory", "known_cache_root", "user_review")
    elif scope is DirectoryScope.AGED_TEMP_ITEM:
        category = CleanupCategory.USER_TEMP
        recovery = RecoveryCapability.VENDOR_REDOWNLOAD_BEST_EFFORT
        lane = ReviewLane.DETERMINISTIC_CANDIDATE
        risk_tier = RiskTier.LOW
        evidence_kind = EvidenceKind.AGE_AND_APPROVED_ROOT
        actionability = Actionability.REVIEW_PLAN
        execution_policy = ExecutionPolicy.USER_CHOICE_DELETE
        reason = (
            f"{path.name}：临时目录中超过 "
            f"{delete_config.old_temp_days} 天未改动的整个条目"
        )
        tags = ("whole_directory", "aged_temp_item", "tool_decision")
    elif scope is DirectoryScope.STALE_VERSION:
        category = CleanupCategory.OTHER
        recovery = RecoveryCapability.VENDOR_REDOWNLOAD_BEST_EFFORT
        lane = ReviewLane.AI_REVIEW
        risk_tier = RiskTier.HIGH
        evidence_kind = EvidenceKind.PATH_HEURISTIC
        actionability = Actionability.AI_REVIEW
        execution_policy = ExecutionPolicy.USER_CHOICE_DELETE
        reason = f"{path.name}：看起来像被更新取代的旧版本目录，但缺少厂商级证据，交 AI 判断"
        tags = ("whole_directory", "stale_version", "ai_review_required")
    else:
        category = CleanupCategory.PROJECT_BUILD_OUTPUT
        recovery = RecoveryCapability.VENDOR_REDOWNLOAD_BEST_EFFORT
        lane = ReviewLane.USER_REVIEW
        risk_tier = RiskTier.MEDIUM
        evidence_kind = EvidenceKind.PATH_HEURISTIC
        actionability = Actionability.USER_REVIEW
        execution_policy = ExecutionPolicy.USER_CHOICE_DELETE
        reason = f"{path.name}：通常是可重建的工具产物，但项目可能有自定义行为，由你决定"
        tags = ("whole_directory", "regenerable_tool_output", "user_review")
    return TriageItem(
        record=record,
        path=record.path,
        logical_size=0,
        allocated_size=None,
        category=category,
        source_domain=source_domain_for_category(
            category, delete_config.category_source_domains
        ),
        lane=lane,
        risk_tier=risk_tier,
        evidence_kind=evidence_kind,
        actionability=actionability,
        execution_policy=execution_policy,
        recovery=recovery,
        reason=reason,
        tags=tags,
        target_kind=CleanupTargetKind.DIRECTORY,
        directory_scope=scope,
    )
'''
    text = sub_once(
        text,
        r"    if scope is DirectoryScope\.KNOWN_CACHE_ROOT and known is not None:.*?        directory_scope=scope,\n    \)\n",
        directory_block,
        flags=re.S,
    )

    text = sub_once(
        text,
        r"    if is_regenerable_byproduct\(path, delete_config\):\n        return _Classification\(.*?\n        \)\n\n    if is_inside_cache_directory",
        '''    if is_regenerable_byproduct(path, delete_config):
        return _Classification(
            _infer_presentation_category(path, delete_config),
            ReviewLane.USER_REVIEW,
            RiskTier.MEDIUM,
            EvidenceKind.PATH_HEURISTIC,
            Actionability.USER_REVIEW,
            ExecutionPolicy.USER_CHOICE_DELETE,
            RecoveryCapability.VENDOR_REDOWNLOAD_BEST_EFFORT,
            "看起来是日志、转储或临时产物，但仅凭文件名不能对所有用户保证可删；由你决定",
            ("byproduct", "user_review"),
        )

    if is_inside_cache_directory''',
        flags=re.S,
    )
    text = sub_once(
        text,
        r"    if is_inside_cache_directory\(path, delete_config\):\n        return _Classification\(.*?\n        \)\n\n    if is_installed_addon_payload",
        '''    if is_inside_cache_directory(path, delete_config):
        return _Classification(
            _infer_presentation_category(path, delete_config),
            ReviewLane.USER_REVIEW,
            RiskTier.MEDIUM,
            EvidenceKind.PATH_HEURISTIC,
            Actionability.USER_REVIEW,
            ExecutionPolicy.USER_CHOICE_DELETE,
            RecoveryCapability.VENDOR_REDOWNLOAD_BEST_EFFORT,
            "目录名看起来像缓存，但通用 cache 名称不足以给所有用户授予删除结论；由你决定",
            ("cache_directory", "user_review"),
        )

    if is_installed_addon_payload''',
        flags=re.S,
    )

    text = replace_once(
        text,
        '''            return _Classification(
                known.category,
                ReviewLane.AI_REVIEW,
                RiskTier.HIGH,
                EvidenceKind.KNOWN_ROOT_HEURISTIC,
                Actionability.AI_REVIEW,
                ExecutionPolicy.USER_CHOICE_DELETE,
                RecoveryCapability.UNKNOWN,
                (
                    f"{known.label}：属于配置列明的已知临时目录，但未达到 "
                    f"{delete_config.old_temp_days} 天阈值；左栏默认勾选，可自行取消"
                ),
                ("known_root", "recent"),
            )
''',
        '''            return _Classification(
                known.category,
                ReviewLane.USER_REVIEW,
                RiskTier.MEDIUM,
                EvidenceKind.KNOWN_ROOT_HEURISTIC,
                Actionability.USER_REVIEW,
                ExecutionPolicy.USER_CHOICE_DELETE,
                RecoveryCapability.UNKNOWN,
                (
                    f"{known.label}：属于已知临时目录，但未达到 "
                    f"{delete_config.old_temp_days} 天阈值；由你决定是否提前清理"
                ),
                ("known_root", "recent", "user_review"),
            )
''',
    )
    text = replace_once(
        text,
        '''            return _Classification(
                known.category,
                ReviewLane.AI_REVIEW,
                RiskTier.HIGH,
                EvidenceKind.KNOWN_ROOT_HEURISTIC,
                Actionability.AI_REVIEW,
                ExecutionPolicy.USER_CHOICE_DELETE,
                RecoveryCapability.VENDOR_REDOWNLOAD_BEST_EFFORT,
                (
                    f"{known.label}：属于配置列明的厂商管理存储；工具判定可清理，"
                    "实际方式仍由你在左栏选择"
                ),
                ("known_root", "vendor_managed"),
            )
''',
        '''            return _Classification(
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
''',
    )
    text = replace_once(
        text,
        '''            return _Classification(
                known.category,
                ReviewLane.AI_REVIEW,
                RiskTier.HIGH,
                EvidenceKind.KNOWN_ROOT_HEURISTIC,
                Actionability.AI_REVIEW,
                ExecutionPolicy.USER_CHOICE_DELETE,
                RecoveryCapability.UNKNOWN,
                f"{known.label}：配置列明的已知缓存位置；清理前可自行关闭相关应用",
                ("known_root", "manual_review"),
            )
''',
        '''            return _Classification(
                known.category,
                ReviewLane.USER_REVIEW,
                RiskTier.MEDIUM,
                EvidenceKind.KNOWN_ROOT_HEURISTIC,
                Actionability.USER_REVIEW,
                ExecutionPolicy.USER_CHOICE_DELETE,
                RecoveryCapability.UNKNOWN,
                f"{known.label}：已识别但没有通用删除结论，由你决定是否清理",
                ("known_root", "manual_review", "user_review"),
            )
''',
    )
    text = replace_once(
        text,
        '''        return _Classification(
            category,
            ReviewLane.AI_REVIEW,
            RiskTier.HIGH,
            EvidenceKind.KNOWN_ROOT_HEURISTIC,
            Actionability.AI_REVIEW,
            ExecutionPolicy.USER_CHOICE_DELETE,
            RecoveryCapability.NONE,
            f"{rule.label}：用户产生的数据，由用户决定；历史分组 {bucket}",
            (*common_tags, "user_decision", f"age_bucket:{bucket}"),
        )
''',
        '''        return _Classification(
            category,
            ReviewLane.USER_REVIEW,
            RiskTier.MEDIUM,
            EvidenceKind.KNOWN_ROOT_HEURISTIC,
            Actionability.USER_REVIEW,
            ExecutionPolicy.USER_CHOICE_DELETE,
            RecoveryCapability.NONE,
            f"{rule.label}：用户产生的数据，由用户决定；历史分组 {bucket}",
            (*common_tags, "user_decision", "user_review", f"age_bucket:{bucket}"),
        )
''',
    )

    path.write_text(text, encoding="utf-8")


def patch_app() -> None:
    path = Path("src/devclean/ui/app.py")
    text = path.read_text(encoding="utf-8")

    text = replace_once(
        text,
        '''"""DevClean GUI: scan selected fixed drives, sort into two buckets, delete.

Two buckets, because the tool has exactly two answers about any file: it is sure
the file can go, or it is not sure and the question goes to a model.  The
three public rule files define where to scan and what to delete or keep.  Only
items the model explicitly leaves UNSURE can be decided by the user in the same
right-hand bucket; there is no third queue.
''',
        '''"""DevClean GUI: scan selected fixed drives, separate confidence, delete.

DevClean uses deterministic local rules first, user judgment second, and AI only
for residual ambiguity. The right-hand review pane may contain both user-review
and AI-review items, but only the AI_REVIEW subset can be exported to a model.
''',
    )

    text = sub_once(
        text,
        r"def _is_vouched_for\(item: TriageItem\) -> bool:.*?def _rows_of\(",
        '''def is_direct_cleanup_eligible(item: TriageItem) -> bool:
    """Return whether DevClean has a universal local cleanup conclusion."""

    return (
        item.lane is ReviewLane.DETERMINISTIC_CANDIDATE
        and item.actionability is Actionability.REVIEW_PLAN
        and item.execution_policy is ExecutionPolicy.USER_CHOICE_DELETE
        and item.risk_tier is not RiskTier.PROTECTED
    )


def is_user_review_eligible(item: TriageItem) -> bool:
    """Return whether the item is understandable locally but needs user intent."""

    return (
        item.lane is ReviewLane.USER_REVIEW
        and item.actionability is Actionability.USER_REVIEW
        and item.execution_policy is ExecutionPolicy.USER_CHOICE_DELETE
        and item.risk_tier is not RiskTier.PROTECTED
    )


def is_ai_review_eligible(item: TriageItem) -> bool:
    """Return whether local evidence is genuinely insufficient and AI may help."""

    return (
        item.target_kind is CleanupTargetKind.FILE
        and item.lane is ReviewLane.AI_REVIEW
        and item.actionability is Actionability.AI_REVIEW
        and item.execution_policy is ExecutionPolicy.USER_CHOICE_DELETE
        and item.risk_tier is not RiskTier.PROTECTED
    )


def _configured_delete_eligible(item: TriageItem) -> bool:
    """Configured DELETE may promote only an item the executor already accepts."""

    return (
        is_direct_cleanup_eligible(item)
        or is_user_review_eligible(item)
        or is_ai_review_eligible(item)
    )


def _rows_of(''',
        flags=re.S,
    )

    text = replace_once(
        text,
        '''    if is_ai_review_eligible(item):
        return _UNSURE_BUCKET
''',
        '''    if is_user_review_eligible(item) or is_ai_review_eligible(item):
        return _UNSURE_BUCKET
''',
    )
    text = replace_once(
        text,
        '''            title="不确定，交 AI 判断",
            hint=(
                "工具认不出这些是什么。导回结果后可删的会移到左边；"
                "AI 判断可能不准确，使用外部或付费模型可能产生费用。"
                "导出文件包含本机完整路径，请自行选择可信的模型；"
                "同目录生成型文件名会合并提问；AI 仍不确定的由你决定。"
            ),
''',
        '''            title="需要判断：先由你决定，必要时再用 AI",
            hint=(
                "能解释但不能对所有用户保证可删的项目，直接由你决定，不花 AI 费用；"
                "只有本工具真正认不出的项目才会导出给 AI。"
                "AI 判断可能不准确且可能产生费用，导出文件包含本机完整路径；"
                "AI 仍不确定的也回到你这里最终决定。"
            ),
''',
    )

    text = sub_once(
        text,
        r"    def _selected_ai_unsure_items\(self\) -> tuple\[TriageItem, \.\.\.\]:\n.*?\n\n\n    def _delete\(",
        '''    def _selected_ai_unsure_items(self) -> tuple[TriageItem, ...]:
        """Return selected review rows that the user may decide without AI."""

        if not hasattr(self, "_unsure_tree"):
            return ()
        selected = set(self._unsure_tree.selection())
        return tuple(
            item
            for item in self._unsure
            if item.path in selected
            and (
                is_user_review_eligible(item)
                or normalise_path(item.path) in self._ai_unsure_reasons
            )
        )


    def _delete(''',
        flags=re.S,
    )

    export_pattern = r"    def _export_for_ai\(self\) -> None:\n.*?\n    def _import_from_ai\(self\) -> None:"
    match = re.search(export_pattern, text, flags=re.S)
    if match is None:
        raise RuntimeError("AI export method not found")
    block = match.group(0)
    block = replace_once(
        block,
        '''    def _export_for_ai(self) -> None:
        if not self._unsure:
            messagebox.showinfo("DevClean", "没有需要 AI 判断的项。")
            return
        groups = _group_ai_candidates(self._unsure, self._rules)
''',
        '''    def _export_for_ai(self) -> None:
        ai_items = tuple(
            item for item in self._unsure if is_ai_review_eligible(item)
        )
        if not ai_items:
            messagebox.showinfo(
                "DevClean",
                "当前没有真正需要 AI 判断的项；右侧其余项目可以直接由你决定。",
            )
            return
        groups = _group_ai_candidates(ai_items, self._rules)
''',
    )
    block = block.replace("len(self._unsure)", "len(ai_items)")
    text = text[: match.start()] + block + text[match.end() :]

    text = sub_once(
        text,
        r"    def _decide_ai_unsure\(self\) -> None:\n.*?\n    def _forget_verdicts\(self\) -> None:",
        '''    def _decide_ai_unsure(self) -> None:
        """Persist a local user decision without spending AI when it is unnecessary."""

        items = self._selected_ai_unsure_items()
        if not items:
            messagebox.showinfo(
                "DevClean",
                "请先选中右侧可由你判断的项目；真正未知的项目需要先交 AI。",
            )
            return
        preview: list[str] = []
        for item in items[:12]:
            key = normalise_path(item.path)
            ai_reason = self._ai_unsure_reasons.get(key)
            explanation = (
                f"AI 说明：{ai_reason}" if ai_reason else f"DevClean 说明：{item.reason}"
            )
            preview.append(f"{item.path}\\n{explanation}")
        if len(items) > 12:
            preview.append(f"……另有 {len(items) - 12:,} 项")
        answer = messagebox.askyesnocancel(
            "由你决定",
            "\\n\\n".join(preview)
            + "\\n\\n选择“是”：记为可以删除并移到左侧；"
            "选择“否”：记为确定保留；选择“取消”：不作修改。",
        )
        if answer is None:
            return
        decision = RuleDecision.DELETE if answer else RuleDecision.KEEP
        verdicts = []
        for item in items:
            key = normalise_path(item.path)
            source_reason = self._ai_unsure_reasons.get(key, item.reason)
            verdicts.append(
                (
                    item.path,
                    decision,
                    "用户在 DevClean 界面中最终决定"
                    + ("可删除" if answer else "保留")
                    + f"；依据：{source_reason}",
                )
            )
        rules_saved = True
        try:
            self._rules = add_user_verdicts(load_rules(), verdicts)
        except (OSError, RuleConfigError, UnicodeError) as error:
            rules_saved = False
            messagebox.showwarning(
                "用户决定未能保存",
                f"本次决定仍会应用，但规则文件没有更新：{error}",
            )
        selected_paths = {item.path for item in items}
        for item in items:
            self._ai_unsure_reasons.pop(normalise_path(item.path), None)
        if rules_saved and self._session is not None:
            self._publish(self._session)
        else:
            self._unsure = [
                item for item in self._unsure if item.path not in selected_paths
            ]
            if decision is RuleDecision.DELETE:
                self._deletable = sorted(
                    self._deletable + list(items),
                    key=self._size_of,
                    reverse=True,
                )
                self._checked.update(selected_paths)
            self._fill(self._deletable_tree, self._deletable)
            self._fill(self._unsure_tree, self._unsure)
            self._refresh_totals()
            self._sync_buttons()
        choice = "可以删除，已移到左侧并勾选" if answer else "确定保留"
        persistence = (
            "已写入规则，下次扫描会直接复用你的决定。"
            if rules_saved
            else "仅本次生效；规则修复前无法长期记住。"
        )
        self._status.set(f"你已决定 {len(items):,} 项{choice}。{persistence}")

    def _forget_verdicts(self) -> None:''',
        flags=re.S,
    )

    text = replace_once(
        text,
        '''                        else "扫描完成。左边是可以删除的，右边需要 AI 判断。"
''',
        '''                        else "扫描完成。左边是工具确定可删的；右边优先由你判断，真正未知的再交 AI。"
''',
    )
    text = replace_once(
        text,
        '''    "is_ai_review_eligible",
    "is_direct_cleanup_eligible",
''',
        '''    "is_ai_review_eligible",
    "is_direct_cleanup_eligible",
    "is_user_review_eligible",
''',
    )

    path.write_text(text, encoding="utf-8")


def patch_tests() -> None:
    path = Path("tests/test_scan_and_triage.py")
    text = path.read_text(encoding="utf-8")
    text = replace_once(
        text,
        '''        lane=ReviewLane.AI_REVIEW,
        risk_tier=RiskTier.HIGH,
        evidence_kind=EvidenceKind.KNOWN_ROOT_HEURISTIC,
        actionability=Actionability.AI_REVIEW,
''',
        '''        lane=ReviewLane.DETERMINISTIC_CANDIDATE,
        risk_tier=RiskTier.LOW,
        evidence_kind=EvidenceKind.KNOWN_ROOT_HEURISTIC,
        actionability=Actionability.REVIEW_PLAN,
''',
    )
    text += '''


def test_confidence_lanes_do_not_spend_ai_on_user_review() -> None:
    direct = _item(r"G:\\work\\safe-cache\\payload.bin", size=10)
    user_review = replace(
        direct,
        path=r"G:\\work\\maybe-cache\\payload.bin",
        record=replace(direct.record, path=r"G:\\work\\maybe-cache\\payload.bin"),
        lane=ReviewLane.USER_REVIEW,
        risk_tier=RiskTier.MEDIUM,
        actionability=Actionability.USER_REVIEW,
        tags=("user_review",),
    )
    ai_review = replace(
        direct,
        path=r"G:\\work\\unknown\\payload.bin",
        record=replace(direct.record, path=r"G:\\work\\unknown\\payload.bin"),
        lane=ReviewLane.AI_REVIEW,
        risk_tier=RiskTier.HIGH,
        actionability=Actionability.AI_REVIEW,
        tags=("ai_review_required",),
    )

    assert app.is_direct_cleanup_eligible(direct)
    assert not app.is_ai_review_eligible(direct)
    assert app.is_user_review_eligible(user_review)
    assert not app.is_direct_cleanup_eligible(user_review)
    assert not app.is_ai_review_eligible(user_review)
    assert app.is_ai_review_eligible(ai_review)
    assert not app.is_user_review_eligible(ai_review)


def test_partition_keeps_user_review_out_of_direct_cleanup() -> None:
    rules = default_rules()
    direct = _item(r"G:\\work\\safe-cache\\payload.bin", size=10)
    user_review = replace(
        _item(r"G:\\work\\maybe-cache\\payload.bin", size=20),
        lane=ReviewLane.USER_REVIEW,
        risk_tier=RiskTier.MEDIUM,
        actionability=Actionability.USER_REVIEW,
        tags=("user_review",),
    )
    ai_review = replace(
        _item(r"G:\\work\\unknown\\payload.bin", size=30),
        lane=ReviewLane.AI_REVIEW,
        risk_tier=RiskTier.HIGH,
        actionability=Actionability.AI_REVIEW,
        tags=("ai_review_required",),
    )
    session = TriageSession(
        review_sample_per_category=rules.scan.review_sample_per_category
    )
    for item in (direct, user_review, ai_review):
        session.observe_path(item.path, rules)
        session.add(item)

    deletable, needs_review = app._partition_items(session, rules)

    assert deletable == (direct,)
    assert set(needs_review) == {user_review, ai_review}
'''
    path.write_text(text, encoding="utf-8")


def main() -> None:
    patch_triage()
    patch_app()
    patch_tests()


if __name__ == "__main__":
    main()
