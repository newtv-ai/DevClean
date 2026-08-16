"""Public semantic boundary for application-aware cleanup.

The stable Codex engine and each audited application profile live behind this
facade. Generic scan/delete code imports only this module, so USER-owned history
and KEEP state can be inventoried but never receive generic deletion authority.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime
from pathlib import PureWindowsPath

from devclean.core import _application_cleanup_impl as _impl
from devclean.core._application_cleanup_impl import (
    CODEX_RULES,
    ApplicationCleanupRule,
    ApplicationPolicyDecision,
    ApplicationRoot,
    DecisionOwner,
    LastUseStrategy,
    MatchKind,
    PolicyAction,
    RebuildCost,
    effective_idle_days,
)
from devclean.core.android_sdk_cleanup import (
    ANDROID_SDK_RULES,
    android_sdk_audited_tool_roots,
    android_sdk_process_running,
    android_sdk_scan_roots,
    clear_android_sdk_process_cache,
    evaluate_android_sdk_path,
    match_android_sdk_rule,
    whole_tree_android_sdk_rule,
)
from devclean.core.android_studio_cleanup import (
    ANDROID_STUDIO_RULES,
    android_studio_audited_tool_roots,
    android_studio_process_running,
    android_studio_scan_roots,
    clear_android_studio_process_cache,
    evaluate_android_studio_path,
    match_android_studio_rule,
    whole_tree_android_studio_rule,
)
from devclean.core.brave_cleanup import (
    BRAVE_RULES,
    brave_audited_tool_roots,
    brave_process_running,
    brave_scan_roots,
    clear_brave_process_cache,
    evaluate_brave_path,
    match_brave_rule,
    whole_tree_brave_rule,
)
from devclean.core.chrome_cleanup import (
    CHROME_RULES,
    chrome_audited_tool_roots,
    chrome_process_running,
    chrome_scan_roots,
    clear_chrome_process_cache,
    evaluate_chrome_path,
    match_chrome_rule,
    whole_tree_chrome_rule,
)
from devclean.core.claude_cleanup import (
    CLAUDE_RULES,
    claude_application_roots,
    claude_process_running,
    claude_scan_roots,
    clear_claude_process_cache,
    evaluate_claude_path,
    match_claude_rule,
)
from devclean.core.cursor_cleanup import (
    CURSOR_RULES,
    clear_cursor_process_cache,
    cursor_application_roots,
    cursor_process_running,
    cursor_scan_roots,
    evaluate_cursor_path,
    match_cursor_rule,
)
from devclean.core.edge_cleanup import (
    EDGE_RULES,
    clear_edge_process_cache,
    edge_audited_tool_roots,
    edge_process_running,
    edge_scan_roots,
    evaluate_edge_path,
    match_edge_rule,
    whole_tree_edge_rule,
)
from devclean.core.firefox_cleanup import (
    FIREFOX_RULES,
    clear_firefox_process_cache,
    evaluate_firefox_path,
    firefox_audited_tool_roots,
    firefox_process_running,
    firefox_scan_roots,
    match_firefox_rule,
    whole_tree_firefox_rule,
)
from devclean.core.gradle_cleanup import (
    GRADLE_RULES,
    clear_gradle_process_cache,
    evaluate_gradle_path,
    gradle_audited_tool_roots,
    gradle_process_running,
    gradle_scan_roots,
    match_gradle_rule,
    whole_tree_gradle_rule,
)
from devclean.core.jetbrains_cleanup import (
    JETBRAINS_RULES,
    clear_jetbrains_process_cache,
    evaluate_jetbrains_path,
    jetbrains_audited_tool_roots,
    jetbrains_process_running,
    jetbrains_scan_roots,
    match_jetbrains_rule,
    whole_tree_jetbrains_rule,
)
from devclean.core.npm_cleanup import (
    NPM_RULES,
    clear_npm_process_cache,
    evaluate_npm_path,
    match_npm_rule,
    npm_audited_tool_roots,
    npm_process_running,
    npm_scan_roots,
    whole_tree_npm_rule,
)
from devclean.core.opera_cleanup import (
    OPERA_RULES,
    clear_opera_process_cache,
    evaluate_opera_path,
    match_opera_rule,
    opera_audited_tool_roots,
    opera_process_running,
    opera_scan_roots,
    whole_tree_opera_rule,
)
from devclean.core.pnpm_cleanup import (
    PNPM_RULES,
    clear_pnpm_process_cache,
    evaluate_pnpm_path,
    match_pnpm_rule,
    pnpm_audited_tool_roots,
    pnpm_process_running,
    pnpm_scan_roots,
    whole_tree_pnpm_rule,
)
from devclean.core.toolbox_cleanup import (
    TOOLBOX_RULES,
    clear_toolbox_process_cache,
    evaluate_toolbox_path,
    match_toolbox_rule,
    toolbox_audited_tool_roots,
    toolbox_process_running,
    toolbox_scan_roots,
    whole_tree_toolbox_rule,
)
from devclean.core.trae_cleanup import (
    TRAE_RULES,
    clear_trae_process_cache,
    evaluate_trae_path,
    match_trae_rule,
    trae_audited_tool_roots,
    trae_process_running,
    trae_scan_roots,
    whole_tree_trae_rule,
)
from devclean.core.vivaldi_cleanup import (
    VIVALDI_RULES,
    clear_vivaldi_process_cache,
    evaluate_vivaldi_path,
    match_vivaldi_rule,
    vivaldi_audited_tool_roots,
    vivaldi_process_running,
    vivaldi_scan_roots,
    whole_tree_vivaldi_rule,
)
from devclean.core.vscode_cleanup import (
    VSCODE_RULES,
    clear_vscode_process_cache,
    evaluate_vscode_path,
    match_vscode_rule,
    vscode_audited_tool_roots,
    vscode_process_running,
    vscode_scan_roots,
    whole_tree_vscode_rule,
)
from devclean.core.windsurf_cleanup import (
    WINDSURF_RULES,
    clear_windsurf_process_cache,
    evaluate_windsurf_path,
    match_windsurf_rule,
    whole_tree_windsurf_rule,
    windsurf_audited_tool_roots,
    windsurf_process_running,
    windsurf_scan_roots,
)

_ORIGINAL_APPLICATION_ROOTS = _impl.application_roots
_ORIGINAL_EVALUATE_APPLICATION_PATH = _impl.evaluate_application_path
_ORIGINAL_MATCH_APPLICATION_RULE = _impl.match_application_rule
_ORIGINAL_APPLICATION_PROCESS_RUNNING = _impl.application_process_running


def application_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[ApplicationRoot, ...]:
    """Return audited roots represented by the fixed-key application profiles."""

    return (
        *_ORIGINAL_APPLICATION_ROOTS(environment),
        *claude_application_roots(environment),
        *cursor_application_roots(environment),
    )


def application_scan_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[PureWindowsPath, ...]:
    """Return application-specific roots that may contain reclaimable storage."""

    codex = tuple(root.path for root in _ORIGINAL_APPLICATION_ROOTS(environment))
    return tuple(
        dict.fromkeys(
            (
                *codex,
                *claude_scan_roots(environment),
                *cursor_scan_roots(environment),
                *vscode_scan_roots(environment),
                *trae_scan_roots(environment),
                *windsurf_scan_roots(environment),
                *npm_scan_roots(environment),
                *pnpm_scan_roots(environment),
                *chrome_scan_roots(environment),
                *edge_scan_roots(environment),
                *brave_scan_roots(environment),
                *vivaldi_scan_roots(environment),
                *opera_scan_roots(environment),
                *firefox_scan_roots(environment),
                *jetbrains_scan_roots(environment),
                *toolbox_scan_roots(environment),
                *android_studio_scan_roots(environment),
                *gradle_scan_roots(environment),
                *android_sdk_scan_roots(environment),
            )
        )
    )


def audited_dynamic_tool_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[tuple[PureWindowsPath, ApplicationCleanupRule], ...]:
    """Return whole-tree TOOL roots for profiles with multiple/dynamic roots."""

    return (
        *vscode_audited_tool_roots(environment),
        *trae_audited_tool_roots(environment),
        *windsurf_audited_tool_roots(environment),
        *npm_audited_tool_roots(environment),
        *pnpm_audited_tool_roots(environment),
        *chrome_audited_tool_roots(environment),
        *edge_audited_tool_roots(environment),
        *brave_audited_tool_roots(environment),
        *vivaldi_audited_tool_roots(environment),
        *opera_audited_tool_roots(environment),
        *firefox_audited_tool_roots(environment),
        *jetbrains_audited_tool_roots(environment),
        *toolbox_audited_tool_roots(environment),
        *android_studio_audited_tool_roots(environment),
        *gradle_audited_tool_roots(environment),
        *android_sdk_audited_tool_roots(environment),
    )


def match_application_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    """Return the most-specific audited application rule for *path*."""

    android_sdk = match_android_sdk_rule(path, environment)
    if android_sdk is not None:
        return android_sdk
    gradle = match_gradle_rule(path, environment)
    if gradle is not None:
        return gradle
    android_studio = match_android_studio_rule(path, environment)
    if android_studio is not None:
        return android_studio
    toolbox = match_toolbox_rule(path, environment)
    if toolbox is not None:
        return toolbox
    jetbrains = match_jetbrains_rule(path, environment)
    if jetbrains is not None:
        return jetbrains
    firefox = match_firefox_rule(path, environment)
    if firefox is not None:
        return firefox
    opera = match_opera_rule(path, environment)
    if opera is not None:
        return opera
    vivaldi = match_vivaldi_rule(path, environment)
    if vivaldi is not None:
        return vivaldi
    brave = match_brave_rule(path, environment)
    if brave is not None:
        return brave
    edge = match_edge_rule(path, environment)
    if edge is not None:
        return edge
    chrome = match_chrome_rule(path, environment)
    if chrome is not None:
        return chrome
    windsurf = match_windsurf_rule(path, environment)
    if windsurf is not None:
        return windsurf
    trae = match_trae_rule(path, environment)
    if trae is not None:
        return trae
    vscode = match_vscode_rule(path, environment)
    if vscode is not None:
        return vscode
    cursor = match_cursor_rule(path, environment)
    if cursor is not None:
        return cursor
    claude = match_claude_rule(path, environment)
    if claude is not None:
        return claude
    pnpm = match_pnpm_rule(path, environment)
    if pnpm is not None:
        return pnpm
    npm = match_npm_rule(path, environment)
    if npm is not None:
        return npm
    return _ORIGINAL_MATCH_APPLICATION_RULE(path, environment)


def evaluate_application_path(
    path: str | os.PathLike[str],
    *,
    logical_size: int,
    last_used: datetime | None,
    now: datetime | None = None,
    process_running: bool | None = None,
    environment: Mapping[str, str] | None = None,
) -> ApplicationPolicyDecision | None:
    """Evaluate one path for the generic scan/review pipeline.

    TOOL items retain their normal recommendation. USER-owned data keeps its
    metadata internally but is projected to ``KEEP_PROTECTED`` for the generic
    pipeline; a dedicated application action is the only place a user can choose
    to remove it.
    """

    decision = evaluate_android_sdk_path(
        path,
        logical_size=logical_size,
        last_used=last_used,
        now=now,
        process_running=process_running,
        environment=environment,
    )
    if decision is None:
        decision = evaluate_gradle_path(
            path,
            logical_size=logical_size,
            last_used=last_used,
            now=now,
            process_running=process_running,
            environment=environment,
        )
    if decision is None:
        decision = evaluate_android_studio_path(
            path,
            logical_size=logical_size,
            last_used=last_used,
            now=now,
            process_running=process_running,
            environment=environment,
        )
    if decision is None:
        decision = evaluate_toolbox_path(
            path,
            logical_size=logical_size,
            last_used=last_used,
            now=now,
            process_running=process_running,
            environment=environment,
        )
    if decision is None:
        decision = evaluate_jetbrains_path(
            path,
            logical_size=logical_size,
            last_used=last_used,
            now=now,
            process_running=process_running,
            environment=environment,
        )
    if decision is None:
        decision = evaluate_firefox_path(
            path,
            logical_size=logical_size,
            last_used=last_used,
            now=now,
            process_running=process_running,
            environment=environment,
        )
    if decision is None:
        decision = evaluate_opera_path(
            path,
            logical_size=logical_size,
            last_used=last_used,
            now=now,
            process_running=process_running,
            environment=environment,
        )
    if decision is None:
        decision = evaluate_vivaldi_path(
            path,
            logical_size=logical_size,
            last_used=last_used,
            now=now,
            process_running=process_running,
            environment=environment,
        )
    if decision is None:
        decision = evaluate_brave_path(
            path,
            logical_size=logical_size,
            last_used=last_used,
            now=now,
            process_running=process_running,
            environment=environment,
        )
    if decision is None:
        decision = evaluate_edge_path(
            path,
            logical_size=logical_size,
            last_used=last_used,
            now=now,
            process_running=process_running,
            environment=environment,
        )
    if decision is None:
        decision = evaluate_chrome_path(
            path,
            logical_size=logical_size,
            last_used=last_used,
            now=now,
            process_running=process_running,
            environment=environment,
        )
    if decision is None:
        decision = evaluate_windsurf_path(
            path,
            logical_size=logical_size,
            last_used=last_used,
            now=now,
            process_running=process_running,
            environment=environment,
        )
    if decision is None:
        decision = evaluate_trae_path(
            path,
            logical_size=logical_size,
            last_used=last_used,
            now=now,
            process_running=process_running,
            environment=environment,
        )
    if decision is None:
        decision = evaluate_vscode_path(
            path,
            logical_size=logical_size,
            last_used=last_used,
            now=now,
            process_running=process_running,
            environment=environment,
        )
    if decision is None:
        decision = evaluate_cursor_path(
            path,
            logical_size=logical_size,
            last_used=last_used,
            now=now,
            process_running=process_running,
            environment=environment,
        )
    if decision is None:
        decision = evaluate_claude_path(
            path,
            logical_size=logical_size,
            last_used=last_used,
            now=now,
            process_running=process_running,
            environment=environment,
        )
    if decision is None:
        decision = evaluate_pnpm_path(
            path,
            logical_size=logical_size,
            last_used=last_used,
            now=now,
            process_running=process_running,
            environment=environment,
        )
    if decision is None:
        decision = evaluate_npm_path(
            path,
            logical_size=logical_size,
            last_used=last_used,
            now=now,
            process_running=process_running,
            environment=environment,
        )
    if decision is None:
        decision = _ORIGINAL_EVALUATE_APPLICATION_PATH(
            path,
            logical_size=logical_size,
            last_used=last_used,
            now=now,
            process_running=process_running,
            environment=environment,
        )
    if decision is None or decision.rule.owner is not DecisionOwner.USER:
        return decision
    return replace(decision, action=PolicyAction.KEEP_PROTECTED)


def application_process_running(app_id: str) -> bool:
    if app_id == "android_sdk":
        return android_sdk_process_running()
    if app_id == "gradle":
        return gradle_process_running()
    if app_id == "android_studio":
        return android_studio_process_running()
    if app_id == "toolbox":
        return toolbox_process_running()
    if app_id == "jetbrains":
        return jetbrains_process_running()
    if app_id == "firefox":
        return firefox_process_running()
    if app_id == "opera":
        return opera_process_running()
    if app_id == "vivaldi":
        return vivaldi_process_running()
    if app_id == "brave":
        return brave_process_running()
    if app_id == "edge":
        return edge_process_running()
    if app_id == "chrome":
        return chrome_process_running()
    if app_id == "pnpm":
        return pnpm_process_running()
    if app_id == "npm":
        return npm_process_running()
    if app_id == "windsurf":
        return windsurf_process_running()
    if app_id == "trae":
        return trae_process_running()
    if app_id == "vscode":
        return vscode_process_running()
    if app_id == "cursor":
        return cursor_process_running()
    if app_id == "claude":
        return claude_process_running()
    return _ORIGINAL_APPLICATION_PROCESS_RUNNING(app_id)


def clear_process_cache() -> None:
    _impl.clear_process_cache()
    clear_claude_process_cache()
    clear_cursor_process_cache()
    clear_vscode_process_cache()
    clear_trae_process_cache()
    clear_windsurf_process_cache()
    clear_npm_process_cache()
    clear_pnpm_process_cache()
    clear_chrome_process_cache()
    clear_edge_process_cache()
    clear_brave_process_cache()
    clear_vivaldi_process_cache()
    clear_opera_process_cache()
    clear_firefox_process_cache()
    clear_jetbrains_process_cache()
    clear_toolbox_process_cache()
    clear_android_studio_process_cache()
    clear_gradle_process_cache()
    clear_android_sdk_process_cache()


def process_guard_allows(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> bool:
    """Refuse USER/KEEP mutation, then re-check any application process guard."""

    clear_process_cache()
    rule = match_application_rule(path, environment)
    if rule is not None and rule.owner is not DecisionOwner.TOOL:
        return False
    if rule is None or not rule.requires_process_closed:
        return True
    return not application_process_running(rule.app_id)


def whole_tree_application_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    """Return a TOOL rule only when *path* is exactly an audited whole-tree root."""

    dynamic = whole_tree_android_sdk_rule(path, environment)
    if dynamic is not None:
        return dynamic
    dynamic = whole_tree_gradle_rule(path, environment)
    if dynamic is not None:
        return dynamic
    dynamic = whole_tree_android_studio_rule(path, environment)
    if dynamic is not None:
        return dynamic
    dynamic = whole_tree_toolbox_rule(path, environment)
    if dynamic is not None:
        return dynamic
    dynamic = whole_tree_jetbrains_rule(path, environment)
    if dynamic is not None:
        return dynamic
    dynamic = whole_tree_firefox_rule(path, environment)
    if dynamic is not None:
        return dynamic
    dynamic = whole_tree_opera_rule(path, environment)
    if dynamic is not None:
        return dynamic
    dynamic = whole_tree_vivaldi_rule(path, environment)
    if dynamic is not None:
        return dynamic
    dynamic = whole_tree_brave_rule(path, environment)
    if dynamic is not None:
        return dynamic
    dynamic = whole_tree_edge_rule(path, environment)
    if dynamic is not None:
        return dynamic
    dynamic = whole_tree_chrome_rule(path, environment)
    if dynamic is not None:
        return dynamic
    dynamic = whole_tree_pnpm_rule(path, environment)
    if dynamic is not None:
        return dynamic
    dynamic = whole_tree_npm_rule(path, environment)
    if dynamic is not None:
        return dynamic
    dynamic = whole_tree_windsurf_rule(path, environment)
    if dynamic is not None:
        return dynamic
    dynamic = whole_tree_trae_rule(path, environment)
    if dynamic is not None:
        return dynamic
    dynamic = whole_tree_vscode_rule(path, environment)
    if dynamic is not None:
        return dynamic
    rule = match_application_rule(path, environment)
    if (
        rule is None
        or rule.owner is not DecisionOwner.TOOL
        or not rule.allow_whole_tree
        or rule.root_key == "ANYWHERE"
    ):
        return None
    roots = {root.key: root.path for root in application_roots(environment)}
    root = roots.get(rule.root_key)
    if root is None:
        return None
    normalized = _impl._normalize(path)
    for expanded in _impl._expand_braces(rule.relative_pattern):
        candidate = PureWindowsPath(root) / expanded if expanded else PureWindowsPath(root)
        if normalized == _impl._normalize(candidate):
            return rule
    return None


def application_display_name(app_id: str) -> str:
    return {
        "android_sdk": "Android SDK",
        "gradle": "Gradle",
        "android_studio": "Android Studio",
        "brave": "Brave",
        "chrome": "Chrome / Chromium",
        "edge": "Microsoft Edge",
        "firefox": "Mozilla Firefox",
        "jetbrains": "JetBrains IDE",
        "toolbox": "JetBrains Toolbox",
        "opera": "Opera / Opera GX",
        "vivaldi": "Vivaldi",
        "codex": "Codex",
        "claude": "Claude Code",
        "cursor": "Cursor",
        "npm": "npm",
        "pnpm": "pnpm",
        "vscode": "VS Code",
        "trae": "Trae",
        "windsurf": "Windsurf",
    }.get(app_id, app_id)


__all__ = [
    "ANDROID_SDK_RULES",
    "ANDROID_STUDIO_RULES",
    "BRAVE_RULES",
    "CHROME_RULES",
    "CLAUDE_RULES",
    "CODEX_RULES",
    "CURSOR_RULES",
    "EDGE_RULES",
    "FIREFOX_RULES",
    "GRADLE_RULES",
    "JETBRAINS_RULES",
    "NPM_RULES",
    "OPERA_RULES",
    "PNPM_RULES",
    "TOOLBOX_RULES",
    "TRAE_RULES",
    "VIVALDI_RULES",
    "VSCODE_RULES",
    "WINDSURF_RULES",
    "ApplicationCleanupRule",
    "ApplicationPolicyDecision",
    "ApplicationRoot",
    "DecisionOwner",
    "LastUseStrategy",
    "MatchKind",
    "PolicyAction",
    "RebuildCost",
    "application_display_name",
    "application_process_running",
    "application_roots",
    "application_scan_roots",
    "audited_dynamic_tool_roots",
    "clear_process_cache",
    "effective_idle_days",
    "evaluate_application_path",
    "match_application_rule",
    "process_guard_allows",
    "whole_tree_application_rule",
]
