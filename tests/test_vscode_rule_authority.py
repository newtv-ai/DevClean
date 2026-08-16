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


def test_ai_cannot_decide_vscode_workspace_history_backups_or_extensions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _environment(tmp_path, monkeypatch)
    workspace = (
        r"C:\Users\person\AppData\Roaming\Code\User\workspaceStorage"
        r"\abc\chatSessions\thread.jsonl"
    )
    backup = r"C:\Users\person\AppData\Roaming\Code\Backups\window\untitled.txt"
    extension = r"C:\Users\person\.vscode\extensions\publisher.ext-1.2.3\extension.js"
    baseline = load_rules()
    before = baseline.ai_rule_count

    updated = add_ai_verdicts(
        baseline,
        [
            (workspace, RuleDecision.DELETE, "old chat"),
            (backup, RuleDecision.DELETE, "looks temporary"),
            (extension, RuleDecision.DELETE, "large extension"),
        ],
    )

    assert updated.decision_for(workspace) is None
    assert updated.decision_for(backup) is None
    assert updated.decision_for(extension) is None
    assert updated.ai_rule_count == before


def test_user_vscode_workspace_delete_does_not_become_generic_file_rule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _environment(tmp_path, monkeypatch)
    workspace_db = (
        r"C:\Users\person\AppData\Roaming\Code\User\workspaceStorage"
        r"\abc\state.vscdb"
    )
    updated = add_user_verdicts(
        load_rules(),
        [(workspace_db, RuleDecision.DELETE, "用户明确想清旧工作区聊天")],
    )
    assert updated.decision_for(workspace_db) is None


def test_ai_cannot_decide_portable_vscode_user_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _environment(tmp_path, monkeypatch)
    monkeypatch.setenv("VSCODE_PORTABLE", r"E:\VSCode\data")
    portable_chat = (
        r"E:\VSCode\data\user-data\User\workspaceStorage"
        r"\abc\chatSessions\thread.jsonl"
    )
    updated = add_ai_verdicts(
        load_rules(),
        [(portable_chat, RuleDecision.DELETE, "portable cache-looking path")],
    )
    assert updated.decision_for(portable_chat) is None
