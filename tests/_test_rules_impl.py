from __future__ import annotations

import json
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from devclean.core.user_rules import (
    DEFAULT_BACKUP_NAME,
    DELETE_RULES_NAME,
    KEEP_RULES_NAME,
    MAX_DECISION_RULES,
    SCAN_RULES_NAME,
    DecisionRule,
    RuleConfigError,
    RuleDecision,
    RuleMatch,
    UserRules,
    add_ai_verdicts,
    default_backup_path,
    default_rules,
    load_rules,
    parse_rule_documents,
    render_rule_documents,
    restore_default_rules,
)


def test_packaged_rule_documents_are_current_and_round_trip() -> None:
    rules = default_rules()

    assert rules.scan.delete_root_ids == set()
    assert len(rules.scan.known_cleanup_roots) >= 30
    assert MAX_DECISION_RULES == 100_000
    assert parse_rule_documents(*render_rule_documents(rules)) == rules


def test_old_rule_schema_is_rejected() -> None:
    scan, delete, keep = render_rule_documents(default_rules())
    payload = json.loads(scan)
    payload["schema_version"] = 2

    with pytest.raises(RuleConfigError, match="schema_version=3"):
        parse_rule_documents(json.dumps(payload), delete, keep)


def test_regex_rules_work_and_keep_wins() -> None:
    base = default_rules()
    delete_regex = DecisionRule(
        rule_id="delete_tmp_in_cache",
        group="manual",
        match=RuleMatch.PATH_REGEX,
        value=r"/cache/.+\.tmp$",
    )
    keep_name = DecisionRule(
        rule_id="keep_important",
        group="manual",
        match=RuleMatch.FILENAME_REGEX,
        value=r"^important\.tmp$",
    )
    rules = UserRules(
        scan=base.scan,
        delete=replace(base.delete, rules=(delete_regex,)),
        keep=replace(base.keep, rules=(keep_name,)),
    )

    assert rules.decision_for(Path("G:/work/cache/disposable.tmp")) is RuleDecision.DELETE
    assert rules.decision_for(Path("G:/work/cache/important.tmp")) is RuleDecision.KEEP


def test_backup_is_written_when_custom_rules_first_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEVCLEAN_DATA_DIR", str(tmp_path))
    load_rules()
    assert default_backup_path() == tmp_path / DEFAULT_BACKUP_NAME
    assert default_backup_path().is_file()


def test_restore_defaults_resets_custom_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEVCLEAN_DATA_DIR", str(tmp_path))
    rules = load_rules()
    custom = UserRules(
        scan=rules.scan,
        delete=replace(
            rules.delete,
            rules=(
                DecisionRule(
                    rule_id="custom_delete",
                    group="manual",
                    match=RuleMatch.EXACT_PATH,
                    value=r"G:\custom\target.tmp",
                ),
            ),
        ),
        keep=rules.keep,
    )
    scan_json, delete_json, keep_json = render_rule_documents(custom)
    rule_dir = tmp_path / "rules"
    rule_dir.mkdir(parents=True, exist_ok=True)
    (rule_dir / SCAN_RULES_NAME).write_text(scan_json, encoding="utf-8")
    (rule_dir / DELETE_RULES_NAME).write_text(delete_json, encoding="utf-8")
    (rule_dir / KEEP_RULES_NAME).write_text(keep_json, encoding="utf-8")

    restore_default_rules()
    restored = load_rules()
    assert restored.delete.rules == ()


def test_ai_rules_are_persisted_and_reloadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEVCLEAN_DATA_DIR", str(tmp_path))
    target = r"C:\Users\person\AppData\Local\Tool\cache\old.bin"
    base = load_rules()
    updated = add_ai_verdicts(
        base,
        [(target, RuleDecision.DELETE, "AI reviewed disposable cache")],
    )

    assert updated.decision_for(target) is RuleDecision.DELETE
    reloaded = load_rules()
    assert reloaded.decision_for(target) is RuleDecision.DELETE


def test_ai_keeps_are_persisted_and_reloadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEVCLEAN_DATA_DIR", str(tmp_path))
    target = r"C:\Users\person\AppData\Local\Tool\state\important.db"
    base = load_rules()
    updated = add_ai_verdicts(
        base,
        [(target, RuleDecision.KEEP, "AI reviewed persistent state")],
    )

    assert updated.decision_for(target) is RuleDecision.KEEP
    reloaded = load_rules()
    assert reloaded.decision_for(target) is RuleDecision.KEEP


def test_restore_defaults_clears_ai_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEVCLEAN_DATA_DIR", str(tmp_path))
    target = r"C:\Users\person\AppData\Local\Tool\cache\old.bin"
    updated = add_ai_verdicts(
        load_rules(),
        [(target, RuleDecision.DELETE, "AI reviewed disposable cache")],
    )
    assert updated.ai_rule_count == 1

    restore_default_rules()
    restored = load_rules()
    assert restored.ai_rule_count == 0


def test_rule_bundle_can_be_zipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEVCLEAN_DATA_DIR", str(tmp_path))
    load_rules()
    bundle = tmp_path / "rules.zip"
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.write(tmp_path / "rules" / SCAN_RULES_NAME, SCAN_RULES_NAME)
        archive.write(tmp_path / "rules" / DELETE_RULES_NAME, DELETE_RULES_NAME)
        archive.write(tmp_path / "rules" / KEEP_RULES_NAME, KEEP_RULES_NAME)

    assert bundle.is_file()
