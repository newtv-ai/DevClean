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
    monkeypatch.setenv("ProgramFiles", r"C:\Program Files")
    monkeypatch.setenv("ProgramFiles(x86)", r"C:\Program Files (x86)")
    monkeypatch.setenv("ProgramData", r"C:\ProgramData")


def test_ai_cannot_delete_brave_profile_or_updater_protected_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _environment(tmp_path, monkeypatch)
    protected = (
        (
            r"C:\Users\person\AppData\Local\BraveSoftware\Brave-Browser"
            r"\User Data\Default\History"
        ),
        (
            r"C:\Users\person\AppData\Local\BraveSoftware\Brave-Browser"
            r"\User Data\Default\Login Data"
        ),
        (
            r"C:\Users\person\AppData\Local\BraveSoftware\Brave-Browser"
            r"\User Data\Default\Service Worker\CacheStorage\origin\data"
        ),
        (
            r"C:\Program Files (x86)\BraveSoftware\Update"
            r"\Install\{A}\brave_installer-delta-x64.exe"
        ),
        r"C:\ProgramData\BraveSoftware\Update\Log\BraveUpdate.log",
        (
            r"C:\Program Files (x86)\BraveSoftware\Update"
            r"\1.3.361.143\BraveUpdate.exe"
        ),
    )
    baseline = load_rules()
    before = baseline.ai_rule_count
    updated = add_ai_verdicts(
        baseline,
        [(path, RuleDecision.DELETE, "old or large") for path in protected],
    )
    for path in protected:
        assert updated.decision_for(path) is None
    assert updated.ai_rule_count == before


def test_user_delete_of_brave_cache_storage_does_not_become_generic_rule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _environment(tmp_path, monkeypatch)
    cache_storage = (
        r"C:\Users\person\AppData\Local\BraveSoftware\Brave-Browser"
        r"\User Data\Default\Service Worker\CacheStorage\origin\data"
    )
    updated = add_user_verdicts(
        load_rules(),
        [(cache_storage, RuleDecision.DELETE, "用户主动清理站点离线数据")],
    )
    assert updated.decision_for(cache_storage) is None


def test_user_delete_of_brave_updater_staging_does_not_become_generic_rule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _environment(tmp_path, monkeypatch)
    staging = (
        r"C:\Program Files (x86)\BraveSoftware\Update"
        r"\Install\{A}\brave_installer-delta-x64.exe"
    )
    updated = add_user_verdicts(
        load_rules(),
        [(staging, RuleDecision.DELETE, "用户希望清理 Brave Update 安装缓存")],
    )
    assert updated.decision_for(staging) is None


def test_ai_can_learn_brave_http_cache_but_not_updater_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _environment(tmp_path, monkeypatch)
    cache = (
        r"C:\Users\person\AppData\Local\BraveSoftware\Brave-Browser"
        r"\User Data\Default\Cache\Cache_Data\f_001"
    )
    staging = (
        r"C:\Program Files (x86)\BraveSoftware\Update"
        r"\Install\{A}\brave_installer-delta-x64.exe"
    )
    log = r"C:\ProgramData\BraveSoftware\Update\Log\BraveUpdate.log"
    updated = add_ai_verdicts(
        load_rules(),
        [
            (cache, RuleDecision.DELETE, "regenerable Brave HTTP cache"),
            (staging, RuleDecision.DELETE, "stale downloaded installer staging"),
            (log, RuleDecision.DELETE, "old updater diagnostic log"),
        ],
    )
    assert updated.decision_for(cache) is RuleDecision.DELETE
    assert updated.decision_for(staging) is None
    assert updated.decision_for(log) is None
