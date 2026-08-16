from __future__ import annotations

from pathlib import Path

import pytest

from devclean.core.user_rules import RuleDecision, add_ai_verdicts, add_user_verdicts, load_rules


def _environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEVCLEAN_DATA_DIR", str(tmp_path / "DevClean-data"))
    monkeypatch.setenv("USERPROFILE", r"C:\Users\person")
    monkeypatch.setenv("APPDATA", r"C:\Users\person\AppData\Roaming")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\person\AppData\Local")
    monkeypatch.setenv("TEMP", r"C:\Users\person\AppData\Local\Temp")


def test_ai_cannot_delete_chrome_profile_or_updater_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _environment(tmp_path, monkeypatch)
    protected = (
        r"C:\Users\person\AppData\Local\Google\Chrome\User Data\Default\History",
        r"C:\Users\person\AppData\Local\Google\Chrome\User Data\Default\Login Data",
        r"C:\Users\person\AppData\Local\Google\Chrome\User Data\Default\Preferences",
        r"C:\Users\person\AppData\Local\Google\Chrome\User Data\Default\Extensions\abc\manifest.json",
        r"C:\Users\person\AppData\Local\Google\GoogleUpdater\prefs.json",
        r"C:\Users\person\AppData\Local\Google\GoogleUpdater\140.0.0.0\updater.exe",
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


def test_user_delete_of_chrome_history_does_not_become_generic_rule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _environment(tmp_path, monkeypatch)
    history = (
        r"C:\Users\person\AppData\Local\Google\Chrome\User Data\Default\History"
    )
    updated = add_user_verdicts(
        load_rules(),
        [(history, RuleDecision.DELETE, "用户想清空浏览历史")],
    )
    assert updated.decision_for(history) is None


def test_ai_can_still_learn_chrome_owned_cache_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _environment(tmp_path, monkeypatch)
    cache_file = (
        r"C:\Users\person\AppData\Local\Google\Chrome\User Data"
        r"\Default\Cache\Cache_Data\f_001"
    )
    updated = add_ai_verdicts(
        load_rules(),
        [(cache_file, RuleDecision.DELETE, "regenerable Chrome cache")],
    )
    assert updated.decision_for(cache_file) is RuleDecision.DELETE
