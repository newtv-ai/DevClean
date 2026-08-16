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


def test_ai_cannot_decide_trae_workspace_global_state_or_extensions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _environment(tmp_path, monkeypatch)
    workspace = (
        r"C:\Users\person\AppData\Roaming\Trae\User\workspaceStorage"
        r"\abc\state.vscdb"
    )
    global_state = (
        r"C:\Users\person\AppData\Roaming\Trae\User\globalStorage"
        r"\vendor.ai\state.db"
    )
    extension = r"C:\Users\person\.trae\extensions\publisher.ext\extension.js"
    baseline = load_rules()
    before = baseline.ai_rule_count

    updated = add_ai_verdicts(
        baseline,
        [
            (workspace, RuleDecision.DELETE, "old workspace cache"),
            (global_state, RuleDecision.DELETE, "large sqlite"),
            (extension, RuleDecision.DELETE, "large extension"),
        ],
    )

    assert updated.decision_for(workspace) is None
    assert updated.decision_for(global_state) is None
    assert updated.decision_for(extension) is None
    assert updated.ai_rule_count == before


def test_user_trae_workspace_delete_does_not_become_generic_rule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _environment(tmp_path, monkeypatch)
    workspace = (
        r"C:\Users\person\AppData\Roaming\Trae\User\workspaceStorage"
        r"\abc\state.vscdb"
    )
    updated = add_user_verdicts(
        load_rules(),
        [(workspace, RuleDecision.DELETE, "用户明确想清理旧 Trae 工作区状态")],
    )
    assert updated.decision_for(workspace) is None


def test_ai_can_still_learn_trae_tool_cache_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _environment(tmp_path, monkeypatch)
    cache = r"C:\Users\person\AppData\Roaming\Trae\Cache\data_0"
    updated = add_ai_verdicts(
        load_rules(),
        [(cache, RuleDecision.DELETE, "regenerable Electron cache")],
    )
    assert updated.decision_for(cache) is RuleDecision.DELETE
