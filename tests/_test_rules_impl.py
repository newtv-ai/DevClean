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
    assert rules.delete.rules == ()
    assert rules.keep.rules == ()
    assert rules.ai_rule_count == 0
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
    assert rules.decision_for(Path("G:/work/src/code.py")) is None


def test_ai_verdict_is_persisted_and_default_backup_is_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEVCLEAN_DATA_DIR", str(tmp_path / "DevClean-data"))
    rules = load_rules()
    target = str(tmp_path / "cache" / "answer.bin")

    updated = add_ai_verdicts(
        rules,
        [(target, RuleDecision.DELETE, "AI confirmed this exact cache entry")],
    )

    assert updated.decision_for(target) is RuleDecision.DELETE
    assert load_rules().decision_for(target) is RuleDecision.DELETE
    assert updated.ai_rule_count == rules.ai_rule_count + 1
    backup = default_backup_path()
    assert backup.name == DEFAULT_BACKUP_NAME
    with zipfile.ZipFile(backup) as archive:
        assert archive.namelist() == [
            SCAN_RULES_NAME,
            DELETE_RULES_NAME,
            KEEP_RULES_NAME,
        ]


def test_existing_sidecar_uses_packaged_data_only_to_rebuild_missing_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEVCLEAN_DATA_DIR", str(tmp_path / "DevClean-data"))
    original = load_rules()
    from devclean.core import user_rules as module

    packaged_documents = module._packaged_documents

    def fail_packaged_read() -> tuple[str, str, str]:
        raise RuleConfigError("packaged data temporarily unavailable")

    monkeypatch.setattr(module, "_packaged_documents", fail_packaged_read)

    # With the visible backup present, ordinary loading never reopens the EXE
    # resource package.
    assert load_rules() == original
    default_backup_path().unlink()
    # Losing only the backup cannot block valid activity rules, even if the
    # packaged resource is temporarily unavailable.
    assert load_rules() == original
    assert not default_backup_path().exists()

    # A normal later launch can reconstruct the deleted default backup without
    # replacing or changing any activity rule.
    monkeypatch.setattr(module, "_packaged_documents", packaged_documents)
    assert load_rules() == original
    assert default_backup_path().is_file()

    monkeypatch.setattr(module, "_packaged_documents", fail_packaged_read)
    module.scan_rules_path().unlink()
    assert load_rules() == original
    changed = UserRules(
        scan=original.scan,
        delete=replace(
            original.delete,
            rules=(
                DecisionRule(
                    rule_id="temporary-change",
                    group="manual",
                    match=RuleMatch.EXACT_PATH,
                    value=r"C:\temporary-change.bin",
                ),
            ),
        ),
        keep=original.keep,
    )
    module.save_rules(changed)
    assert load_rules() == changed
    assert restore_default_rules() == original
    assert load_rules() == original


def test_legacy_packaged_decisions_are_removed_without_erasing_new_user_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEVCLEAN_DATA_DIR", str(tmp_path / "DevClean-data"))
    clean_defaults = default_rules()
    clean_documents = render_rule_documents(clean_defaults)
    legacy_delete = DecisionRule(
        rule_id="legacy-packaged-delete",
        group="ai_import",
        match=RuleMatch.EXACT_PATH,
        value=r"C:\Users\legacy\machine-specific-cache.bin",
        source="AI_IMPORT",
        reason="accidentally shipped machine decision",
        updated_at="2026-07-27T00:00:00+00:00",
    )
    user_delete = DecisionRule(
        rule_id="real-user-delete",
        group="user_decision",
        match=RuleMatch.EXACT_PATH,
        value=r"D:\user-selected\disposable.bin",
        source="USER_DECISION",
        reason="created after installation",
        updated_at="2026-08-20T00:00:00+00:00",
    )
    legacy = UserRules(
        scan=clean_defaults.scan,
        delete=replace(clean_defaults.delete, rules=(legacy_delete,)),
        keep=clean_defaults.keep,
    )
    legacy_documents = render_rule_documents(legacy)

    from devclean.core import user_rules as module

    module.rules_dir().mkdir(parents=True, exist_ok=True)
    for path, document in zip(
        (module.scan_rules_path(), module.delete_rules_path(), module.keep_rules_path()),
        render_rule_documents(
            UserRules(
                scan=legacy.scan,
                delete=replace(legacy.delete, rules=(legacy_delete, user_delete)),
                keep=legacy.keep,
            )
        ),
        strict=True,
    ):
        path.write_text(document, encoding="utf-8")
    with zipfile.ZipFile(module.default_backup_path(), "w") as archive:
        for name, document in zip(
            (SCAN_RULES_NAME, DELETE_RULES_NAME, KEEP_RULES_NAME),
            legacy_documents,
            strict=True,
        ):
            archive.writestr(name, document.encode("utf-8"))

    monkeypatch.setattr(module, "_packaged_documents", lambda: clean_documents)
    migrated = load_rules()

    assert legacy_delete not in migrated.delete.rules
    assert user_delete in migrated.delete.rules
    assert migrated.decision_for(user_delete.value) is RuleDecision.DELETE
    with zipfile.ZipFile(default_backup_path()) as archive:
        assert archive.read(DELETE_RULES_NAME).decode("utf-8") == clean_documents[1]


def test_restore_defaults_prefers_current_packaged_templates_over_stale_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEVCLEAN_DATA_DIR", str(tmp_path / "DevClean-data"))
    clean = default_rules()
    clean_documents = render_rule_documents(clean)
    stale_rule = DecisionRule(
        rule_id="stale-default-delete",
        group="ai_import",
        match=RuleMatch.EXACT_PATH,
        value=r"C:\stale-default.bin",
        source="AI_IMPORT",
    )
    stale = UserRules(
        scan=clean.scan,
        delete=replace(clean.delete, rules=(stale_rule,)),
        keep=clean.keep,
    )
    stale_documents = render_rule_documents(stale)

    from devclean.core import user_rules as module

    module.rules_dir().mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(module.default_backup_path(), "w") as archive:
        for name, document in zip(
            (SCAN_RULES_NAME, DELETE_RULES_NAME, KEEP_RULES_NAME),
            stale_documents,
            strict=True,
        ):
            archive.writestr(name, document.encode("utf-8"))
    monkeypatch.setattr(module, "_packaged_documents", lambda: clean_documents)

    restored = restore_default_rules()

    assert restored.delete.rules == ()
    assert restored.keep.rules == ()
    assert restored.ai_rule_count == 0
    with zipfile.ZipFile(default_backup_path()) as archive:
        assert archive.read(DELETE_RULES_NAME).decode("utf-8") == clean_documents[1]


def test_ai_rules_port_user_profile_and_reuse_dated_path_shapes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEVCLEAN_DATA_DIR", str(tmp_path / "DevClean-data"))
    monkeypatch.setenv("USERPROFILE", r"C:\Users\alice")
    monkeypatch.setenv("USERNAME", "alice")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\alice\AppData\Local")
    monkeypatch.setenv("APPDATA", r"C:\Users\alice\AppData\Roaming")
    monkeypatch.setenv("TEMP", r"C:\Users\alice\AppData\Local\Temp")
    target = (
        r"C:\Users\alice\.codex\sessions\2026\07\27"
        r"\rollout-2026-07-27T10-20-30-1234567890.jsonl"
    )
    tomorrow = (
        r"C:\Users\alice\.codex\sessions\2026\07\28"
        r"\rollout-2026-07-28T09-00-00-0987654321.jsonl"
    )

    updated = add_ai_verdicts(
        load_rules(),
        [(target, RuleDecision.DELETE, "same generated session type")],
    )

    assert updated.decision_for(tomorrow) is RuleDecision.DELETE
    assert any(rule.match is RuleMatch.PATH_GLOB for rule in updated.delete.rules)
    assert all("alice" not in rule.value.casefold() for rule in updated.delete.rules)

    monkeypatch.setenv("USERPROFILE", r"C:\Users\bob")
    monkeypatch.setenv("USERNAME", "bob")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\bob\AppData\Local")
    monkeypatch.setenv("APPDATA", r"C:\Users\bob\AppData\Roaming")
    monkeypatch.setenv("TEMP", r"C:\Users\bob\AppData\Local\Temp")
    other_user_rules = UserRules(
        scan=updated.scan,
        delete=updated.delete,
        keep=updated.keep,
    )
    other_user_tomorrow = tomorrow.replace(r"C:\Users\alice", r"C:\Users\bob")
    assert other_user_rules.decision_for(other_user_tomorrow) is RuleDecision.DELETE


def test_conventional_local_temp_rule_survives_redirected_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEVCLEAN_DATA_DIR", str(tmp_path / "DevClean-data"))
    monkeypatch.setenv("USERPROFILE", r"C:\Users\alice")
    monkeypatch.setenv("USERNAME", "alice")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\alice\AppData\Local")
    monkeypatch.setenv("APPDATA", r"C:\Users\alice\AppData\Roaming")
    monkeypatch.setenv("TEMP", r"D:\redirected-temp")
    target = r"C:\Users\alice\AppData\Local\Temp\cache\answer.bin"

    updated = add_ai_verdicts(
        load_rules(),
        [(target, RuleDecision.DELETE, "regenerable temporary cache")],
    )

    assert updated.decision_for(target) is RuleDecision.DELETE
    assert any(
        rule.match is RuleMatch.EXACT_PATH
        and rule.value.casefold() == r"%LOCALAPPDATA%\Temp\cache\answer.bin".casefold()
        for rule in updated.delete.rules
    )
    updated = add_ai_verdicts(
        updated,
        [
            (
                r"D:\redirected-temp\cache\redirected.bin",
                RuleDecision.DELETE,
                "regenerable redirected temporary cache",
            )
        ],
    )
    assert any(
        rule.match is RuleMatch.EXACT_PATH
        and rule.value.casefold() == r"%TEMP%\cache\redirected.bin".casefold()
        for rule in updated.delete.rules
    )

    monkeypatch.setenv("USERPROFILE", r"C:\Users\bob")
    monkeypatch.setenv("USERNAME", "bob")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\bob\AppData\Local")
    monkeypatch.setenv("APPDATA", r"C:\Users\bob\AppData\Roaming")
    monkeypatch.setenv("TEMP", r"E:\other-redirected-temp")
    other_user_rules = UserRules(
        scan=updated.scan,
        delete=updated.delete,
        keep=updated.keep,
    )
    assert (
        other_user_rules.decision_for(r"C:\Users\bob\AppData\Local\Temp\cache\answer.bin")
        is RuleDecision.DELETE
    )
    assert (
        other_user_rules.decision_for(r"E:\other-redirected-temp\cache\redirected.bin")
        is RuleDecision.DELETE
    )


def test_ai_rules_replace_encoded_foreign_username_and_dynamic_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEVCLEAN_DATA_DIR", str(tmp_path / "DevClean-data"))
    monkeypatch.setenv("USERPROFILE", r"C:\Users\bob")
    monkeypatch.setenv("USERNAME", "bob")
    source = (
        r"C:\Users\alice\.claude\projects"
        r"\c--users-alice--work"
        r"\e4b3139f-6dc7-4470-b002-6f3a0a91d822.jsonl"
    )
    matching_bob_path = (
        r"C:\Users\bob\.claude\projects"
        r"\c--users-bob--work"
        r"\bc15d6c4-b1ca-4b82-9f37-9d72aea9b27a.jsonl"
    )

    updated = add_ai_verdicts(
        load_rules(),
        [(source, RuleDecision.KEEP, "same generated session type")],
    )

    assert updated.decision_for(matching_bob_path) is RuleDecision.KEEP
    assert all(
        "alice" not in rule.value.casefold()
        for rule in (*updated.delete.rules, *updated.keep.rules)
    )
    assert any(
        "%USERNAME%" in rule.value and rule.match is RuleMatch.PATH_GLOB
        for rule in updated.keep.rules
    )


def test_dynamic_number_does_not_get_duplicated_as_a_fake_suffix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEVCLEAN_DATA_DIR", str(tmp_path / "DevClean-data"))
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\person\AppData\Local")
    source = (
        r"C:\Users\person\AppData\Local\Browser\Safe Browsing"
        r"\urlsoceng.store.4_13429459072546848"
    )
    next_generated = (
        r"C:\Users\person\AppData\Local\Browser\Safe Browsing"
        r"\urlsoceng.store.4_13429460000000000"
    )

    baseline = load_rules()
    baseline_templates = {
        rule.value for rule in baseline.delete.rules if rule.match is RuleMatch.PATH_GLOB
    }
    updated = add_ai_verdicts(
        baseline,
        [(source, RuleDecision.DELETE, "regenerable browser data")],
    )

    assert updated.decision_for(next_generated) is RuleDecision.DELETE
    templates = {rule.value for rule in updated.delete.rules if rule.match is RuleMatch.PATH_GLOB}
    expected = r"%LOCALAPPDATA%\browser\safe browsing\urlsoceng.store.4_*"
    assert expected in templates
    assert templates - baseline_templates == {expected}


def test_age_dependent_delete_is_portable_but_not_generalized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEVCLEAN_DATA_DIR", str(tmp_path / "DevClean-data"))
    monkeypatch.setenv("USERPROFILE", r"C:\Users\person")
    old_session = (
        r"C:\Users\person\.codex\sessions\2026\01\02"
        r"\rollout-2026-01-02T10-20-30.jsonl"
    )
    new_session = (
        r"C:\Users\person\.codex\sessions\2026\07\27"
        r"\rollout-2026-07-27T10-20-30.jsonl"
    )

    baseline = load_rules()
    baseline_templates = {
        (rule.match, rule.value)
        for rule in (*baseline.delete.rules, *baseline.keep.rules)
        if rule.match is RuleMatch.PATH_GLOB
    }
    updated = add_ai_verdicts(
        baseline,
        [
            (
                old_session,
                RuleDecision.DELETE,
                "这是超过 90 天未使用的旧会话记录",
            )
        ],
    )

    assert updated.decision_for(old_session) is RuleDecision.DELETE
    assert updated.decision_for(new_session) is None
    assert {
        (rule.match, rule.value)
        for rule in (*updated.delete.rules, *updated.keep.rules)
        if rule.match is RuleMatch.PATH_GLOB
    } == baseline_templates
    assert all("person" not in rule.value.casefold() for rule in updated.delete.rules)

    updated = add_ai_verdicts(
        updated,
        [
            (
                new_session,
                RuleDecision.KEEP,
                "今天生成的会话仍需保留",
            )
        ],
    )
    unseen_session = (
        r"C:\Users\person\.codex\sessions\2026\07\28"
        r"\rollout-2026-07-28T10-20-30.jsonl"
    )
    assert updated.decision_for(old_session) is RuleDecision.DELETE
    assert updated.decision_for(new_session) is RuleDecision.KEEP
    assert updated.decision_for(unseen_session) is None
    assert {
        (rule.match, rule.value)
        for rule in (*updated.delete.rules, *updated.keep.rules)
        if rule.match is RuleMatch.PATH_GLOB
    } == baseline_templates


def test_conflicting_ai_shape_removes_template_and_keeps_exact_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEVCLEAN_DATA_DIR", str(tmp_path / "DevClean-data"))
    monkeypatch.setenv("USERPROFILE", r"C:\Users\person")
    first = (
        r"C:\Users\person\.codex\sessions\2026\07\27"
        r"\rollout-2026-07-27T10-20-30-1234567890.jsonl"
    )
    second = (
        r"C:\Users\person\.codex\sessions\2026\07\28"
        r"\rollout-2026-07-28T10-20-30-0987654321.jsonl"
    )
    third = (
        r"C:\Users\person\.codex\sessions\2026\07\29"
        r"\rollout-2026-07-29T10-20-30-1122334455.jsonl"
    )

    baseline = load_rules()
    baseline_templates = {
        (rule.match, rule.value)
        for rule in (*baseline.delete.rules, *baseline.keep.rules)
        if rule.match is RuleMatch.PATH_GLOB
    }
    rules = add_ai_verdicts(
        baseline,
        [(first, RuleDecision.DELETE, "delete first")],
    )
    rules = add_ai_verdicts(
        rules,
        [(second, RuleDecision.KEEP, "keep second")],
    )

    assert rules.decision_for(first) is RuleDecision.DELETE
    assert rules.decision_for(second) is RuleDecision.KEEP
    assert rules.decision_for(third) is None
    assert {
        (rule.match, rule.value)
        for rule in (*rules.delete.rules, *rules.keep.rules)
        if rule.match is RuleMatch.PATH_GLOB
    } == baseline_templates

    # A later same-shape answer must not resurrect a template after conflict.
    rules = add_ai_verdicts(
        rules,
        [(third, RuleDecision.DELETE, "delete third")],
    )
    assert rules.decision_for(third) is RuleDecision.DELETE
    assert {
        (rule.match, rule.value)
        for rule in (*rules.delete.rules, *rules.keep.rules)
        if rule.match is RuleMatch.PATH_GLOB
    } == baseline_templates
