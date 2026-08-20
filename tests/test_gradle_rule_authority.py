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
    monkeypatch.setenv("GRADLE_USER_HOME", r"E:\Gradle\UserHome")


def test_ai_cannot_reintroduce_raw_gradle_cache_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _environment(tmp_path, monkeypatch)
    protected = (
        r"E:\Gradle\UserHome\caches\9.6.1\fileHashes\fileHashes.bin",
        r"E:\Gradle\UserHome\caches\build-cache-1\abc123",
        r"E:\Gradle\UserHome\daemon\9.6.1\daemon-123.out.log",
    )
    baseline = load_rules()
    before = baseline.ai_rule_count
    updated = add_ai_verdicts(
        baseline,
        [(path, RuleDecision.DELETE, "old regenerable Gradle cache") for path in protected],
    )

    for path in protected:
        assert updated.decision_for(path) is None
    assert updated.ai_rule_count == before


def test_user_delete_does_not_turn_protected_gradle_cache_into_generic_rule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _environment(tmp_path, monkeypatch)
    path = r"E:\Gradle\UserHome\caches\9.6.1\fileHashes\fileHashes.bin"
    updated = add_user_verdicts(
        load_rules(),
        [(path, RuleDecision.DELETE, "用户希望清理 Gradle cache")],
    )

    assert updated.decision_for(path) is None
