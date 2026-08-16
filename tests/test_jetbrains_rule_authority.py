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
    monkeypatch.setenv("DEVCLEAN_JETBRAINS_CONFIG_DIR", r"D:\JetBrains\Config")
    monkeypatch.setenv("DEVCLEAN_JETBRAINS_SYSTEM_DIR", r"D:\JetBrains\System")
    monkeypatch.setenv("DEVCLEAN_JETBRAINS_PLUGINS_DIR", r"D:\JetBrains\Plugins")
    monkeypatch.setenv("DEVCLEAN_JETBRAINS_LOG_DIR", r"D:\JetBrains\Logs")


def test_ai_cannot_delete_jetbrains_config_plugins_history_or_browser_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _environment(tmp_path, monkeypatch)
    protected = (
        r"D:\JetBrains\Config\options\editor.xml",
        r"D:\JetBrains\Plugins\my.plugin\lib\plugin.jar",
        r"D:\JetBrains\System\LocalHistory\storageData",
        r"D:\JetBrains\System\jcef_cache\Cookies",
        r"D:\JetBrains\System\caches\names.dat",
        r"D:\JetBrains\System\unknown\state.db",
    )
    baseline = load_rules()
    before = baseline.ai_rule_count
    updated = add_ai_verdicts(
        baseline,
        [(path, RuleDecision.DELETE, "looks large") for path in protected],
    )

    for path in protected:
        assert updated.decision_for(path) is None
    assert updated.ai_rule_count == before


def test_user_delete_does_not_turn_jetbrains_user_state_into_generic_rule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _environment(tmp_path, monkeypatch)
    history = r"D:\JetBrains\System\LocalHistory\storageData"
    cookies = r"D:\JetBrains\System\jcef_cache\Cookies"
    updated = add_user_verdicts(
        load_rules(),
        [
            (history, RuleDecision.DELETE, "用户想删除本地历史"),
            (cookies, RuleDecision.DELETE, "用户想清理内置浏览器数据"),
        ],
    )

    assert updated.decision_for(history) is None
    assert updated.decision_for(cookies) is None


def test_ai_can_learn_exact_jetbrains_tool_cache_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _environment(tmp_path, monkeypatch)
    index_file = r"D:\JetBrains\System\index\file.idx"
    updated = add_ai_verdicts(
        load_rules(),
        [(index_file, RuleDecision.DELETE, "regenerable IDE index")],
    )

    assert updated.decision_for(index_file) is RuleDecision.DELETE
