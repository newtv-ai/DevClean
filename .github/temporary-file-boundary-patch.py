from __future__ import annotations

from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    if old not in text:
        raise RuntimeError(f"expected patch anchor missing in {path}: {old[:80]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8", newline="\n")


# Learned/default knowledge is intentionally reusable for files.  Directory
# decisions use an explicit source and exact-path-only matcher so a file rule
# can never grow into whole-tree authority.
replace_once(
    "src/devclean/core/_user_rules_impl.py",
    'MAX_RULE_VALUE_CHARS: Final = 32_767\n',
    'MAX_RULE_VALUE_CHARS: Final = 32_767\n'
    '_DIRECTORY_DECISION_SOURCE: Final = "USER_DIRECTORY_DECISION"\n',
)

replace_once(
    "src/devclean/core/_user_rules_impl.py",
    '''    _delete_matcher: _DecisionMatcher = field(init=False, repr=False, compare=False)\n    _keep_matcher: _DecisionMatcher = field(init=False, repr=False, compare=False)\n    _ai_rule_count: int = field(init=False, repr=False, compare=False)\n\n    def __post_init__(self) -> None:\n        object.__setattr__(self, "_delete_matcher", _DecisionMatcher.compile(self.delete.rules))\n        object.__setattr__(self, "_keep_matcher", _DecisionMatcher.compile(self.keep.rules))\n        object.__setattr__(\n''',
    '''    _delete_matcher: _DecisionMatcher = field(init=False, repr=False, compare=False)\n    _keep_matcher: _DecisionMatcher = field(init=False, repr=False, compare=False)\n    _delete_directory_matcher: _DecisionMatcher = field(init=False, repr=False, compare=False)\n    _keep_directory_matcher: _DecisionMatcher = field(init=False, repr=False, compare=False)\n    _ai_rule_count: int = field(init=False, repr=False, compare=False)\n\n    def __post_init__(self) -> None:\n        file_delete = tuple(\n            rule for rule in self.delete.rules if rule.source != _DIRECTORY_DECISION_SOURCE\n        )\n        file_keep = tuple(\n            rule for rule in self.keep.rules if rule.source != _DIRECTORY_DECISION_SOURCE\n        )\n        directory_delete = tuple(\n            rule\n            for rule in self.delete.rules\n            if rule.source == _DIRECTORY_DECISION_SOURCE and rule.match is RuleMatch.EXACT_PATH\n        )\n        directory_keep = tuple(\n            rule\n            for rule in self.keep.rules\n            if rule.source == _DIRECTORY_DECISION_SOURCE and rule.match is RuleMatch.EXACT_PATH\n        )\n        object.__setattr__(self, "_delete_matcher", _DecisionMatcher.compile(file_delete))\n        object.__setattr__(self, "_keep_matcher", _DecisionMatcher.compile(file_keep))\n        object.__setattr__(\n            self, "_delete_directory_matcher", _DecisionMatcher.compile(directory_delete)\n        )\n        object.__setattr__(\n            self, "_keep_directory_matcher", _DecisionMatcher.compile(directory_keep)\n        )\n        object.__setattr__(\n''',
)

replace_once(
    "src/devclean/core/_user_rules_impl.py",
    '''    def decision_for(self, path: str | Path) -> RuleDecision | None:\n        """Return the configured decision, with KEEP taking priority."""\n\n        if self._keep_matcher.matches(path):\n            return RuleDecision.KEEP\n        if self._delete_matcher.matches(path):\n            return RuleDecision.DELETE\n        return None\n\n    @property\n''',
    '''    def decision_for(self, path: str | Path) -> RuleDecision | None:\n        """Return the configured file decision, with KEEP taking priority."""\n\n        if self._keep_matcher.matches(path):\n            return RuleDecision.KEEP\n        if self._delete_matcher.matches(path):\n            return RuleDecision.DELETE\n        return None\n\n    def directory_decision_for(self, path: str | Path) -> RuleDecision | None:\n        """Return only an explicit exact-path directory decision from the UI."""\n\n        if self._keep_directory_matcher.matches(path):\n            return RuleDecision.KEEP\n        if self._delete_directory_matcher.matches(path):\n            return RuleDecision.DELETE\n        return None\n\n    @property\n''',
)

replace_once(
    "src/devclean/core/_user_rules_impl.py",
    '''def add_user_verdicts(\n    rules: UserRules,\n    verdicts: list[tuple[str, RuleDecision, str]],\n) -> UserRules:\n    """Persist the user's final decision with the same reusable rule shapes."""\n\n    return _add_verdict_rules(\n        rules,\n        verdicts,\n        source="USER_DECISION",\n        group="user_decision",\n    )\n\n\ndef _add_verdict_rules(\n''',
    '''def add_user_verdicts(\n    rules: UserRules,\n    verdicts: list[tuple[str, RuleDecision, str]],\n) -> UserRules:\n    """Persist the user's final file decision with reusable file-rule shapes."""\n\n    return _add_verdict_rules(\n        rules,\n        verdicts,\n        source="USER_DECISION",\n        group="user_decision",\n    )\n\n\ndef add_user_directory_verdicts(\n    rules: UserRules,\n    verdicts: list[tuple[str, RuleDecision, str]],\n) -> UserRules:\n    """Persist explicit directory choices as exact-path-only rules."""\n\n    incoming: dict[str, tuple[str, RuleDecision, str]] = {}\n    for path, decision, reason in verdicts:\n        normalized = normalise_path(path)\n        incoming[normalized.casefold()] = (normalized, decision, reason)\n    if not incoming:\n        return rules\n\n    removal = {\n        _stored_rule_key(RuleMatch.EXACT_PATH, path)\n        for path, _decision, _reason in incoming.values()\n    }\n    delete = [\n        rule\n        for rule in rules.delete.rules\n        if not (\n            rule.source == _DIRECTORY_DECISION_SOURCE\n            and _stored_rule_key(rule.match, rule.value) in removal\n        )\n    ]\n    keep = [\n        rule\n        for rule in rules.keep.rules\n        if not (\n            rule.source == _DIRECTORY_DECISION_SOURCE\n            and _stored_rule_key(rule.match, rule.value) in removal\n        )\n    ]\n    used_ids = {rule.rule_id for rule in (*delete, *keep)}\n    now = datetime.now(UTC).isoformat()\n    for path, decision, reason in incoming.values():\n        rule_id = _unique_rule_id(\n            _decision_rule_id(_DIRECTORY_DECISION_SOURCE, f"exact_path:{path}"),\n            used_ids,\n        )\n        used_ids.add(rule_id)\n        entry = DecisionRule(\n            rule_id=rule_id,\n            group="user_directory_decision",\n            match=RuleMatch.EXACT_PATH,\n            value=path,\n            source=_DIRECTORY_DECISION_SOURCE,\n            reason=reason[:MAX_REASON_CHARS],\n            updated_at=now,\n        )\n        (delete if decision is RuleDecision.DELETE else keep).append(entry)\n    updated = UserRules(\n        scan=rules.scan,\n        delete=replace(rules.delete, rules=_bounded_rules(delete)),\n        keep=replace(rules.keep, rules=_bounded_rules(keep)),\n    )\n    save_rules(updated)\n    return updated\n\n\ndef _add_verdict_rules(\n''',
)

# Application ownership guards also apply to an explicit directory choice.
replace_once(
    "src/devclean/core/user_rules.py",
    '''_ORIGINAL_ADD_AI_VERDICTS = _impl.add_ai_verdicts\n_ORIGINAL_ADD_USER_VERDICTS = _impl.add_user_verdicts\n_ORIGINAL_LOAD_RULES = _impl.load_rules\n''',
    '''_ORIGINAL_ADD_AI_VERDICTS = _impl.add_ai_verdicts\n_ORIGINAL_ADD_USER_VERDICTS = _impl.add_user_verdicts\n_ORIGINAL_ADD_USER_DIRECTORY_VERDICTS = _impl.add_user_directory_verdicts\n_ORIGINAL_LOAD_RULES = _impl.load_rules\n''',
)

replace_once(
    "src/devclean/core/user_rules.py",
    '''def load_rules(*, create_missing: bool = True) -> UserRules:\n''',
    '''def add_user_directory_verdicts(\n    rules: UserRules,\n    verdicts: list[tuple[str, RuleDecision, str]],\n) -> UserRules:\n    """Persist an exact directory choice without granting generic tree authority."""\n\n    allowed = [\n        verdict\n        for verdict in verdicts\n        if not (\n            verdict[1] is RuleDecision.DELETE\n            and _owner_for_path(verdict[0]) in {DecisionOwner.USER, DecisionOwner.KEEP}\n        )\n    ]\n    updated = (\n        _ORIGINAL_ADD_USER_DIRECTORY_VERDICTS(rules, allowed) if allowed else rules\n    )\n    return _persist_if_sanitized(updated)\n\n\ndef load_rules(*, create_missing: bool = True) -> UserRules:\n''',
)

replace_once(
    "src/devclean/core/user_rules.py",
    '''_impl.add_ai_verdicts = add_ai_verdicts\n_impl.add_user_verdicts = add_user_verdicts\n_impl.load_rules = load_rules\n''',
    '''_impl.add_ai_verdicts = add_ai_verdicts\n_impl.add_user_verdicts = add_user_verdicts\n_impl.add_user_directory_verdicts = add_user_directory_verdicts\n_impl.load_rules = load_rules\n''',
)

replace_once(
    "src/devclean/core/user_rules.py",
    '''    "add_user_verdicts",\n    "clear_ai_rules",\n''',
    '''    "add_user_verdicts",\n    "add_user_directory_verdicts",\n    "clear_ai_rules",\n''',
)

# Keep-path tracking and rule lookup are target-kind aware.  File knowledge can
# protect a containing directory, but it can never directly decide the directory.
replace_once(
    "src/devclean/core/triage.py",
    '''    def observe_path(self, path: str, rules: UserRules) -> None:\n        """Retain only observed paths protected by a configured KEEP rule."""\n\n        if self._observation_rules is None:\n            self._observation_rules = rules\n        elif self._observation_rules is not rules:\n            raise ValueError("one scan session must use one pinned rule set")\n        if rules.decision_for(path) is RuleDecision.KEEP:\n            self._observed_keep_paths.add(normalise_path(path))\n            self._keep_cache_rules = None\n            self._keep_cache = ()\n''',
    '''    def observe_path(\n        self,\n        path: str,\n        rules: UserRules,\n        *,\n        target_kind: CleanupTargetKind = CleanupTargetKind.FILE,\n    ) -> None:\n        """Retain observed FILE/DIRECTORY paths protected by their own rule kind."""\n\n        if self._observation_rules is None:\n            self._observation_rules = rules\n        elif self._observation_rules is not rules:\n            raise ValueError("one scan session must use one pinned rule set")\n        decision = (\n            rules.directory_decision_for(path)\n            if target_kind is CleanupTargetKind.DIRECTORY\n            else rules.decision_for(path)\n        )\n        if decision is RuleDecision.KEEP:\n            self._observed_keep_paths.add(normalise_path(path))\n            self._keep_cache_rules = None\n            self._keep_cache = ()\n''',
)

replace_once(
    "src/devclean/core/triage.py",
    '''        kept_paths.update(\n            normalise_path(item.path)\n            for item in self.iter_items()\n            if rules.decision_for(item.path) is RuleDecision.KEEP\n        )\n''',
    '''        kept_paths.update(\n            normalise_path(item.path)\n            for item in self.iter_items()\n            if (\n                rules.directory_decision_for(item.path)\n                if item.target_kind is CleanupTargetKind.DIRECTORY\n                else rules.decision_for(item.path)\n            )\n            is RuleDecision.KEEP\n        )\n''',
)

# GUI applies learned/default rules to files only.  Directory choices are an
# explicit, exact-path UI decision and are never generalized.
replace_once(
    "src/devclean/ui/app.py",
    '''    add_ai_verdicts,\n    add_user_verdicts,\n    clear_ai_rules,\n''',
    '''    add_ai_verdicts,\n    add_user_directory_verdicts,\n    add_user_verdicts,\n    clear_ai_rules,\n''',
)

replace_once(
    "src/devclean/ui/app.py",
    '''    decision = rules.decision_for(item.path)\n    if decision is RuleDecision.KEEP:\n''',
    '''    decision = (\n        rules.directory_decision_for(item.path)\n        if item.target_kind is CleanupTargetKind.DIRECTORY\n        else rules.decision_for(item.path)\n    )\n    if decision is RuleDecision.KEEP:\n''',
)

replace_once(
    "src/devclean/ui/app.py",
    '''                if record.kind in {\n                    ScanRecordKind.FILE,\n                    ScanRecordKind.DIRECTORY,\n                }:\n                    session.observe_path(record.path, active_rules)\n                if record.kind is ScanRecordKind.FILE:\n''',
    '''                if record.kind is ScanRecordKind.FILE:\n                    session.observe_path(\n                        record.path,\n                        active_rules,\n                        target_kind=CleanupTargetKind.FILE,\n                    )\n''',
)

replace_once(
    "src/devclean/ui/app.py",
    '''                elif record.kind is ScanRecordKind.DIRECTORY:\n                    item = triage_directory(\n''',
    '''                elif record.kind is ScanRecordKind.DIRECTORY:\n                    session.observe_path(\n                        record.path,\n                        active_rules,\n                        target_kind=CleanupTargetKind.DIRECTORY,\n                    )\n                    item = triage_directory(\n''',
)

replace_once(
    "src/devclean/ui/app.py",
    '''        verdicts = []\n        for item in items:\n            key = normalise_path(item.path)\n            source_reason = self._ai_unsure_reasons.get(key, item.reason)\n            verdicts.append(\n                (\n                    item.path,\n                    decision,\n                    "用户在 DevClean 界面中最终决定"\n                    + ("可删除" if answer else "保留")\n                    + f"；依据：{source_reason}",\n                )\n            )\n        rules_saved = True\n        try:\n            self._rules = add_user_verdicts(load_rules(), verdicts)\n''',
    '''        file_verdicts: list[tuple[str, RuleDecision, str]] = []\n        directory_verdicts: list[tuple[str, RuleDecision, str]] = []\n        for item in items:\n            key = normalise_path(item.path)\n            source_reason = self._ai_unsure_reasons.get(key, item.reason)\n            verdict = (\n                item.path,\n                decision,\n                "用户在 DevClean 界面中最终决定"\n                + ("可删除" if answer else "保留")\n                + f"；依据：{source_reason}",\n            )\n            if item.target_kind is CleanupTargetKind.DIRECTORY:\n                directory_verdicts.append(verdict)\n            else:\n                file_verdicts.append(verdict)\n        rules_saved = True\n        try:\n            latest_rules = load_rules()\n            if file_verdicts:\n                latest_rules = add_user_verdicts(latest_rules, file_verdicts)\n            if directory_verdicts:\n                latest_rules = add_user_directory_verdicts(\n                    latest_rules, directory_verdicts\n                )\n            self._rules = latest_rules\n''',
)

# A focused regression suite locks the cross-machine file knowledge contract and
# the separate exact directory decision lane.
test_path = Path("tests/test_learned_rule_target_boundary.py")
test_path.write_text(
    '''from __future__ import annotations\n\nfrom pathlib import Path\n\nimport pytest\n\nfrom devclean.core.cleanup_catalog import CleanupCategory, SourceDomain\nfrom devclean.core.triage import (\n    Actionability,\n    CleanupTargetKind,\n    DirectoryScope,\n    EvidenceKind,\n    ExecutionPolicy,\n    RecoveryCapability,\n    ReviewLane,\n    RiskTier,\n    TriageItem,\n    TriageSession,\n)\nfrom devclean.core.user_rules import (\n    RuleDecision,\n    add_ai_verdicts,\n    add_user_directory_verdicts,\n    default_rules,\n)\nfrom devclean.scanner.filesystem import ScanRecord, ScanRecordKind\nfrom devclean.ui import app\n\n\ndef _item(path: str, *, directory: bool) -> TriageItem:\n    kind = ScanRecordKind.DIRECTORY if directory else ScanRecordKind.FILE\n    record = ScanRecord(\n        root=str(Path(path).parent),\n        path=path,\n        kind=kind,\n        depth=1,\n        logical_size=10,\n        allocated_size=10,\n        raw_allocated_size=10,\n        volume_serial=7,\n        file_id="1" * 32,\n        file_id_kind="file_id_128",\n        link_count=1,\n        attributes=0,\n        creation_time_ns=100,\n        last_write_time_ns=200,\n    )\n    return TriageItem(\n        record=record,\n        path=path,\n        logical_size=10,\n        allocated_size=10,\n        category=CleanupCategory.OTHER,\n        source_domain=SourceDomain.GENERAL_STORAGE,\n        lane=ReviewLane.USER_REVIEW if directory else ReviewLane.AI_REVIEW,\n        risk_tier=RiskTier.MEDIUM if directory else RiskTier.HIGH,\n        evidence_kind=EvidenceKind.FILESYSTEM_OBSERVATION,\n        actionability=Actionability.USER_REVIEW if directory else Actionability.AI_REVIEW,\n        execution_policy=ExecutionPolicy.USER_CHOICE_DELETE,\n        recovery=RecoveryCapability.UNKNOWN,\n        reason="explicit review candidate",\n        target_kind=(\n            CleanupTargetKind.DIRECTORY if directory else CleanupTargetKind.FILE\n        ),\n        directory_scope=DirectoryScope.REGENERABLE_TOOL_OUTPUT if directory else None,\n    )\n\n\ndef _partition(item: TriageItem, rules):\n    session = TriageSession(review_sample_per_category=10)\n    session.add(item)\n    return app._partition_items(session, rules)\n\n\ndef test_learned_file_delete_still_promotes_a_file(\n    tmp_path: Path, monkeypatch: pytest.MonkeyPatch\n) -> None:\n    monkeypatch.setenv("DEVCLEAN_DATA_DIR", str(tmp_path / "data"))\n    path = r"G:\\scratch\\opaque.bin"\n    rules = add_ai_verdicts(\n        default_rules(),\n        [(path, RuleDecision.DELETE, "confirmed common disposable file")],\n    )\n\n    deletable, unsure = _partition(_item(path, directory=False), rules)\n\n    assert [item.path for item in deletable] == [path]\n    assert unsure == ()\n\n\ndef test_learned_file_delete_cannot_promote_same_named_directory(\n    tmp_path: Path, monkeypatch: pytest.MonkeyPatch\n) -> None:\n    monkeypatch.setenv("DEVCLEAN_DATA_DIR", str(tmp_path / "data"))\n    path = r"G:\\scratch\\opaque.bin"\n    rules = add_ai_verdicts(\n        default_rules(),\n        [(path, RuleDecision.DELETE, "confirmed common disposable file")],\n    )\n\n    deletable, unsure = _partition(_item(path, directory=True), rules)\n\n    assert deletable == ()\n    assert [item.path for item in unsure] == [path]\n    assert rules.directory_decision_for(path) is None\n\n\ndef test_explicit_directory_delete_is_exact_and_actionable(\n    tmp_path: Path, monkeypatch: pytest.MonkeyPatch\n) -> None:\n    monkeypatch.setenv("DEVCLEAN_DATA_DIR", str(tmp_path / "data"))\n    path = r"G:\\scratch\\review-this-directory"\n    rules = add_user_directory_verdicts(\n        default_rules(),\n        [(path, RuleDecision.DELETE, "user explicitly chose this directory")],\n    )\n\n    deletable, unsure = _partition(_item(path, directory=True), rules)\n\n    assert [item.path for item in deletable] == [path]\n    assert unsure == ()\n    assert rules.directory_decision_for(path) is RuleDecision.DELETE\n    assert rules.decision_for(path) is None\n\n\ndef test_explicit_directory_keep_does_not_hide_same_path_as_a_file_rule(\n    tmp_path: Path, monkeypatch: pytest.MonkeyPatch\n) -> None:\n    monkeypatch.setenv("DEVCLEAN_DATA_DIR", str(tmp_path / "data"))\n    path = r"G:\\scratch\\review-this-directory"\n    rules = add_user_directory_verdicts(\n        default_rules(),\n        [(path, RuleDecision.KEEP, "user explicitly kept this directory")],\n    )\n\n    deletable, unsure = _partition(_item(path, directory=True), rules)\n\n    assert deletable == ()\n    assert unsure == ()\n    assert rules.directory_decision_for(path) is RuleDecision.KEEP\n    assert rules.decision_for(path) is None\n''',
    encoding="utf-8",
    newline="\n",
)

# Document the corrected contract without reverting the generic fail-closed work.
doc = Path("docs/generic-review-routing-reaudit.md")
text = doc.read_text(encoding="utf-8")
marker = "\n## "
section = (
    "\n## Learned/default knowledge boundary\n\n"
    "Cross-machine learned knowledge is supported for **files**. A confirmed common "
    "software file may therefore ship in DELETE/KEEP defaults or be learned from AI/user "
    "review. Those rules are evaluated only for file observations.\n\n"
    "Directories use a separate authority lane. A user may explicitly decide an already "
    "eligible review directory, but that choice is stored as an exact-path-only directory "
    "decision and is never generalized into a prefix/glob/tree rule. Generic or unknown "
    "directories remain protected; source-proven vendor directories keep their audited "
    "deterministic policy.\n"
)
if "## Learned/default knowledge boundary" not in text:
    position = text.find(marker)
    if position == -1:
        text += section
    else:
        text = text[:position] + section + text[position:]
    doc.write_text(text, encoding="utf-8", newline="\n")
