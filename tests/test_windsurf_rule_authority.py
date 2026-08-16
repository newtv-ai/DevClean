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


def test_ai_cannot_decide_windsurf_user_site_or_authored_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _environment(tmp_path, monkeypatch)
    protected = (
        r"C:\Users\person\.codeium\windsurf\cascade\conversation.db",
        r"C:\Users\person\.codeium\windsurf\memories\workspace\memory.md",
        r"C:\Users\person\.windsurf\plans\migration.md",
        r"C:\Users\person\.codeium\windsurf\mcp_config.json",
        r"C:\Users\person\.windsurf\extensions\publisher.ext\extension.js",
        (
            r"C:\Users\person\AppData\Roaming\Windsurf"
            r"\Service Worker\CacheStorage\origin\entry"
        ),
    )
    baseline = load_rules()
    before = baseline.ai_rule_count
    updated = add_ai_verdicts(
        baseline,
        [(path, RuleDecision.DELETE, "large old state") for path in protected],
    )
    for path in protected:
        assert updated.decision_for(path) is None
    assert updated.ai_rule_count == before


def test_user_windsurf_history_or_cache_storage_delete_is_not_generic_rule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _environment(tmp_path, monkeypatch)
    cascade = r"C:\Users\person\.codeium\windsurf\cascade\conversation.db"
    cache_storage = (
        r"C:\Users\person\AppData\Roaming\Windsurf"
        r"\Service Worker\CacheStorage\origin\entry"
    )
    updated = add_user_verdicts(
        load_rules(),
        [
            (cascade, RuleDecision.DELETE, "用户明确选择删除旧 Cascade 历史"),
            (cache_storage, RuleDecision.DELETE, "用户明确选择清理离线站点数据"),
        ],
    )
    assert updated.decision_for(cascade) is None
    assert updated.decision_for(cache_storage) is None


def test_ai_can_still_learn_windsurf_tool_cache_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _environment(tmp_path, monkeypatch)
    cache = r"C:\Users\person\AppData\Roaming\Windsurf\Cache\data_0"
    updated = add_ai_verdicts(
        load_rules(),
        [(cache, RuleDecision.DELETE, "regenerable Electron cache")],
    )
    assert updated.decision_for(cache) is RuleDecision.DELETE
