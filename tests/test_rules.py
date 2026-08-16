"""Original rule-engine tests plus Codex semantic-authority regressions."""

from __future__ import annotations

from pathlib import Path

import pytest

from devclean.core.user_rules import (
    RuleDecision,
    UserRules,
    add_ai_verdicts,
    add_user_verdicts,
    load_rules,
)
from tests import _test_rules_impl as _original

_REPLACED = {
    "test_ai_rules_port_user_profile_and_reuse_dated_path_shapes",
    "test_age_dependent_delete_is_portable_but_not_generalized",
    "test_conflicting_ai_shape_removes_template_and_keeps_exact_answers",
}

for _name in dir(_original):
    if _name.startswith("test_") and _name not in _REPLACED:
        globals()[_name] = getattr(_original, _name)


def _codex_test_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEVCLEAN_DATA_DIR", str(tmp_path / "DevClean-data"))
    monkeypatch.setenv("USERPROFILE", r"C:\Users\person")
    monkeypatch.setenv("USERNAME", "person")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\person\AppData\Local")
    monkeypatch.setenv("APPDATA", r"C:\Users\person\AppData\Roaming")
    monkeypatch.setenv("TEMP", r"C:\Users\person\AppData\Local\Temp")


def test_ai_cannot_decide_or_generalize_codex_session_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _codex_test_environment(tmp_path, monkeypatch)
    target = (
        r"C:\Users\person\.codex\sessions\2026\07\27"
        r"\rollout-2026-07-27T10-20-30-1234567890.jsonl"
    )
    tomorrow = (
        r"C:\Users\person\.codex\sessions\2026\07\28"
        r"\rollout-2026-07-28T09-00-00-0987654321.jsonl"
    )
    baseline = load_rules()
    baseline_ai_count = baseline.ai_rule_count

    updated = add_ai_verdicts(
        baseline,
        [(target, RuleDecision.DELETE, "AI suggested deleting old history")],
    )

    assert updated.decision_for(target) is None
    assert updated.decision_for(tomorrow) is None
    assert updated.ai_rule_count == baseline_ai_count
    assert load_rules().decision_for(target) is None


def test_user_codex_history_delete_does_not_become_generic_file_rule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _codex_test_environment(tmp_path, monkeypatch)
    old_session = (
        r"C:\Users\person\.codex\sessions\2026\01\02"
        r"\rollout-2026-01-02T10-20-30.jsonl"
    )
    newer_session = (
        r"C:\Users\person\.codex\sessions\2026\07\27"
        r"\rollout-2026-07-27T10-20-30.jsonl"
    )
    baseline = load_rules()

    updated = add_user_verdicts(
        baseline,
        [(old_session, RuleDecision.DELETE, "用户选择清理 90 天以上历史")],
    )

    # USER-owned history is deleted only through Codex's application action;
    # the generic exact-file rule engine must not gain deletion authority.
    assert updated.decision_for(old_session) is None
    assert updated.decision_for(newer_session) is None

    updated = add_user_verdicts(
        updated,
        [(newer_session, RuleDecision.KEEP, "用户选择保留近期会话")],
    )
    unseen = (
        r"C:\Users\person\.codex\sessions\2026\07\29"
        r"\rollout-2026-07-29T10-20-30.jsonl"
    )
    assert updated.decision_for(old_session) is None
    assert updated.decision_for(newer_session) is RuleDecision.KEEP
    assert updated.decision_for(unseen) is None


def test_generic_ai_shape_conflicts_still_collapse_to_exact_answers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEVCLEAN_DATA_DIR", str(tmp_path / "DevClean-data"))
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\person\AppData\Local")
    first = (
        r"C:\Users\person\AppData\Local\GenericTool\cache"
        r"\run-2026-07-27T10-20-30-1234567890.tmp"
    )
    second = (
        r"C:\Users\person\AppData\Local\GenericTool\cache"
        r"\run-2026-07-28T10-20-30-0987654321.tmp"
    )
    third = (
        r"C:\Users\person\AppData\Local\GenericTool\cache"
        r"\run-2026-07-29T10-20-30-1122334455.tmp"
    )

    updated = add_ai_verdicts(
        load_rules(),
        [(first, RuleDecision.DELETE, "regenerable generated cache")],
    )
    assert updated.decision_for(second) is RuleDecision.DELETE

    updated = add_ai_verdicts(
        updated,
        [(second, RuleDecision.KEEP, "keep this generated cache instance")],
    )
    assert updated.decision_for(first) is RuleDecision.DELETE
    assert updated.decision_for(second) is RuleDecision.KEEP
    assert updated.decision_for(third) is None

    rebuilt = UserRules(
        scan=updated.scan,
        delete=updated.delete,
        keep=updated.keep,
    )
    assert rebuilt.decision_for(first) is RuleDecision.DELETE
    assert rebuilt.decision_for(second) is RuleDecision.KEEP
    assert rebuilt.decision_for(third) is None
