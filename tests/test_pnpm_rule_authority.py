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
    monkeypatch.setenv("PNPM_HOME", r"D:\pnpm-home")
    monkeypatch.setenv("PNPM_CONFIG_STORE_DIR", r"E:\pnpm-store")
    monkeypatch.setenv("PNPM_CONFIG_GLOBAL_DIR", r"F:\pnpm-global")
    monkeypatch.setenv("PNPM_CONFIG_GLOBAL_BIN_DIR", r"G:\pnpm-bin")
    monkeypatch.setenv("PNPM_CONFIG_CACHE_DIR", r"H:\pnpm-cache")


def test_ai_cannot_delete_pnpm_owned_or_user_review_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _environment(tmp_path, monkeypatch)
    protected = (
        r"E:\pnpm-store\v10\files\aa\blob",
        r"E:\pnpm-store\v10\links\react\index.json",
        r"F:\pnpm-global\5\node_modules\typescript\lib\tsc.js",
        r"G:\pnpm-bin\pnpm.cmd",
        r"D:\pnpm-home\pnpm.exe",
        r"H:\pnpm-cache\dlx\hash\pkg\index.js",
        r"H:\pnpm-cache\v11\metadata\registry.npmjs.org\react.json",
        r"H:\pnpm-cache\metadata-v1.3\registry.npmjs.org\react.json",
        r"C:\Users\person\src\app\pnpm-lock.yaml",
        r"C:\Users\person\src\app\pnpm-workspace.yaml",
    )
    baseline = load_rules()
    before = baseline.ai_rule_count
    updated = add_ai_verdicts(
        baseline,
        [(path, RuleDecision.DELETE, "old or large pnpm data") for path in protected],
    )
    for path in protected:
        assert updated.decision_for(path) is None
    assert updated.ai_rule_count == before


@pytest.mark.parametrize(
    "path",
    (
        r"E:\pnpm-store\v10\files\aa\blob",
        r"H:\pnpm-cache\dlx\hash\pkg\index.js",
        r"H:\pnpm-cache\v11\metadata\registry.npmjs.org\react.json",
    ),
)
def test_user_delete_of_pnpm_owned_lane_does_not_become_generic_rule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    _environment(tmp_path, monkeypatch)
    updated = add_user_verdicts(
        load_rules(),
        [(path, RuleDecision.DELETE, "用户选择 pnpm 专用清理")],
    )
    assert updated.decision_for(path) is None
