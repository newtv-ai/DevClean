from __future__ import annotations

from pathlib import Path


def replace_once(text: str, old: str, new: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"expected one exact match, got {count}: {old[:100]!r}")
    return text.replace(old, new, 1)


triage_path = Path("src/devclean/core/triage.py")
triage = triage_path.read_text(encoding="utf-8")
triage = replace_once(
    triage,
    '''    elif scope is DirectoryScope.STALE_VERSION:
        category = CleanupCategory.OTHER
        recovery = RecoveryCapability.VENDOR_REDOWNLOAD_BEST_EFFORT
        lane = ReviewLane.AI_REVIEW
        risk_tier = RiskTier.HIGH
        evidence_kind = EvidenceKind.PATH_HEURISTIC
        actionability = Actionability.AI_REVIEW
        execution_policy = ExecutionPolicy.USER_CHOICE_DELETE
        reason = f"{path.name}：看起来像被更新取代的旧版本目录，但缺少厂商级证据，交 AI 判断"
        tags = ("whole_directory", "stale_version", "ai_review_required")
''',
    '''    elif scope is DirectoryScope.STALE_VERSION:
        category = CleanupCategory.OTHER
        recovery = RecoveryCapability.VENDOR_REDOWNLOAD_BEST_EFFORT
        lane = ReviewLane.USER_REVIEW
        risk_tier = RiskTier.MEDIUM
        evidence_kind = EvidenceKind.PATH_HEURISTIC
        actionability = Actionability.USER_REVIEW
        execution_policy = ExecutionPolicy.USER_CHOICE_DELETE
        reason = f"{path.name}：看起来像被更新取代的旧版本目录，但缺少厂商级证据，由你决定"
        tags = ("whole_directory", "stale_version", "user_review")
''',
)
triage_path.write_text(triage, encoding="utf-8")

app_path = Path("src/devclean/ui/app.py")
app = app_path.read_text(encoding="utf-8")
app = replace_once(
    app,
    '''            and (
                is_user_review_eligible(item)
                or normalise_path(item.path) in self._ai_unsure_reasons
            )
''',
    '''            and (
                is_user_review_eligible(item)
                or is_ai_review_eligible(item)
                or normalise_path(item.path) in self._ai_unsure_reasons
            )
''',
)
app = replace_once(
    app,
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
    '''    def _export_for_ai(self) -> None:
        selected_paths = (
            set(self._unsure_tree.selection())
            if hasattr(self, "_unsure_tree")
            else set()
        )
        if selected_paths:
            ai_items = tuple(
                item
                for item in self._unsure
                if item.path in selected_paths
                and item.target_kind is CleanupTargetKind.FILE
                and (
                    is_user_review_eligible(item)
                    or is_ai_review_eligible(item)
                )
            )
            if not ai_items:
                messagebox.showinfo(
                    "DevClean",
                    "选中的项目没有可发送给 AI 的文件；整个目录目前请由你直接决定。",
                )
                return
        else:
            ai_items = tuple(
                item for item in self._unsure if is_ai_review_eligible(item)
            )
            if not ai_items:
                messagebox.showinfo(
                    "DevClean",
                    "没有必须交 AI 的未知文件。若你对某个“你来决定”的文件也拿不准，"
                    "先选中它，再点“导出给 AI”。",
                )
                return
        groups = _group_ai_candidates(ai_items, self._rules)
''',
)
app = replace_once(
    app,
    '''                "只有本工具真正认不出的项目才会导出给 AI。"
''',
    '''                "本工具真正认不出的项目会默认交给 AI；你拿不准的文件也可以选中后主动交 AI。"
''',
)
app_path.write_text(app, encoding="utf-8")
