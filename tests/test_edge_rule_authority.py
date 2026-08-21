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


def test_ai_cannot_delete_edge_profile_site_or_updater_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _environment(tmp_path, monkeypatch)
    protected = (
        (
            r"C:\Users\person\AppData\Local\Microsoft\Edge\User Data\Default"
            r"\History"
        ),
        (
            r"C:\Users\person\AppData\Local\Microsoft\Edge\User Data\Default"
            r"\Login Data"
        ),
        (
            r"C:\Users\person\AppData\Local\Microsoft\Edge\User Data\Default"
            r"\Service Worker\CacheStorage\origin\entry"
        ),
        (
            r"C:\Users\person\AppData\Local\Microsoft\Edge\Update"
            r"\payload.bin"
        ),
        r"C:\ProgramData\Microsoft\EdgeUpdate\Log\MicrosoftEdgeUpdate.log",
        r"C:\ProgramData\Microsoft\EdgeUpdate\Log\MicrosoftEdgeUpdate.log.bak",
        r"C:\Users\person\AppData\Local\Temp\msedge_installer.log",
    )
    baseline = load_rules()
    before = baseline.ai_rule_count
    updated = add_ai_verdicts(
        baseline,
        [(path, RuleDecision.DELETE, "large old Edge state") for path in protected],
    )
    for path in protected:
        assert updated.decision_for(path) is None
    assert updated.ai_rule_count == before


def test_user_edge_site_data_delete_is_not_generic_file_rule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _environment(tmp_path, monkeypatch)
    cache_storage = (
        r"C:\Users\person\AppData\Local\Microsoft\Edge\User Data\Default"
        r"\Service Worker\CacheStorage\origin\entry"
    )
    updated = add_user_verdicts(
        load_rules(),
        [(cache_storage, RuleDecision.DELETE, "用户明确想清理 Edge 离线站点数据")],
    )
    assert updated.decision_for(cache_storage) is None


def test_ai_can_learn_edge_owned_http_cache_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _environment(tmp_path, monkeypatch)
    cache_file = (
        r"C:\Users\person\AppData\Local\Microsoft\Edge\User Data\Default"
        r"\Cache\Cache_Data\f_001"
    )
    updated = add_ai_verdicts(
        load_rules(),
        [(cache_file, RuleDecision.DELETE, "regenerable Edge HTTP cache")],
    )
    assert updated.decision_for(cache_file) is RuleDecision.DELETE
