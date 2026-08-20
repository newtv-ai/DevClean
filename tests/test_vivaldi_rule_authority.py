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


def test_ai_cannot_delete_vivaldi_profile_or_diagnostic_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _environment(tmp_path, monkeypatch)
    protected = (
        r"C:\Users\person\AppData\Local\Vivaldi\User Data\Default\History",
        r"C:\Users\person\AppData\Local\Vivaldi\User Data\Default\Login Data",
        (
            r"C:\Users\person\AppData\Local\Vivaldi\User Data\Default"
            r"\Service Worker\CacheStorage\origin\data"
        ),
        (
            r"C:\Users\person\AppData\Local\Vivaldi\User Data\Default"
            r"\Extensions\abc\manifest.json"
        ),
        (
            r"C:\Users\person\AppData\Local\Vivaldi\User Data"
            r"\Crashpad\reports\7b93c6b0-0001.dmp"
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


def test_user_delete_of_vivaldi_history_or_crash_dump_is_not_generic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _environment(tmp_path, monkeypatch)
    history = r"C:\Users\person\AppData\Local\Vivaldi\User Data\Default\History"
    crash = (
        r"C:\Users\person\AppData\Local\Vivaldi\User Data"
        r"\Crashpad\reports\7b93c6b0-0001.dmp"
    )
    updated = add_user_verdicts(
        load_rules(),
        [
            (history, RuleDecision.DELETE, "用户想清空浏览历史"),
            (crash, RuleDecision.DELETE, "用户认为旧崩溃文件可以删除"),
        ],
    )
    assert updated.decision_for(history) is None
    assert updated.decision_for(crash) is None


def test_ai_can_learn_http_cache_but_not_vivaldi_crashpad_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _environment(tmp_path, monkeypatch)
    cache_file = (
        r"C:\Users\person\AppData\Local\Vivaldi\User Data"
        r"\Default\Cache\Cache_Data\f_001"
    )
    crash = (
        r"C:\Users\person\AppData\Local\Vivaldi\User Data"
        r"\Crashpad\reports\7b93c6b0-0001.dmp"
    )
    updated = add_ai_verdicts(
        load_rules(),
        [
            (cache_file, RuleDecision.DELETE, "regenerable Vivaldi cache"),
            (crash, RuleDecision.DELETE, "old diagnostic dump"),
        ],
    )
    assert updated.decision_for(cache_file) is RuleDecision.DELETE
    assert updated.decision_for(crash) is None
