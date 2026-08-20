from __future__ import annotations

from pathlib import Path

import pytest

from devclean.core.user_rules import (
    RuleDecision,
    add_ai_verdicts,
    add_user_verdicts,
    load_rules,
)


def _environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEVCLEAN_DATA_DIR", str(tmp_path / "DevClean-data"))
    monkeypatch.setenv("USERPROFILE", r"C:\Users\person")
    monkeypatch.setenv("APPDATA", r"C:\Users\person\AppData\Roaming")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\person\AppData\Local")
    monkeypatch.setenv("TEMP", r"C:\Users\person\AppData\Local\Temp")


def test_ai_cannot_delete_opera_profile_site_data_or_recovery_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _environment(tmp_path, monkeypatch)
    protected = (
        (
            r"C:\Users\person\AppData\Roaming\Opera Software\Opera Stable"
            r"\Default\History"
        ),
        (
            r"C:\Users\person\AppData\Roaming\Opera Software\Opera GX Stable"
            r"\Default\Login Data"
        ),
        (
            r"C:\Users\person\AppData\Roaming\Opera Software\Opera Stable"
            r"\Default\Service Worker\CacheStorage\origin\data"
        ),
        (
            r"C:\Users\person\AppData\Roaming\Opera Software\Opera Stable"
            r"\Default\Sessions\Session_123"
        ),
        (
            r"C:\Users\person\AppData\Roaming\Opera Software\Opera Stable"
            r"\Default.old\Sessions\Session_122"
        ),
    )
    baseline = load_rules()
    before = baseline.ai_rule_count
    updated = add_ai_verdicts(
        baseline,
        [(path, RuleDecision.DELETE, "looks old or large") for path in protected],
    )
    for path in protected:
        assert updated.decision_for(path) is None
    assert updated.ai_rule_count == before


def test_user_delete_of_opera_history_does_not_become_generic_rule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _environment(tmp_path, monkeypatch)
    history = (
        r"C:\Users\person\AppData\Roaming\Opera Software\Opera Stable"
        r"\Default\History"
    )
    updated = add_user_verdicts(
        load_rules(),
        [(history, RuleDecision.DELETE, "用户想清空 Opera 历史")],
    )
    assert updated.decision_for(history) is None


def test_ai_can_learn_opera_owned_local_cache_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _environment(tmp_path, monkeypatch)
    cache_file = (
        r"C:\Users\person\AppData\Local\Opera Software\Opera GX Stable"
        r"\Default\Cache\Cache_Data\f_001"
    )
    updated = add_ai_verdicts(
        load_rules(),
        [(cache_file, RuleDecision.DELETE, "regenerable Opera cache")],
    )
    assert updated.decision_for(cache_file) is RuleDecision.DELETE


def test_learned_verdicts_cannot_restore_opera_system_cache_raw_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _environment(tmp_path, monkeypatch)
    system_cache = (
        r"C:\Users\person\AppData\Local\Opera Software\Opera Stable"
        r"\Default\System Cache\Cache_Data\data_0"
    )
    baseline = load_rules()
    ai_before = baseline.ai_rule_count
    ai_updated = add_ai_verdicts(
        baseline,
        [(system_cache, RuleDecision.DELETE, "old regenerable system cache")],
    )
    assert ai_updated.decision_for(system_cache) is None
    assert ai_updated.ai_rule_count == ai_before

    user_updated = add_user_verdicts(
        baseline,
        [(system_cache, RuleDecision.DELETE, "用户要求删除 Opera System Cache")],
    )
    assert user_updated.decision_for(system_cache) is None
