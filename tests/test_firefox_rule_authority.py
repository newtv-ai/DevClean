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
    monkeypatch.setenv("PROGRAMDATA", r"C:\ProgramData")
    monkeypatch.setenv("TEMP", r"C:\Users\person\AppData\Local\Temp")
    monkeypatch.setenv("DEVCLEAN_FIREFOX_PROFILE_DIR", r"D:\PortableFirefox\Profile")


def test_ai_cannot_delete_firefox_profile_roaming_or_update_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _environment(tmp_path, monkeypatch)
    protected = (
        r"C:\Users\person\AppData\Roaming\Mozilla\Firefox\profiles.ini",
        (
            r"C:\Users\person\AppData\Roaming\Mozilla\Firefox\Profile Groups"
            r"\profile-group.sqlite"
        ),
        (
            r"C:\Users\person\AppData\Roaming\Mozilla\Firefox\Profiles"
            r"\abc.default-release\places.sqlite"
        ),
        r"D:\PortableFirefox\Profile\logins.json",
        r"C:\ProgramData\Mozilla\updates\install-hash\updates\0\update.mar",
        r"C:\ProgramData\Mozilla\updates\install-hash\updates\0\update.status",
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


def test_user_delete_of_firefox_history_or_update_payload_is_not_generic_rule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _environment(tmp_path, monkeypatch)
    history = (
        r"C:\Users\person\AppData\Roaming\Mozilla\Firefox\Profiles"
        r"\abc.default-release\places.sqlite"
    )
    payload = r"C:\ProgramData\Mozilla\updates\install-hash\updates\0\update.mar"
    updated = add_user_verdicts(
        load_rules(),
        [
            (history, RuleDecision.DELETE, "用户想清空浏览历史"),
            (payload, RuleDecision.DELETE, "用户想删除更新包"),
        ],
    )
    assert updated.decision_for(history) is None
    assert updated.decision_for(payload) is None


def test_ai_can_still_learn_firefox_owned_cache_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _environment(tmp_path, monkeypatch)
    cache_file = (
        r"C:\Users\person\AppData\Local\Mozilla\Firefox\Profiles"
        r"\abc.default-release\cache2\entries\abcdef"
    )
    updated = add_ai_verdicts(
        load_rules(),
        [(cache_file, RuleDecision.DELETE, "regenerable Firefox cache")],
    )
    assert updated.decision_for(cache_file) is RuleDecision.DELETE
